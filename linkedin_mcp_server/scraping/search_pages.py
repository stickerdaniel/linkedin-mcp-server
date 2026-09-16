"""Multi-page walk and row parsing shared by people and company search."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import logging

from linkedin_mcp_server.scraping.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
    rate_limited_section_error,
)
from linkedin_mcp_server.scraping.link_metadata import (
    _SEARCH_RESULTS_REFERENCE_CAP,
    Reference,
    dedupe_references,
)
from linkedin_mcp_server.scraping.search_parse import parse_result_count
from linkedin_mcp_server.scraping.session import ScrapingSession, nav_delay

logger = logging.getLogger(__name__)

_CardParser = Callable[[str, Sequence[Mapping[str, Any]]], list[dict[str, Any]]]


def search_rows(
    parser: _CardParser, pages: Sequence[ExtractedSection], kind: str
) -> tuple[list[dict[str, Any]], int | None]:
    """Rows across the fetched results pages, deduped by URL, plus the result
    count from the first page.

    Each page is parsed on its own: the pages are only joined into one text
    for ``sections`` afterwards, so a card can never straddle the separator.
    A parser failure is logged and yields no rows for that page; the raw text
    still reaches the caller, and a parser bug must never take the tool down.

    ``kind`` is the reference kind the parser pairs rows with. A page that
    carries such references but parses to no rows is a page of cards the
    text parser did not recognise (a layout change, or a locale whose
    degree and followers tokens differ), and is warned about rather than
    passed off as an empty result.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    result_count = parse_result_count(pages[0].text) if pages else None
    for index, page in enumerate(pages):
        try:
            page_rows = parser(page.text, page.references)
        except Exception:
            logger.warning(
                "Could not parse result cards on page %d", index + 1, exc_info=True
            )
            continue
        if not page_rows:
            anchors = sum(1 for ref in page.references if ref.get("kind") == kind)
            if anchors:
                logger.warning(
                    "Page %d: %d references but no result rows parsed "
                    "(unrecognised card layout or locale)",
                    index + 1,
                    anchors,
                )
        for row in page_rows:
            url = row.get("url")
            if url is not None:
                if url in seen:
                    continue
                seen.add(url)
            rows.append(row)
    return rows, result_count


@dataclass(frozen=True)
class SearchPages:
    """What a paged search walk gathered before the pages are joined.

    ``pages`` carry every anchor (uncapped) for row pairing; ``page_references``
    hold each page's capped, deduped references in page order, ready for one
    more ``dedupe_references`` pass by the caller.
    """

    page_texts: list[str] = field(default_factory=list)
    page_references: list[Reference] = field(default_factory=list)
    pages: list[ExtractedSection] = field(default_factory=list)
    section_errors: dict[str, dict[str, Any]] = field(default_factory=dict)


async def paginate_search(
    capture: SectionCapture,
    session: ScrapingSession,
    base_url: str,
    *,
    kind: str,
    max_pages: int,
    pace_first: bool,
) -> SearchPages:
    """Walk LinkedIn's ``&page=N`` facet from ``base_url`` up to ``max_pages``.

    Stops once a page adds no new ``kind`` reference: running past the last
    page re-serves it, and that is detected by URL rather than by parsing
    LinkedIn's localized "no results" copy. A page that comes back throttled
    or errored ends the walk and is reported in ``section_errors``; the pages
    gathered before it are kept.

    ``pace_first`` spaces the first page like every later one, for a caller
    whose facet resolution has just navigated.
    """
    gathered = SearchPages()
    seen_urls: set[str] = set()

    for page_num in range(1, max_pages + 1):
        if page_num > 1 or pace_first:
            await session.pace(nav_delay())

        url = base_url if page_num == 1 else f"{base_url}&page={page_num}"
        # Uncapped: the rows pair against every anchor on the page, and a
        # people card carries up to two mutual-connection anchors of its
        # own, so the section cap would strand the later cards without a
        # URL. The cap is applied per page to ``page_references`` below.
        extracted = await capture.capture(
            url,
            "search_results",
            CapturePlan(CaptureMode.SEARCH_RESULTS, apply_cap=False),
        )

        if not extracted.text or extracted.text == RATE_LIMITED_SECTION_TEXT:
            # Rate limit first: it is the more specific diagnosis, and a
            # page that was throttled may carry a generic error too.
            if extracted.text == RATE_LIMITED_SECTION_TEXT:
                gathered.section_errors["search_results"] = rate_limited_section_error()
            elif extracted.error:
                gathered.section_errors["search_results"] = extracted.error
            break

        new_urls = {
            ref["url"] for ref in extracted.references if ref["kind"] == kind
        } - seen_urls
        if not new_urls and page_num > 1:
            # A later page with nothing new is the last page served again;
            # keeping its text would join the same people twice. The first
            # page is kept regardless so an empty result still shows its text.
            logger.debug("No new %s references on page %d, stopping", kind, page_num)
            break

        gathered.page_texts.append(extracted.text)
        gathered.pages.append(extracted)
        if extracted.references:
            gathered.page_references.extend(
                dedupe_references(
                    extracted.references, cap=_SEARCH_RESULTS_REFERENCE_CAP
                )
            )
        if not new_urls:
            break
        seen_urls |= new_urls

    return gathered
