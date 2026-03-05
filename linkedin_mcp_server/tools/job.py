"""
LinkedIn job scraping tools with search and detail extraction.

Uses innerText extraction for resilient job data capture.
"""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.dependencies import Depends
from pydantic import Field

from linkedin_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_extractor
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping import LinkedInExtractor

logger = logging.getLogger(__name__)


def register_job_tools(mcp: FastMCP) -> None:
    """Register all job-related tools with the MCP server."""

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Job Details",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job", "scraping"},
    )
    async def get_job_details(
        job_id: str,
        ctx: Context,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Get job details for a specific job posting on LinkedIn.

        Args:
            job_id: LinkedIn job ID (e.g., "4252026496", "3856789012")
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url and sections (name -> raw text).
            The LLM should parse the raw text to extract job details.
        """
        try:
            logger.info("Scraping job: %s", job_id)

            await ctx.report_progress(
                progress=0, total=100, message="Starting job scrape"
            )

            result = await extractor.scrape_job(job_id)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except Exception as e:
            raise_tool_error(e, "get_job_details")  # NoReturn

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Search Jobs",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job", "search"},
    )
    async def search_jobs(
        keywords: str,
        ctx: Context,
        location: str | None = None,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 3,
        date_posted: str | None = None,
        job_type: str | None = None,
        experience_level: str | None = None,
        work_type: str | None = None,
        easy_apply: bool = False,
        sort_by: str | None = None,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Search for jobs on LinkedIn.

        Returns job_ids that can be passed to get_job_details for full info.

        Args:
            keywords: Search keywords (e.g., "software engineer", "data scientist")
            ctx: FastMCP context for progress reporting
            location: Optional location filter (e.g., "San Francisco", "Remote")
            max_pages: Maximum number of result pages to load (1-10, default 3)
            date_posted: Filter by posting date (past_hour, past_24_hours, past_week, past_month)
            job_type: Filter by job type, comma-separated (full_time, part_time, contract, temporary, volunteer, internship, other)
            experience_level: Filter by experience level, comma-separated (internship, entry, associate, mid_senior, director, executive)
            work_type: Filter by work type, comma-separated (on_site, remote, hybrid)
            easy_apply: Only show Easy Apply jobs (default false)
            sort_by: Sort results (date, relevance)

        Returns:
            Dict with url, sections (name -> raw text), and job_ids (list of
            numeric job ID strings usable with get_job_details).
        """
        try:
            logger.info(
                "Searching jobs: keywords='%s', location='%s', max_pages=%d",
                keywords,
                location,
                max_pages,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Starting job search"
            )

            result = await extractor.search_jobs(
                keywords,
                location=location,
                max_pages=max_pages,
                date_posted=date_posted,
                job_type=job_type,
                experience_level=experience_level,
                work_type=work_type,
                easy_apply=easy_apply,
                sort_by=sort_by,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except Exception as e:
            raise_tool_error(e, "search_jobs")  # NoReturn
