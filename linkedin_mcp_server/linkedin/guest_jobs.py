"""Job postings from LinkedIn's public guest endpoint: plain HTTP, no browser or login.

``https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}`` serves the
logged-out posting page as HTML. Fetching it directly replaces driving the
signed-in browser to each posting (``JobReader.read_job``), which costs a full
page navigation per job and returns sidebars and upsells along with the text.

Only the fields an agent reads are parsed. Every failure is reported per job,
so one bad id never discards the others.
"""

from __future__ import annotations

import asyncio
import logging
import re
from html.parser import HTMLParser
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2

from linkedin_mcp_server.linkedin.identifiers import job_view_url, normalize_job_id

logger = logging.getLogger(__name__)

GUEST_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
DEFAULT_WORKERS = 3
#: Hard ceiling. More parallel requests to an unauthenticated endpoint is what
#: gets an address flagged as a bot.
MAX_WORKERS = 5
_TIMEOUT_SECONDS = 15.0
#: 999 is LinkedIn's own "request denied" status for suspected automation.
_RATE_LIMITED = frozenset({429, 999})


class _GuestPostingParser(HTMLParser):
    """Collect the top card, the description and the criteria list."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}
        self.company_url: str | None = None
        self.criteria: dict[str, str] = {}
        self._target: str | None = None
        self._target_tag = ""
        self._buffer: list[str] = []
        self._criterion = ""
        self._markup_depth = 0
        self._markup: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())

        if self._markup_depth:
            if tag == "div":
                self._markup_depth += 1
            elif tag == "li":
                self._markup.append("\n- ")
            elif tag == "br":
                self._markup.append("\n")
            return
        if tag == "div" and "show-more-less-html__markup" in classes:
            self._markup_depth = 1
            return
        if self._target is not None:
            return

        target = None
        if tag == "h2" and "top-card-layout__title" in classes:
            target = "title"
        elif tag == "a" and "topcard__org-name-link" in classes:
            target = "company"
            href = attributes.get("href")
            if href and self.company_url is None:
                self.company_url = href.split("?")[0]
        elif (
            tag == "span" and {"topcard__flavor", "topcard__flavor--bullet"} <= classes
        ):
            target = "location"
        elif tag == "span" and "posted-time-ago__text" in classes:
            target = "posted"
        elif tag == "figcaption" and "num-applicants__caption" in classes:
            target = "applicants"
        elif tag == "h3" and "description__job-criteria-subheader" in classes:
            target = "criterion_name"
        elif tag == "span" and "description__job-criteria-text" in classes:
            target = "criterion_value"

        if target is not None:
            self._target, self._target_tag, self._buffer = target, tag, []

    def handle_endtag(self, tag: str) -> None:
        if self._markup_depth:
            if tag == "div":
                self._markup_depth -= 1
            elif tag == "p":
                self._markup.append("\n\n")
            elif tag in ("ul", "ol"):
                self._markup.append("\n")
            return
        if self._target is None or tag != self._target_tag:
            return

        text = " ".join("".join(self._buffer).split())
        if self._target == "criterion_name":
            self._criterion = re.sub(r"\W+", "_", text.lower()).strip("_")
        elif self._target == "criterion_value":
            if self._criterion:
                self.criteria.setdefault(self._criterion, text)
        else:
            self.fields.setdefault(self._target, text)
        self._target = None

    def handle_data(self, data: str) -> None:
        if self._markup_depth:
            self._markup.append(re.sub(r"\s+", " ", data))
        elif self._target is not None:
            self._buffer.append(data)

    @property
    def description(self) -> str:
        lines = ("".join(self._markup)).split("\n")
        text = "\n".join(re.sub(r"^-\s+", "- ", line.strip()) for line in lines)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_guest_posting(html: str) -> dict[str, Any] | None:
    """The fields of a guest posting page, or None when it has no description."""
    parser = _GuestPostingParser()
    parser.feed(html)
    parser.close()

    description = parser.description
    if not description:
        return None
    return {
        "title": parser.fields.get("title"),
        "company": parser.fields.get("company"),
        "company_url": parser.company_url,
        "location": parser.fields.get("location"),
        "posted": parser.fields.get("posted"),
        "applicants": parser.fields.get("applicants"),
        "criteria": parser.criteria,
        "description": description,
    }


def _error(job_id: str, code: str, message: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "job_id": job_id,
        "url": job_view_url(job_id, "/"),
        "status": "error",
        "error": code,
    }
    if message:
        result["error_message"] = message
    return result


async def fetch_jobs(
    job_ids: list[str],
    n_workers: int = DEFAULT_WORKERS,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Fetch several postings concurrently, at most ``n_workers`` at a time.

    Raises ValueError for an ``n_workers`` outside 1..MAX_WORKERS, an empty
    list, and (as InvalidReferenceError) a malformed id, all before any
    request. After that nothing raises: each job is a ``status: "ok"`` result
    or a ``status: "error"`` one with a short ``error`` code. Results keep the
    order of the ids, duplicates removed. There are no retries; once LinkedIn
    answers a request with a rate limit, jobs not yet started are skipped
    rather than sent into it.

    ``transport`` exists for tests. ``on_progress`` receives completed and
    total after each job, successful or not.
    """
    if not 1 <= n_workers <= MAX_WORKERS:
        raise ValueError(
            f"n_workers must be between 1 and {MAX_WORKERS} (got {n_workers}). "
            "More concurrent requests to LinkedIn's guest endpoint risk "
            "bot detection."
        )
    ids = list(dict.fromkeys(normalize_job_id(job_id) for job_id in job_ids))
    if not ids:
        raise ValueError("job_ids must contain at least one job ID")

    semaphore = asyncio.Semaphore(n_workers)
    rate_limited = asyncio.Event()
    progress_lock = asyncio.Lock()
    completed = 0

    async with httpx2.AsyncClient(
        timeout=_TIMEOUT_SECONDS, transport=transport
    ) as client:

        async def fetch_one(job_id: str) -> dict[str, Any]:
            nonlocal completed
            try:
                async with semaphore:
                    if rate_limited.is_set():
                        return _error(job_id, "skipped_rate_limited")
                    try:
                        response = await client.get(GUEST_URL.format(job_id=job_id))
                    except httpx2.HTTPError as exc:
                        return _error(
                            job_id, "request_failed", f"{type(exc).__name__}: {exc}"
                        )

                if response.status_code in _RATE_LIMITED:
                    rate_limited.set()
                    return _error(
                        job_id, "rate_limited", f"HTTP {response.status_code}"
                    )
                if response.status_code == 404:
                    return _error(job_id, "not_found")
                if response.status_code != 200:
                    return _error(job_id, f"http_{response.status_code}")
                try:
                    posting = parse_guest_posting(response.text)
                except Exception:
                    logger.exception("Could not parse guest posting %s", job_id)
                    return _error(job_id, "parse_failed")
                if posting is None:
                    return _error(
                        job_id,
                        "description_missing",
                        "The page has no description; the posting may be closed.",
                    )
                return {
                    "job_id": job_id,
                    "url": job_view_url(job_id, "/"),
                    "status": "ok",
                    **posting,
                }
            finally:
                if on_progress is not None:
                    async with progress_lock:
                        completed += 1
                        await on_progress(completed, len(ids))

        jobs = await asyncio.gather(*(fetch_one(job_id) for job_id in ids))

    ok = sum(job["status"] == "ok" for job in jobs)
    return {
        "jobs": jobs,
        "summary": {"requested": len(ids), "ok": ok, "failed": len(ids) - ok},
    }
