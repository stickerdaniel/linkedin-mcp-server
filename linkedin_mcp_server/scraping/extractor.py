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

    async def scrape_person(
        self, username: str, fields: PersonScrapingFields
    ) -> dict[str, Any]:
        """Scrape a person profile with configurable sections.

        Returns:
            {url, sections: {name: text}, pages_visited, sections_requested}
        """
        fields |= PersonScrapingFields.BASIC_INFO
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
                PersonScrapingFields.HONORS,
                "honors",
                "/details/honors/",
                False,
            ),
            (
                PersonScrapingFields.LANGUAGES,
                "languages",
                "/details/languages/",
                False,
            ),
            (
                PersonScrapingFields.CONTACT_INFO,
                "contact_info",
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
            except LinkedInScraperException:
                raise
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
        fields |= CompanyScrapingFields.ABOUT
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
            except LinkedInScraperException:
                raise
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

    async def _extract_job_listings(self) -> list[dict[str, str]]:
        """Extract structured job details from each card on the search results page.

        Walks up the DOM from each job link to its card container and parses
        the card text for metadata (company, location, pay, etc.).

        Returns:
            [{job_id, title, company, location, work_type, pay, benefits,
              easy_apply, status}]
        """
        return await self._page.evaluate("""() => {
            const links = document.querySelectorAll('a[href*="/jobs/view/"]');
            const seen = new Set();
            const results = [];

            for (const link of links) {
                const match = link.href.match(/\\/jobs\\/view\\/(\\d+)/);
                if (!match || seen.has(match[1])) continue;
                seen.add(match[1]);

                const title = link.innerText.trim().split('\\n')[0];
                if (!title) continue;

                // Walk up to find the job card container (the <li> or card div)
                let card = link;
                for (let i = 0; i < 8; i++) {
                    if (!card.parentElement) break;
                    card = card.parentElement;
                    // Stop at list item or a large enough container
                    if (card.tagName === 'LI' || card.getAttribute('data-occludable-job-id'))
                        break;
                }

                const cardText = card.innerText || '';
                const lines = cardText.split('\\n').map(l => l.trim()).filter(l => l);

                let company = '';
                let location = '';
                let work_type = '';
                let pay = '';
                let benefits = '';
                let easy_apply = '';
                let status = '';

                const workTypes = ['On-site', 'Hybrid', 'Remote'];
                const statusPhrases = [
                    'Actively reviewing applicants',
                    'Be an early applicant',
                ];

                for (const line of lines) {
                    // Pay: starts with $ or contains /hr or /yr
                    if (!pay && (line.match(/^\\$[\\d,.]+/) || line.match(/\\$[\\d,.]+\\/[yh]r/))) {
                        // Split pay from benefits on the · separator
                        const parts = line.split('·').map(p => p.trim());
                        pay = parts[0] || '';
                        if (parts[1]) benefits = parts[1];
                        continue;
                    }
                    // Benefits without pay (e.g. "401(k), +1 benefit")
                    if (!benefits && !pay && line.match(/^(\\d+ benefits|401|Medical|Vision|Dental)/i)) {
                        benefits = line;
                        continue;
                    }
                    // Location: "City, ST" pattern with optional (Work Type)
                    if (!location && line.match(/,\\s*[A-Z]{2}/) && !line.includes('alumni')) {
                        const locMatch = line.match(/^(.+?)\\s*\\(([^)]+)\\)\\s*$/);
                        if (locMatch) {
                            location = locMatch[1].trim();
                            if (workTypes.includes(locMatch[2])) work_type = locMatch[2];
                        } else {
                            location = line;
                        }
                        continue;
                    }
                    // Also match "United States (Remote)" style
                    if (!location && line.match(/United States/i)) {
                        const locMatch = line.match(/^(.+?)\\s*\\(([^)]+)\\)\\s*$/);
                        if (locMatch) {
                            location = locMatch[1].trim();
                            if (workTypes.includes(locMatch[2])) work_type = locMatch[2];
                        } else {
                            location = line;
                        }
                        continue;
                    }
                    // Easy Apply
                    if (line === 'Easy Apply') { easy_apply = 'true'; continue; }
                    // Status
                    if (statusPhrases.includes(line)) { status = line; continue; }
                }

                // Company is typically the first non-title, non-duplicate line
                // Skip title duplicates and "with verification" lines
                for (const line of lines) {
                    if (line === title) continue;
                    if (line.includes('with verification')) continue;
                    if (line === 'Promoted' || line === 'Viewed') continue;
                    if (line === 'Easy Apply') continue;
                    if (line.match(/^\\$/) || line.match(/\\/[yh]r/)) continue;
                    if (line.match(/alumni|school/)) continue;
                    if (line.match(/,\\s*[A-Z]{2}/) || line.match(/United States/i)) continue;
                    if (statusPhrases.includes(line)) continue;
                    if (line.match(/^(\\d+ benefits|401|Medical|Vision|Dental)/i)) continue;
                    // This should be the company name
                    company = line;
                    break;
                }

                results.push({
                    job_id: match[1],
                    title: title,
                    company: company,
                    location: location,
                    work_type: work_type,
                    pay: pay,
                    benefits: benefits,
                    easy_apply: easy_apply,
                    status: status,
                });
            }
            return results;
        }""")

    async def _scroll_job_list(
        self, pause_time: float = 0.8, max_scrolls: int = 25
    ) -> None:
        """Scroll the job list sidebar to load all lazy-rendered cards.

        Finds the scrollable ancestor of the first job link and scrolls it,
        rather than relying on specific CSS class names.  Also scrolls the
        window as a fallback for layouts that use page-level scroll.

        Args:
            pause_time: Time to pause between scrolls (seconds)
            max_scrolls: Maximum number of scroll attempts
        """
        for i in range(max_scrolls):
            prev_count = await self._page.evaluate(
                """() => document.querySelectorAll('a[href*="/jobs/view/"]').length"""
            )

            # Scroll every scrollable ancestor of the job list
            await self._page.evaluate("""() => {
                const jobLink = document.querySelector('a[href*="/jobs/view/"]');
                if (!jobLink) return;

                let el = jobLink.parentElement;
                while (el && el !== document.body) {
                    // 10px buffer to ignore minor rounding/border differences
                    if (el.scrollHeight > el.clientHeight + 10) {
                        el.scrollTop = el.scrollHeight;
                    }
                    el = el.parentElement;
                }

                window.scrollTo(0, document.body.scrollHeight);
            }""")

            await asyncio.sleep(pause_time)

            new_count = await self._page.evaluate(
                """() => document.querySelectorAll('a[href*="/jobs/view/"]').length"""
            )
            logger.debug("Scroll %d: job links %d -> %d", i + 1, prev_count, new_count)

            if new_count == prev_count:
                # Extra pause on first scroll in case of slow loading
                if i == 0:
                    await asyncio.sleep(pause_time * 2)
                    continue
                logger.debug("No new jobs after scroll %d, stopping", i + 1)
                break

    async def _extract_job_page(self, url: str) -> tuple[str, list[dict[str, str]]]:
        """Load a single job search results page, extract text and listings."""
        await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await detect_rate_limit(self._page)

        try:
            await self._page.wait_for_selector("main", timeout=5000)
        except PlaywrightTimeoutError:
            logger.debug("No <main> element found on %s", url)

        await handle_modal_close(self._page)

        # Scroll the job list sidebar to load all lazy-rendered cards
        await self._scroll_job_list(pause_time=0.8, max_scrolls=20)

        # Extract text
        raw = await self._page.evaluate(
            """() => {
                const main = document.querySelector('main');
                return main ? main.innerText : document.body.innerText;
            }"""
        )
        text = strip_linkedin_noise(raw) if raw else ""

        # Extract structured job listings
        try:
            listings = await self._extract_job_listings()
        except Exception as e:
            logger.warning("Failed to extract job listings from %s: %s", url, e)
            listings = []

        return text, listings

    async def search_jobs(
        self,
        keywords: str,
        location: str | None = None,
        max_pages: int = 3,
    ) -> dict[str, Any]:
        """Search for jobs and extract results across multiple pages.

        Args:
            keywords: Search keywords.
            location: Optional location filter.
            max_pages: Number of search result pages to scrape (1-100, default 3).
                       Each page has ~10 results. Higher values take longer and
                       increase the risk of rate limiting.

        Returns:
            {url, sections: {name: text}, job_listings: [{job_id, title, company,
             location, work_type, pay, benefits, easy_apply, status}],
             pages_visited, sections_requested}
        """
        max_pages = max(1, min(max_pages, 100))

        base_params = f"keywords={quote_plus(keywords)}"
        if location:
            base_params += f"&location={quote_plus(location)}"

        all_listings: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        all_text_parts: list[str] = []
        pages_visited: list[str] = []

        for page_num in range(max_pages):
            start = page_num * 25
            params = base_params if start == 0 else f"{base_params}&start={start}"
            url = f"https://www.linkedin.com/jobs/search/?{params}"

            try:
                text, listings = await self._extract_job_page(url)
            except LinkedInScraperException:
                raise
            except Exception as e:
                logger.warning("Failed to load job search page %d: %s", page_num + 1, e)
                break

            pages_visited.append(url)
            if text:
                all_text_parts.append(text)

            # Deduplicate across pages
            new_on_page = 0
            for listing in listings:
                if listing["job_id"] not in seen_ids:
                    seen_ids.add(listing["job_id"])
                    all_listings.append(listing)
                    new_on_page += 1

            logger.info(
                "Page %d: found %d listings (%d new, %d total)",
                page_num + 1,
                len(listings),
                new_on_page,
                len(all_listings),
            )

            # Stop early if this page returned nothing new
            if new_on_page == 0:
                logger.info(
                    "No new listings on page %d, stopping pagination", page_num + 1
                )
                break

            # Delay between pages to avoid rate limiting
            if page_num < max_pages - 1:
                await asyncio.sleep(_NAV_DELAY)

        sections: dict[str, str] = {}
        if all_text_parts:
            sections["search_results"] = "\n\n---\n\n".join(all_text_parts)

        first_url = f"https://www.linkedin.com/jobs/search/?{base_params}"
        return {
            "url": first_url,
            "sections": sections,
            "job_listings": all_listings,
            "pages_visited": pages_visited,
            "sections_requested": ["search_results"],
        }

    async def search_people(
        self,
        keywords: str,
        location: str | None = None,
    ) -> dict[str, Any]:
        """Search for people and extract the results page.

        Returns:
            {url, sections: {name: text}, pages_visited, sections_requested}
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
            "pages_visited": [url],
            "sections_requested": ["search_results"],
        }
