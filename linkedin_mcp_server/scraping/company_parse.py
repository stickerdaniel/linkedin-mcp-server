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

# LinkedIn renders the headcount band in a stable, localisable-but-mostly-English
# shape: "1,001-5,000 employees", "10,001+ employees", "51-200 employees".
_SIZE = re.compile(
    r"(\d[\d,]*(?:\s*-\s*[\d,]+|\+)?)\s*(?:employees|associated members)",
    re.I,
)
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
            # A URL-looking website may arrive without the label on some
            # layouts; the labelled capture is preferred but we clean it either
            # way.
            if field == "website":
                url_m = _URL.search(val)
                val = url_m.group(0) if url_m else val
            if val:
                out[field] = val

    size = _SIZE.search(text)
    if size:
        out["employee_count"] = re.sub(r"\s+", "", size.group(1)) + " employees"

    return out


class ParsedJobs(NamedTuple):
    count: int | None
    sample: list[str]


def parse_jobs(text: str) -> ParsedJobs:
    """Count and sample the open roles on a company Jobs tab.

    ``count`` is read from LinkedIn's own "N open jobs" / "N jobs" heading when
    present, since that is authoritative; otherwise it falls back to the number
    of distinct role lines actually seen, which under-counts but never invents.
    ``sample`` is a handful of titles for the buying-signal read (a company
    hiring Salesforce admins is investing in Salesforce now).
    """
    if not text:
        return ParsedJobs(None, [])

    count: int | None = None
    heading = re.search(
        r"([\d,]+)\s*(?:open\s+jobs?|jobs?\b|open\s+roles?|open\s+positions?)",
        text,
        re.I,
    )
    if heading:
        count = int(heading.group(1).replace(",", ""))

    # Role lines on the jobs tab are short title-case strings; keep a few
    # distinct ones as the sample. This is a heuristic, hence the raw text is
    # cached too.
    seen: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not (3 <= len(line) <= 80):
            continue
        if re.search(
            r"\b(engineer|developer|manager|architect|consultant|analyst|"
            r"director|administrator|specialist|lead|designer|sales|success|"
            r"salesforce|devops|account|product|marketing|officer|head)\b",
            line,
            re.I,
        ) and not re.search(r"(open jobs|see all|show more|·|filter)", line, re.I):
            if line not in seen:
                seen.append(line)
        if len(seen) >= 8:
            break

    # If we saw roles but no heading, the count is at least what we saw.
    if count is None and seen:
        count = len(seen)

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
        hits.append(
            {
                "name": (ref.get("text") or slug).strip(),
                "slug": slug,
                "url": f"https://www.linkedin.com/company/{slug}",
            }
        )
    return hits
