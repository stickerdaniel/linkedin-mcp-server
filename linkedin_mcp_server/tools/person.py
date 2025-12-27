# src/linkedin_mcp_server/tools/person.py
"""
LinkedIn person profile scraping tools with structured data extraction.

Provides MCP tools for extracting comprehensive LinkedIn profile information including
experience, education, skills, and contact details with proper error handling.
"""

import logging
from typing import Any, Dict

from fastmcp import FastMCP

from linkedin_mcp_server.error_handler import handle_tool_error
from linkedin_mcp_server.scraper_adapter import get_scraper_adapter

logger = logging.getLogger(__name__)


def register_person_tools(mcp: FastMCP) -> None:
    """
    Register all person-related tools with the MCP server.

    Args:
        mcp (FastMCP): The MCP server instance
    """

    @mcp.tool()
    async def get_person_profile(linkedin_username: str) -> Dict[str, Any]:
        """
        Get a specific person's LinkedIn profile.

        Args:
            linkedin_username (str): LinkedIn username (e.g., "stickerdaniel", "anistji")

        Returns:
            Dict[str, Any]: Structured data from the person's profile
        """
        try:
            scraper = get_scraper_adapter()
            return scraper.get_person_profile(linkedin_username)
        except Exception as e:
            return handle_tool_error(e, "get_person_profile")
