"""LinkedIn job tools for browser search and guest-endpoint detail fetching."""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_ready_extractor, tool_errors
from linkedin_mcp_server.linkedin.guest_jobs import (
    DEFAULT_WORKERS,
    fetch_jobs,
)
from linkedin_mcp_server.linkedin.identifiers import normalize_job_id
from linkedin_mcp_server.linkedin.job_policy import JobsTrackerStage

logger = logging.getLogger(__name__)


def _documents_dir() -> Path:
    """Where a search is saved when the caller names no path. A function so tests can move it."""
    return Path.home() / "Documents"


def _write_search_results(result: dict[str, Any], output_path: str | None) -> Path:
    """Write the whole search result as JSON and return the file it went to."""
    if output_path:
        path = Path(output_path).expanduser()
        if path.is_dir():
            raise ToolError(f"output_path is a directory, not a file: {path}")
    else:
        stem = datetime.now().strftime("%d_%m_%Y_%H_%M_%S")
        directory = _documents_dir()
        path = directory / f"{stem}.json"
        # Two searches in one second must not overwrite each other.
        suffix = 0
        while path.exists():
            suffix += 1
            path = directory / f"{stem}_{suffix}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        raise ToolError(f"Could not write the search results to {path}: {exc}") from exc
    return path


def register_job_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register all job-related tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Job Details",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job"},
    )
    @tool_errors
    async def get_job_details(
        job_ids: list[str],
        ctx: Context,
        # No Field(le=...) bound: pydantic would answer 6 with a bare "less than
        # or equal to 5", and fetch_jobs says why the limit exists.
        n_workers: int = DEFAULT_WORKERS,
    ) -> dict[str, Any]:
        """
        Get details for one or more job postings on LinkedIn.

        Reads LinkedIn's public guest endpoint over plain HTTP: no browser,
        no login, and the postings are fetched concurrently. Replaces the
        earlier one-job-at-a-time browser reading.

        Args:
            job_ids: LinkedIn job IDs (e.g., ["4252026496", "3856789012"]) or
                /jobs/view/ URLs. Duplicates are fetched once.
            ctx: FastMCP context for progress reporting
            n_workers: Concurrent requests (1-5, default 3). Above 5 is
                refused: more parallel requests risk LinkedIn flagging a bot.

        Returns:
            Dict with jobs (one per id, in order) and summary
            {requested, ok, failed}. A successful job has status "ok", job_id,
            url, title, company, company_url, location, posted, applicants,
            criteria (seniority_level, employment_type, job_function,
            industries, when shown) and description (plain text). A failed
            job has status "error" and error: not_found, rate_limited,
            skipped_rate_limited (not sent after a rate limit), http_<code>,
            request_failed, parse_failed or description_missing (often a
            closed posting). One failure never discards the other jobs.
            Not included: the signed-in view (applied/closed state, people to
            reach out to), similar jobs, apply links; use get_job_apply_url
            for the application link.
        """
        logger.info("Reading %d job(s) with %d worker(s)", len(job_ids), n_workers)

        async def report_progress(completed: int, total: int) -> None:
            await ctx.report_progress(
                progress=completed,
                total=total,
                message=f"Fetched {completed} of {total} postings",
            )

        try:
            result = await fetch_jobs(job_ids, n_workers, on_progress=report_progress)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

        await ctx.report_progress(progress=100, total=100, message="Complete")

        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Job Apply URL",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job"},
    )
    @tool_errors
    async def get_job_apply_url(
        job_id: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """
        Get how a job posting takes applications, and the employer's application link.

        Reads the posting without clicking anything.

        Args:
            job_id: LinkedIn job ID (e.g., "4252026496", "3856789012")
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url and apply: {type, url?}. type is easy_apply,
            external, applied, closed or unknown. url is the employer's
            application link as LinkedIn gives it, for external postings;
            it is not opened, so a short link is returned unexpanded.
            A posting that could not be read returns section_errors instead.
        """
        job_id = normalize_job_id(job_id)
        extractor = await get_ready_extractor(ctx, tool_name="get_job_apply_url")
        logger.info("Reading apply link: %s", job_id)

        await ctx.report_progress(progress=0, total=100, message="Opening job posting")

        result = await extractor.get_job_apply_url(job_id)

        await ctx.report_progress(progress=100, total=100, message="Complete")

        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Jobs",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job", "search"},
    )
    @tool_errors
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
        output_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Search for jobs on LinkedIn.

        The full results are saved to a JSON file and only a short summary is
        returned, so a large search does not fill the context. Read the file
        for the results and the job_ids to pass to get_job_details.

        LinkedIn is moving accounts to a redesigned job search, which keeps
        only the keywords, date_posted and paging. It drops the filters
        marked "Classic search only" and reports them in section_errors as
        filters_dropped. When it drops the location or work type, search again
        with them in the keywords ("remote python developer in Lyon") and keep
        doing so; words such as "senior" or "internship" there narrow the
        results too. Quotes do not make a phrase exact on that search.

        Args:
            keywords: Search keywords (e.g., "software engineer", "data scientist")
            ctx: FastMCP context for progress reporting
            location: Optional location filter (e.g., "San Francisco", "Remote"). Classic search only.
            max_pages: Maximum number of result pages to load (1-10, default 3)
            date_posted: Filter by posting date (past_hour, past_24_hours, past_week, past_month)
            job_type: Filter by job type, comma-separated (full_time, part_time, contract, temporary, volunteer, internship, other). Classic search only.
            experience_level: Filter by experience level, comma-separated (internship, entry, associate, mid_senior, director, executive). Classic search only.
            work_type: Filter by work type, comma-separated (on_site, remote, hybrid). Classic search only.
            easy_apply: Only show Easy Apply jobs (default false). Classic search only.
            sort_by: Sort results (date, relevance). Classic search only.
            output_path: File to save the full results to (JSON). Default: a
                file named DD_MM_YYYY_HH_MM_SS.json in the user's Documents folder.

        Returns:
            Dict with output_file (absolute path of the saved results), url,
            job_count, total ({count, exact}; the result count LinkedIn
            advertises, exact false for a lower bound such as "1,000+"),
            section_errors (only when present: filters_dropped, or
            no_matching_jobs for a search with no matches) and note.
            The file holds the complete result: url, sections (name -> raw
            text), job_ids (numeric job ID strings usable with
            get_job_details), promoted_job_ids (the subset LinkedIn marks as
            promoted, present only when every page could be read), total,
            references and section_errors.
        """
        # Before the browser, because FastMCP is already timing this call
        # and the extractor's budget is a fraction of the same figure. A
        # cold start that spends three of ten seconds left it planning
        # against eight it no longer had, and the call was cancelled with
        # every page it had gathered.
        started = time.monotonic()
        extractor = await get_ready_extractor(ctx, tool_name="search_jobs")
        logger.info(
            "Searching jobs: keywords='%s', location='%s', max_pages=%d",
            keywords,
            location,
            max_pages,
        )

        await ctx.report_progress(progress=0, total=100, message="Starting job search")

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
            # What is left of the figure FastMCP cancels this call on.
            tool_timeout=max(0.0, tool_timeout - (time.monotonic() - started)),
        )

        output_file = _write_search_results(result, output_path)

        await ctx.report_progress(progress=100, total=100, message="Complete")

        summary: dict[str, Any] = {
            "output_file": str(output_file.resolve()),
            "url": result.get("url"),
            "job_count": len(result.get("job_ids", [])),
            "total": result.get("total"),
        }
        if result.get("section_errors"):
            summary["section_errors"] = result["section_errors"]
        summary["note"] = (
            "The full results, including job_ids, are in output_file. Read that "
            "file when you need them; pass job_ids to get_job_details."
        )
        return summary

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Saved Jobs",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job"},
    )
    @tool_errors
    async def get_saved_jobs(
        ctx: Context,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 3,
        stage: JobsTrackerStage = "saved",
    ) -> dict[str, Any]:
        """
        List the authenticated user's jobs at one stage of LinkedIn's job tracker.

        Returns job_ids that can be passed to get_job_details for full info.

        Args:
            ctx: FastMCP context for progress reporting
            max_pages: Maximum number of tracker pages to load (1-10, default 3)
            stage: Tracker tab: saved (default), in_progress, applied or
                archived. applied lists Easy Apply submissions and external
                applications the user confirmed to LinkedIn.

        Returns:
            Dict with url, sections (name -> raw text), job_ids (list of
            numeric job ID strings usable with get_job_details), and optional references.
        """
        extractor = await get_ready_extractor(ctx, tool_name="get_saved_jobs")
        logger.info("Fetching %s jobs (max_pages=%d)", stage, max_pages)

        await ctx.report_progress(progress=0, total=100, message="Loading saved jobs")

        result = await extractor.get_saved_jobs(max_pages=max_pages, stage=stage)

        await ctx.report_progress(progress=100, total=100, message="Complete")

        return result
