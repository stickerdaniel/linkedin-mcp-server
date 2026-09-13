"""Multi-page walk shared by the search workflows."""

from __future__ import annotations

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
    rate_limited_section_error,
)
from linkedin_mcp_server.scraping.link_metadata import Reference
from linkedin_mcp_server.scraping.session import NAV_DELAY, ScrapingSession

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchPages:
    """What a paged search walk gathered before the pages are joined.

    ``page_references`` hold each page's references in page order, ready for
    one ``dedupe_references`` pass by the caller.
    """

    page_texts: list[str] = field(default_factory=list)
    page_references: list[Reference] = field(default_factory=list)
    section_errors: dict[str, dict[str, Any]] = field(default_factory=dict)


async def paginate_search(
    capture: SectionCapture,
    session: ScrapingSession,
    base_url: str,
    *,
    kind: str,
    max_pages: int,
) -> SearchPages:
    """Walk LinkedIn's ``&page=N`` facet from ``base_url`` up to ``max_pages``.

    Stops once a page adds no new ``kind`` reference: running past the last
    page re-serves it, and that is detected by URL rather than by parsing
    LinkedIn's localized "no results" copy. A page that comes back throttled
    or errored ends the walk and is reported in ``section_errors``; the pages
    gathered before it are kept.
    """
    gathered = SearchPages()
    seen_urls: set[str] = set()

    for page_num in range(1, max_pages + 1):
        if page_num > 1:
            await session.pace(NAV_DELAY)

        url = base_url if page_num == 1 else f"{base_url}&page={page_num}"
        extracted = await capture.capture(
            url,
            "search_results",
            CapturePlan(CaptureMode.SEARCH_RESULTS),
        )

        if not extracted.text or extracted.text == RATE_LIMITED_SECTION_TEXT:
            # Rate limit first: it is the more specific diagnosis, and a
            # page that was throttled may carry a generic error too.
            if extracted.text == RATE_LIMITED_SECTION_TEXT:
                gathered.section_errors["search_results"] = rate_limited_section_error()
            elif extracted.error:
                gathered.section_errors["search_results"] = extracted.error
            break

        gathered.page_texts.append(extracted.text)
        if extracted.references:
            gathered.page_references.extend(extracted.references)

        new_urls = {
            ref["url"] for ref in extracted.references if ref["kind"] == kind
        } - seen_urls
        if not new_urls:
            logger.debug("No new %s references on page %d, stopping", kind, page_num)
            break
        seen_urls |= new_urls

    return gathered
