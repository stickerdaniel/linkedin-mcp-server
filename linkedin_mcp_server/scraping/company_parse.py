"""Best-effort structured extraction from company page/search innerText.

The rest of this codebase returns raw section text and lets the caller parse
it, precisely because LinkedIn's markup is a moving target. These parsers do
not replace that -- the raw text is always kept alongside -- they exist so the
cache can hold typed fields (industry, headcount band, open-roles count) that
survive without an LLM in the loop.

Everything here is a pure function over text: no browser, no network, no clock.
Each is deliberately conservative -- it would rather return "" than guess,
because a wrong industry cached for 90 days is worse than a blank one.
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


def parse_jobs(text: str) -> ParsedJobs:
    """Count and sample the open roles on a company Jobs tab.

    ``count`` is read *only* from LinkedIn's own "N open jobs" heading, which is
    authoritative; with no heading it stays ``None``. It is never derived from
    how many role-ish lines were scraped -- a company with zero openings still
    renders the nav/footer, and counting those would invent a positive hiring
    signal, the exact thing this module must not do. ``sample`` is a best-effort
    handful of titles for the buying-signal read; the raw Jobs text is cached
    alongside as the source of truth.
    """
    if not text:
        return ParsedJobs(None, [])

    count: int | None = None
    heading = re.search(
        r"([\d,]+)\s*(?:open\s+jobs?|open\s+roles?|open\s+positions?|jobs?\b)",
        text,
        re.I,
    )
    if heading:
        count = int(heading.group(1).replace(",", ""))

    seen: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not (3 <= len(line) <= 80):
            continue
        if _ROLE_HINT.search(line) and not _JOBS_NOISE.search(line):
            if line not in seen:
                seen.append(line)
        if len(seen) >= 8:
            break

    return ParsedJobs(count, seen)


def parse_search_results(references: list[dict]) -> list[dict[str, str]]:
    """Reduce company-search link references to {name, url} hits.

    LinkedIn's company-search results page carries ~10 company links, each a
    reference with the company's display text. Industry/location sit in the
    surrounding innerText rather than the link, so the caller keeps the raw
    ``search_results`` text for that; this just gives the clean company list
    and their canonical /company/<slug> URLs to key the cache on.
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
        # A whitespace-only text ("  ") is truthy, so `text or slug` would keep
        # it and then .strip() to "" -- an empty name later blows up the cache
        # key. Strip first, fall back to slug only when nothing survives.
        name = (ref.get("text") or "").strip() or slug
        hits.append(
            {
                "name": name,
                "slug": slug,
                "url": f"https://www.linkedin.com/company/{slug}",
            }
        )
    return hits
