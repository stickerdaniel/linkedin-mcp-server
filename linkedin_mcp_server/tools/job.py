# src/linkedin_mcp_server/tools/job.py
"""
Job-related tools for LinkedIn MCP server.

This module provides tools for scraping LinkedIn job postings and searches.
"""

from typing import Dict, Any, List
from mcp.server.fastmcp import FastMCP
from linkedin_scraper import Job, JobSearch

from linkedin_mcp_server.drivers.chrome import get_or_create_driver


def register_job_tools(mcp: FastMCP) -> None:
    """
    Register all job-related tools with the MCP server.

    Args:
        mcp (FastMCP): The MCP server instance
    """

    @mcp.tool()
    async def get_job_details(job_url: str) -> Dict[str, Any]:
        """
        Scrape job details from a LinkedIn job posting.

        Args:
            job_url (str): The LinkedIn URL of the job posting

        Returns:
            Dict[str, Any]: Structured data from the job posting
        """
        driver = get_or_create_driver()

        try:
            print(f"💼 Scraping job: {job_url}")
            job = Job(job_url, driver=driver, close_on_complete=False)

            # Convert job object to a dictionary
            return job.to_dict()
        except Exception as e:
            print(f"❌ Error scraping job: {e}")
            return {"error": f"Failed to scrape job posting: {str(e)}"}

    @mcp.tool()
    async def search_jobs(search_term: str) -> List[Dict[str, Any]]:
        """
        Search for jobs on LinkedIn with the given search term.

        Args:
            search_term (str): The job search query

        Returns:
            List[Dict[str, Any]]: List of job search results
        """
        driver = get_or_create_driver()

        try:
            print(f"🔍 Searching jobs: {search_term}")
            job_search = JobSearch(driver=driver, close_on_complete=False, scrape=False)
            jobs = job_search.search(search_term)

            # Convert job objects to dictionaries
            return [job.to_dict() for job in jobs]
        except Exception as e:
            print(f"❌ Error searching jobs: {e}")
            return [{"error": f"Failed to search jobs: {str(e)}"}]

    @mcp.tool()
    async def get_recommended_jobs() -> List[Dict[str, Any]]:
        """
        Get recommended jobs from your LinkedIn homepage.

        Returns:
            List[Dict[str, Any]]: List of recommended jobs
        """
        driver = get_or_create_driver()

        try:
            print("📋 Getting recommended jobs")
            job_search = JobSearch(
                driver=driver,
                close_on_complete=False,
                scrape=True,
                scrape_recommended_jobs=True,
            )

            # Get recommended jobs and convert to dictionaries
            if hasattr(job_search, "recommended_jobs"):
                return [job.to_dict() for job in job_search.recommended_jobs]
            else:
                return []
        except Exception as e:
            print(f"❌ Error getting recommended jobs: {e}")
            return [{"error": f"Failed to get recommended jobs: {str(e)}"}]
