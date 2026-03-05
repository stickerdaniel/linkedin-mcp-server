"""Core extraction engine using innerText instead of DOM selectors."""

import asyncio
import logging
import re
from typing import Any
from urllib.parse import quote_plus

from patchright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.core.utils import (
    detect_rate_limit,
    handle_modal_close,
    scroll_job_sidebar,
    scroll_to_bottom,
)

from .fields import COMPANY_SECTIONS, PERSON_SECTIONS

logger = logging.getLogger(__name__)

# Delay between page navigations to avoid rate limiting
_NAV_DELAY = 2.0

# Backoff before retrying a rate-limited page
_RATE_LIMIT_RETRY_DELAY = 5.0

# Returned as section text when LinkedIn rate-limits the page
_RATE_LIMITED_MSG = "[Rate limited] LinkedIn blocked this section. Try again later or request fewer sections."

# Patterns that mark the start of LinkedIn page chrome (sidebar/footer).
# Everything from the earliest match onwards is stripped.
_NOISE_MARKERS: list[re.Pattern[str]] = [
    # Footer nav links: "About" immediately followed by "Accessibility" or "Talent Solutions"
    re.compile(r"^About\n+(?:Accessibility|Talent Solutions)", re.MULTILINE),
    # Sidebar profile recommendations
    re.compile(r"^More profiles for you$", re.MULTILINE),
    # Sidebar premium upsell
    re.compile(r"^Explore premium profiles$", re.MULTILINE),
    # InMail upsell in contact info overlay
    re.compile(r"^Get up to .+ replies when you message with InMail$", re.MULTILINE),
]


def strip_linkedin_noise(text: str) -> str:
    """Remove LinkedIn page chrome (footer, sidebar recommendations) from innerText.

    Finds the earliest occurrence of any known noise marker and truncates there.
    """
    earliest = len(text)
    for pattern in _NOISE_MARKERS:
        match = pattern.search(text)
        if match and match.start() < earliest:
            earliest = match.start()

    return text[:earliest].strip()


class LinkedInExtractor:
    """Extracts LinkedIn page content via navigate-scroll-innerText pattern."""

    def __init__(self, page: Page):
        self._page = page

    async def extract_page(self, url: str) -> str:
        """Navigate to a URL, scroll to load lazy content, and extract innerText.

        Retries once after a backoff when the page returns only LinkedIn chrome
        (sidebar/footer noise with no actual content), which indicates a soft
        rate limit.

        Raises LinkedInScraperException subclasses (rate limit, auth, etc.).
        Returns _RATE_LIMITED_MSG sentinel when soft-rate-limited after retry.
        Returns empty string for unexpected non-domain failures (error isolation).
        """
        try:
            result = await self._extract_page_once(url)
            if result != _RATE_LIMITED_MSG:
                return result

            # Retry once after backoff
            logger.info("Retrying %s after %.0fs backoff", url, _RATE_LIMIT_RETRY_DELAY)
            await asyncio.sleep(_RATE_LIMIT_RETRY_DELAY)
            return await self._extract_page_once(url)

        except LinkedInScraperException:
            raise
        except Exception as e:
            logger.warning("Failed to extract page %s: %s", url, e)
            return ""

    async def _extract_page_once(self, url: str) -> str:
        """Single attempt to navigate, scroll, and extract innerText."""
        await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await detect_rate_limit(self._page)

        # Wait for main content to render
        try:
            await self._page.wait_for_selector("main", timeout=5000)
        except PlaywrightTimeoutError:
            logger.debug("No <main> element found on %s", url)

        # Dismiss any modals blocking content
        await handle_modal_close(self._page)

        # Scroll to trigger lazy loading
        await scroll_to_bottom(self._page, pause_time=0.5, max_scrolls=5)

        # Extract text from main content area
        raw = await self._page.evaluate(
            """() => {
                const main = document.querySelector('main');
                return main ? main.innerText : document.body.innerText;
            }"""
        )

        if not raw:
            return ""
        cleaned = strip_linkedin_noise(raw)
        if not cleaned and raw.strip():
            logger.warning(
                "Page %s returned only LinkedIn chrome (likely rate-limited)", url
            )
            return _RATE_LIMITED_MSG
        return cleaned

    async def _extract_overlay(self, url: str) -> str:
        """Extract content from an overlay/modal page (e.g. contact info).

        LinkedIn renders contact info as a native <dialog> element.
        Falls back to `<main>` if no dialog is found.

        Retries once after a backoff when the overlay returns only LinkedIn
        chrome (noise), mirroring `extract_page` behavior.
        """
        try:
            result = await self._extract_overlay_once(url)
            if result != _RATE_LIMITED_MSG:
                return result

            logger.info(
                "Retrying overlay %s after %.0fs backoff",
                url,
                _RATE_LIMIT_RETRY_DELAY,
            )
            await asyncio.sleep(_RATE_LIMIT_RETRY_DELAY)
            return await self._extract_overlay_once(url)

        except LinkedInScraperException:
            raise
        except Exception as e:
            logger.warning("Failed to extract overlay %s: %s", url, e)
            return ""

    async def _extract_overlay_once(self, url: str) -> str:
        """Single attempt to extract content from an overlay/modal page."""
        await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await detect_rate_limit(self._page)

        # Wait for the dialog/modal to render (LinkedIn uses native <dialog>)
        try:
            await self._page.wait_for_selector(
                "dialog[open], .artdeco-modal__content", timeout=5000
            )
        except PlaywrightTimeoutError:
            logger.debug("No modal overlay found on %s, falling back to main", url)

        # NOTE: Do NOT call handle_modal_close() here — the contact-info
        # overlay *is* a dialog/modal. Dismissing it would destroy the
        # content before the JS evaluation below can read it.

        raw = await self._page.evaluate(
            """() => {
                const dialog = document.querySelector('dialog[open]');
                if (dialog) return dialog.innerText.trim();
                const modal = document.querySelector('.artdeco-modal__content');
                if (modal) return modal.innerText.trim();
                const main = document.querySelector('main');
                return main ? main.innerText.trim() : document.body.innerText.trim();
            }"""
        )

        if not raw:
            return ""
        cleaned = strip_linkedin_noise(raw)
        if not cleaned and raw.strip():
            logger.warning(
                "Overlay %s returned only LinkedIn chrome (likely rate-limited)",
                url,
            )
            return _RATE_LIMITED_MSG
        return cleaned

    async def scrape_person(self, username: str, requested: set[str]) -> dict[str, Any]:
        """Scrape a person profile with configurable sections.

        Returns:
            {url, sections: {name: text}}
        """
        requested = requested | {"main_profile"}
        base_url = f"https://www.linkedin.com/in/{username}"
        sections: dict[str, str] = {}

        first = True
        for section_name, (suffix, is_overlay) in PERSON_SECTIONS.items():
            if section_name not in requested:
                continue

            if not first:
                await asyncio.sleep(_NAV_DELAY)
            first = False

            url = base_url + suffix
            try:
                if is_overlay:
                    text = await self._extract_overlay(url)
                else:
                    text = await self.extract_page(url)

                if text:
                    sections[section_name] = text
            except LinkedInScraperException:
                raise
            except Exception as e:
                logger.warning("Error scraping section %s: %s", section_name, e)

        return {
            "url": f"{base_url}/",
            "sections": sections,
        }

    async def scrape_company(
        self, company_name: str, requested: set[str]
    ) -> dict[str, Any]:
        """Scrape a company profile with configurable sections.

        Returns:
            {url, sections: {name: text}}
        """
        requested = requested | {"about"}
        base_url = f"https://www.linkedin.com/company/{company_name}"
        sections: dict[str, str] = {}

        first = True
        for section_name, (suffix, is_overlay) in COMPANY_SECTIONS.items():
            if section_name not in requested:
                continue

            if not first:
                await asyncio.sleep(_NAV_DELAY)
            first = False

            url = base_url + suffix
            try:
                if is_overlay:
                    text = await self._extract_overlay(url)
                else:
                    text = await self.extract_page(url)

                if text:
                    sections[section_name] = text
            except LinkedInScraperException:
                raise
            except Exception as e:
                logger.warning("Error scraping section %s: %s", section_name, e)

        return {
            "url": f"{base_url}/",
            "sections": sections,
        }

    async def scrape_job(self, job_id: str) -> dict[str, Any]:
        """Scrape a single job posting.

        Returns:
            {url, sections: {name: text}}
        """
        url = f"https://www.linkedin.com/jobs/view/{job_id}/"
        text = await self.extract_page(url)

        sections: dict[str, str] = {}
        if text:
            sections["job_posting"] = text

        return {
            "url": url,
            "sections": sections,
        }

    async def _extract_job_ids(self) -> list[str]:
        """Extract unique job IDs from job card links on the current page.

        Finds all `a[href*="/jobs/view/"]` links and extracts the numeric
        job ID from each href. Returns deduplicated IDs in DOM order.
        """
        return await self._page.evaluate(
            """() => {
                const links = document.querySelectorAll('a[href*="/jobs/view/"]');
                const seen = new Set();
                const ids = [];
                for (const a of links) {
                    const match = a.href.match(/\\/jobs\\/view\\/(\\d+)/);
                    if (match && !seen.has(match[1])) {
                        seen.add(match[1]);
                        ids.push(match[1]);
                    }
                }
                return ids;
            }"""
        )

    async def search_jobs(
        self,
        keywords: str,
        location: str | None = None,
        max_pages: int = 3,
    ) -> dict[str, Any]:
        """Search for jobs with pagination and job ID extraction.

        Scrolls the job sidebar (not the main page) and paginates through
        results. Stops early when a page yields no new job IDs.

        Args:
            keywords: Search keywords
            location: Optional location filter
            max_pages: Maximum pages to load (1-10, default 3)

        Returns:
            {url, sections: {search_results: text}, job_ids: [str]}
        """
        max_pages = max(1, min(10, max_pages))

        params = f"keywords={quote_plus(keywords)}"
        if location:
            params += f"&location={quote_plus(location)}"

        base_url = f"https://www.linkedin.com/jobs/search/?{params}"
        all_job_ids: list[str] = []
        seen_ids: set[str] = set()
        page_texts: list[str] = []

        for page_num in range(max_pages):
            if page_num > 0:
                await asyncio.sleep(_NAV_DELAY)

            url = base_url if page_num == 0 else f"{base_url}&start={len(seen_ids)}"

            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await detect_rate_limit(self._page)

                try:
                    await self._page.wait_for_selector("main", timeout=5000)
                except PlaywrightTimeoutError:
                    logger.debug("No <main> element found on %s", url)

                await handle_modal_close(self._page)
                await scroll_job_sidebar(self._page, pause_time=0.5, max_scrolls=5)

                # Extract job IDs from hrefs
                page_ids = await self._extract_job_ids()
                new_ids = [jid for jid in page_ids if jid not in seen_ids]

                if not new_ids and page_num > 0:
                    logger.debug("No new job IDs on page %d, stopping", page_num + 1)
                    break

                for jid in new_ids:
                    seen_ids.add(jid)
                    all_job_ids.append(jid)

                # Extract innerText
                raw = await self._page.evaluate(
                    """() => {
                        const main = document.querySelector('main');
                        return main ? main.innerText : document.body.innerText;
                    }"""
                )
                if raw:
                    cleaned = strip_linkedin_noise(raw)
                    if cleaned:
                        page_texts.append(cleaned)

            except LinkedInScraperException:
                raise
            except Exception as e:
                logger.warning("Error on search page %d: %s", page_num + 1, e)
                break

        return {
            "url": base_url,
            "sections": {"search_results": "\n---\n".join(page_texts)}
            if page_texts
            else {},
            "job_ids": all_job_ids,
        }

    async def search_people(
        self,
        keywords: str,
        location: str | None = None,
    ) -> dict[str, Any]:
        """Search for people and extract the results page.

        Returns:
            {url, sections: {name: text}}
        """
        params = f"keywords={quote_plus(keywords)}"
        if location:
            params += f"&location={quote_plus(location)}"

        url = f"https://www.linkedin.com/search/results/people/?{params}"
        text = await self.extract_page(url)

        sections: dict[str, str] = {}
        if text:
            sections["search_results"] = text

        return {
            "url": url,
            "sections": sections,
        }
