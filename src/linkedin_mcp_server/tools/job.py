# src/linkedin_mcp_server/tools/job.py
"""
Job-related tools for LinkedIn MCP server.

This module provides tools for accessing LinkedIn job postings and searches.
"""

from typing import Dict, Any, List
import logging
from mcp.server.fastmcp import FastMCP

from linkedin_mcp_server.client import LinkedInClientManager

logger = logging.getLogger(__name__)


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
        try:
            client = LinkedInClientManager.get_client()

            # Extract job ID from URL
            if "/jobs/view/" in job_url:
                job_id = job_url.split("/jobs/view/")[1].split("/")[0]
            else:
                job_id = job_url  # Assume it's already a job ID

            print(f"💼 Retrieving job: {job_id}")

            # Get job details
            job = client.get_job(job_id)

            # Try to get job skills
            try:
                job_skills = client.get_job_skills(job_id)
                if job_skills:
                    job["skills"] = job_skills
            except Exception as skills_e:
                logger.warning(f"Could not retrieve job skills: {skills_e}")

            return job
        except Exception as e:
            logger.error(f"Error retrieving job: {e}")
            return {"error": f"Failed to retrieve job details: {str(e)}"}

    @mcp.tool()
    async def search_jobs(search_term: str) -> List[Dict[str, Any]]:
        """
        Search for jobs on LinkedIn with the given search term.

        Args:
            search_term (str): The job search query

        Returns:
            List[Dict[str, Any]]: List of job search results
        """
        try:
            client = LinkedInClientManager.get_client()

            print(f"🔍 Searching jobs: {search_term}")

            # Search for jobs
            jobs = client.search_jobs(search_term=search_term)
            return jobs
        except Exception as e:
            logger.error(f"Error searching jobs: {e}")
            return [{"error": f"Failed to search jobs: {str(e)}"}]

    @mcp.tool()
    async def get_recommended_jobs() -> List[Dict[str, Any]]:
        """
        Get recommended jobs from your LinkedIn homepage.

        Returns:
            List[Dict[str, Any]]: List of recommended jobs
        """
        try:
            client = LinkedInClientManager.get_client()

            print("📋 Getting recommended jobs")

            # Get recommended jobs
            jobs = client.get_recommended_jobs()
            return jobs
        except Exception as e:
            logger.error(f"Error getting recommended jobs: {e}")
            return [{"error": f"Failed to get recommended jobs: {str(e)}"}]
