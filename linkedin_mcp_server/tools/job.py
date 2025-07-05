# src/linkedin_mcp_server/tools/job.py
"""
Job tools for LinkedIn MCP server.

This module provides tools for scraping LinkedIn job postings and searches.
"""

from typing import Any, Dict, List

from fastmcp import FastMCP
from linkedin_scraper import Job, JobSearch

from linkedin_mcp_server.error_handler import (
    handle_linkedin_errors,
    handle_linkedin_errors_list,
    safe_get_driver,
)


def register_job_tools(mcp: FastMCP) -> None:
    """
    Register all job-related tools with the MCP server.

    Args:
        mcp (FastMCP): The MCP server instance
    """

    @mcp.tool()
    @handle_linkedin_errors
    async def get_job_details(job_url: str) -> Dict[str, Any]:
        """
        Scrape job details from a LinkedIn job posting.

        IMPORTANT: Only use direct LinkedIn job URLs in the format:
        https://www.linkedin.com/jobs/view/XXXXXXXX/ where XXXXXXXX is the job ID.

        This tool extracts comprehensive job information including title, company,
        location, posting date, application count, and full job description.

        Args:
            job_url (str): The LinkedIn job posting URL to scrape

        Returns:
            Dict[str, Any]: Structured job data including title, company, location, posting date,
                          application count, and job description (may be empty if content is protected)
        """
        driver = safe_get_driver()

        print(f"💼 Scraping job: {job_url}")
        job = Job(job_url, driver=driver, close_on_complete=False)

        # Convert job object to a dictionary
        return job.to_dict()

    @mcp.tool()
    @handle_linkedin_errors_list
    async def search_jobs(search_term: str) -> List[Dict[str, Any]]:
        """
        Search for jobs on LinkedIn (Note: This tool has compatibility issues).

        Args:
            search_term (str): The search term to use for job search

        Returns:
            List[Dict[str, Any]]: List of job search results
        """
        driver = safe_get_driver()

        print(f"🔍 Searching jobs: {search_term}")
        job_search = JobSearch(driver=driver, close_on_complete=False, scrape=False)
        jobs = job_search.search(search_term)

        # Convert job objects to dictionaries
        return [job.to_dict() for job in jobs]

    @mcp.tool()
    @handle_linkedin_errors_list
    async def get_recommended_jobs() -> List[Dict[str, Any]]:
        """
        Get recommended jobs from LinkedIn (Note: This tool has compatibility issues).

        Returns:
            List[Dict[str, Any]]: List of recommended jobs
        """
        driver = safe_get_driver()

        print("📋 Getting recommended jobs")
        job_search = JobSearch(
            driver=driver,
            close_on_complete=False,
            scrape=False,
        )

        if job_search.recommended_jobs:
            return [job.to_dict() for job in job_search.recommended_jobs]
        else:
            return []
