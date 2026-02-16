"""Core extraction engine using innerText instead of DOM selectors."""

import asyncio
import logging
from typing import Any
from urllib.parse import quote_plus

from patchright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.utils import (
    detect_rate_limit,
    handle_modal_close,
    scroll_to_bottom,
)

from .fields import (
    COMPANY_SECTION_MAP,
    PERSON_SECTION_MAP,
    CompanyScrapingFields,
    PersonScrapingFields,
)

logger = logging.getLogger(__name__)

# Delay between page navigations to avoid rate limiting
_NAV_DELAY = 1.0


class LinkedInExtractor:
    """Extracts LinkedIn page content via navigate-scroll-innerText pattern."""

    def __init__(self, page: Page):
        self._page = page

    async def extract_page(self, url: str) -> str:
        """Navigate to a URL, scroll to load lazy content, and extract innerText.

        Returns empty string on failure (error isolation per section).
        """
        try:
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
            text = await self._page.evaluate(
                """() => {
                    const main = document.querySelector('main');
                    return main ? main.innerText : document.body.innerText;
                }"""
            )

            return text.strip() if text else ""

        except Exception as e:
            logger.warning("Failed to extract page %s: %s", url, e)
            return ""

    async def _extract_overlay(self, url: str) -> str:
        """Extract content from an overlay/modal page (e.g. contact info).

        Falls back to `.artdeco-modal__content` if `<main>` is empty.
        """
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await detect_rate_limit(self._page)

            # Wait for modal content
            try:
                await self._page.wait_for_selector(
                    "main, .artdeco-modal__content", timeout=5000
                )
            except PlaywrightTimeoutError:
                logger.debug("No overlay content found on %s", url)

            await handle_modal_close(self._page)

            text = await self._page.evaluate(
                """() => {
                    const main = document.querySelector('main');
                    const mainText = main ? main.innerText.trim() : '';
                    if (mainText) return mainText;
                    const modal = document.querySelector('.artdeco-modal__content');
                    return modal ? modal.innerText.trim() : document.body.innerText.trim();
                }"""
            )

            return text.strip() if text else ""

        except Exception as e:
            logger.warning("Failed to extract overlay %s: %s", url, e)
            return ""

    async def scrape_person(
        self, username: str, fields: PersonScrapingFields
    ) -> dict[str, Any]:
        """Scrape a person profile with configurable sections.

        Returns:
            {url, sections: {name: text}, pages_visited, sections_requested}
        """
        base_url = f"https://www.linkedin.com/in/{username}"
        sections: dict[str, str] = {}
        pages_visited: list[str] = []

        # Map flags to (section_name, url_suffix, is_overlay)
        page_map: list[tuple[PersonScrapingFields, str, str, bool]] = [
            (PersonScrapingFields.BASIC_INFO, "main_profile", "/", False),
            (
                PersonScrapingFields.EXPERIENCE,
                "experience",
                "/details/experience/",
                False,
            ),
            (
                PersonScrapingFields.EDUCATION,
                "education",
                "/details/education/",
                False,
            ),
            (
                PersonScrapingFields.INTERESTS,
                "interests",
                "/details/interests/",
                False,
            ),
            (
                PersonScrapingFields.ACCOMPLISHMENTS,
                "honors",
                "/details/honors/",
                False,
            ),
            (
                PersonScrapingFields.ACCOMPLISHMENTS,
                "languages",
                "/details/languages/",
                False,
            ),
            (
                PersonScrapingFields.CONTACTS,
                "contacts",
                "/overlay/contact-info/",
                True,
            ),
        ]

        for flag, section_name, suffix, is_overlay in page_map:
            if not (flag & fields):
                continue

            url = base_url + suffix
            try:
                if is_overlay:
                    text = await self._extract_overlay(url)
                else:
                    text = await self.extract_page(url)

                if text:
                    sections[section_name] = text
                pages_visited.append(url)
            except Exception as e:
                logger.warning("Error scraping section %s: %s", section_name, e)
                pages_visited.append(url)

            # Delay between navigations
            await asyncio.sleep(_NAV_DELAY)

        # Build sections_requested from flags
        requested = ["main_profile"]
        reverse_map = {v: k for k, v in PERSON_SECTION_MAP.items()}
        for flag in PersonScrapingFields:
            if flag in fields and flag in reverse_map:
                requested.append(reverse_map[flag])

        return {
            "url": f"{base_url}/",
            "sections": sections,
            "pages_visited": pages_visited,
            "sections_requested": requested,
        }

    async def scrape_company(
        self, company_name: str, fields: CompanyScrapingFields
    ) -> dict[str, Any]:
        """Scrape a company profile with configurable sections.

        Returns:
            {url, sections: {name: text}, pages_visited, sections_requested}
        """
        base_url = f"https://www.linkedin.com/company/{company_name}"
        sections: dict[str, str] = {}
        pages_visited: list[str] = []

        page_map: list[tuple[CompanyScrapingFields, str, str]] = [
            (CompanyScrapingFields.ABOUT, "about", "/about/"),
            (CompanyScrapingFields.POSTS, "posts", "/posts/"),
            (CompanyScrapingFields.JOBS, "jobs", "/jobs/"),
        ]

        for flag, section_name, suffix in page_map:
            if not (flag & fields):
                continue

            url = base_url + suffix
            try:
                text = await self.extract_page(url)
                if text:
                    sections[section_name] = text
                pages_visited.append(url)
            except Exception as e:
                logger.warning("Error scraping section %s: %s", section_name, e)
                pages_visited.append(url)

            await asyncio.sleep(_NAV_DELAY)

        # Build sections_requested from flags
        requested = ["about"]
        reverse_map = {v: k for k, v in COMPANY_SECTION_MAP.items()}
        for flag in CompanyScrapingFields:
            if flag in fields and flag in reverse_map:
                requested.append(reverse_map[flag])

        return {
            "url": f"{base_url}/",
            "sections": sections,
            "pages_visited": pages_visited,
            "sections_requested": requested,
        }

    async def scrape_job(self, job_id: str) -> dict[str, Any]:
        """Scrape a single job posting.

        Returns:
            {url, sections: {name: text}, pages_visited, sections_requested}
        """
        url = f"https://www.linkedin.com/jobs/view/{job_id}/"
        text = await self.extract_page(url)

        sections: dict[str, str] = {}
        if text:
            sections["job_posting"] = text

        return {
            "url": url,
            "sections": sections,
            "pages_visited": [url],
            "sections_requested": ["job_posting"],
        }

    async def search_jobs(
        self, keywords: str, location: str | None = None
    ) -> dict[str, Any]:
        """Search for jobs and extract the results page.

        Returns:
            {url, sections: {name: text}, pages_visited, sections_requested}
        """
        params = f"keywords={quote_plus(keywords)}"
        if location:
            params += f"&location={quote_plus(location)}"

        url = f"https://www.linkedin.com/jobs/search/?{params}"
        text = await self.extract_page(url)

        sections: dict[str, str] = {}
        if text:
            sections["search_results"] = text

        return {
            "url": url,
            "sections": sections,
            "pages_visited": [url],
            "sections_requested": ["search_results"],
        }
