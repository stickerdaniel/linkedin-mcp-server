# src/linkedin_mcp_server/tools/person.py
"""
Person profile tools for LinkedIn MCP server.

This module provides tools for scraping LinkedIn person profiles.
"""

from typing import Dict, Any, List
from mcp.server.fastmcp import FastMCP
from linkedin_scraper import Person

from src.linkedin_mcp_server.drivers.chrome import get_or_create_driver


def register_person_tools(mcp: FastMCP) -> None:
    """
    Register all person-related tools with the MCP server.

    Args:
        mcp (FastMCP): The MCP server instance
    """

    @mcp.tool()
    async def get_person_profile(linkedin_url: str) -> Dict[str, Any]:
        """
        Scrape a person's LinkedIn profile.

        Args:
            linkedin_url (str): The LinkedIn URL of the person's profile

        Returns:
            Dict[str, Any]: Structured data from the person's profile
        """
        driver = get_or_create_driver()

        try:
            print(f"🔍 Scraping profile: {linkedin_url}")
            person = Person(linkedin_url, driver=driver, close_on_complete=False)

            # Convert experiences to structured dictionaries
            experiences: List[Dict[str, Any]] = [
                {
                    "position_title": exp.position_title,
                    "company": exp.institution_name,
                    "from_date": exp.from_date,
                    "to_date": exp.to_date,
                    "duration": exp.duration,
                    "location": exp.location,
                    "description": exp.description,
                }
                for exp in person.experiences
            ]

            # Convert educations to structured dictionaries
            educations: List[Dict[str, Any]] = [
                {
                    "institution": edu.institution_name,
                    "degree": edu.degree,
                    "from_date": edu.from_date,
                    "to_date": edu.to_date,
                    "description": edu.description,
                }
                for edu in person.educations
            ]

            # Convert interests to list of titles
            interests: List[str] = [interest.title for interest in person.interests]

            # Convert accomplishments to structured dictionaries
            accomplishments: List[Dict[str, str]] = [
                {"category": acc.category, "title": acc.title}
                for acc in person.accomplishments
            ]

            # Convert contacts to structured dictionaries
            contacts: List[Dict[str, str]] = [
                {
                    "name": contact.name,
                    "occupation": contact.occupation,
                    "url": contact.url,
                }
                for contact in person.contacts
            ]

            # Return the complete profile data
            return {
                "name": person.name,
                "about": person.about,
                "experiences": experiences,
                "educations": educations,
                "interests": interests,
                "accomplishments": accomplishments,
                "contacts": contacts,
                "company": person.company,
                "job_title": person.job_title,
                "open_to_work": getattr(person, "open_to_work", False),
            }
        except Exception as e:
            print(f"❌ Error scraping profile: {e}")
            return {"error": f"Failed to scrape profile: {str(e)}"}
