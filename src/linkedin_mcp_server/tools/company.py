# src/linkedin_mcp_server/tools/company.py
"""
Company profile tools for LinkedIn MCP server.

This module provides tools for accessing LinkedIn company profiles.
"""

from typing import Dict, Any, List
import logging
from mcp.server.fastmcp import FastMCP

from linkedin_mcp_server.client import LinkedInClientManager

logger = logging.getLogger(__name__)


def register_company_tools(mcp: FastMCP) -> None:
    """
    Register all company-related tools with the MCP server.

    Args:
        mcp (FastMCP): The MCP server instance
    """

    @mcp.tool()
    async def get_company_profile(
        linkedin_url: str, get_employees: bool = False
    ) -> Dict[str, Any]:
        """
        Scrape a company's LinkedIn profile.

        Args:
            linkedin_url (str): The LinkedIn URL of the company's profile
            get_employees (bool): Whether to scrape the company's employees (slower)

        Returns:
            Dict[str, Any]: Structured data from the company's profile
        """
        try:
            client = LinkedInClientManager.get_client()

            # Extract company name/ID from URL
            if "/company/" in linkedin_url:
                company_id = linkedin_url.split("/company/")[1].split("/")[0]
            else:
                company_id = linkedin_url  # Assume it's already a company ID

            print(f"🏢 Retrieving company: {company_id}")

            # Get comprehensive company data
            company = client.get_company(company_id)

            # Get updates if available
            try:
                updates = client.get_company_updates(
                    public_id=company_id, max_results=10
                )
                if updates:
                    company["recent_updates"] = updates
            except Exception as updates_e:
                logger.warning(f"Could not retrieve company updates: {updates_e}")

            return company
        except Exception as e:
            logger.error(f"Error retrieving company: {e}")
            return {"error": f"Failed to retrieve company profile: {str(e)}"}

    @mcp.tool()
    async def search_companies(keywords: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search for companies on LinkedIn.

        Args:
            keywords (str): Search terms
            limit (int): Maximum number of results to return

        Returns:
            List[Dict[str, Any]]: List of company search results
        """
        try:
            client = LinkedInClientManager.get_client()

            print(f"🔍 Searching companies: {keywords}")

            # Search for companies with the given keywords
            companies = client.search_companies(keywords=keywords, limit=limit)
            return companies
        except Exception as e:
            logger.error(f"Error searching companies: {e}")
            return [{"error": f"Failed to search companies: {str(e)}"}]

    @mcp.tool()
    async def get_school(linkedin_url: str) -> Dict[str, Any]:
        """
        Get information about a school/university from LinkedIn.

        Args:
            linkedin_url (str): The LinkedIn URL of the school/university

        Returns:
            Dict[str, Any]: Structured data about the school
        """
        try:
            client = LinkedInClientManager.get_client()

            # Extract school name/ID from URL
            if "/school/" in linkedin_url:
                school_id = linkedin_url.split("/school/")[1].split("/")[0]
            else:
                school_id = linkedin_url  # Assume it's already a school ID

            print(f"🏫 Retrieving school: {school_id}")

            # Get school data
            school = client.get_school(school_id)
            return school
        except Exception as e:
            logger.error(f"Error retrieving school: {e}")
            return {"error": f"Failed to retrieve school information: {str(e)}"}
