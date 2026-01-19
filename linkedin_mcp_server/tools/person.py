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
