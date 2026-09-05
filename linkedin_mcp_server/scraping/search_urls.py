"""URL grammar and filter validation for LinkedIn's four search surfaces."""

from __future__ import annotations

from urllib.parse import quote_plus

import json
import re

from linkedin_mcp_server.scraping.contracts import FilterValidationError

# Normalization maps for job search filters. Job search encodes recency as
# ``f_TPR=r<seconds>``; content search uses named tokens, hence the separate
# ``CONTENT_DATE_POSTED_MAP`` below.
JOB_DATE_POSTED_MAP = {
    "past_hour": "r3600",
    "past_24_hours": "r86400",
    "past_week": "r604800",
    "past_month": "r2592000",
}

EXPERIENCE_LEVEL_MAP = {
    "internship": "1",
    "entry": "2",
    "associate": "3",
    "mid_senior": "4",
    "director": "5",
    "executive": "6",
}

JOB_TYPE_MAP = {
    "full_time": "F",
    "part_time": "P",
    "contract": "C",
    "temporary": "T",
    "volunteer": "V",
    "internship": "I",
    "other": "O",
}

WORK_TYPE_MAP = {"on_site": "1", "remote": "2", "hybrid": "3"}

SORT_BY_MAP = {"date": "DD", "relevance": "R"}

# Content (post) search uses literal ``datePosted`` tokens inside a JSON-list
# facet, e.g. ``datePosted=["past-week"]`` — unlike job search, which uses
# ``f_TPR=r<seconds>`` codes. The three hyphenated values are LinkedIn's
# complete set, verified live: the filter dropdown offers exactly Past 24
# hours / week / month, and anything else is ignored while still being echoed
# back in the url, so a near-miss spelling returns unfiltered results that
# look filtered. The underscore keys are this server's own spelling, carried
# over so ``date_posted`` reads the same here as in ``search_jobs``
# (``JOB_DATE_POSTED_MAP``); ``past_hour`` has no content-search equivalent.
CONTENT_DATE_POSTED_MAP = {
    "past-24h": "past-24h",
    "past_24_hours": "past-24h",
    "past-week": "past-week",
    "past_week": "past-week",
    "past-month": "past-month",
    "past_month": "past-month",
}

# Valid tokens for the people-search ``network`` facet.
# LinkedIn accepts "F" (1st-degree), "S" (2nd-degree), "O" (3rd-degree and beyond).
NETWORK_TOKENS = ("F", "S", "O")


def _normalize_csv(value: str, mapping: dict[str, str]) -> str:
    """Normalize a comma-separated filter value using the provided mapping."""
    parts = [v.strip() for v in value.split(",")]
    return ",".join(mapping.get(p, p) for p in parts)


def _encode_list_facet(values: list[str]) -> str:
    """Encode a list of string values for a LinkedIn search list facet.

    LinkedIn's people- and content-search URLs use JSON-list encoded facets of
    the form ``["A","B"]``. This helper URL-encodes the rendered JSON so the
    final URL contains e.g. ``%5B%22F%22%5D`` for ``["F"]``.
    """
    return quote_plus(json.dumps(values, separators=(",", ":")))


def build_job_search_url(
    keywords: str,
    location: str | None = None,
    date_posted: str | None = None,
    job_type: str | None = None,
    experience_level: str | None = None,
    work_type: str | None = None,
    easy_apply: bool = False,
    sort_by: str | None = None,
) -> str:
    """Build a LinkedIn job search URL with optional filters.

    Human-readable names are normalized to LinkedIn URL codes.
    Comma-separated values are normalized individually.
    Unknown values pass through unchanged.
    """
    params = f"keywords={quote_plus(keywords)}"
    if location:
        params += f"&location={quote_plus(location)}"

    if date_posted:
        mapped = JOB_DATE_POSTED_MAP.get(date_posted.strip(), date_posted)
        params += f"&f_TPR={quote_plus(mapped)}"
    if job_type:
        params += f"&f_JT={_normalize_csv(job_type, JOB_TYPE_MAP)}"
    if experience_level:
        params += f"&f_E={_normalize_csv(experience_level, EXPERIENCE_LEVEL_MAP)}"
    if work_type:
        params += f"&f_WT={_normalize_csv(work_type, WORK_TYPE_MAP)}"
    if easy_apply:
        params += "&f_EA=true"
    if sort_by:
        mapped = SORT_BY_MAP.get(sort_by.strip(), sort_by)
        params += f"&sortBy={quote_plus(mapped)}"

    return f"https://www.linkedin.com/jobs/search/?{params}"


def build_people_search_url(
    keywords: str,
    location: str | None = None,
    network: list[str] | None = None,
    current_company: str | None = None,
) -> str:
    """Build a LinkedIn people search URL, refusing filters LinkedIn ignores.

    Both refusals happen before a URL exists, so a workflow calling this can
    never navigate on a filter LinkedIn would swallow. An unknown ``network``
    token and a plain-text ``currentCompany`` are each accepted by the URL and
    then dropped, which answers with the unfiltered result set while the
    request still reads as filtered.
    """
    if network is not None:
        invalid = [t for t in network if t not in NETWORK_TOKENS]
        if invalid:
            raise FilterValidationError(
                "Invalid network token(s) "
                f"{invalid!r}; expected any of {list(NETWORK_TOKENS)!r}"
            )

    if current_company and not re.fullmatch(r"[0-9]+", current_company):
        raise FilterValidationError(
            f"current_company must be a numeric LinkedIn company URN id "
            f"(e.g. '1115' for SAP); got {current_company!r}. Plain-text "
            f"company names are silently ignored by LinkedIn. Look up the "
            f'URN via get_company_profile -> references["about"].'
        )

    params = f"keywords={quote_plus(keywords)}"
    if location:
        params += f"&location={quote_plus(location)}"
    if network:
        params += f"&network={_encode_list_facet(network)}"
    if current_company:
        params += f"&currentCompany={_encode_list_facet([current_company])}"

    return f"https://www.linkedin.com/search/results/people/?{params}"


def build_company_search_url(keywords: str) -> str:
    """Build a LinkedIn company search URL.

    One parameter today, and a function anyway: every search URL this server
    navigates to is then written in one place, so the next caller has nothing
    to assemble by hand and no second spelling of the host and route to keep
    in step with the other three.
    """
    return (
        "https://www.linkedin.com/search/results/companies/"
        f"?keywords={quote_plus(keywords)}"
    )


def build_content_search_url(
    keywords: str,
    date_posted: str | None = None,
) -> str:
    """Build a LinkedIn content (post) search URL.

    Reproduces the ``FACETED_SEARCH`` URL LinkedIn produces from the
    Posts results tab, e.g. for "Buscamos Unity" in the past week:
    ``/search/results/content/?keywords=Buscamos+Unity&origin=FACETED_SEARCH&datePosted=%5B%22past-week%22%5D``

    The ``datePosted`` facet is a one-element JSON list carrying a literal
    LinkedIn token, URL-encoded — unlike job search, which uses
    ``f_TPR=r<seconds>``. The value is mapped through
    ``CONTENT_DATE_POSTED_MAP`` so the server's own underscore spelling
    reaches LinkedIn in the form it recognizes. An unmapped value is refused
    here rather than sent, because LinkedIn ignores one instead of rejecting
    it and answers an unfiltered search that reads as a filtered one.
    """
    if (
        date_posted is not None
        and date_posted.strip()
        and date_posted.strip() not in CONTENT_DATE_POSTED_MAP
    ):
        raise FilterValidationError(
            f"Invalid date_posted {date_posted!r}; expected one of "
            f"{list(CONTENT_DATE_POSTED_MAP)!r}."
        )

    params = f"keywords={quote_plus(keywords)}&origin=FACETED_SEARCH"
    if date_posted and date_posted.strip():
        token = CONTENT_DATE_POSTED_MAP.get(date_posted.strip(), date_posted.strip())
        params += f"&datePosted={_encode_list_facet([token])}"
    return f"https://www.linkedin.com/search/results/content/?{params}"
