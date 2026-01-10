"""
LinkedIn company profile scraping tools.

Provides MCP tools for extracting company information from LinkedIn
with comprehensive error handling.
"""

import logging
from typing import Any, Dict

from fastmcp import FastMCP
from linkedin_scraper import CompanyScraper
from mcp.types import ToolAnnotations

from linkedin_mcp_server.callbacks import MCPProgressCallback
from linkedin_mcp_server.drivers.browser import (
    ensure_authenticated,
    get_or_create_browser,
)
from linkedin_mcp_server.error_handler import handle_tool_error

logger = logging.getLogger(__name__)


def register_company_tools(mcp: FastMCP) -> None:
    """
    Register all company-related tools with the MCP server.

    Args:
        mcp: The MCP server instance
    """

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Company Profile",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def get_company_profile(company_name: str) -> Dict[str, Any]:
        """
        Get a specific company's LinkedIn profile.

        Args:
            company_name: LinkedIn company name (e.g., "docker", "anthropic", "microsoft")

        Returns:
            Structured data from the company's profile including name, about,
            headquarters, industry, size, and more.
        """
        try:
            # Validate session before scraping
            await ensure_authenticated()

            # Construct LinkedIn URL from company name
            linkedin_url = f"https://www.linkedin.com/company/{company_name}/"

            logger.info(f"Scraping company: {linkedin_url}")

            browser = await get_or_create_browser()
            scraper = CompanyScraper(browser.page, callback=MCPProgressCallback())
            company = await scraper.scrape(linkedin_url)

            return company.to_dict()

        except Exception as e:
            return handle_tool_error(e, "get_company_profile")
