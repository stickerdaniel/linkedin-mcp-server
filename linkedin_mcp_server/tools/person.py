"""
LinkedIn person profile scraping tools.

Provides MCP tools for extracting comprehensive LinkedIn profile information including
experience, education, skills, and contact details.
"""

import logging
from typing import Any, Dict

from fastmcp import Context, FastMCP
from linkedin_scraper import PersonScraper
from mcp.types import ToolAnnotations

from linkedin_mcp_server.callbacks import MCPContextProgressCallback
from linkedin_mcp_server.drivers.browser import (
    ensure_authenticated,
    get_or_create_browser,
)
from linkedin_mcp_server.error_handler import handle_tool_error

logger = logging.getLogger(__name__)


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
            Structured data from the person's profile including name, about,
            experiences, educations, and more.
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
