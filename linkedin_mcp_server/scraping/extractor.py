"""Core extraction engine using innerText instead of DOM selectors."""

import asyncio
import logging
import re
from typing import Any, Callable, Awaitable
from urllib.parse import quote_plus

from patchright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import LinkedInScraperException, RateLimitError
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


def _parse_contact_record(
    profile_text: str, contact_text: str
) -> dict[str, str | None]:
    """Parse raw innerText blobs into structured contact fields.

    Profile text layout (first lines):
        Name\\n\\n· 1st\\n\\nHeadline\\n\\nLocation\\n\\n·\\n\\nContact info\\n\\nCompany

    Contact info overlay layout:
        Email\\n\\nuser@example.com\\n\\nPhone\\n\\n+123...\\n\\n...
    """
    result: dict[str, str | None] = {
        "first_name": None,
        "last_name": None,
        "headline": None,
        "location": None,
        "company": None,
        "email": None,
        "phone": None,
        "website": None,
        "birthday": None,
    }

    # --- Parse profile text ---
    if profile_text:
        lines = [ln.strip() for ln in profile_text.split("\n")]
        non_empty = [ln for ln in lines if ln]

        if non_empty:
            # Line 1 → full name
            full_name = non_empty[0]
            parts = full_name.split(None, 1)
            result["first_name"] = parts[0] if parts else full_name
            result["last_name"] = parts[1] if len(parts) > 1 else None

        # Find connection degree marker (· 1st, · 2nd, · 3rd)
        degree_idx: int | None = None
        for i, ln in enumerate(non_empty):
            if re.match(r"^·\s*\d+(st|nd|rd)$", ln):
                degree_idx = i
                break

        if degree_idx is not None and degree_idx + 1 < len(non_empty):
            result["headline"] = non_empty[degree_idx + 1]

            # Location is the next non-empty line after headline
            if degree_idx + 2 < len(non_empty):
                candidate = non_empty[degree_idx + 2]
                # Skip if it's just the "·" separator or "Contact info"
                if candidate not in ("·", "Contact info"):
                    result["location"] = candidate

        # Company: line after "Contact info"
        for i, ln in enumerate(non_empty):
            if ln == "Contact info" and i + 1 < len(non_empty):
                result["company"] = non_empty[i + 1]
                break

    # --- Parse contact info overlay ---
    if contact_text:
        # Extract labeled fields: "Label\n\nvalue"
        for field, label in [
            ("email", "Email"),
            ("phone", "Phone"),
            ("birthday", "Birthday"),
        ]:
            match = re.search(
                rf"(?:^|\n){re.escape(label)}\s*\n\s*\n\s*(.+)",
                contact_text,
            )
            if match:
                result[field] = match.group(1).strip()

        # Website may include a type annotation like "(Blog)" or "(Portfolio)"
        match = re.search(r"(?:^|\n)Website\s*\n\s*\n\s*(.+)", contact_text)
        if match:
            result["website"] = match.group(1).strip()

    return result


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

    # ------------------------------------------------------------------
    # Connections bulk export
    # ------------------------------------------------------------------

    async def scrape_connections_list(
        self,
        limit: int = 0,
        max_scrolls: int = 50,
    ) -> dict[str, Any]:
        """Scrape the authenticated user's connections list via infinite scroll.

        Args:
            limit: Maximum connections to return (0 = unlimited).
            max_scrolls: Maximum scroll iterations (~1s pause each).

        Returns:
            {connections: [{username, name, headline}, ...], total, url, pages_visited}
        """
        url = "https://www.linkedin.com/mynetwork/invite-connect/connections/"

        await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await detect_rate_limit(self._page)

        try:
            await self._page.wait_for_selector("main", timeout=10000)
        except PlaywrightTimeoutError:
            logger.debug("No <main> element on connections page")

        await handle_modal_close(self._page)

        # Deep scroll to load all connections (infinite scroll)
        await scroll_to_bottom(self._page, pause_time=1.0, max_scrolls=max_scrolls)

        # Extract connection data from profile link elements
        raw_connections: list[dict[str, str]] = await self._page.evaluate(
            """() => {
                const results = [];
                const seen = new Set();
                const links = document.querySelectorAll('main a[href*="/in/"]');
                for (const a of links) {
                    const href = a.getAttribute('href') || '';
                    const match = href.match(/\\/in\\/([^/?#]+)/);
                    if (!match) continue;
                    const username = match[1];
                    if (seen.has(username)) continue;
                    seen.add(username);

                    // Walk up to the connection card container
                    const card = a.closest('li') || a.parentElement;

                    // Name: try known selectors, then the link's own visible text
                    let name = '';
                    if (card) {
                        const nameEl = card.querySelector(
                            '.mn-connection-card__name, .entity-result__title-text, span[dir="ltr"], span.t-bold'
                        );
                        if (nameEl) name = nameEl.innerText.trim();
                    }
                    if (!name) {
                        // The profile link itself often contains the person's name
                        const linkText = a.innerText.trim();
                        if (linkText && linkText.length < 80) name = linkText;
                    }

                    // Headline: try known selectors, then parse card text
                    let headline = '';
                    if (card) {
                        const headlineEl = card.querySelector(
                            '.mn-connection-card__occupation, .entity-result__primary-subtitle, span.t-normal'
                        );
                        if (headlineEl) headline = headlineEl.innerText.trim();
                    }
                    if (!headline && card) {
                        // Fallback: split card text by newlines, second non-empty line is usually headline
                        const lines = card.innerText.split('\\n').map(l => l.trim()).filter(Boolean);
                        if (lines.length >= 2) headline = lines[1];
                    }

                    results.push({ username, name, headline });
                }
                return results;
            }"""
        )

        # Apply limit
        if limit > 0:
            raw_connections = raw_connections[:limit]

        return {
            "connections": raw_connections,
            "total": len(raw_connections),
            "url": url,
            "pages_visited": [url],
        }

    async def scrape_contact_batch(
        self,
        usernames: list[str],
        chunk_size: int = 5,
        chunk_delay: float = 30.0,
        progress_cb: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Enrich a list of profiles with contact details in chunked batches.

        For each username: scrapes main profile + contact_info overlay.

        Args:
            usernames: List of LinkedIn usernames to enrich.
            chunk_size: Profiles per chunk before a long pause.
            chunk_delay: Seconds to pause between chunks.
            progress_cb: Optional async callback(completed, total) for progress.

        Returns:
            {contacts: [{username, first_name, last_name, email, phone,
             headline, location, company, website, birthday,
             profile_raw, contact_info_raw}],
             total, failed, rate_limited, pages_visited}
        """
        contacts: list[dict[str, Any]] = []
        failed: list[str] = []
        pages_visited: list[str] = []
        total = len(usernames)
        rate_limited = False

        for chunk_idx in range(0, total, chunk_size):
            chunk = usernames[chunk_idx : chunk_idx + chunk_size]

            for username in chunk:
                profile_url = f"https://www.linkedin.com/in/{username}/"
                contact_url = (
                    f"https://www.linkedin.com/in/{username}/overlay/contact-info/"
                )

                try:
                    # Scrape main profile page
                    profile_text = await self.extract_page(profile_url)
                    pages_visited.append(profile_url)

                    # Scrape contact info overlay
                    contact_text = await self._extract_overlay(contact_url)
                    pages_visited.append(contact_url)

                    parsed = _parse_contact_record(profile_text, contact_text)
                    contacts.append(
                        {
                            "username": username,
                            **parsed,
                            "profile_raw": profile_text,
                            "contact_info_raw": contact_text,
                        }
                    )

                except RateLimitError:
                    logger.warning("Rate limited during contact batch at %s", username)
                    rate_limited = True
                    break
                except Exception as e:
                    logger.warning("Failed to scrape %s: %s", username, e)
                    failed.append(username)

                # Brief delay between individual profiles
                await asyncio.sleep(_NAV_DELAY)

            if rate_limited:
                break

            # Report progress after each chunk
            completed = min(chunk_idx + len(chunk), total)
            if progress_cb:
                await progress_cb(completed, total)

            # Pause between chunks (skip after last chunk)
            if chunk_idx + chunk_size < total:
                logger.info(
                    "Chunk complete (%d/%d). Pausing %.0fs...",
                    completed,
                    total,
                    chunk_delay,
                )
                await asyncio.sleep(chunk_delay)

        return {
            "contacts": contacts,
            "total": len(contacts),
            "failed": failed,
            "rate_limited": rate_limited,
            "pages_visited": pages_visited,
        }
