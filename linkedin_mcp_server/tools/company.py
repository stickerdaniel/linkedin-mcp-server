# src/linkedin_mcp_server/tools/company.py
"""
LinkedIn company profile scraping tools with employee data extraction.

Provides MCP tools for extracting company information, employee lists, and company
insights from LinkedIn with configurable depth and comprehensive error handling.
"""

import logging
from typing import Any, Dict

from fastmcp import FastMCP

from linkedin_mcp_server.error_handler import handle_tool_error
from linkedin_mcp_server.scraper_adapter import get_scraper_adapter

logger = logging.getLogger(__name__)


def register_company_tools(mcp: FastMCP) -> None:
    """
    Register all company-related tools with the MCP server.

    Args:
        mcp (FastMCP): The MCP server instance
    """

    @mcp.tool()
    async def get_company_profile(
        company_name: str, get_employees: bool = False
    ) -> Dict[str, Any]:
        """
        Get a specific company's LinkedIn profile.

        Args:
            company_name (str): LinkedIn company name (e.g., "docker", "anthropic", "microsoft")
            get_employees (bool): Whether to scrape the company's employees (slower)

        Returns:
            Dict[str, Any]: Structured data from the company's profile
        """
        try:
            scraper = get_scraper_adapter()
            return scraper.get_company_profile(company_name, get_employees)
        except Exception as e:
            return handle_tool_error(e, "get_company_profile")
