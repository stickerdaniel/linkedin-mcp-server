"""URL grammar and filter validation for LinkedIn's four search surfaces."""

from __future__ import annotations

from collections.abc import Sequence
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
# ``profileLanguage`` takes ISO 639-1 codes ("en", "de"); anything else is a typo.
_PROFILE_LANGUAGE_RE = re.compile(r"[a-z]{2}")
# ``school`` takes the numeric id ``schoolFilter`` filters on and nothing
# else: the schools search page carries no ``schoolFilter`` anchor and no
# numeric id in any ``/school/`` href (measured live 2026-09-12), so a name
# cannot be resolved to one from here.
_SCHOOL_ID_RE = re.compile(r"[0-9]+")

# Company-search ``industryCompanyVertical`` facet: LinkedIn's numeric industry ids,
# keyed by the industry name as the filter dropdown labels it (casefolded,
# commas stripped, whitespace collapsed -- see ``_normalize_industry_name``).
# ponytail: partial table; unknown names raise, pass the numeric id
COMPANY_INDUSTRY_IDS: dict[str, str] = {
    "software development": "4",
    "technology information and internet": "6",
    "telecommunications": "8",
    "business consulting and services": "11",
    "biotechnology research": "12",
    "hospitals and health care": "14",
    "pharmaceutical manufacturing": "15",
    "retail": "27",
    "banking": "41",
    "insurance": "42",
    "financial services": "43",
    "real estate": "44",
    "construction": "48",
    "advertising services": "80",
    "it services and it consulting": "96",
    "staffing and recruiting": "104",
}

# Company-search ``companySize`` facet letters, keyed by the headcount bucket
# as LinkedIn labels it. Callers may pass either side of the mapping. The
# letters follow LinkedIn's ``staffCountRange`` enum, which starts at
# self-employed.
# TODO(live-verify): letters recalled, not measured; a dropdown probe is queued.
COMPANY_SIZE_LETTERS: dict[str, str] = {
    "self-employed": "A",
    "1-10": "B",
    "11-50": "C",
    "51-200": "D",
    "201-500": "E",
    "501-1000": "F",
    "1001-5000": "G",
    "5001-10000": "H",
    "10001+": "I",
}


def _normalize_csv(value: str, mapping: dict[str, str]) -> str:
    """Normalize a comma-separated filter value using the provided mapping."""
    parts = [v.strip() for v in value.split(",")]
    return ",".join(mapping.get(p, p) for p in parts)


def _normalize_industry_name(name: str) -> str:
    """Casefold, drop commas and collapse whitespace for industry lookup.

    LinkedIn labels one entry "Technology, Information and Internet"; a
    client that transmits list params as a comma-separated string cannot
    carry that comma, so the lookup ignores it on both sides.
    """
    return " ".join(name.replace(",", " ").casefold().split())


def as_list(value: str | list[str] | None) -> list[str]:
    """One value or a list of them, as a list; ``None`` is empty."""
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _encode_list_facet(values: Sequence[str]) -> str:
    """Encode a list of string values for a LinkedIn search list facet.

    LinkedIn's people- and content-search URLs use JSON-list encoded facets of
    the form ``["A","B"]``. This helper URL-encodes the rendered JSON so the
    final URL contains e.g. ``%5B%22F%22%5D`` for ``["F"]``.
    """
    return quote_plus(json.dumps(list(values), separators=(",", ":")))


def network_tokens(values: list[str] | None) -> list[str]:
    """The ``network`` facet tokens, refused before a URL exists.

    An unknown token is accepted by the URL and then dropped, which answers
    with the unfiltered result set while the request still reads as filtered.
    """
    tokens = list(values or [])
    invalid = [t for t in tokens if t not in NETWORK_TOKENS]
    if invalid:
        raise FilterValidationError(
            "Invalid network token(s) "
            f"{invalid!r}; expected any of {list(NETWORK_TOKENS)!r}"
        )
    return tokens


def industry_ids(values: str | list[str] | None) -> list[str]:
    """Numeric industry ids from ids passed verbatim or names in the table.

    Shared by people search (``industry``) and company search
    (``industryCompanyVertical``); an unknown name raises, listing the names
    this server knows.
    """
    ids: list[str] = []
    for raw in as_list(values):
        token = raw.strip()
        if re.fullmatch(r"[0-9]+", token):
            ids.append(token)
            continue
        mapped = COMPANY_INDUSTRY_IDS.get(_normalize_industry_name(token))
        if mapped is None:
            raise FilterValidationError(
                f"Unknown industry {raw!r}; pass LinkedIn's numeric industry "
                f"id, or one of the names this server knows: "
                f"{list(COMPANY_INDUSTRY_IDS)!r}"
            )
        ids.append(mapped)
    return ids


def company_size_letters(values: str | list[str] | None) -> list[str]:
    """``companySize`` facet letters from letters or headcount buckets."""
    letters: list[str] = []
    for raw in as_list(values):
        token = raw.strip()
        if token.upper() in COMPANY_SIZE_LETTERS.values():
            letters.append(token.upper())
            continue
        mapped = COMPANY_SIZE_LETTERS.get(token.casefold())
        if mapped is None:
            raise FilterValidationError(
                f"Unknown company size {raw!r}; expected a headcount bucket "
                f"{list(COMPANY_SIZE_LETTERS)!r} or a facet letter "
                f"{list(COMPANY_SIZE_LETTERS.values())!r}"
            )
        letters.append(mapped)
    return letters


def profile_languages(values: str | list[str] | None) -> list[str]:
    """Lower-cased ISO 639-1 codes for ``profileLanguage``; anything else raises."""
    languages = [code.strip().lower() for code in as_list(values)]
    invalid = [code for code in languages if not _PROFILE_LANGUAGE_RE.fullmatch(code)]
    if invalid:
        raise FilterValidationError(
            f"Invalid profile_language {invalid!r}; expected "
            'two-letter ISO 639-1 codes such as "en"'
        )
    return languages


def school_id(value: str | None) -> str | None:
    """The numeric ``schoolFilter`` id, or None when no school was given.

    A name is refused: nothing on LinkedIn's schools search page resolves it
    to the id (see ``_SCHOOL_ID_RE``), and the message says where to find it.
    """
    token = value.strip() if value else None
    if token and not _SCHOOL_ID_RE.fullmatch(token):
        raise FilterValidationError(
            f"Invalid school {value!r}; pass the numeric school id. Find it "
            "on LinkedIn: people search -> All filters -> School -> pick "
            'one, then the URL shows schoolFilter=["<id>"].'
        )
    return token or None


def require_people_criteria(
    *,
    keywords: str | None = None,
    location: str | None = None,
    network: list[str] | None = None,
    current_companies: Sequence[str] = (),
    past_companies: Sequence[str] = (),
    industry_ids: Sequence[str] = (),
    school_id: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    languages: Sequence[str] = (),
    title: str | None = None,
) -> None:
    """Refuse a people search that would navigate for the unfiltered list.

    Every argument is the already-validated form (ids, not names), so this
    runs after the pure validators and before any navigation: a search with
    no criterion, or with a title alone, never costs a page load.
    """
    other_criteria = (
        keywords,
        location,
        network,
        current_companies,
        past_companies,
        industry_ids,
        school_id,
        first_name,
        last_name,
        languages,
    )
    if not any(other_criteria) and not title:
        raise FilterValidationError(
            "search_people needs at least one of keywords, location, network, "
            "current_company, past_company, title, industry, school, "
            "first_name, last_name or profile_language"
        )
    # LinkedIn ignores titleFreeText (measured live), so a title on its
    # own would navigate and return the unfiltered worldwide list.
    if title and not any(other_criteria):
        raise FilterValidationError(
            "search_people cannot filter by title alone: LinkedIn ignores "
            "titleFreeText. Put the title in keywords as a quoted phrase "
            f"(keywords='\"{title}\"'), or combine title with another "
            "facet such as location or current_company"
        )


def require_company_criteria(
    *,
    keywords: str | None = None,
    industry_ids: Sequence[str] = (),
    size_letters: Sequence[str] = (),
    hq_location: str | None = None,
) -> None:
    """Refuse a company search with nothing to narrow the result set."""
    if not (keywords or industry_ids or size_letters or hq_location):
        raise FilterValidationError(
            "search_companies needs at least one of keywords, industry, "
            "size or hq_location"
        )


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
    keywords: str | None = None,
    *,
    geo_id: str | None = None,
    network: list[str] | None = None,
    current_company_ids: Sequence[str] = (),
    past_company_ids: Sequence[str] = (),
    industry_ids: Sequence[str] = (),
    school_id: str | None = None,
    title: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    languages: Sequence[str] = (),
) -> str:
    """Build a LinkedIn people search URL from already-resolved facet values.

    Every facet that LinkedIn filters on by id (``geoUrn``, ``currentCompany``,
    ``pastCompany``, ``industry``, ``schoolFilter``) takes the id here: a name
    in any of them is accepted by the URL and then dropped, which answers with
    the unfiltered result set while the request still reads as filtered, so
    names are resolved or refused before this is called. ``network`` tokens
    are validated again here so the builder is safe to call on its own; the
    check is pure and costs nothing.
    """
    network = network_tokens(network)

    params: list[str] = []
    if keywords:
        params.append(f"keywords={quote_plus(keywords)}")
    if geo_id:
        params.append(f"geoUrn={_encode_list_facet([geo_id])}")
    if network:
        params.append(f"network={_encode_list_facet(network)}")
    if current_company_ids:
        params.append(f"currentCompany={_encode_list_facet(current_company_ids)}")
    # TODO(live-verify): facet names unconfirmed (pastCompany, industry,
    # schoolFilter, lastName, profileLanguage). currentCompany, firstName
    # and a keyword-less search were confirmed live on 2026-09-12.
    if past_company_ids:
        params.append(f"pastCompany={_encode_list_facet(past_company_ids)}")
    if industry_ids:
        params.append(f"industry={_encode_list_facet(industry_ids)}")
    if school_id:
        params.append(f"schoolFilter={_encode_list_facet([school_id])}")
    if title:
        # live-verified: ignored on 2026-09-12 in the SDUI variant. Kept
        # because another results-page variant may still read it; the
        # tool docstring steers callers to a quoted phrase in keywords.
        params.append(f"titleFreeText={quote_plus(title)}")
    if first_name:
        params.append(f"firstName={quote_plus(first_name)}")
    if last_name:
        params.append(f"lastName={quote_plus(last_name)}")
    if languages:
        params.append(f"profileLanguage={_encode_list_facet(languages)}")

    return "https://www.linkedin.com/search/results/people/?" + "&".join(params)


def build_company_search_url(
    keywords: str | None = None,
    *,
    industry_ids: Sequence[str] = (),
    size_letters: Sequence[str] = (),
    geo_id: str | None = None,
    has_jobs: bool = False,
) -> str:
    """Build a LinkedIn company search URL from already-resolved facet values.

    Facets narrow the result set on LinkedIn's side, so a shortlist built
    from one costs a navigation per page rather than one per company. Ids and
    letters arrive validated (``industry_ids``, ``company_size_letters``) and
    the headquarters as the geo id its own dropdown produced.
    """
    params: list[str] = []
    if keywords:
        params.append(f"keywords={quote_plus(keywords)}")
    if industry_ids:
        # ``companyIndustry`` is stripped by LinkedIn (measured live
        # 2026-09-12); this is the name its own company-filter UI writes.
        # TODO(live-verify): industryCompanyVertical unconfirmed
        params.append(f"industryCompanyVertical={_encode_list_facet(industry_ids)}")
    if size_letters:
        params.append(f"companySize={_encode_list_facet(size_letters)}")
    if geo_id:
        params.append(f"companyHqGeo={_encode_list_facet([geo_id])}")
    if has_jobs:
        # The JSON-string form LinkedIn normalises a bare ``true`` to
        # (measured live 2026-09-12), sent directly.
        params.append("hasJobs=%22true%22")

    return "https://www.linkedin.com/search/results/companies/?" + "&".join(params)


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
