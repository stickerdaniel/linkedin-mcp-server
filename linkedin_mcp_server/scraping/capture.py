"""Generic page and overlay section capture."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

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

# Backoff before retrying a temporarily blocked page. Shared with the job page
# reads that still live on the facade, so that one relocation cannot quietly
# give the two paths different retry policies.
RATE_LIMIT_RETRY_DELAY = 5.0


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
        """Navigate to a URL, scroll to load lazy content, and extract innerText.

        Retries once after a backoff when the page returns only LinkedIn chrome
        (sidebar/footer noise with no actual content), which indicates a soft
        rate limit.

        Raises LinkedInScraperException subclasses (rate limit, auth, etc.).
        Returns RATE_LIMITED_SECTION_TEXT sentinel when soft-rate-limited after retry.
        Returns empty string for unexpected non-domain failures (error isolation).
        """
        try:
            result = await self._extract_page_once(url, section_name, max_scrolls)
            if result.text != RATE_LIMITED_SECTION_TEXT:
                return result

            # Retry once after backoff
            logger.info("Retrying %s after %.0fs backoff", url, RATE_LIMIT_RETRY_DELAY)
            await self._session.delay(RATE_LIMIT_RETRY_DELAY)
            return await self._extract_page_once(url, section_name, max_scrolls)

        except LinkedInScraperException:
            raise
        except Exception as e:
            logger.warning("Failed to extract page %s: %s", url, e)
            return ExtractedSection(
                text="",
                references=[],
                error=build_issue_diagnostics(
                    e,
                    context="extract_page",
                    target_url=url,
                    section_name=section_name,
                ),
            )

    async def _extract_page_once(
        self,
        url: str,
        section_name: str,
        max_scrolls: int | None = None,
    ) -> ExtractedSection:
        """Single attempt to navigate, scroll, and extract innerText."""
        await self._navigator._navigate_to_page(url)
        return await self._extract_loaded_section(url, section_name, max_scrolls)

    async def _extract_loaded_section(
        self,
        url: str,
        section_name: str,
        max_scrolls: int | None = None,
    ) -> ExtractedSection:
        """Run the post-navigation extraction pipeline on the current page.

        Assumes the bound page already points at ``url`` (or its post-redirect
        equivalent). Performs rate-limit detection, modal dismissal, lazy-load
        scrolling, innerText extraction, noise truncation, and reference
        building — everything ``_extract_page_once`` does after the goto.
        """
        await self._session.check_rate_limit()

        # Wait for main content to render
        try:
            await self._session.page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("No <main> element found on %s", url)

        # Dismiss any modals blocking content
        await self._session.dismiss_modal()

        # Activity feed pages lazy-load post content after the tab header.
        # Company posts pages (/company/<slug>/posts/) lazy-load the same way
        # but don't carry a /recent-activity/ path, so match them too. Matched
        # on the parsed path, since the url can carry a query string
        # (?viewAsMember=true) that a raw suffix check would miss.
        path = urlparse(url).path
        is_activity = "/recent-activity/" in path or (
            "/company/" in path and path.rstrip("/").endswith("/posts")
        )
        if is_activity:
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

        # Search results pages load a placeholder first then fill in results
        # via JavaScript. Wait for actual content before extracting.
        is_search = "/search/results/" in url
        if is_search:
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

        # Company people pages (/company/<slug>/people/) initially render only
        # the company header in <main>; the employee listing hydrates later
        # via JS. Wait until at least one /in/ profile anchor appears inside
        # <main> so innerText extraction sees the actual list. Use a 5s
        # timeout instead of the 10s pattern shared with is_search/is_details
        # — empty/restricted listings are common here (small companies,
        # privacy settings) and a full 10s wait per call adds up.
        is_company_people = "/company/" in url and "/people/" in url
        if is_company_people:
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

        # Profile detail pages (/details/experience/, /details/education/, etc.)
        # initially render sidebar recommendations into <main> while the section
        # panel loads asynchronously. Wait until the panel replaces the sidebar.
        # The sidebar placeholder starts with "Load more" or "More profiles for you".
        is_details = "/details/" in url
        if is_details:
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

        # Detail pages paginate with a "Show more" button inside <main>, not scroll.
        # Click it until it disappears or the budget runs out.
        if is_details:
            max_clicks = max_scrolls if max_scrolls is not None else 5
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

        # Scroll to trigger lazy loading
        if is_activity:
            scrolls = max_scrolls if max_scrolls is not None else 10
            await self._session.scroll_body(pause_time=1.0, max_scrolls=scrolls)
        else:
            scrolls = max_scrolls if max_scrolls is not None else 5
            await self._session.scroll_body(pause_time=0.5, max_scrolls=scrolls)

        # Extract text from main content area
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
    ) -> ExtractedSection:
        """Extract content from an overlay/modal page (e.g. contact info).

        LinkedIn renders contact info as a native <dialog> element.
        Falls back to `<main>` if no dialog is found.

        Retries once after a backoff when the overlay returns only LinkedIn
        chrome (noise), mirroring `extract_page` behavior.
        """
        try:
            result = await self._extract_overlay_once(url, section_name)
            if result.text != RATE_LIMITED_SECTION_TEXT:
                return result

            logger.info(
                "Retrying overlay %s after %.0fs backoff",
                url,
                RATE_LIMIT_RETRY_DELAY,
            )
            await self._session.delay(RATE_LIMIT_RETRY_DELAY)
            return await self._extract_overlay_once(url, section_name)

        except LinkedInScraperException:
            raise
        except Exception as e:
            logger.warning("Failed to extract overlay %s: %s", url, e)
            return ExtractedSection(
                text="",
                references=[],
                error=build_issue_diagnostics(
                    e,
                    context="extract_overlay",
                    target_url=url,
                    section_name=section_name,
                ),
            )

    async def _extract_overlay_once(
        self,
        url: str,
        section_name: str,
    ) -> ExtractedSection:
        """Single attempt to extract content from an overlay/modal page."""
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()

        # Wait for the dialog/modal to render (LinkedIn uses native <dialog>)
        try:
            await self._session.page.wait_for_selector(
                "dialog[open], .artdeco-modal__content"
            )
        except PlaywrightTimeoutError:
            logger.debug("No modal overlay found on %s, falling back to main", url)

        # NOTE: Do NOT call the session's dismiss_modal() here — the contact-info
        # overlay *is* a dialog/modal. Dismissing it would destroy the
        # content before the JS evaluation below can read it.

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
