"""Content-search workflow behind the LinkedIn "Posts" tab."""

from __future__ import annotations

from typing import Any

from linkedin_mcp_server.scraping.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.scraping.contracts import RATE_LIMITED_SECTION_TEXT
from linkedin_mcp_server.scraping.link_metadata import (
    _SEARCH_RESULTS_REFERENCE_CAP,
    Reference,
    dedupe_references,
)
from linkedin_mcp_server.scraping.search_urls import build_content_search_url


class PostSearch:
    """Own the one workflow whose subject is LinkedIn post content.

    The search is a single capture, so the section reader is the only
    collaborator it takes: there is no walk to pace and no page to navigate
    itself.
    """

    def __init__(self, capture: SectionCapture):
        self._capture = capture

    async def search_posts(
        self,
        keywords: str,
        date_posted: str | None = None,
        max_posts: int = 10,
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
            max_posts: Stop scrolling once this many result cards are loaded
                (default 10). Content search is an infinite scroll with no
                per-page URL, so the loop counts cards rather than pages; the
                page may hold a few more than this when a scroll batch
                overshoots.

        Returns:
            {url, sections: {search_results: text}} plus optional ``references``
            (post authors, companies, linked jobs) and ``section_errors``.
            Verified live: the results page carries no per-post permalink
            anchors, so a post is addressable only through its author; the
            ``/in/`` entries in ``references["search_results"]`` make the
            result usable as a prospect list. The LLM should parse the raw
            text to extract each post's author, headline, body, date, and
            reaction counts.
        """
        # Builds before it navigates, so a recency filter LinkedIn would
        # ignore is refused rather than answered with unfiltered results.
        url = build_content_search_url(keywords, date_posted=date_posted)
        # Uncapped: the section cap (15) is below what ``max_posts`` allows,
        # and a post is addressable only through its author, so a capped
        # capture cut the prospect list short. Capped here instead, as the
        # paged searches do, so the cap grows with the request.
        extracted = await self._capture.capture(
            url,
            section_name="search_results",
            plan=CapturePlan(
                CaptureMode.SEARCH_RESULTS | CaptureMode.CONTENT_SEARCH,
                max_posts=max_posts,
                apply_cap=False,
            ),
        )

        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
            sections["search_results"] = extracted.text
            if extracted.references:
                references["search_results"] = dedupe_references(
                    extracted.references,
                    cap=max(max_posts, _SEARCH_RESULTS_REFERENCE_CAP),
                )
        elif extracted.text == RATE_LIMITED_SECTION_TEXT:
            section_errors["search_results"] = {
                "error_type": "rate_limit",
                "error_message": extracted.text,
            }
        elif extracted.error:
            section_errors["search_results"] = extracted.error

        result: dict[str, Any] = {"url": url, "sections": sections}
        if references:
            result["references"] = references
        if section_errors:
            result["section_errors"] = section_errors
        return result
