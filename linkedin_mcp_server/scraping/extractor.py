"""Core extraction engine using innerText instead of DOM selectors."""

import asyncio
from dataclasses import dataclass
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
from linkedin_mcp_server.scraping.link_metadata import (
    Reference,
    build_references,
    dedupe_references,
)

from .fields import COMPANY_SECTIONS, PERSON_SECTIONS

logger = logging.getLogger(__name__)

# Delay between page navigations to avoid rate limiting
_NAV_DELAY = 2.0

# Backoff before retrying a rate-limited page
_RATE_LIMIT_RETRY_DELAY = 5.0

# Returned as section text when LinkedIn rate-limits the page
_RATE_LIMITED_MSG = "[Rate limited] LinkedIn blocked this section. Try again later or request fewer sections."

# LinkedIn shows 25 results per page
_PAGE_SIZE = 25

# Normalization maps for job search filters
_DATE_POSTED_MAP = {
    "past_hour": "r3600",
    "past_24_hours": "r86400",
    "past_week": "r604800",
    "past_month": "r2592000",
}

_EXPERIENCE_LEVEL_MAP = {
    "internship": "1",
    "entry": "2",
    "associate": "3",
    "mid_senior": "4",
    "director": "5",
    "executive": "6",
}

_JOB_TYPE_MAP = {
    "full_time": "F",
    "part_time": "P",
    "contract": "C",
    "temporary": "T",
    "volunteer": "V",
    "internship": "I",
    "other": "O",
}

_WORK_TYPE_MAP = {"on_site": "1", "remote": "2", "hybrid": "3"}

_SORT_BY_MAP = {"date": "DD", "relevance": "R"}


def _normalize_csv(value: str, mapping: dict[str, str]) -> str:
    """Normalize a comma-separated filter value using the provided mapping."""
    parts = [v.strip() for v in value.split(",")]
    return ",".join(mapping.get(p, p) for p in parts)


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
    # Footer nav clusters in profile/posts pages
    re.compile(
        r"^(?:Careers|Privacy & Terms|Questions\?|Select language)\n+"
        r"(?:Privacy & Terms|Questions\?|Select language|Advertising|Ad Choices|"
        r"[A-Za-z]+ \([A-Za-z]+\))",
        re.MULTILINE,
    ),
]

_NOISE_LINES: list[re.Pattern[str]] = [
    re.compile(r"^(?:Play|Pause|Playback speed|Turn fullscreen on|Fullscreen)$"),
    re.compile(r"^(?:Show captions|Close modal window|Media player modal window)$"),
    re.compile(r"^(?:Loaded:.*|Remaining time.*|Stream Type.*)$"),
]


@dataclass
class ExtractedSection:
    """Text and compact references extracted from a loaded LinkedIn section."""

    text: str
    references: list[Reference]


def strip_linkedin_noise(text: str) -> str:
    """Remove LinkedIn page chrome (footer, sidebar recommendations) from innerText.

    Finds the earliest occurrence of any known noise marker and truncates there.
    """
    earliest = len(text)
    for pattern in _NOISE_MARKERS:
        match = pattern.search(text)
        if match and match.start() < earliest:
            earliest = match.start()

    cleaned = text[:earliest].strip()
    filtered_lines = [
        line
        for line in cleaned.splitlines()
        if not any(pattern.match(line.strip()) for pattern in _NOISE_LINES)
    ]
    return "\n".join(filtered_lines).strip()


class LinkedInExtractor:
    """Extracts LinkedIn page content via navigate-scroll-innerText pattern."""

    def __init__(self, page: Page):
        self._page = page

    async def extract_page(
        self,
        url: str,
        section_name: str | None = None,
    ) -> ExtractedSection:
        """Navigate to a URL, scroll to load lazy content, and extract innerText.

        Retries once after a backoff when the page returns only LinkedIn chrome
        (sidebar/footer noise with no actual content), which indicates a soft
        rate limit.

        Raises LinkedInScraperException subclasses (rate limit, auth, etc.).
        Returns _RATE_LIMITED_MSG sentinel when soft-rate-limited after retry.
        Returns empty string for unexpected non-domain failures (error isolation).
        """
        try:
            result = await self._extract_page_once(url, section_name)
            if result.text != _RATE_LIMITED_MSG:
                return result

            # Retry once after backoff
            logger.info("Retrying %s after %.0fs backoff", url, _RATE_LIMIT_RETRY_DELAY)
            await asyncio.sleep(_RATE_LIMIT_RETRY_DELAY)
            return await self._extract_page_once(url, section_name)

        except LinkedInScraperException:
            raise
        except Exception as e:
            logger.warning("Failed to extract page %s: %s", url, e)
            return ExtractedSection(text="", references=[])

    async def _extract_page_once(
        self,
        url: str,
        section_name: str | None = None,
    ) -> ExtractedSection:
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

        # Activity feed pages lazy-load post content after the tab header
        is_activity = "/recent-activity/" in url
        if is_activity:
            try:
                await self._page.wait_for_function(
                    """() => {
                        const main = document.querySelector('main');
                        if (!main) return false;
                        return main.innerText.length > 200;
                    }""",
                    timeout=10000,
                )
            except PlaywrightTimeoutError:
                logger.debug("Activity feed content did not appear on %s", url)

        # Scroll to trigger lazy loading
        if is_activity:
            await scroll_to_bottom(self._page, pause_time=1.0, max_scrolls=10)
        else:
            await scroll_to_bottom(self._page, pause_time=0.5, max_scrolls=5)

        # Extract text from main content area
        raw_result = await self._extract_root_content(["main"])
        raw = raw_result["text"]

        if not raw:
            return ExtractedSection(text="", references=[])
        cleaned = strip_linkedin_noise(raw)
        if not cleaned and raw.strip():
            logger.warning(
                "Page %s returned only LinkedIn chrome (likely rate-limited)", url
            )
            return ExtractedSection(text=_RATE_LIMITED_MSG, references=[])
        return ExtractedSection(
            text=cleaned,
            references=build_references(raw_result["references"], section_name or ""),
        )

    async def _extract_overlay(
        self,
        url: str,
        section_name: str | None = None,
    ) -> ExtractedSection:
        """Extract content from an overlay/modal page (e.g. contact info).

        LinkedIn renders contact info as a native <dialog> element.
        Falls back to `<main>` if no dialog is found.

        Retries once after a backoff when the overlay returns only LinkedIn
        chrome (noise), mirroring `extract_page` behavior.
        """
        try:
            result = await self._extract_overlay_once(url, section_name)
            if result.text != _RATE_LIMITED_MSG:
                return result

            logger.info(
                "Retrying overlay %s after %.0fs backoff",
                url,
                _RATE_LIMIT_RETRY_DELAY,
            )
            await asyncio.sleep(_RATE_LIMIT_RETRY_DELAY)
            return await self._extract_overlay_once(url, section_name)

        except LinkedInScraperException:
            raise
        except Exception as e:
            logger.warning("Failed to extract overlay %s: %s", url, e)
            return ExtractedSection(text="", references=[])

    async def _extract_overlay_once(
        self,
        url: str,
        section_name: str | None = None,
    ) -> ExtractedSection:
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

        raw_result = await self._extract_root_content(
            ["dialog[open]", ".artdeco-modal__content", "main"],
        )
        raw = raw_result["text"]

        if not raw:
            return ExtractedSection(text="", references=[])
        cleaned = strip_linkedin_noise(raw)
        if not cleaned and raw.strip():
            logger.warning(
                "Overlay %s returned only LinkedIn chrome (likely rate-limited)",
                url,
            )
            return ExtractedSection(text=_RATE_LIMITED_MSG, references=[])
        return ExtractedSection(
            text=cleaned,
            references=build_references(raw_result["references"], section_name or ""),
        )

    async def scrape_person(self, username: str, requested: set[str]) -> dict[str, Any]:
        """Scrape a person profile with configurable sections.

        Returns:
            {url, sections: {name: text}}
        """
        requested = requested | {"main_profile"}
        base_url = f"https://www.linkedin.com/in/{username}"
        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}

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
                    extracted = await self._extract_overlay(
                        url, section_name=section_name
                    )
                else:
                    extracted = await self.extract_page(url, section_name=section_name)

                if extracted.text:
                    sections[section_name] = extracted.text
                    if extracted.references:
                        references[section_name] = extracted.references
            except LinkedInScraperException:
                raise
            except Exception as e:
                logger.warning("Error scraping section %s: %s", section_name, e)

        result: dict[str, Any] = {
            "url": f"{base_url}/",
            "sections": sections,
        }
        if references:
            result["references"] = references
        return result

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
        references: dict[str, list[Reference]] = {}

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
                    extracted = await self._extract_overlay(
                        url, section_name=section_name
                    )
                else:
                    extracted = await self.extract_page(url, section_name=section_name)

                if extracted.text:
                    sections[section_name] = extracted.text
                    if extracted.references:
                        references[section_name] = extracted.references
            except LinkedInScraperException:
                raise
            except Exception as e:
                logger.warning("Error scraping section %s: %s", section_name, e)

        result: dict[str, Any] = {
            "url": f"{base_url}/",
            "sections": sections,
        }
        if references:
            result["references"] = references
        return result

    async def scrape_job(self, job_id: str) -> dict[str, Any]:
        """Scrape a single job posting.

        Returns:
            {url, sections: {name: text}}
        """
        url = f"https://www.linkedin.com/jobs/view/{job_id}/"
        extracted = await self.extract_page(url, section_name="job_posting")

        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        if extracted.text:
            sections["job_posting"] = extracted.text
        if extracted.references:
            references["job_posting"] = extracted.references

        result: dict[str, Any] = {
            "url": url,
            "sections": sections,
        }
        if references:
            result["references"] = references
        return result

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

    async def _extract_search_page(
        self,
        url: str,
        section_name: str,
    ) -> ExtractedSection:
        """Extract innerText from a job search page with soft rate-limit retry.

        Mirrors the noise-only detection and single-retry behavior of
        ``extract_page`` / ``_extract_page_once`` so that callers get a
        ``_RATE_LIMITED_MSG`` sentinel instead of silent empty results.
        """
        try:
            result = await self._extract_search_page_once(url, section_name)
            if result.text != _RATE_LIMITED_MSG:
                return result

            logger.info(
                "Retrying search page %s after %.0fs backoff",
                url,
                _RATE_LIMIT_RETRY_DELAY,
            )
            await asyncio.sleep(_RATE_LIMIT_RETRY_DELAY)
            result = await self._extract_search_page_once(url, section_name)
            if result.text == _RATE_LIMITED_MSG:
                logger.warning("Search page %s still rate-limited after retry", url)
            return result

        except LinkedInScraperException:
            raise
        except Exception as e:
            logger.warning("Failed to extract search page %s: %s", url, e)
            return ExtractedSection(text="", references=[])

    async def _extract_search_page_once(
        self,
        url: str,
        section_name: str = "",
    ) -> ExtractedSection:
        """Single attempt to navigate, scroll sidebar, and extract innerText."""
        await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await detect_rate_limit(self._page)

        main_found = True
        try:
            await self._page.wait_for_selector("main", timeout=5000)
        except PlaywrightTimeoutError:
            logger.debug("No <main> element found on %s", url)
            main_found = False

        await handle_modal_close(self._page)
        if main_found:
            await scroll_job_sidebar(self._page, pause_time=0.5, max_scrolls=5)

        raw_result = await self._extract_root_content(["main"])
        raw = raw_result["text"]
        if raw_result["source"] == "body":
            logger.debug("No <main> at evaluation time on %s, using body fallback", url)
        elif not main_found:
            logger.debug(
                "<main> appeared after wait timeout on %s, sidebar scroll was skipped",
                url,
            )

        if not raw:
            return ExtractedSection(text="", references=[])
        cleaned = strip_linkedin_noise(raw)
        if not cleaned and raw.strip():
            logger.warning(
                "Search page %s returned only LinkedIn chrome (likely rate-limited)",
                url,
            )
            return ExtractedSection(text=_RATE_LIMITED_MSG, references=[])
        return ExtractedSection(
            text=cleaned,
            references=build_references(raw_result["references"], section_name),
        )

    async def _get_total_search_pages(self) -> int | None:
        """Read total page count from LinkedIn's pagination state element.

        Parses the "Page X of Y" text from ``.jobs-search-pagination__page-state``.
        Returns ``None`` when the element is absent or unparseable.

        NOTE: This is a deliberate DOM exception. The element has ``display: none``
        (screen-reader only), so the text never appears in ``innerText``. A class-based
        selector is the only reliable way to read it. Gracefully returns ``None`` if
        LinkedIn renames the class — pagination just falls back to ``max_pages``.
        """
        text = await self._page.evaluate(
            """() => {
                const el = document.querySelector(
                    '.jobs-search-pagination__page-state'
                );
                return el ? el.textContent.trim() : null;
            }"""
        )
        if not text:
            return None
        match = re.search(r"of\s+(\d+)", text)
        return int(match.group(1)) if match else None

    @staticmethod
    def _build_job_search_url(
        keywords: str,
        location: str | None = None,
        date_posted: str | None = None,
        job_type: str | None = None,
        experience_level: str | None = None,
        work_type: str | None = None,
        easy_apply: bool = False,
        sort_by: str | None = None,
    ) -> str:
        """Build a LinkedIn job search URL with optional filters.

        Human-readable names are normalized to LinkedIn URL codes.
        Comma-separated values are normalized individually.
        Unknown values pass through unchanged.
        """
        params = f"keywords={quote_plus(keywords)}"
        if location:
            params += f"&location={quote_plus(location)}"

        if date_posted:
            mapped = _DATE_POSTED_MAP.get(date_posted.strip(), date_posted)
            params += f"&f_TPR={quote_plus(mapped)}"
        if job_type:
            params += f"&f_JT={_normalize_csv(job_type, _JOB_TYPE_MAP)}"
        if experience_level:
            params += f"&f_E={_normalize_csv(experience_level, _EXPERIENCE_LEVEL_MAP)}"
        if work_type:
            params += f"&f_WT={_normalize_csv(work_type, _WORK_TYPE_MAP)}"
        if easy_apply:
            params += "&f_EA=true"
        if sort_by:
            mapped = _SORT_BY_MAP.get(sort_by.strip(), sort_by)
            params += f"&sortBy={quote_plus(mapped)}"

        return f"https://www.linkedin.com/jobs/search/?{params}"

    async def search_jobs(
        self,
        keywords: str,
        location: str | None = None,
        max_pages: int = 3,
        date_posted: str | None = None,
        job_type: str | None = None,
        experience_level: str | None = None,
        work_type: str | None = None,
        easy_apply: bool = False,
        sort_by: str | None = None,
    ) -> dict[str, Any]:
        """Search for jobs with pagination and job ID extraction.

        Scrolls the job sidebar (not the main page) and paginates through
        results. Uses LinkedIn's "Page X of Y" indicator to cap pagination,
        and stops early when a page yields no new job IDs.

        Args:
            keywords: Search keywords
            location: Optional location filter
            max_pages: Maximum pages to load (1-10, default 3)
            date_posted: Filter by date posted (past_hour, past_24_hours, past_week, past_month)
            job_type: Filter by job type (full_time, part_time, contract, temporary, volunteer, internship, other)
            experience_level: Filter by experience level (internship, entry, associate, mid_senior, director, executive)
            work_type: Filter by work type (on_site, remote, hybrid)
            easy_apply: Only show Easy Apply jobs
            sort_by: Sort results (date, relevance)

        Returns:
            {url, sections: {search_results: text}, job_ids: [str]}
        """
        base_url = self._build_job_search_url(
            keywords,
            location=location,
            date_posted=date_posted,
            job_type=job_type,
            experience_level=experience_level,
            work_type=work_type,
            easy_apply=easy_apply,
            sort_by=sort_by,
        )
        all_job_ids: list[str] = []
        seen_ids: set[str] = set()
        page_texts: list[str] = []
        page_references: list[Reference] = []
        total_pages: int | None = None
        total_pages_queried = False

        for page_num in range(max_pages):
            # Stop if we already know we've reached the last page
            if total_pages is not None and page_num >= total_pages:
                logger.debug("All %d pages fetched, stopping", total_pages)
                break

            if page_num > 0:
                await asyncio.sleep(_NAV_DELAY)

            url = (
                base_url
                if page_num == 0
                else f"{base_url}&start={page_num * _PAGE_SIZE}"
            )

            try:
                extracted = await self._extract_search_page(
                    url, section_name="search_results"
                )

                if not extracted.text or extracted.text == _RATE_LIMITED_MSG:
                    # Navigation failed or rate-limited; skip ID extraction
                    break

                # Read total pages from pagination state (once only, best-effort)
                if not total_pages_queried:
                    total_pages_queried = True
                    try:
                        total_pages = await self._get_total_search_pages()
                    except Exception as e:
                        logger.debug("Could not read total pages: %s", e)
                    else:
                        if total_pages is not None:
                            logger.debug("LinkedIn reports %d total pages", total_pages)

                # Extract job IDs from hrefs (page is already loaded)
                if not self._page.url.startswith(
                    "https://www.linkedin.com/jobs/search/"
                ):
                    logger.debug(
                        "Unexpected page URL after extraction: %s — "
                        "skipping job ID extraction",
                        self._page.url,
                    )
                    page_texts.append(extracted.text)
                    if extracted.references:
                        page_references.extend(extracted.references)
                    break
                page_ids = await self._extract_job_ids()
                new_ids = [jid for jid in page_ids if jid not in seen_ids]

                if not new_ids:
                    page_texts.append(extracted.text)
                    if extracted.references:
                        page_references.extend(extracted.references)
                    logger.debug("No new job IDs on page %d, stopping", page_num + 1)
                    break

                for jid in new_ids:
                    seen_ids.add(jid)
                    all_job_ids.append(jid)

                page_texts.append(extracted.text)
                if extracted.references:
                    page_references.extend(extracted.references)

            except LinkedInScraperException:
                raise
            except Exception as e:
                logger.warning("Error on search page %d: %s", page_num + 1, e)
                break

        result: dict[str, Any] = {
            "url": base_url,
            "sections": {"search_results": "\n---\n".join(page_texts)}
            if page_texts
            else {},
            "job_ids": all_job_ids,
        }
        if page_references:
            result["references"] = {
                "search_results": dedupe_references(page_references, cap=15)
            }
        return result

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
        extracted = await self.extract_page(url, section_name="search_results")

        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        if extracted.text:
            sections["search_results"] = extracted.text
        if extracted.references:
            references["search_results"] = extracted.references

        result: dict[str, Any] = {
            "url": url,
            "sections": sections,
        }
        if references:
            result["references"] = references
        return result

    async def _extract_root_content(
        self,
        selectors: list[str],
    ) -> dict[str, Any]:
        """Extract innerText and raw anchor metadata from the first matching root."""
        result = await self._page.evaluate(
            """({ selectors }) => {
                const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
                const containerSelector = 'section, article, li, div';
                const headingSelector = 'h1, h2, h3';
                const directHeadingSelector = ':scope > h1, :scope > h2, :scope > h3';
                const MAX_HEADING_CONTAINERS = 300;

                const getHeadingText = element => {
                    if (!element) return '';

                    const heading =
                        element.matches && element.matches(headingSelector)
                            ? element
                            : element.querySelector
                              ? element.querySelector(directHeadingSelector)
                              : null;

                    return normalize(heading?.innerText || heading?.textContent);
                };

                const getPreviousHeading = node => {
                    let sibling = node?.previousElementSibling || null;
                    for (let index = 0; sibling && index < 3; index += 1) {
                        const heading = getHeadingText(sibling);
                        if (heading) {
                            return heading;
                        }
                        sibling = sibling.previousElementSibling;
                    }
                    return '';
                };

                const root = selectors
                    .map(selector => document.querySelector(selector))
                    .find(Boolean);
                const source = root ? 'root' : 'body';
                const container = root || document.body;
                const text = container ? (container.innerText || '').trim() : '';
                const headingMap = new WeakMap();

                const candidateContainers = [
                    container,
                    ...Array.from(container.querySelectorAll(containerSelector)).slice(
                        0,
                        MAX_HEADING_CONTAINERS,
                    ),
                ];
                candidateContainers.forEach(node => {
                    const ownHeading = getHeadingText(node);
                    const previousHeading = getPreviousHeading(node);
                    const heading = ownHeading || previousHeading;
                    if (heading) {
                        headingMap.set(node, heading);
                    }
                });

                const findHeading = element => {
                    let current = element.closest(containerSelector) || container;
                    for (let depth = 0; current && depth < 4; depth += 1) {
                        const heading = headingMap.get(current);
                        if (heading) {
                            return heading;
                        }
                        if (current === container) {
                            break;
                        }
                        current = current.parentElement?.closest(containerSelector) || null;
                    }
                    return '';
                };

                const references = Array.from(container.querySelectorAll('a[href]'))
                    .map(anchor => {
                        const rawHref = (anchor.getAttribute('href') || '').trim();
                        if (!rawHref || rawHref === '#') {
                            return null;
                        }

                        const href = rawHref.startsWith('#')
                            ? rawHref
                            : (anchor.href || rawHref);

                        return {
                            href,
                            text: normalize(anchor.innerText || anchor.textContent),
                            aria_label: normalize(anchor.getAttribute('aria-label')),
                            title: normalize(anchor.getAttribute('title')),
                            heading: findHeading(anchor),
                            in_article: Boolean(anchor.closest('article')),
                            in_nav: Boolean(anchor.closest('nav')),
                            in_footer: Boolean(anchor.closest('footer')),
                        };
                    })
                    .filter(Boolean);

                return { source, text, references };
            }""",
            {"selectors": selectors},
        )
        return result
