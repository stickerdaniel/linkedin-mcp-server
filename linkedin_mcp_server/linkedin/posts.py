"""Content-search workflow behind the LinkedIn "Posts" tab."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import json

from linkedin_mcp_server.linkedin.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.linkedin.contracts import RATE_LIMITED_SECTION_TEXT
from linkedin_mcp_server.linkedin.link_metadata import Reference
from linkedin_mcp_server.linkedin.search_urls import (
    CONTENT_SORT_BY_MAP,
    build_content_search_url,
)
from linkedin_mcp_server.linkedin.session import PageSession

# Content search is an infinite scroll with no ``&start=`` pagination, so
# ``max_pages`` caps scroll depth instead of fetching discrete pages. One
# nominal "page" is this many scrolls.
_CONTENT_SCROLLS_PER_REQUESTED_PAGE = 5


def _retains_sort(landed_url: str, token: str) -> bool:
    try:
        values = parse_qs(urlsplit(landed_url).query, keep_blank_values=True).get(
            "sortBy", []
        )
        return len(values) == 1 and json.loads(values[0]) == [token]
    except ValueError:
        return False


class PostSearch:
    """Own the one workflow whose subject is LinkedIn post content.

    The section reader owns navigation and capture; the session supplies the
    settled URL so an ignored sort cannot look as though it was retained.
    """

    def __init__(self, session: PageSession, capture: SectionCapture):
        self._session = session
        self._capture = capture

    async def search_posts(
        self,
        keywords: str,
        date_posted: str | None = None,
        max_pages: int = 3,
        content_type: str | None = None,
        sort_by: str | None = None,
    ) -> dict[str, Any]:
        """Search LinkedIn posts/content and extract the results page.

        Reproduces the LinkedIn "Posts" content-search tab — the surface for
        catching informal "we're hiring" / "Buscamos ..." posts before a
        formal job listing exists.

        Args:
            keywords: Free-text query (e.g. "Buscamos Unity", "estamos contratando").
            date_posted: Optional recency filter, one of the keys of
                ``search_urls.CONTENT_DATE_POSTED_MAP``. Invalid values raise
                ``FilterValidationError`` (a ``ValueError`` subclass) rather
                than reaching LinkedIn, which would ignore them silently and
                return unfiltered results that look filtered.
            content_type: Optional "Content type" filter, one of the keys of
                ``search_urls.CONTENT_TYPE_MAP`` (e.g. "documents" for the
                PDFs and slide decks people upload). Refused when unknown,
                like ``date_posted``.
            sort_by: Optional ``date`` / ``date_posted`` for Latest or
                ``relevance`` for Top match. Unknown values are refused;
                omission or a blank value keeps LinkedIn's default ordering.
            max_pages: Scroll depth, expressed in result "pages" of roughly
                ``_CONTENT_SCROLLS_PER_REQUESTED_PAGE`` scrolls each (default
                3). Content search is an infinite scroll with no per-page URL,
                so this caps how far the page is scrolled rather than fetching
                discrete ``&start=`` pages.

        Returns:
            {url, sections: {search_results: text}} plus optional ``references``
            (post authors, companies, linked jobs, and ``feed_post`` permalinks
            read from the payload responses) and ``section_errors``.
            Verified live: the results page renders no per-post permalink
            anchors in the DOM; the permalinks come from the JSON/document
            responses instead, in either ``/feed/update/<urn>/`` or
            ``/posts/<slug>`` form (both valid). The LLM should parse the raw
            text to extract each post's author, headline, body, date, and
            reaction counts.
            If the settled URL drops or changes an explicit sort, results are
            retained with a ``filters_dropped`` section error.
        """
        # Builds before it navigates, so a recency filter LinkedIn would
        # ignore is refused rather than answered with unfiltered results.
        url = build_content_search_url(
            keywords,
            date_posted=date_posted,
            content_type=content_type,
            sort_by=sort_by,
        )
        sort_token = (
            CONTENT_SORT_BY_MAP[sort_by.strip()]
            if sort_by and sort_by.strip()
            else None
        )
        max_scrolls = max(1, max_pages) * _CONTENT_SCROLLS_PER_REQUESTED_PAGE
        extracted = await self._capture.capture(
            url,
            section_name="search_results",
            plan=CapturePlan(
                CaptureMode.SEARCH_RESULTS | CaptureMode.POST_PERMALINKS,
                max_scrolls,
            ),
        )

        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
            sections["search_results"] = extracted.text
            if extracted.references:
                references["search_results"] = extracted.references
        elif extracted.text == RATE_LIMITED_SECTION_TEXT:
            section_errors["search_results"] = {
                "error_type": "rate_limit",
                "error_message": extracted.text,
            }
        elif extracted.error:
            section_errors["search_results"] = extracted.error

        if sort_token is not None and extracted.text != RATE_LIMITED_SECTION_TEXT:
            if extracted.error:
                section_errors["search_results"] = extracted.error
            elif not _retains_sort(self._session.page.url, sort_token):
                section_errors["search_results"] = {
                    "error_type": "filters_dropped",
                    "error_message": (
                        f"LinkedIn did not keep sortBy={json.dumps([sort_token])}; "
                        "the returned post order is not verified."
                    ),
                }

        result: dict[str, Any] = {"url": url, "sections": sections}
        if references:
            result["references"] = references
        if section_errors:
            result["section_errors"] = section_errors
        return result
