"""A persistent, TTL'd cache of company firmographics and open roles.

Company research over a large network is dominated by repetition: the same
employer recurs across dozens of connections, and firmographics barely move
between lookups. Re-fetching each company page every time is what turns a
one-afternoon job into a two-month one, so the cache is not an optimization
here -- it is what makes the work finish at all.

Two facts about a company age at very different rates, so they carry separate
TTLs:

* **Firmographics** (industry, headcount band, HQ, website) change on the order
  of years. A long TTL (default 90 days) is safe and keeps a re-run almost
  entirely off LinkedIn.
* **Open roles** are the volatile buying signal -- a company hiring Salesforce
  admins this month is investing in the platform this month. A short TTL
  (default 14 days) keeps that fresh without re-paying for the slow fields.

The store is a JSON file per company, written atomically. Nothing here touches
a browser or the clock beyond an explicit ``now``, so it is tested directly.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# How long each class of fact stays fresh before a re-fetch is warranted.
DEFAULT_FIRMOGRAPHICS_TTL = timedelta(days=90)
DEFAULT_JOBS_TTL = timedelta(days=14)


def ttl_from_days(raw: str | None, default: timedelta) -> timedelta:
    """Parse a TTL given in days, falling back to ``default`` on anything odd.

    A misconfigured env var must not make the cache unusable, and a
    non-positive TTL would mean "always stale" (every lookup re-fetches),
    which defeats the cache -- so both are rejected in favour of the default.
    """
    if raw is None or not raw.strip():
        return default
    try:
        days = float(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric cache TTL %r; using %s", raw, default)
        return default
    if days <= 0:
        logger.warning("Ignoring non-positive cache TTL %r; using %s", raw, default)
        return default
    return timedelta(days=days)


_LEGAL_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|limited|gmbh|bv|b\.v|nv|plc|sa|s\.a|ag|co|corp|"
    r"corporation|company|group|holdings?|international|global|"
    r"technologies|technology|pvt|private|pte|llp|kft|as|oy|ab)\b\.?",
    re.I,
)
_TLD = re.compile(r"\.(com|io|co|net|org|ai|cloud|dev|app|inc)\b", re.I)


def normalize_company_name(name: str) -> str:
    """Collapse spellings of one company to a single cache key.

    "Salesforce", "Salesforce.com, Inc." and "salesforce" must all resolve to
    one record, otherwise the cache misses on trivial variation and the whole
    point is lost. Mirrors the normalisation used to group the export.
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    s = re.sub(r"[‐-―]", "-", s)
    s = re.sub(r"\s+-\s+.*$", "", s)  # drop " - descriptor" tails
    s = _TLD.sub("", s)
    s = _LEGAL_SUFFIX.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class CompanyRecord:
    """One company's cached facts, each half independently timestamped."""

    key: str
    display_name: str = ""

    # Firmographics half.
    industry: str = ""
    employee_count: str = ""
    headquarters: str = ""
    website: str = ""
    linkedin_url: str = ""
    firmographics_source: str = ""  # "search" | "company_page"
    firmographics_fetched_at: str = ""  # ISO 8601, empty = never

    # Open-roles half.
    open_roles_count: int | None = None
    open_roles_sample: list[str] = field(default_factory=list)
    jobs_fetched_at: str = ""

    # Raw section text, kept as the fallback the LLM can re-parse -- mirrors
    # the rest of this codebase, which returns innerText and lets the caller
    # extract rather than trusting a brittle parser.
    raw_about: str = ""
    raw_jobs: str = ""

    def has_firmographics(self) -> bool:
        return bool(self.firmographics_fetched_at)

    def has_jobs(self) -> bool:
        return bool(self.jobs_fetched_at)

    def firmographics_fresh(self, now: datetime, ttl: timedelta) -> bool:
        return _fresh(self.firmographics_fetched_at, now, ttl)

    def jobs_fresh(self, now: datetime, ttl: timedelta) -> bool:
        return _fresh(self.jobs_fetched_at, now, ttl)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CompanyRecord:
        # Ignore unknown keys so an older or newer file shape still loads.
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


def _fresh(stamp: str, now: datetime, ttl: timedelta) -> bool:
    if not stamp:
        return False
    try:
        fetched = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    return now - fetched < ttl


class CompanyCache:
    """One JSON file per company under ``root``, keyed by normalised name."""

    def __init__(
        self,
        root: Path | str = "~/.linkedin-mcp/companies",
        *,
        firmographics_ttl: timedelta = DEFAULT_FIRMOGRAPHICS_TTL,
        jobs_ttl: timedelta = DEFAULT_JOBS_TTL,
    ) -> None:
        self.root = Path(root).expanduser()
        self.firmographics_ttl = firmographics_ttl
        self.jobs_ttl = jobs_ttl

    def _path(self, key: str) -> Path:
        safe = re.sub(r"[^a-z0-9]+", "-", key).strip("-")
        if not safe:
            raise ValueError(f"Company key {key!r} normalises to nothing usable")
        return self.root / f"{safe}.json"

    def get(self, name: str) -> CompanyRecord | None:
        key = normalize_company_name(name)
        if not key:
            return None
        path = self._path(key)
        if not path.exists():
            return None
        try:
            return CompanyRecord.from_dict(json.loads(path.read_text("utf-8")))
        except (json.JSONDecodeError, OSError, TypeError) as e:
            logger.warning("Ignoring unreadable cache file %s: %s", path, e)
            return None

    def get_or_new(self, name: str) -> CompanyRecord:
        return self.get(name) or CompanyRecord(
            key=normalize_company_name(name), display_name=name.strip()
        )

    def save(self, record: CompanyRecord) -> None:
        path = self._path(record.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record.to_dict(), indent=2), "utf-8")
        tmp.replace(path)

    def needs_firmographics(self, name: str, now: datetime) -> bool:
        """True when a firmographics fetch is warranted (missing or stale)."""
        rec = self.get(name)
        return rec is None or not rec.firmographics_fresh(now, self.firmographics_ttl)

    def needs_jobs(self, name: str, now: datetime) -> bool:
        """True when an open-roles fetch is warranted (missing or stale)."""
        rec = self.get(name)
        return rec is None or not rec.jobs_fresh(now, self.jobs_ttl)

    def record_firmographics(
        self,
        name: str,
        now: datetime,
        *,
        source: str,
        industry: str = "",
        employee_count: str = "",
        headquarters: str = "",
        website: str = "",
        linkedin_url: str = "",
        raw_about: str = "",
    ) -> CompanyRecord:
        rec = self.get_or_new(name)
        rec.firmographics_source = source
        rec.firmographics_fetched_at = now.isoformat()
        # Only overwrite a field when the new fetch actually carries it; a
        # cheap search hit must not blank out headquarters a deep fetch found.
        if industry:
            rec.industry = industry
        if employee_count:
            rec.employee_count = employee_count
        if headquarters:
            rec.headquarters = headquarters
        if website:
            rec.website = website
        if linkedin_url:
            rec.linkedin_url = linkedin_url
        if raw_about:
            rec.raw_about = raw_about
        self.save(rec)
        return rec

    def record_jobs(
        self,
        name: str,
        now: datetime,
        *,
        count: int | None,
        sample: list[str],
        raw_jobs: str = "",
    ) -> CompanyRecord:
        rec = self.get_or_new(name)
        rec.open_roles_count = count
        rec.open_roles_sample = sample
        rec.jobs_fetched_at = now.isoformat()
        if raw_jobs:
            rec.raw_jobs = raw_jobs
        self.save(rec)
        return rec

    def list_keys(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.stem for p in self.root.glob("*.json"))
