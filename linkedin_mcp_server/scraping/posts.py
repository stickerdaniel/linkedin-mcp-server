"""Post-content workflows: content search and the saved-items list."""

from __future__ import annotations

from typing import Any

import logging

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.scraping.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
)
from linkedin_mcp_server.scraping.link_metadata import (
    Reference,
    build_references,
    dedupe_references,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.search_urls import build_content_search_url
from linkedin_mcp_server.scraping.session import ScrapingSession
from linkedin_mcp_server.scraping.text import (
    filter_linkedin_noise_lines,
    truncate_linkedin_noise,
)

logger = logging.getLogger(__name__)

# Content search is an infinite scroll with no ``&start=`` pagination, so
# ``max_pages`` caps scroll depth instead of fetching discrete pages. One
# nominal "page" is this many scrolls.
_CONTENT_SCROLLS_PER_REQUESTED_PAGE = 5

SAVED_POSTS_URL = "https://www.linkedin.com/my-items/saved-posts/"

# Progress while scrolling the saved-items list, counted from anchors in
# <main>. ``/feed/update/`` covers posts, ``/pulse/`` covers articles, and a
# URL pattern is locale-independent — the page offers no countable container
# structure to lean on instead. ``?start=`` offsets are a no-op on this
# surface: verified live on 2026-09-15, where ``?start=10`` returned the
# first ten items again (unlike saved-jobs, which paginates by offset).
_SAVED_ITEM_ANCHOR_COUNT_JS = r"""() => {
    const main = document.querySelector('main') || document.body;
    return main.querySelectorAll(
        'a[href*="/feed/update/"], a[href*="/pulse/"]'
    ).length;
}"""

_MAX_SAVED_POSTS_SCROLLS = 12
_MAX_SAVED_POSTS_STALE = 3


class PostSearch:
    """Own the workflows whose subject is LinkedIn post content.

    Content search is a single scrolled capture, while saved items need
    their own scroll loop with an anchor-count progress signal. Both stay
    here so the facade sees one owner for post-shaped surfaces.
    """

    def __init__(
        self,
        session: ScrapingSession,
        navigator: PageNavigator,
        content: PageContentReader,
        capture: SectionCapture,
    ):
        self._session = session
        self._navigator = navigator
        self._content = content
        self._capture = capture

    async def search_posts(
        self,
        keywords: str,
        date_posted: str | None = None,
        max_pages: int = 3,
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
            max_pages: Scroll depth, expressed in result "pages" of roughly
                ``_CONTENT_SCROLLS_PER_REQUESTED_PAGE`` scrolls each (default
                3). Content search is an infinite scroll with no per-page URL,
                so this caps how far the page is scrolled rather than fetching
                discrete ``&start=`` pages.

        Returns:
            {url, sections: {search_results: text}} plus optional ``references``
            (post authors, companies, linked jobs) and ``section_errors``.
            Verified live: the results page carries no per-post permalink
            anchors, so a post is addressable only through its author.
            The LLM should parse the raw text to extract each post's author,
            headline, body, date, and reaction counts.
        """
        # Builds before it navigates, so a recency filter LinkedIn would
        # ignore is refused rather than answered with unfiltered results.
        url = build_content_search_url(keywords, date_posted=date_posted)
        max_scrolls = max(1, max_pages) * _CONTENT_SCROLLS_PER_REQUESTED_PAGE
        extracted = await self._capture.capture(
            url,
            section_name="search_results",
            plan=CapturePlan(CaptureMode.SEARCH_RESULTS, max_scrolls),
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

        result: dict[str, Any] = {"url": url, "sections": sections}
        if references:
            result["references"] = references
        if section_errors:
            result["section_errors"] = section_errors
        return result

    async def get_saved_posts(self, num_posts: int = 10) -> dict[str, Any]:
        """List the authenticated user's saved posts and articles.

        Navigates to ``/my-items/saved-posts/`` and scrolls until at least
        ``num_posts`` saved-item anchors are present in ``<main>``, or the
        list stops growing. Saved-post anchors take the
        ``/feed/update/<urn>/`` form, saved-article anchors the
        ``/pulse/<slug>/`` form; both are kept while author and company
        links are dropped, like the home feed's reference filter. Because
        the count signal counts in-page anchors, no locale-dependent text
        participates in deciding when to stop.

        Truncated post bodies are not auto-expanded; the full text of any
        item is reachable through its permalink in ``references["saved_posts"]``.

        Returns:
            {url, sections: {saved_posts: text}, references? , section_errors?}
        """
        try:
            extracted = await self._get_saved_posts_once(num_posts)
            return self._saved_posts_result(extracted)
        except LinkedInScraperException:
            raise
        except Exception as e:
            logger.warning("Failed to extract saved posts: %s", e)
            result: dict[str, Any] = {"url": SAVED_POSTS_URL, "sections": {}}
            result["section_errors"] = {
                "saved_posts": build_issue_diagnostics(e, context="extract_saved_posts")
            }
            return result

    def _saved_posts_result(self, extracted: ExtractedSection) -> dict[str, Any]:
        """Assemble the tool-shaped dict from one capture."""
        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
            sections["saved_posts"] = extracted.text
            if extracted.references:
                references["saved_posts"] = extracted.references
        elif extracted.text == RATE_LIMITED_SECTION_TEXT:
            section_errors["saved_posts"] = {
                "error_type": "rate_limit",
                "error_message": extracted.text,
            }
        elif extracted.error:
            section_errors["saved_posts"] = extracted.error

        result: dict[str, Any] = {"url": SAVED_POSTS_URL, "sections": sections}
        if references:
            result["references"] = references
        if section_errors:
            result["section_errors"] = section_errors
        return result

    async def _get_saved_posts_once(self, num_posts: int) -> ExtractedSection:
        """Single attempt: navigate, scroll until enough saved items, extract."""
        page = self._session.page
        await self._navigator._navigate_to_page(SAVED_POSTS_URL)
        await self._session.check_rate_limit()

        try:
            await page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("No <main> element found on %s", SAVED_POSTS_URL)

        await self._session.dismiss_modal()

        stale_count = 0
        for scroll in range(_MAX_SAVED_POSTS_SCROLLS):
            count = await page.evaluate(_SAVED_ITEM_ANCHOR_COUNT_JS)
            logger.debug("Saved posts scroll %d: %d item anchors", scroll, count)
            if count >= num_posts:
                break

            # window.scrollBy advances this list; the home feed needs
            # mouse.wheel because it scrolls a container of its own.
            await page.evaluate("window.scrollBy(0, 2000)")
            await self._session.delay(1.0)

            new_count = await page.evaluate(_SAVED_ITEM_ANCHOR_COUNT_JS)
            if new_count > count:
                stale_count = 0
            else:
                stale_count += 1
                logger.debug(
                    "Saved posts stale scroll %d/%d (still at %d item anchors)",
                    stale_count,
                    _MAX_SAVED_POSTS_STALE,
                    new_count,
                )
                if stale_count >= _MAX_SAVED_POSTS_STALE:
                    logger.debug("Saved posts list stopped growing")
                    break

        raw_result = await self._content._extract_root_content(["main"])
        raw = raw_result["text"]

        if not raw:
            return ExtractedSection(text="", references=[])
        truncated = truncate_linkedin_noise(raw)
        if not truncated and raw.strip():
            logger.warning(
                "Page %s returned only LinkedIn chrome (likely rate-limited)",
                SAVED_POSTS_URL,
            )
            return ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[])
        cleaned = filter_linkedin_noise_lines(truncated)
        # Keep only saved-item permalinks. Author and company anchors ride
        # along in the DOM and would dilute the cap, exactly like the home
        # feed's filter in feed_payload.build_feed_references.
        references = dedupe_references(
            [
                ref
                for ref in build_references(raw_result["references"], "saved_posts")
                if ref["kind"] in ("feed_post", "article")
            ],
            cap=50,
        )
        return ExtractedSection(text=cleaned, references=references)
