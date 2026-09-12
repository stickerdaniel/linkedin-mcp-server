"""Core extraction engine using innerText instead of DOM selectors."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from patchright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.scraping.capture import SectionCapture
from linkedin_mcp_server.scraping.company import CompanyScraper
from linkedin_mcp_server.scraping.connection_actions import ConnectionActions
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import (
    ExtractedSection,
    # Re-exported, not used: the search filters that raise it moved to
    # `search_urls`, while the MCP tool wrappers still catch the class through
    # this module. The redundant alias is what marks that as deliberate.
    FilterValidationError as FilterValidationError,
    # Re-exported for the same reason: the job workflow held the last call
    # inside this module, while `tools/company.py` and `tools/feed.py` still
    # build their rate-limit entry through this name.
    rate_limited_section_error as rate_limited_section_error,
)
from linkedin_mcp_server.scraping.conversations import ConversationReader
from linkedin_mcp_server.scraping.feed import FeedScraper
from linkedin_mcp_server.scraping.job_pages import JobPageReader
from linkedin_mcp_server.scraping.jobs import JobScraper
from linkedin_mcp_server.scraping.message_sender import MessageSender
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.person import PersonScraper
from linkedin_mcp_server.scraping.posts import PostSearch
from linkedin_mcp_server.scraping.profile_page import ProfilePageReader
from linkedin_mcp_server.scraping.session import ScrapingSession
from linkedin_mcp_server.scraping.text import (
    # Re-exported, not used: the conversation reader holds the last call
    # inside this package, while `tests/scraping/test_facade_contracts.py`
    # pins the identity of the name imported from here. The redundant
    # alias is what marks that as deliberate.
    strip_conversation_chrome as strip_conversation_chrome,
    strip_linkedin_noise,
)


if TYPE_CHECKING:
    from linkedin_mcp_server.callbacks import ProgressCallback

logger = logging.getLogger(__name__)


class LinkedInExtractor:
    """Extracts LinkedIn page content via navigate-scroll-innerText pattern."""

    def __init__(self, page: Page):
        self._session = ScrapingSession(page)
        self._navigator = PageNavigator(self._session)
        self._content = PageContentReader(self._session)
        self._capture = SectionCapture(self._session, self._navigator, self._content)
        self._feed = FeedScraper(self._session, self._navigator, self._content)
        self._message_sender = MessageSender(self._session, self._navigator)
        self._profile_page = ProfilePageReader(
            self._session,
            lambda: self._message_sender._read_profile_message_target(),
        )
        self._person = PersonScraper(
            self._session, self._navigator, self._capture, self._profile_page
        )
        self._company = CompanyScraper(self._session, self._capture)
        # Narrow and late-bound, like the reader above: the workflow needs one
        # main-profile read and nothing else of the person scraper, and
        # resolving `scrape_person` at call time keeps the facade's own frozen
        # delegate on that path rather than capturing what it delegates to
        # today.
        self._connection = ConnectionActions(
            self._session,
            self._navigator,
            lambda username: self.scrape_person(username, {"main_profile"}),
        )
        # The page reader is a service under the job workflows rather than a
        # peer of them, so it is built here and handed down rather than
        # resolved from the facade later.
        self._job_pages = JobPageReader(self._session, self._navigator, self._content)
        self._jobs = JobScraper(self._navigator, self._capture, self._job_pages)
        self._posts = PostSearch(self._capture)
        self._conversations = ConversationReader(
            self._session, self._navigator, self._content, self._profile_page
        )
        self._page = page

    # ------------------------------------------------------------------
    # Generic browser helpers for LLM-driven connection flow
    # ------------------------------------------------------------------

    async def get_page_text(self) -> str:
        """Extract innerText from the main content area of the current page."""
        text = await self._page.evaluate(
            "() => (document.querySelector('main') || document.body).innerText || ''"
        )
        return strip_linkedin_noise(text) if isinstance(text, str) else ""

    async def click_button_by_text(
        self, text: str, *, scope: str = "main", timeout: int = 5000
    ) -> bool:
        """Click the first button/link whose visible text is exactly *text*.

        Uses a regex filter for exact matching to avoid substring false
        positives (e.g. "Connect" matching "connections").
        Returns True if clicked, False if no match found.
        """
        matches = (
            self._page.locator(scope)
            .locator("button, a, [role='button']")
            .filter(has_text=re.compile(rf"^{re.escape(text)}$"))
        )
        count = await matches.count()
        logger.debug("click_button_by_text(%r): %d matches in %s", text, count, scope)
        if count == 0:
            return False
        target = matches.first
        try:
            await target.scroll_into_view_if_needed(timeout=timeout)
        except Exception:
            logger.debug("Scroll failed for button '%s'", text, exc_info=True)
        try:
            await target.click(timeout=timeout)
            return True
        except Exception:
            logger.debug("Click failed for button '%s'", text, exc_info=True)
            return False

    async def _locator_is_visible(self, selector: str, *, timeout: int = 2000) -> bool:
        """Return whether the first matching locator is visible."""
        locator = self._page.locator(selector)
        try:
            if await locator.count() == 0:
                return False
        except Exception:
            return False

        first = locator.first
        try:
            await first.wait_for(state="visible", timeout=timeout)
            return True
        except PlaywrightTimeoutError:
            return False
        except Exception:
            try:
                return bool(await first.is_visible())
            except Exception:
                return False

    async def _click_first(self, selector: str, *, timeout: int = 5000) -> None:
        """Click the first visible locator that matches a selector."""
        target = self._page.locator(selector).first
        try:
            await target.scroll_into_view_if_needed(timeout=timeout)
        except Exception:
            logger.debug("Could not scroll %s into view", selector, exc_info=True)
        await target.click(timeout=timeout)

    async def extract_feed(
        self,
        num_posts: int = 10,
    ) -> ExtractedSection:
        """Scrape the LinkedIn home feed, scrolling until *num_posts* are loaded."""
        return await self._feed.extract_feed(num_posts)

    async def extract_page(
        self,
        url: str,
        section_name: str,
        max_scrolls: int | None = None,
    ) -> ExtractedSection:
        """Navigate to a URL, scroll to load lazy content, and extract innerText."""
        return await self._capture.extract_page(url, section_name, max_scrolls)

    async def scrape_person(
        self,
        username: str,
        requested: set[str],
        callbacks: ProgressCallback | None = None,
        max_scrolls: int | None = None,
        *,
        main_profile_already_loaded: bool = False,
        allow_self_alias: bool = False,
    ) -> dict[str, Any]:
        """Scrape a person profile with configurable sections."""
        return await self._person.scrape_person(
            username,
            requested,
            callbacks,
            max_scrolls,
            main_profile_already_loaded=main_profile_already_loaded,
            allow_self_alias=allow_self_alias,
        )

    async def get_my_profile(
        self,
        sections: set[str] | None = None,
        callbacks: ProgressCallback | None = None,
        max_scrolls: int | None = None,
    ) -> dict[str, Any]:
        """Scrape the authenticated user's own LinkedIn profile."""
        return await self._person.get_my_profile(sections, callbacks, max_scrolls)

    async def connect_with_person(
        self,
        username: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Send a LinkedIn connection request or accept an incoming one."""
        return await self._connection.connect_with_person(username, note=note)

    async def get_sidebar_profiles(self, username: str) -> dict[str, Any]:
        """Extract profile links from sidebar sections on a profile page."""
        return await self._person.get_sidebar_profiles(username)

    def _extract_thread_id(url: str) -> str | None:
        """Parse a LinkedIn thread id from a messaging thread URL."""
        match = re.search(r"/messaging/thread/([^/?#]+)/", url)
        return match.group(1) if match else None

    async def scrape_company(
        self,
        company_name: str,
        requested: set[str],
        callbacks: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Scrape a company profile with configurable sections."""
        return await self._company.scrape_company(company_name, requested, callbacks)

    async def get_company_employees(
        self,
        company_name: str,
        keywords: str | None = None,
    ) -> dict[str, Any]:
        """List employees at a company from the /people/ page."""
        return await self._company.get_company_employees(company_name, keywords)

    async def scrape_job(self, job_id: str) -> dict[str, Any]:
        """Scrape a single job posting."""
        return await self._jobs.scrape_job(job_id)

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
        tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Search for jobs with pagination and job ID extraction."""
        return await self._jobs.search_jobs(
            keywords,
            location,
            max_pages,
            date_posted,
            job_type,
            experience_level,
            work_type,
            easy_apply,
            sort_by,
            tool_timeout,
        )

    async def get_saved_jobs(self, max_pages: int = 3) -> dict[str, Any]:
        """List the authenticated user's saved job postings."""
        return await self._jobs.get_saved_jobs(max_pages)

    async def search_people(
        self,
        keywords: str,
        location: str | None = None,
        network: list[str] | None = None,
        current_company: str | None = None,
    ) -> dict[str, Any]:
        """Search for people and extract the results page."""
        return await self._person.search_people(
            keywords,
            location=location,
            network=network,
            current_company=current_company,
        )

    async def search_companies(
        self,
        keywords: str,
    ) -> dict[str, Any]:
        """Search for companies and extract the results page."""
        return await self._company.search_companies(keywords)

    async def search_posts(
        self,
        keywords: str,
        date_posted: str | None = None,
        max_pages: int = 3,
    ) -> dict[str, Any]:
        """Search LinkedIn posts/content and extract the results page."""
        return await self._posts.search_posts(
            keywords,
            date_posted=date_posted,
            max_pages=max_pages,
        )

    async def get_inbox(self, limit: int = 20) -> dict[str, Any]:
        """List recent conversations from the messaging inbox."""
        return await self._conversations.get_inbox(limit)

    async def get_conversation(
        self,
        linkedin_username: str | None = None,
        thread_id: str | None = None,
        index: int = 0,
    ) -> dict[str, Any]:
        """Read a specific messaging conversation by thread ID or username."""
        return await self._conversations.get_conversation(
            linkedin_username, thread_id, index
        )

    async def search_conversations(
        self, keywords: str, limit: int = 20
    ) -> dict[str, Any]:
        """Search messages by keyword."""
        return await self._conversations.search_conversations(keywords, limit)

    async def send_message(
        self,
        linkedin_username: str,
        message: str,
        *,
        confirm_send: bool,
        profile_urn: str | None = None,
    ) -> dict[str, Any]:
        """Compose and send a new message with explicit confirmation gating."""
        return await self._message_sender.send_message(
            linkedin_username,
            message,
            confirm_send=confirm_send,
            profile_urn=profile_urn,
        )
