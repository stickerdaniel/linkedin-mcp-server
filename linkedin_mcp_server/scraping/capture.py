"""Generic page and overlay section capture."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Flag, auto
from urllib.parse import urlparse

import logging
import re

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
    filter_linkedin_noise_lines,
    truncate_linkedin_noise,
)

logger = logging.getLogger(__name__)

# Backoff before retrying a temporarily blocked page. Owned here rather than
# copied, because the job-page reads that still sit on the facade share it: two
# constants would let one relocation give the two retry paths different policies
# without anything failing.
RATE_LIMIT_RETRY_DELAY = 5.0


class CaptureMode(Flag):
    """Independent post-navigation behaviors applied during section capture."""

    STANDARD = 0
    ACTIVITY = auto()
    SEARCH_RESULTS = auto()
    COMPANY_PEOPLE = auto()
    DETAILS = auto()
    OVERLAY = auto()


@dataclass(frozen=True)
class CapturePlan:
    """Immutable policy for one section capture."""

    mode: CaptureMode = CaptureMode.STANDARD
    max_scrolls: int | None = None


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
    ):
        self._session = session
        self._navigator = navigator
        self._content = content

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
        """Navigate and capture a section according to an explicit plan."""
        try:
            result = await self._capture_once(url, section_name, plan)
            if result.text != RATE_LIMITED_SECTION_TEXT:
                return result

            if CaptureMode.OVERLAY in plan.mode:
                logger.info(
                    "Retrying overlay %s after %.0fs backoff",
                    url,
                    RATE_LIMIT_RETRY_DELAY,
                )
            else:
                logger.info(
                    "Retrying %s after %.0fs backoff", url, RATE_LIMIT_RETRY_DELAY
                )
            await self._session.delay(RATE_LIMIT_RETRY_DELAY)
            return await self._capture_once(url, section_name, plan)

        except LinkedInScraperException:
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
                    """() => {
                        const main = document.querySelector('main');
                        if (!main) return false;
                        const text = main.innerText.trimStart();
                        return !text.startsWith('Load more')
                            && !text.startsWith('More profiles for you')
                            && !text.startsWith('Explore premium profiles');
                    }""",
                    timeout=10000,
                )
            except PlaywrightTimeoutError:
                logger.debug("Detail section content did not appear on %s", url)

        if CaptureMode.DETAILS in plan.mode:
            max_clicks = plan.max_scrolls if plan.max_scrolls is not None else 5
            for i in range(max_clicks):
                button = self._session.page.locator("main button").filter(
                    has_text=re.compile(r"^Show (more|all)\b", re.IGNORECASE)
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
                    await self._session.delay(1.0)
                except PlaywrightTimeoutError:
                    logger.debug("Show more click timed out after %d clicks", i)
                    break
                except Exception as e:
                    logger.debug("Show more click failed: %s", e)
                    break

        if CaptureMode.ACTIVITY in plan.mode:
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
            references=build_references(raw_result["references"], section_name),
        )

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
