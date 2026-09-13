"""Generic page and overlay section capture."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Flag, auto
from urllib.parse import urlparse

import logging

from patchright._impl._errors import TargetClosedError
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
)
from linkedin_mcp_server.scraping.link_metadata import build_references
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession
from linkedin_mcp_server.scraping.text import (
    DETAIL_CAPTURE_EN_US,
    DetailCaptureTextTable,
    filter_linkedin_noise_lines,
    truncate_linkedin_noise,
)

logger = logging.getLogger(__name__)

# Content search is an infinite scroll with no ``&start=`` pagination, and
# the results render in an inner scrollable region, so ``window.scrollTo``
# never moves it. ``_scroll_content_search_results`` wheel-scrolls instead
# and stops on a result count, so this is only a runaway guard.
CONTENT_SEARCH_MAX_SCROLLS = 20

# Counts result cards on a content-search page. Every card links its author
# (``/in/`` or ``/company/``), but so does every @-mention in a post body,
# and one mention-heavy post reached ``max_posts`` before the first wheel
# when the count was distinct hrefs. Anchors are grouped by their nearest
# list-item or article ancestor, which is structure rather than layout; where
# no such ancestor exists the distinct hrefs stand in, which is the old
# count. Either way the estimate errs toward scrolling further, never toward
# reporting fewer cards than the anchors seen.
# TODO(live-verify): the ancestor chain of a content-search card is
# unverified live; the fallback is what makes an unexpected chain harmless.
CONTENT_SEARCH_COUNT_JS = r"""() => {
    const main = document.querySelector('main');
    if (!main) return 0;
    const cards = new Set(), hrefs = new Set();
    for (const a of main.querySelectorAll(
        'a[href*="/in/"], a[href*="/company/"]'
    )) {
        hrefs.add(a.getAttribute('href').split('?')[0]);
        const card = a.closest('li, article, [role="article"]');
        if (card) cards.add(card);
    }
    return cards.size || hrefs.size;
}"""

# Wall-clock ceiling on one content-search wheel loop. Without it the worst
# case is every round polling to its full wait, which is twice the tool
# timeout's comfortable share.
CONTENT_SEARCH_SCROLL_BUDGET = 60.0


class CaptureMode(Flag):
    """Independent post-navigation behaviors applied during section capture."""

    STANDARD = 0
    ACTIVITY = auto()
    SEARCH_RESULTS = auto()
    COMPANY_PEOPLE = auto()
    DETAILS = auto()
    OVERLAY = auto()
    CONTENT_SEARCH = auto()


@dataclass(frozen=True)
class CapturePlan:
    """Immutable policy for one section capture.

    ``max_posts`` only applies under ``CONTENT_SEARCH``, where the scroll is
    count-driven rather than depth-driven. ``apply_cap=False`` returns every
    reference the page carries; the caller then owns the section's cap.
    """

    mode: CaptureMode = CaptureMode.STANDARD
    max_scrolls: int | None = None
    max_posts: int | None = None
    apply_cap: bool = True


def capture_plan_for_url(url: str, max_scrolls: int | None = None) -> CapturePlan:
    """Translate a generic compatibility URL into its historical capture policy."""
    path = urlparse(url).path
    mode = CaptureMode.STANDARD
    if "/recent-activity/" in path or (
        "/company/" in path and path.rstrip("/").endswith("/posts")
    ):
        mode |= CaptureMode.ACTIVITY
    if "/search/results/" in url:
        mode |= CaptureMode.SEARCH_RESULTS
    if "/search/results/content/" in path:
        mode |= CaptureMode.CONTENT_SEARCH
    if "/company/" in url and "/people/" in url:
        mode |= CaptureMode.COMPANY_PEOPLE
    if "/details/" in url:
        mode |= CaptureMode.DETAILS
    return CapturePlan(mode=mode, max_scrolls=max_scrolls)


class SectionCapture:
    """Capture one section from a loaded page or from an overlay dialog."""

    def __init__(
        self,
        session: ScrapingSession,
        navigator: PageNavigator,
        content: PageContentReader,
        detail_text: DetailCaptureTextTable = DETAIL_CAPTURE_EN_US,
    ):
        self._session = session
        self._navigator = navigator
        self._content = content
        self._detail_text = detail_text

    async def extract_page(
        self,
        url: str,
        section_name: str,
        max_scrolls: int | None = None,
    ) -> ExtractedSection:
        """Compatibility adapter for generic URL-derived page capture."""
        return await self.capture(
            url,
            section_name,
            capture_plan_for_url(url, max_scrolls),
        )

    async def capture(
        self,
        url: str,
        section_name: str,
        plan: CapturePlan,
    ) -> ExtractedSection:
        """Navigate and capture a section according to an explicit plan.

        Retries after a backoff when the page returns only LinkedIn chrome
        (sidebar/footer noise with no actual content), which indicates a soft
        rate limit, for as long as the scrape-wide retry budget allows. Page
        and overlay reads draw on the same budget, because the requests land
        on the same limit.
        """
        try:
            result = await self._capture_once(url, section_name, plan)
            if result.text != RATE_LIMITED_SECTION_TEXT:
                return result

            if not await self._session.claim_soft_retry(url):
                return result
            return await self._capture_once(url, section_name, plan)

        except LinkedInScraperException:
            raise
        except TargetClosedError:
            # A closed target is not a property of the section; every later
            # section would fail identically, so it is the call that has to
            # fail, not the section. Isolating it here is where the incident's
            # error was swallowed.
            raise
        except Exception as e:
            is_overlay = CaptureMode.OVERLAY in plan.mode
            logger.warning(
                "Failed to extract %s %s: %s",
                "overlay" if is_overlay else "page",
                url,
                e,
            )
            return ExtractedSection(
                text="",
                references=[],
                error=build_issue_diagnostics(
                    e,
                    context="extract_overlay" if is_overlay else "extract_page",
                    target_url=url,
                    section_name=section_name,
                ),
            )

    async def _capture_once(
        self,
        url: str,
        section_name: str,
        plan: CapturePlan,
    ) -> ExtractedSection:
        """Single attempt to navigate and capture a section."""
        await self._navigator._navigate_to_page(url)
        if CaptureMode.OVERLAY in plan.mode:
            return await self._extract_overlay_content(url, section_name)
        return await self._extract_loaded_section(url, section_name, plan)

    async def _extract_loaded_section(
        self,
        url: str,
        section_name: str,
        plan: CapturePlan,
    ) -> ExtractedSection:
        """Run an explicit post-navigation extraction plan on the current page."""
        await self._session.check_rate_limit()

        try:
            await self._session.page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("No <main> element found on %s", url)

        await self._session.dismiss_modal()

        if CaptureMode.ACTIVITY in plan.mode:
            try:
                await self._session.page.wait_for_function(
                    """() => {
                        const main = document.querySelector('main');
                        if (!main) return false;
                        return main.innerText.length > 200;
                    }""",
                    timeout=10000,
                )
            except PlaywrightTimeoutError:
                logger.debug("Activity feed content did not appear on %s", url)

        if CaptureMode.SEARCH_RESULTS in plan.mode:
            try:
                await self._session.page.wait_for_function(
                    """() => {
                        const main = document.querySelector('main');
                        if (!main) return false;
                        return main.innerText.length > 100;
                    }""",
                    timeout=10000,
                )
            except PlaywrightTimeoutError:
                logger.debug("Search results content did not appear on %s", url)

        # Employee text hydrates after the company header. The profile anchors
        # are the only stable structural signal that the listing has arrived.
        # Empty and restricted listings are common, so keep the shorter timeout.
        if CaptureMode.COMPANY_PEOPLE in plan.mode:
            try:
                await self._session.page.wait_for_function(
                    """() => {
                        const main = document.querySelector('main');
                        if (!main) return false;
                        return main.querySelectorAll('a[href*="/in/"]').length > 0;
                    }""",
                    timeout=5000,
                )
            except PlaywrightTimeoutError:
                logger.debug("Company people listing did not appear on %s", url)

        if CaptureMode.DETAILS in plan.mode:
            try:
                await self._session.page.wait_for_function(
                    self._detail_text.readiness_expression(),
                    timeout=10000,
                )
            except PlaywrightTimeoutError:
                logger.debug("Detail section content did not appear on %s", url)

        if CaptureMode.DETAILS in plan.mode:
            max_clicks = plan.max_scrolls if plan.max_scrolls is not None else 5
            for i in range(max_clicks):
                button = self._session.page.locator("main button").filter(
                    has_text=self._detail_text.expansion_button_pattern
                )
                try:
                    if await button.count() == 0:
                        logger.debug("No 'Show more' button after %d clicks", i)
                        break
                    target = button.first
                    if not await target.is_visible():
                        break
                    await target.scroll_into_view_if_needed(timeout=2000)
                    await target.click(timeout=2000)
                    await self._session.pace(1.0)
                except PlaywrightTimeoutError:
                    logger.debug("Show more click timed out after %d clicks", i)
                    break
                except Exception as e:
                    logger.debug("Show more click failed: %s", e)
                    break

        if CaptureMode.CONTENT_SEARCH in plan.mode:
            await self._scroll_content_search_results(
                plan.max_posts if plan.max_posts is not None else 10
            )
        elif CaptureMode.ACTIVITY in plan.mode:
            scrolls = plan.max_scrolls if plan.max_scrolls is not None else 10
            await self._session.scroll_body(pause_time=1.0, max_scrolls=scrolls)
        else:
            scrolls = plan.max_scrolls if plan.max_scrolls is not None else 5
            await self._session.scroll_body(pause_time=0.5, max_scrolls=scrolls)

        raw_result = await self._content._extract_root_content(["main"])
        raw = raw_result["text"]

        if not raw:
            return ExtractedSection(text="", references=[])
        truncated = truncate_linkedin_noise(raw)
        if not truncated and raw.strip():
            logger.warning(
                "Page %s returned only LinkedIn chrome (likely rate-limited)", url
            )
            return ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[])
        cleaned = filter_linkedin_noise_lines(truncated)
        return ExtractedSection(
            text=cleaned,
            references=build_references(
                raw_result["references"], section_name, apply_cap=plan.apply_cap
            ),
        )

    async def _count_content_search_results(self) -> int:
        """Count result cards on a content-search page.

        Runs ``CONTENT_SEARCH_COUNT_JS``: author and mention anchors
        (profile or company links) grouped by their nearest ``li``,
        ``article`` or ``role="article"`` ancestor, so a post with nine
        @-mentions is one card rather than ten. Without such an ancestor the
        count falls back to distinct hrefs, the previous behaviour, where a
        mention-heavy post reached ``max_posts`` before the first wheel.

        An estimate that errs toward over-scrolling, never truncation: under
        the fallback two posts by one author count as one card, and a card
        split across ancestors counts more than once, both of which only
        cost the loop another round.

        TODO(live-verify): the ancestor chain of content-search cards is
        unverified live.
        """
        return await self._session.page.evaluate(CONTENT_SEARCH_COUNT_JS)

    async def _scroll_content_search_results(self, max_posts: int) -> int:
        """Wheel-scroll content-search results until ``max_posts`` cards show.

        Same shape as the feed loop: the results live in their own scroll
        container, so ``window.scrollTo`` is a no-op and only a wheel over
        the viewport moves it. Stops once the card count reaches
        ``max_posts``, after ``_MAX_STALE`` rounds without a new card, or
        when ``CONTENT_SEARCH_SCROLL_BUDGET`` runs out: without the deadline
        the worst case is every round polling to its full wait, which is
        twice the tool timeout's comfortable share. Returns the final count;
        any stop below ``max_posts`` is logged as a warning.
        """
        # TODO(live-verify): wheel-scroll loading of content-search cards is
        # unmeasured; the diagnosis (window.scrollTo never moved the results)
        # was live, this loop was not.
        _MAX_STALE = 3
        _BATCH_WAIT = 6
        _WHEEL_DELTA = 2000
        stale_count = 0
        deadline = self._session.monotonic() + CONTENT_SEARCH_SCROLL_BUDGET
        stop_reason: str | None = None

        page = self._session.page
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        cx, cy = viewport["width"] // 2, viewport["height"] // 2
        await page.mouse.move(cx, cy)

        count = await self._count_content_search_results()
        for i in range(CONTENT_SEARCH_MAX_SCROLLS):
            logger.debug("Content search scroll %d: %d results", i, count)
            if count >= max_posts:
                break
            if self._session.monotonic() >= deadline:
                stop_reason = f"{CONTENT_SEARCH_SCROLL_BUDGET:.0f}s scroll budget spent"
                break

            await page.mouse.wheel(0, _WHEEL_DELTA)

            new_count = count
            for _ in range(_BATCH_WAIT):
                await self._session.pace(1.0)
                new_count = await self._count_content_search_results()
                if new_count > count or self._session.monotonic() >= deadline:
                    break

            if new_count > count:
                stale_count = 0
            else:
                stale_count += 1
                logger.debug(
                    "Content search stale scroll %d/%d (still at %d results)",
                    stale_count,
                    _MAX_STALE,
                    new_count,
                )
                if stale_count >= _MAX_STALE:
                    stop_reason = "page stopped producing new results"
                    break
            count = new_count
        else:
            stop_reason = f"{CONTENT_SEARCH_MAX_SCROLLS} scroll rounds spent"

        if count < max_posts:
            logger.warning(
                "content search stopped at %d of max_posts %d: %s",
                count,
                max_posts,
                stop_reason,
            )
        return count

    async def _extract_overlay(
        self,
        url: str,
        section_name: str,
        plan: CapturePlan | None = None,
    ) -> ExtractedSection:
        """Compatibility seam for explicit overlay capture."""
        return await self.capture(
            url,
            section_name,
            plan or CapturePlan(CaptureMode.OVERLAY),
        )

    async def _extract_overlay_once(
        self,
        url: str,
        section_name: str,
    ) -> ExtractedSection:
        """Compatibility seam for a single overlay attempt."""
        return await self._capture_once(
            url,
            section_name,
            CapturePlan(CaptureMode.OVERLAY),
        )

    async def _extract_overlay_content(
        self,
        url: str,
        section_name: str,
    ) -> ExtractedSection:
        """Extract content from the loaded overlay without dismissing it."""
        await self._session.check_rate_limit()

        try:
            await self._session.page.wait_for_selector(
                "dialog[open], .artdeco-modal__content"
            )
        except PlaywrightTimeoutError:
            logger.debug("No modal overlay found on %s, falling back to main", url)

        # The contact-info overlay is the modal, so dismissing it here would
        # destroy the content before the reader can fall back through its roots.
        raw_result = await self._content._extract_root_content(
            ["dialog[open]", ".artdeco-modal__content", "main"],
        )
        raw = raw_result["text"]

        if not raw:
            return ExtractedSection(text="", references=[])
        truncated = truncate_linkedin_noise(raw)
        if not truncated and raw.strip():
            logger.warning(
                "Overlay %s returned only LinkedIn chrome (likely rate-limited)",
                url,
            )
            return ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[])
        cleaned = filter_linkedin_noise_lines(truncated)
        return ExtractedSection(
            text=cleaned,
            references=build_references(raw_result["references"], section_name),
        )
