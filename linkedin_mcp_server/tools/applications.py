"""
LinkedIn job application tools.

Provides job bookmarking (save_job), applied-job history listing
(list_applied), and Easy Apply inspection / submission with explicit
confirm_send gating (easy_apply_inspect / easy_apply_submit).

All write actions follow the existing send_message pattern:
- read-only actions are annotated with `readOnlyHint=True`
- write actions require an explicit `confirm_send=True` argument and
  are annotated with `destructiveHint=True`. When confirm_send is
  False, the tool returns a structured preview without taking the
  action.

The Easy Apply submit tool only supports zero-question Easy Apply
postings: it inspects the dialog, submits when there are no
questions, and otherwise returns a `manual_review` status with the
detected questions for the human to fill out interactively. This
keeps automated apply runs honest about job-specific questionnaires
and avoids submitting inaccurate / template answers.
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


async def save_job(
    job_id: str,
    confirm_send: bool,
    ctx: Context,
    extractor: Any | None = None,
) -> dict[str, Any]:
    """Bookmark a job posting on LinkedIn (Save Job)."""
    try:
        extractor = extractor or await get_ready_extractor(ctx, tool_name="save_job")
        logger.info("Saving job: %s (confirm_send=%s)", job_id, confirm_send)
        await ctx.report_progress(progress=0, total=100, message="Loading job posting")
        result = await extractor.save_job(job_id, confirm_send=confirm_send)
        await ctx.report_progress(progress=100, total=100, message="Complete")
        return result
    except AuthenticationError as e:
        try:
            await handle_auth_error(e, ctx)
        except Exception as relogin_exc:
            raise_tool_error(relogin_exc, "save_job")
    except Exception as e:
        raise_tool_error(e, "save_job")  # NoReturn


async def list_applied(
    ctx: Context,
    max_pages: Annotated[int, Field(ge=1, le=10)] = 3,
    extractor: Any | None = None,
) -> dict[str, Any]:
    """List jobs the authenticated LinkedIn user has applied to."""
    try:
        extractor = extractor or await get_ready_extractor(ctx, tool_name="list_applied")
        logger.info("Listing applied jobs (max_pages=%d)", max_pages)
        await ctx.report_progress(
            progress=0, total=100, message="Loading applied-jobs history"
        )
        result = await extractor.list_applied_jobs(max_pages=max_pages)
        await ctx.report_progress(progress=100, total=100, message="Complete")
        return result
    except AuthenticationError as e:
        try:
            await handle_auth_error(e, ctx)
        except Exception as relogin_exc:
            raise_tool_error(relogin_exc, "list_applied")
    except Exception as e:
        raise_tool_error(e, "list_applied")  # NoReturn


async def easy_apply_inspect(
    job_id: str,
    ctx: Context,
    extractor: Any | None = None,
) -> dict[str, Any]:
    """Inspect the LinkedIn Easy Apply flow without submitting."""
    try:
        extractor = extractor or await get_ready_extractor(
            ctx, tool_name="easy_apply_inspect"
        )
        logger.info("Inspecting Easy Apply: job_id=%s", job_id)
        await ctx.report_progress(
            progress=0, total=100, message="Loading Easy Apply dialog"
        )
        result = await extractor.easy_apply_inspect(job_id)
        await ctx.report_progress(progress=100, total=100, message="Complete")
        return result
    except AuthenticationError as e:
        try:
            await handle_auth_error(e, ctx)
        except Exception as relogin_exc:
            raise_tool_error(relogin_exc, "easy_apply_inspect")
    except Exception as e:
        raise_tool_error(e, "easy_apply_inspect")  # NoReturn


async def easy_apply_submit(
    job_id: str,
    confirm_send: bool,
    ctx: Context,
    extractor: Any | None = None,
) -> dict[str, Any]:
    """Submit a zero-question, single-step LinkedIn Easy Apply application."""
    try:
        extractor = extractor or await get_ready_extractor(
            ctx, tool_name="easy_apply_submit"
        )
        logger.info(
            "Submitting Easy Apply: job_id=%s confirm_send=%s", job_id, confirm_send
        )
        await ctx.report_progress(progress=0, total=100, message="Submitting Easy Apply")
        result = await extractor.easy_apply_submit(job_id, confirm_send=confirm_send)
        await ctx.report_progress(progress=100, total=100, message="Complete")
        return result
    except AuthenticationError as e:
        try:
            await handle_auth_error(e, ctx)
        except Exception as relogin_exc:
            raise_tool_error(relogin_exc, "easy_apply_submit")
    except Exception as e:
        raise_tool_error(e, "easy_apply_submit")  # NoReturn


def register_application_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register all job-application tools with the MCP server."""
    mcp.tool(
        timeout=tool_timeout,
        title="Save Job",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"job", "actions"},
        exclude_args=["extractor"],
    )(save_job)
    mcp.tool(
        timeout=tool_timeout,
        title="List Applied Jobs",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job", "scraping"},
        exclude_args=["extractor"],
    )(list_applied)
    mcp.tool(
        timeout=tool_timeout,
        title="Inspect Easy Apply",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job", "scraping"},
        exclude_args=["extractor"],
    )(easy_apply_inspect)
    mcp.tool(
        timeout=tool_timeout,
        title="Submit Easy Apply",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"job", "actions"},
        exclude_args=["extractor"],
    )(easy_apply_submit)
