# src/linkedin_mcp_server/tools/person.py
"""
LinkedIn person profile scraping tools with structured data extraction.

Provides MCP tools for extracting comprehensive LinkedIn profile information including
experience, education, skills, and detailed contact information with proper error handling.
"""

import logging
from typing import Any, Dict, List

from fastmcp import FastMCP

from linkedin_mcp_server.error_handler import handle_tool_error, safe_get_driver
from linkedin_mcp_server.scrapers import PersonExtended

logger = logging.getLogger(__name__)


def register_person_tools(mcp: FastMCP) -> None:
    """
    Register all person-related tools with the MCP server.

    Args:
        mcp (FastMCP): The MCP server instance
    """

    @mcp.tool()
    async def get_person_profile(linkedin_username: str, extract_contact_info: bool = True) -> Dict[str, Any]:
        """
        Get a specific person's LinkedIn profile with optional detailed contact information.

        Args:
            linkedin_username (str): LinkedIn username (e.g., "stickerdaniel", "anistji")
            extract_contact_info (bool): Whether to extract detailed contact information from Contact Info modal (default: True)

        Returns:
            Dict[str, Any]: Structured data from the person's profile including contact details
        """
        try:
            # Construct clean LinkedIn URL from username
            linkedin_url = f"https://www.linkedin.com/in/{linkedin_username}/"

            driver = safe_get_driver()

            logger.info(f"Scraping profile: {linkedin_url}")

            # Use PersonExtended for enhanced contact info extraction
            person = PersonExtended(linkedin_url, driver=driver, close_on_complete=False)

            # Use the to_dict method which includes all data including contact info
            profile_data = person.to_dict()

            # If extract_contact_info is False, remove the detailed contact info
            if not extract_contact_info:
                profile_data.pop("contact_info", None)

            return profile_data

        except Exception as e:
            return handle_tool_error(e, "get_person_profile")

