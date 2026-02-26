"""
LinkedIn job scraping tools with search and detail extraction.

Uses innerText extraction for resilient job data capture.
"""

import logging
from typing import Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from linkedin_mcp_server.drivers.browser import (
    ensure_authenticated,
    get_or_create_browser,
)
from linkedin_mcp_server.error_handler import handle_tool_error
from linkedin_mcp_server.scraping import LinkedInExtractor

logger = logging.getLogger(__name__)


def register_job_tools(mcp: FastMCP) -> None:
    """Register all job-related tools with the MCP server."""

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Job Details",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def get_job_details(job_id: str, ctx: Context) -> dict[str, Any]:
        """
        Get job details for a specific job posting on LinkedIn.

        Args:
            job_id: LinkedIn job ID (e.g., "4252026496", "3856789012")
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, sections (name -> raw text), pages_visited, and sections_requested.
            The LLM should parse the raw text to extract job details.
        """
        try:
            await ensure_authenticated()

            logger.info("Scraping job: %s", job_id)

            browser = await get_or_create_browser()
            extractor = LinkedInExtractor(browser.page)

            await ctx.report_progress(
                progress=0, total=100, message="Starting job scrape"
            )

            result = await extractor.scrape_job(job_id)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except Exception as e:
            return handle_tool_error(e, "get_job_details")

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Saved Jobs",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def get_saved_jobs(ctx: Context, max_pages: int = 10) -> dict[str, Any]:
        """
        Get the user's saved/bookmarked jobs from LinkedIn's job tracker.

        Args:
            max_pages: Maximum number of pages to scrape (default 10, ~10 jobs/page).

        Returns:
            Dict with url, sections (name -> raw text), pages_visited, sections_requested,
            and job_ids (list of LinkedIn job ID strings).
            The LLM should parse the raw text to extract saved job listings.
        """
        try:
            await ensure_authenticated()

            logger.info("Scraping saved jobs (max_pages=%d)", max_pages)

            browser = await get_or_create_browser()
            extractor = LinkedInExtractor(browser.page)

            await ctx.report_progress(
                progress=0, total=100, message="Fetching saved jobs"
            )

            async def _report(page: int, total: int, msg: str) -> None:
                pct = min(int(page / max(total, 1) * 100), 99)
                await ctx.report_progress(progress=pct, total=100, message=msg)

            result = await extractor.scrape_saved_jobs(
                max_pages=max_pages, on_progress=_report
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except Exception as e:
            return handle_tool_error(e, "get_saved_jobs")

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
        location: str | None = None,
    ) -> dict[str, Any]:
        """
        Search for jobs on LinkedIn.

        Args:
            keywords: Search keywords (e.g., "software engineer", "data scientist")
            ctx: FastMCP context for progress reporting
            location: Optional location filter (e.g., "San Francisco", "Remote")

        Returns:
            Dict with url, sections (name -> raw text), pages_visited, and sections_requested.
            The LLM should parse the raw text to extract job listings.
        """
        try:
            await ensure_authenticated()

            logger.info(
                "Searching jobs: keywords='%s', location='%s'",
                keywords,
                location,
            )

            browser = await get_or_create_browser()
            extractor = LinkedInExtractor(browser.page)

            await ctx.report_progress(
                progress=0, total=100, message="Starting job search"
            )

            result = await extractor.search_jobs(keywords, location)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except Exception as e:
            return handle_tool_error(e, "search_jobs")
