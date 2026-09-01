"""
LinkedIn job scraping tools with search and detail extraction.

Uses innerText extraction for resilient job data capture.
"""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error

logger = logging.getLogger(__name__)


def register_job_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register all job-related tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Job Details",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_job_details(
        job_id: str,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get job details for a specific job posting on LinkedIn.

        Args:
            job_id: LinkedIn job ID (e.g., "4252026496", "3856789012")
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, sections (name -> raw text), and optional references.
            The LLM should parse the raw text to extract job details.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_job_details"
            )
            logger.info("Scraping job: %s", job_id)

            await ctx.report_progress(
                progress=0, total=100, message="Starting job scrape"
            )

            result = await extractor.scrape_job(job_id)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_job_details")
        except Exception as e:
            raise_tool_error(e, "get_job_details")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Jobs",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job", "search"},
        exclude_args=["extractor"],
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
        extractor: Any | None = None,
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
            Dict with url, sections (name -> raw text), job_ids (list of
            numeric job ID strings usable with get_job_details), and optional references.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="search_jobs"
            )
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

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "search_jobs")
        except Exception as e:
            raise_tool_error(e, "search_jobs")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Job Alert Results",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job", "search"},
        exclude_args=["extractor"],
    )
    async def get_job_alert_results(
        url: str,
        ctx: Context,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 3,
        date_posted: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Fetch job results from a LinkedIn job-alert or saved-search URL.

        Use this instead of search_jobs when you have a specific URL copied
        from a LinkedIn job-alert notification, e.g. a
        "jobs/search-results/?...origin=SEMANTIC_SEARCH_JOB_ALERT..." link.
        search_jobs always builds its own classic search and cannot
        reproduce LinkedIn's personalized/AI-ranked alert feed; this
        navigates to the exact URL you provide instead.

        Args:
            url: A linkedin.com/jobs/search/ or jobs/search-results/ URL
            ctx: FastMCP context for progress reporting
            max_pages: Maximum number of result pages to load (1-10, default 3)
            date_posted: Filter by posting date. Must be one of past_hour,
                past_24_hours, past_week, past_month (LinkedIn silently
                ignores other values rather than applying them), replacing
                any date filter already in the URL. A job-alert URL's own
                filter marks when that alert last fired, not a chosen range,
                so pass this to actually narrow results (e.g. "just show me
                the last week").

        Returns:
            Dict with url, sections (alert_results: raw text), job_ids (list of
            numeric job ID strings usable with get_job_details), and optional references.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_job_alert_results"
            )
            logger.info(
                "Fetching job alert results: url='%s', max_pages=%d", url, max_pages
            )

            await ctx.report_progress(
                progress=0, total=100, message="Starting alert fetch"
            )

            result = await extractor.get_job_alert_results(
                url, max_pages=max_pages, date_posted=date_posted
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_job_alert_results")
        except Exception as e:
            raise_tool_error(e, "get_job_alert_results")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Job Alerts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job", "search"},
        exclude_args=["extractor"],
    )
    async def get_job_alerts(
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List the authenticated user's configured job alerts.

        Returns each alert's name and its canonical search-results URL.
        Pass that URL to get_job_alert_results to fetch the alert's
        current results.

        Args:
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, sections (job_alerts: raw text), alerts (list of
            {name, url}), and optional references.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_job_alerts"
            )
            logger.info("Fetching job alerts")

            await ctx.report_progress(
                progress=0, total=100, message="Loading job alerts"
            )

            result = await extractor.get_job_alerts()

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_job_alerts")
        except Exception as e:
            raise_tool_error(e, "get_job_alerts")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Job Alert Notifications",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job", "search"},
        exclude_args=["extractor"],
    )
    async def get_job_alert_notifications(
        ctx: Context,
        unread_only: bool = False,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List job-alert notifications from LinkedIn's notifications feed.

        Filters to notifications LinkedIn generated because one of your job
        alerts has new results, excluding other notification types
        (connection requests, post reactions, similar-jobs recommendations,
        etc). Each item's url is the alert's delta-filtered search-results
        link (new results since that alert last fired) — pass it to
        get_job_alert_results to fetch those jobs.

        Args:
            ctx: FastMCP context for progress reporting
            unread_only: Only return notifications not yet marked read
            limit: Maximum number of job-alert notifications to return
                (1-50, default 20)

        Returns:
            Dict with url, sections (notifications: raw text), alerts (list of
            {alert_name, url, unread, posted_at}), and optional references.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_job_alert_notifications"
            )
            logger.info(
                "Fetching job alert notifications: unread_only=%s, limit=%d",
                unread_only,
                limit,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Loading notifications"
            )

            result = await extractor.get_job_alert_notifications(
                unread_only=unread_only, limit=limit
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_job_alert_notifications")
        except Exception as e:
            raise_tool_error(e, "get_job_alert_notifications")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Saved Jobs",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_saved_jobs(
        ctx: Context,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 3,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List job postings saved by the authenticated LinkedIn user.

        Returns job_ids that can be passed to get_job_details for full info.

        Args:
            ctx: FastMCP context for progress reporting
            max_pages: Maximum number of saved-jobs pages to load (1-10, default 3)

        Returns:
            Dict with url, sections (name -> raw text), job_ids (list of
            numeric job ID strings usable with get_job_details), and optional references.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_saved_jobs"
            )
            logger.info("Fetching saved jobs (max_pages=%d)", max_pages)

            await ctx.report_progress(
                progress=0, total=100, message="Loading saved jobs"
            )

            result = await extractor.get_saved_jobs(max_pages=max_pages)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_saved_jobs")
        except Exception as e:
            raise_tool_error(e, "get_saved_jobs")  # NoReturn
