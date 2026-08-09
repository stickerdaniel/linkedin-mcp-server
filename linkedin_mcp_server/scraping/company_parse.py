"""Structured extraction of company firmographics from LinkedIn innerText.

These parsers turn a company About / Jobs / search page's text into typed
fields (industry, headcount band, open-roles count) for the cache. They are the
primary product, not a hope: they are pinned by regression tests against real
pages captured live from LinkedIn (see ``TestParseRealLinkedInPages`` in
tests/test_company_cache.py -- Microsoft, Gearset, a deleted page, an empty
jobs tab), including the decoys those real pages carry (a follower count and an
"N associated members" line sitting right next to the size band).

Two principles keep them trustworthy over LinkedIn's shifting markup:

* **Conservative:** a parser returns "" / None rather than guess. A wrong
  industry cached for 90 days is worse than a blank one, and a fabricated
  open-roles count is worse still -- so ``count`` comes only from LinkedIn's own
  heading, never from how many role-ish lines were scraped.
* **Anchored, not greedy:** size is read as a real band next to the "Company
  size" label, never the first "N employees" anywhere (which is a follower
  count on real pages).

Every function is pure over text: no browser, no network, no clock. A deep
fetch also keeps the raw section text on the record for transparency, but the
typed fields above are what the tools serve.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# The headcount *band* is specifically a range or an open-ended top bucket:
# "1,001-5,000 employees", "10,001+ employees", "51-200 employees". A bare
# "1,234 employees" is NOT a band -- on real pages that is the follower /
# "See all N employees on LinkedIn" count in the top card, which sits *above*
# the "Company size" row. Matching a bare count would cache a follower number
# as headcount for 90 days, so the band pattern deliberately requires a dash
# or a trailing '+'.
_SIZE_BAND = re.compile(
    r"(\d[\d,]*\s*-\s*[\d,]+|\d[\d,]*\+)\s*(?:employees|associated members)",
    re.I,
)
# Preferred: the value on the labelled "Company size" row.
_SIZE_LABELLED = re.compile(r"Company size\s*[:\n]\s*([^\n]+)", re.I)
# The About page lists these as labelled rows. Values run to end-of-line.
_ABOUT_FIELDS = {
    "industry": re.compile(r"^\s*Industry\s*[:\n]\s*(.+)$", re.I | re.M),
    "headquarters": re.compile(r"^\s*Headquarters\s*[:\n]\s*(.+)$", re.I | re.M),
    "website": re.compile(r"^\s*Website\s*[:\n]\s*(\S+)$", re.I | re.M),
    "founded": re.compile(r"^\s*Founded\s*[:\n]\s*(.+)$", re.I | re.M),
}
_URL = re.compile(r"https?://[^\s|,]+", re.I)


def _first_line(value: str) -> str:
    return value.strip().splitlines()[0].strip() if value.strip() else ""


def parse_about(text: str) -> dict[str, str]:
    """Pull firmographics from a company About page's innerText.

    Returns only the keys it is confident about; a caller merges these over
    whatever the cache already holds, so missing keys leave prior values intact.
    """
    out: dict[str, str] = {}
    if not text:
        return out

    for field, pattern in _ABOUT_FIELDS.items():
        m = pattern.search(text)
        if m:
            val = _first_line(m.group(1))
            # The website value can carry trailing text after the URL on some
            # layouts; keep just the URL when the labelled line holds one.
            if field == "website":
                url_m = _URL.search(val)
                val = url_m.group(0) if url_m else val
            if val:
                out[field] = val

    # Prefer the band on the labelled "Company size" row; fall back to the
    # first band anywhere. Either way only a real band counts, never a bare
    # follower count.
    labelled = _SIZE_LABELLED.search(text)
    band = None
    if labelled:
        band = _SIZE_BAND.search(labelled.group(1))
    if band is None:
        band = _SIZE_BAND.search(text)
    if band:
        out["employee_count"] = re.sub(r"\s+", "", band.group(1)) + " employees"

    return out


class ParsedJobs(NamedTuple):
    count: int | None
    sample: list[str]


# A line looks like an open role if it carries a role word...
_ROLE_HINT = re.compile(
    r"\b(engineer|developer|architect|consultant|analyst|administrator|"
    r"specialist|designer|manager|director|executive|representative|"
    r"account\s+executive|scientist|recruiter|controller|accountant)\b",
    re.I,
)
# ...but NOT if it is LinkedIn's own chrome. The Jobs tab renders the global
# nav/footer ("Sales Solutions", "Talent Solutions", "Marketing Solutions",
# "Advertising", cookie/privacy links), whose items contain role-ish words but
# are not openings. Excluding "solutions" et al. drops those without dropping a
# genuine "Sales Manager".
_JOBS_NOISE = re.compile(
    r"\b(solutions|linkedin|advertising|privacy|cookie|guidelines|"
    r"accessibility|about|careers\b|help\s+center)\b"
    r"|see\s+all|show\s+more|·|follow|·|jobs?\s+you",
    re.I,
)


_JOB_RESULTS = re.compile(r"(\d[\d,]*)\s*\+?\s*results?\b", re.I)
_NO_JOBS = re.compile(r"no matching jobs|no results|0 results", re.I)


def parse_job_search(text: str) -> ParsedJobs:
    """Count and sample open roles from a job-SEARCH page filtered by company.

    This is the real source of a company's openings: LinkedIn's
    ``/jobs/search/?f_C=<company id>`` page, which shows its "N results" count
    and the job cards. (The company Page's own /jobs/ tab only lists roles
    posted directly on the Page, which most employers don't use, so it reads
    "no jobs" even for companies hiring thousands -- verified live.)

    ``count`` comes from the "N results" header; a "2,000+ results" cap is read
    as its floor (2000). ``sample`` is a best-effort set of titles.
    """
    if not text:
        return ParsedJobs(None, [])
    if _NO_JOBS.search(text):
        return ParsedJobs(0, [])

    count: int | None = None
    m = _JOB_RESULTS.search(text)
    if m:
        count = int(m.group(1).replace(",", ""))

    seen: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not (3 <= len(line) <= 80):
            continue
        # LinkedIn renders each title twice: once plain, once
        # "<title> with verification". Keep the plain line, drop the dup.
        if line.lower().endswith("with verification"):
            continue
        if "results" in line.lower():
            continue
        if _ROLE_HINT.search(line) and not _JOBS_NOISE.search(line):
            if line not in seen:
                seen.append(line)
        if len(seen) >= 8:
            break
    return ParsedJobs(count, seen)


# LinkedIn labels a promoted top result "Page by <Company>" -- verified live,
# where "Page by Salesforce" links to /company/salesforce/. The literal label
# must be stripped or it never matches the query.
_PAGE_BY = re.compile(r"^page by\s+", re.I)
# Follower/relationship blurbs also carry a /company/ link ("Vamsi Krishna &
# 1 other connection follow this page") but are not company names.
_REF_NOISE = re.compile(
    r"follow this page|connections? follow|other connection|alternative to",
    re.I,
)


def parse_search_results(references: list[dict]) -> list[dict[str, str]]:
    """Reduce company-search link references to {name, slug, url} hits.

    LinkedIn's company-search results carry a company link per card, but the
    link *text* is unreliable: the top result is labelled "Page by <Company>",
    and follower/blurb lines also link to /company/. The slug in the URL is the
    canonical identity, so it is the fallback whenever the text is an ad label
    or noise -- that is what lets a query like "Salesforce" resolve to the
    /company/salesforce/ card even though its label reads "Page by Salesforce".
    """
    hits: list[dict[str, str]] = []
    seen: set[str] = set()
    for ref in references or []:
        url = ref.get("url", "")
        m = re.search(r"/company/([^/?#]+)", url)
        if not m:
            continue
        slug = m.group(1)
        if slug in seen:
            continue
        seen.add(slug)
        # Strip the "Page by " ad prefix; drop follower/blurb text entirely.
        # Whatever is left, if anything, is the name; else fall back to the
        # slug (spaces for dashes), which is the reliable company identity.
        text = (ref.get("text") or "").strip()
        if _REF_NOISE.search(text):
            text = ""
        name = _PAGE_BY.sub("", text).strip() or slug.replace("-", " ")
        hits.append(
            {
                "name": name,
                "slug": slug,
                "url": f"https://www.linkedin.com/company/{slug}",
            }
        )
    return hits
