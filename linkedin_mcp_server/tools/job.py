# src/linkedin_mcp_server/tools/job.py
"""
LinkedIn job scraping tools with search and detail extraction capabilities.

Provides MCP tools for job posting details, job searches, and recommendations
with comprehensive filtering and structured data extraction.
"""

import logging
from typing import Any, Dict, List

from fastmcp import FastMCP
from linkedin_scraper import Job, JobSearch

from linkedin_mcp_server.error_handler import (
    handle_tool_error,
    handle_tool_error_list,
    safe_get_driver,
)

logger = logging.getLogger(__name__)


def register_job_tools(mcp: FastMCP) -> None:
    """
    Register all job-related tools with the MCP server.

    Args:
        mcp (FastMCP): The MCP server instance
    """

    @mcp.tool()
    async def get_job_details(job_id: str) -> Dict[str, Any]:
        """
        Get job details for a specific job posting on LinkedIn

        Args:
            job_id (str): LinkedIn job ID (e.g., "4252026496", "3856789012")

        Returns:
            Dict[str, Any]: Structured job data including title, company, location, posting date,
                          application count, and job description (may be empty if content is protected)
        """
        try:
            # Construct clean LinkedIn URL from job ID
            job_url = f"https://www.linkedin.com/jobs/view/{job_id}/"

            driver = safe_get_driver()

            logger.info(f"Scraping job: {job_url}")
            job = Job(job_url, driver=driver, close_on_complete=False)

            # Convert job object to a dictionary
            return job.to_dict()
        except Exception as e:
            return handle_tool_error(e, "get_job_details")

    @mcp.tool()
    async def search_jobs(search_term: str) -> List[Dict[str, Any]]:
        """
        Search for jobs on LinkedIn using a search term.

        Args:
            search_term (str): Search term to use for the job search.

        Returns:
            List[Dict[str, Any]]: List of job search results
        """
        try:
            driver = safe_get_driver()

            logger.info(f"Searching jobs: {search_term}")
            job_search = JobSearch(driver=driver, close_on_complete=False, scrape=False)
            jobs = job_search.search(search_term)

            # Convert job objects to dictionaries
            return [job.to_dict() for job in jobs]
        except Exception as e:
            return handle_tool_error_list(e, "search_jobs")

    @mcp.tool()
    async def get_recommended_jobs() -> List[Dict[str, Any]]:
        """
        Get your personalized recommended jobs from LinkedIn

        Returns:
            List[Dict[str, Any]]: List of recommended jobs
        """
        try:
            driver = safe_get_driver()

            logger.info("Getting recommended jobs")
            job_search = JobSearch(
                driver=driver,
                close_on_complete=False,
                scrape=True,  # Enable scraping to get recommended jobs
                scrape_recommended_jobs=True,
            )

            if hasattr(job_search, "recommended_jobs") and job_search.recommended_jobs:
                return [job.to_dict() for job in job_search.recommended_jobs]
            else:
                return []
        except Exception as e:
            return handle_tool_error_list(e, "get_recommended_jobs")
