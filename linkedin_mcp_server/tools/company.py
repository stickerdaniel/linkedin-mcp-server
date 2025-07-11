# src/linkedin_mcp_server/tools/company.py
"""
LinkedIn company profile scraping tools with employee data extraction.

Provides MCP tools for extracting company information, employee lists, and company
insights from LinkedIn with configurable depth and comprehensive error handling.
"""

import logging
from typing import Any, Dict, List

from fastmcp import FastMCP
from linkedin_scraper import Company

from linkedin_mcp_server.error_handler import handle_tool_error, safe_get_driver

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
            # Construct clean LinkedIn URL from company name
            linkedin_url = f"https://www.linkedin.com/company/{company_name}/"

            driver = safe_get_driver()

            logger.info(f"Scraping company: {linkedin_url}")
            if get_employees:
                logger.info("Fetching employees may take a while...")

            company = Company(
                linkedin_url,
                driver=driver,
                get_employees=get_employees,
                close_on_complete=False,
            )

            # Convert showcase pages to structured dictionaries
            showcase_pages: List[Dict[str, Any]] = [
                {
                    "name": page.name,
                    "linkedin_url": page.linkedin_url,
                    "followers": page.followers,
                }
                for page in company.showcase_pages
            ]

            # Convert affiliated companies to structured dictionaries
            affiliated_companies: List[Dict[str, Any]] = [
                {
                    "name": affiliated.name,
                    "linkedin_url": affiliated.linkedin_url,
                    "followers": affiliated.followers,
                }
                for affiliated in company.affiliated_companies
            ]

            # Build the result dictionary
            result: Dict[str, Any] = {
                "name": company.name,
                "about_us": company.about_us,
                "website": company.website,
                "phone": company.phone,
                "headquarters": company.headquarters,
                "founded": company.founded,
                "industry": company.industry,
                "company_type": company.company_type,
                "company_size": company.company_size,
                "specialties": company.specialties,
                "showcase_pages": showcase_pages,
                "affiliated_companies": affiliated_companies,
                "headcount": company.headcount,
            }

            # Add employees if requested and available
            if get_employees and company.employees:
                result["employees"] = company.employees

            return result
        except Exception as e:
            return handle_tool_error(e, "get_company_profile")
