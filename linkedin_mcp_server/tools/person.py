"""
LinkedIn person profile scraping tools.

Provides MCP tools for extracting comprehensive LinkedIn profile information including
experience, education, interests, accomplishments, and contact details.
"""

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from fastmcp import Context, FastMCP
from linkedin_scraper import PersonScraper
from linkedin_scraper.scrapers.base import BaseScraper
from mcp.types import ToolAnnotations

from linkedin_mcp_server.callbacks import MCPContextProgressCallback
from linkedin_mcp_server.drivers.browser import (
    ensure_authenticated,
    get_or_create_browser,
)
from linkedin_mcp_server.error_handler import handle_tool_error

logger = logging.getLogger(__name__)


class PersonSearchScraper(BaseScraper):
    """Scraper for LinkedIn person search results."""

    async def search(
        self,
        keywords: Optional[str] = None,
        limit: int = 10,
        only_first_degree: bool = False,
    ) -> List[Dict[str, str]]:
        """
        Search for people on LinkedIn.

        Args:
            keywords: Search keywords (e.g., "Cloud Security Architect")
            limit: Maximum number of results to return
            only_first_degree: Whether to limit search to 1st degree connections

        Returns:
            List of dicts with name and profile_url
        """
        logger.info(
            f"Starting person search: keywords='{keywords}', only_first_degree={only_first_degree}"
        )

        search_url = self._build_search_url(keywords, only_first_degree)
        await self.callback.on_start("PersonSearch", search_url)

        await self.navigate_and_wait(search_url)
        await self.callback.on_progress("Navigated to search results", 20)

        try:
            # Wait for search results to appear
            await self.page.wait_for_selector(
                ".reusable-search__result-container", timeout=10000
            )
        except Exception:
            logger.warning("No search results found on page")
            return []

        await self.wait_and_focus(1)
        await self.scroll_page_to_bottom(pause_time=1, max_scrolls=2)
        await self.callback.on_progress("Loaded search results", 50)

        people = await self._extract_people(limit)
        await self.callback.on_progress(f"Found {len(people)} people", 90)

        await self.callback.on_progress("Search complete", 100)
        await self.callback.on_complete("PersonSearch", people)

        logger.info(f"Person search complete: found {len(people)} people")
        return people

    def _build_search_url(
        self, keywords: Optional[str] = None, only_first_degree: bool = False
    ) -> str:
        """Build LinkedIn person search URL."""
        base_url = "https://www.linkedin.com/search/results/people/"
        params = {"origin": "GLOBAL_SEARCH_HEADER"}
        if keywords:
            params["keywords"] = keywords
        if only_first_degree:
            # LinkedIn uses network=["F"] for 1st degree connections
            params["network"] = '["F"]'

        return f"{base_url}?{urlencode(params)}"

    async def _extract_people(self, limit: int) -> List[Dict[str, str]]:
        """Extract person details from search results."""
        people = []
        try:
            containers = await self.page.locator(
                ".reusable-search__result-container"
            ).all()
            for container in containers:
                if len(people) >= limit:
                    break

                # Extract name and URL
                # The name is usually inside a span within a link that has a class like 'app-aware-link'
                link_locator = container.locator('a.app-aware-link[href*="/in/"]').first
                if await link_locator.count() == 0:
                    continue

                href = await link_locator.get_attribute("href")
                if not href:
                    continue

                # Clean URL
                profile_url = href.split("?")[0] if "?" in href else href

                # Extract name - it's often the first visible text or inside an aria-label
                # Attempt to get text from the link itself or its spans
                name_text = await link_locator.inner_text()
                # If name_text has multiple lines (e.g., "Name\nView Name's profile"), take first
                name = name_text.split("\n")[0].strip()

                if name and profile_url:
                    people.append({"name": name, "profile_url": profile_url})

        except Exception as e:
            logger.warning(f"Error extracting people: {e}")

        return people


def register_person_tools(mcp: FastMCP) -> None:
    """
    Register all person-related tools with the MCP server.

    Args:
        mcp: The MCP server instance
    """

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Person Profile",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def get_person_profile(
        linkedin_username: str, ctx: Context
    ) -> Dict[str, Any]:
        """
        Get a specific person's LinkedIn profile.

        Args:
            linkedin_username: LinkedIn username (e.g., "stickerdaniel", "williamhgates")
            ctx: FastMCP context for progress reporting

        Returns:
            Structured data from the person's profile including:
            - linkedin_url, name, location, about, open_to_work
            - experiences: List of work history (position_title, institution_name,
              linkedin_url, from_date, to_date, duration, location, description)
            - educations: List of education (institution_name, degree, linkedin_url,
              from_date, to_date, description)
            - interests: List of interests with category (company, group, school,
              newsletter, influencer) and linkedin_url
            - accomplishments: List of accomplishments (category, title)
            - contacts: List of contact info (type: email/phone/website/linkedin/
              twitter/birthday/address, value, label)
        """
        try:
            # Validate session before scraping
            await ensure_authenticated()

            # Construct LinkedIn URL from username
            linkedin_url = f"https://www.linkedin.com/in/{linkedin_username}/"

            logger.info(f"Scraping profile: {linkedin_url}")

            browser = await get_or_create_browser()
            scraper = PersonScraper(
                browser.page, callback=MCPContextProgressCallback(ctx)
            )
            person = await scraper.scrape(linkedin_url)

            return person.to_dict()

        except Exception as e:
            return handle_tool_error(e, "get_person_profile")

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Search People",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def search_people(
        keywords: str,
        ctx: Context,
        limit: int = 10,
        only_first_degree: bool = False,
    ) -> Dict[str, Any]:
        """
        Search for people on LinkedIn.

        Args:
            keywords: Search keywords (e.g., "Cloud Security Architect", "Recruiter at Google")
            ctx: FastMCP context for progress reporting
            limit: Maximum number of people to return (default: 10)
            only_first_degree: Whether to limit search to your 1st degree connections (default: False)

        Returns:
            Dict with people list (name and profile_url) and count.
        """
        try:
            # Validate session before scraping
            await ensure_authenticated()

            logger.info(
                f"Searching people: keywords='{keywords}', only_first_degree={only_first_degree}"
            )

            browser = await get_or_create_browser()
            scraper = PersonSearchScraper(
                browser.page, callback=MCPContextProgressCallback(ctx)
            )
            people = await scraper.search(
                keywords=keywords,
                limit=limit,
                only_first_degree=only_first_degree,
            )

            return {"people": people, "count": len(people)}

        except Exception as e:
            return handle_tool_error(e, "search_people")
