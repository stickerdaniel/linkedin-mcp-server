"""
LinkedIn job scraping tools with search and detail extraction.

Provides MCP tools for job posting details and job searches
with comprehensive filtering and structured data extraction.
"""

import logging
from typing import Any, Dict, List, Optional

from fastmcp import Context, FastMCP
from linkedin_scraper import JobScraper, JobSearchScraper
from mcp.types import ToolAnnotations

from linkedin_mcp_server.callbacks import MCPContextProgressCallback
from linkedin_mcp_server.drivers.browser import (
    ensure_authenticated,
    get_or_create_browser,
)
from linkedin_mcp_server.error_handler import handle_tool_error, handle_tool_error_list

logger = logging.getLogger(__name__)


def register_job_tools(mcp: FastMCP) -> None:
    """
    Register all job-related tools with the MCP server.

    Args:
        mcp: The MCP server instance
    """

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Job Details",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def get_job_details(job_id: str, ctx: Context) -> Dict[str, Any]:
        """
        Get job details for a specific job posting on LinkedIn.

        Args:
            job_id: LinkedIn job ID (e.g., "4252026496", "3856789012")
            ctx: FastMCP context for progress reporting

        Returns:
            Structured job data including title, company, location,
            posting date, and job description.
        """
        try:
            # Validate session before scraping
            await ensure_authenticated()

            # Construct LinkedIn URL from job ID
            job_url = f"https://www.linkedin.com/jobs/view/{job_id}/"

            logger.info(f"Scraping job: {job_url}")

            browser = await get_or_create_browser()
            scraper = JobScraper(browser.page, callback=MCPContextProgressCallback(ctx))
            job = await scraper.scrape(job_url)

            return job.to_dict()

        except Exception as e:
            return handle_tool_error(e, "get_job_details")

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Search Jobs",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def search_jobs(
        keywords: str,
        ctx: Context,
        location: Optional[str] = None,
        limit: int = 25,
    ) -> List[str] | List[Dict[str, Any]]:
        """
        Search for jobs on LinkedIn.

        Args:
            keywords: Search keywords (e.g., "software engineer", "data scientist")
            ctx: FastMCP context for progress reporting
            location: Optional location filter (e.g., "San Francisco", "Remote")
            limit: Maximum number of job URLs to return (default: 25)

        Returns:
            List of job posting URLs. Use get_job_details to get full details
            for specific jobs.
        """
        try:
            # Validate session before scraping
            await ensure_authenticated()

            logger.info(f"Searching jobs: keywords='{keywords}', location='{location}'")

            browser = await get_or_create_browser()
            scraper = JobSearchScraper(
                browser.page, callback=MCPContextProgressCallback(ctx)
            )
            job_urls = await scraper.search(
                keywords=keywords,
                location=location,
                limit=limit,
            )

            return job_urls

        except Exception as e:
            return handle_tool_error_list(e, "search_jobs")
