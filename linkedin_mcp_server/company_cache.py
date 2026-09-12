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
    founded: str = ""  # as LinkedIn shows it, usually a bare year
    company_type: str = ""  # "Privately Held", "Public Company", ...
    specialties: str = ""  # LinkedIn's own comma-separated free text
    linkedin_url: str = ""
    company_urn: str = ""  # numeric LinkedIn company id, for job-search-by-company
    followers: int | None = None  # from the search card; the About page has none
    firmographics_source: str = ""  # "search" | "company_page"
    firmographics_fetched_at: str = ""  # ISO 8601, empty = never

    # Open-roles half.
    open_roles_count: int | None = None
    open_roles_sample: list[str] = field(default_factory=list)
    jobs_fetched_at: str = ""

    # Raw section text from a deep fetch, kept for transparency and debugging
    # (so a cached record can be audited against what LinkedIn actually showed).
    # The typed fields above are the product; this is not a parse fallback.
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
        founded: str = "",
        company_type: str = "",
        specialties: str = "",
        linkedin_url: str = "",
        company_urn: str = "",
        followers: int | None = None,
        raw_about: str = "",
    ) -> CompanyRecord:
        rec = self.get_or_new(name)

        # Only overwrite a field when the new fetch actually carries it; a
        # cheap search hit must not blank out headquarters a deep fetch found.
        # Nor may it overwrite one: a search card's industry and location are
        # the About page's own rows abbreviated, so once a deep fetch has
        # written the record its typed fields stand and a search only refreshes
        # what the About page does not carry (URL, URN, followers). That holds
        # even for a field the About page left empty: it is not back-filled
        # from a later search card, so the source label stays honest.
        deep = rec.firmographics_source == "company_page"
        if source == "company_page" or not deep:
            if industry:
                rec.industry = industry
            if employee_count:
                rec.employee_count = employee_count
            if headquarters:
                rec.headquarters = headquarters
            if website:
                rec.website = website
            if founded:
                rec.founded = founded
            if company_type:
                rec.company_type = company_type
            if specialties:
                rec.specialties = specialties
        if linkedin_url:
            rec.linkedin_url = linkedin_url
        if company_urn:
            rec.company_urn = company_urn
        if followers is not None:
            rec.followers = followers
        if raw_about:
            rec.raw_about = raw_about

        # Freshness tracks a *read of the About page*, not the mere fact of a
        # write, because the stamp is what lets enrich_company_deep skip the
        # load. A company-search pass never reads that page: its card carries
        # an industry and a location, which are stored and reported under
        # ``source: "search"``, but stamping them fresh-for-90-days would make
        # the deep tier skip a company it never actually read, and would reset
        # the timestamp/source of a record a deep fetch already populated. So
        # only an About write stamps -- when it brings a firmographic field,
        # or when ``raw_about`` holds what the page showed, so an About that
        # parsed to nothing is not re-spent on every call until the TTL. An
        # About write with nothing in ``raw_about`` showed nothing, so it earns
        # no stamp: a failed load must stay stale and be retried.
        carries_firmographics = bool(
            industry
            or employee_count
            or headquarters
            or website
            or founded
            or company_type
            or specialties
        )
        if source == "company_page":
            if carries_firmographics or raw_about:
                rec.firmographics_source = source
                rec.firmographics_fetched_at = now.isoformat()
        elif carries_firmographics and not deep:
            rec.firmographics_source = source

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

    def all_records(self) -> list[CompanyRecord]:
        """Every readable record, in key order.

        A linear scan of the directory, one JSON parse per company. The cache
        grows by the companies one account's network touches -- thousands,
        not millions -- and a few thousand small files read in well under a
        second, so nothing is indexed. Past ~50k records this is the first
        thing to revisit; until then an index would be more code than the
        scan it replaces.
        """
        if not self.root.exists():
            return []
        out: list[CompanyRecord] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                out.append(CompanyRecord.from_dict(json.loads(path.read_text("utf-8"))))
            except (json.JSONDecodeError, OSError, TypeError) as e:
                logger.warning("Skipping unreadable cache file %s: %s", path, e)
        return out


# The headcount is stored as LinkedIn's band string ("51-200 employees",
# "10,001+ employees"), never a bare number, so a numeric filter has to reason
# about the band's ends rather than a point.
_BAND_RANGE = re.compile(r"(\d[\d,]*)\s*-\s*(\d[\d,]*)")
_BAND_OPEN = re.compile(r"(\d[\d,]*)\s*\+")
_YEAR = re.compile(r"\b(\d{4})\b")


def employee_band_bounds(band: str) -> tuple[int, int | None] | None:
    """(low, high) of a stored headcount band; high is None for "N+".

    Returns None when the string is not a band at all, so a caller filtering
    on headcount can exclude the record rather than guess.
    """
    if not band:
        return None
    m = _BAND_RANGE.search(band)
    if m:
        return int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
    m = _BAND_OPEN.search(band)
    if m:
        return int(m.group(1).replace(",", "")), None
    return None


def founded_year(founded: str) -> int | None:
    """The four-digit year in a stored ``founded`` value, if there is one."""
    m = _YEAR.search(founded or "")
    return int(m.group(1)) if m else None


def record_matches(
    rec: CompanyRecord,
    *,
    industry: str | None = None,
    headquarters: str | None = None,
    min_employees: int | None = None,
    max_employees: int | None = None,
    hiring: bool | None = None,
    founded_after: int | None = None,
    founded_before: int | None = None,
) -> bool:
    """Whether a record satisfies every given criterion.

    A record that lacks a field being filtered on is excluded, not passed
    through: a query for "companies with 200+ staff" must not return the ones
    whose headcount was never fetched. Text criteria are case-insensitive
    substrings. Headcount is matched by band overlap -- a 51-200 company
    satisfies ``min_employees=100`` because its band reaches 100 -- since the
    stored value is a band, not a count. Year bounds are inclusive.
    """
    if industry is not None:
        if not rec.industry or industry.lower() not in rec.industry.lower():
            return False
    if headquarters is not None:
        if not rec.headquarters or headquarters.lower() not in rec.headquarters.lower():
            return False
    if min_employees is not None or max_employees is not None:
        bounds = employee_band_bounds(rec.employee_count)
        if bounds is None:
            return False
        low, high = bounds
        if min_employees is not None and high is not None and high < min_employees:
            return False
        if max_employees is not None and low > max_employees:
            return False
    if hiring is not None:
        if rec.open_roles_count is None:
            return False
        if (rec.open_roles_count > 0) != hiring:
            return False
    if founded_after is not None or founded_before is not None:
        year = founded_year(rec.founded)
        if year is None:
            return False
        if founded_after is not None and year < founded_after:
            return False
        if founded_before is not None and year > founded_before:
            return False
    return True
