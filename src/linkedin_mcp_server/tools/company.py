# src/linkedin_mcp_server/tools/company.py
"""
Company profile tools for LinkedIn MCP server.

This module provides tools for scraping LinkedIn company profiles.
"""

from typing import Dict, Any, List
from mcp.server.fastmcp import FastMCP
from linkedin_scraper import Company

from src.linkedin_mcp_server.drivers.chrome import get_or_create_driver


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
        driver = get_or_create_driver()

        try:
            print(f"🏢 Scraping company: {linkedin_url}")
            if get_employees:
                print("⚠️ Fetching employees may take a while...")

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
            print(f"❌ Error scraping company: {e}")
            return {"error": f"Failed to scrape company profile: {str(e)}"}
