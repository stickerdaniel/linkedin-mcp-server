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


def register_application_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register all job-application tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Save Job",
        annotations={"destructiveHint": False, "openWorldHint": True},
        tags={"job", "actions"},
        exclude_args=["extractor"],
    )
    async def save_job(
        job_id: str,
        confirm_send: bool,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Bookmark a job posting on LinkedIn (Save Job).

        This creates a saved-job entry on LinkedIn so the human can
        revisit it later. It does NOT apply to the job. Set
        confirm_send=True to actually click Save; pass False to
        preview the action.

        Args:
            job_id: LinkedIn job ID (e.g. "4252026496").
            confirm_send: Must be True to click Save. False returns a
                preview without changing state.
            ctx: FastMCP context for progress reporting.

        Returns:
            Dict with url, status, message, and saved.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="save_job"
            )
            logger.info(
                "Saving job: %s (confirm_send=%s)", job_id, confirm_send
            )

            await ctx.report_progress(
                progress=0, total=100, message="Loading job posting"
            )

            result = await extractor.save_job(
                job_id, confirm_send=confirm_send
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "save_job")
        except Exception as e:
            raise_tool_error(e, "save_job")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="List Applied Jobs",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job", "scraping"},
        exclude_args=["extractor"],
    )
    async def list_applied(
        ctx: Context,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 3,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List jobs the authenticated LinkedIn user has applied to.

        Reads `/my-items/saved-jobs/?cardType=APPLIED` (LinkedIn's
        canonical page for the applied-jobs history) and returns the
        innerText plus the extracted job IDs so the LLM can cross-
        reference each ID against the local applications.jsonl ledger.

        Args:
            ctx: FastMCP context for progress reporting.
            max_pages: Maximum pages of applied-jobs history to load
                (1-10, default 3). LinkedIn paginates 25 entries per
                page.

        Returns:
            Dict with url, sections (applied_jobs -> raw text), and
            job_ids (list of numeric job ID strings the user has
            already applied to).
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="list_applied"
            )
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

    @mcp.tool(
        timeout=tool_timeout,
        title="Inspect Easy Apply",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job", "scraping"},
        exclude_args=["extractor"],
    )
    async def easy_apply_inspect(
        job_id: str,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Inspect the LinkedIn Easy Apply flow for a job posting WITHOUT
        submitting an application.

        Opens the Easy Apply dialog, captures the first step's
        innerText, and reports any text/select/upload questions. This
        is the read-only counterpart to easy_apply_submit and is
        always safe to call.

        Args:
            job_id: LinkedIn job ID.
            ctx: FastMCP context for progress reporting.

        Returns:
            Dict with url, status (one of: ok, no_easy_apply,
            already_applied, multi_step), step_count, and
            questions (list of dicts with label and field_type).
        """
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

    @mcp.tool(
        timeout=tool_timeout,
        title="Submit Easy Apply",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"job", "actions"},
        exclude_args=["extractor"],
    )
    async def easy_apply_submit(
        job_id: str,
        confirm_send: bool,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Submit a LinkedIn Easy Apply application.

        Only supports zero-question, single-step Easy Apply postings.
        For postings that contain text/select/upload questions or
        multi-step flows the tool returns status="manual_review" with
        the detected questions and does NOT submit. The human must
        either fill the form interactively in their own browser
        session or copy the questions into a structured answer file
        for a future iteration of this tool.

        Args:
            job_id: LinkedIn job ID.
            confirm_send: Must be True to actually click Submit.
                False returns a preview without submitting.
            ctx: FastMCP context for progress reporting.

        Returns:
            Dict with url, status (one of: confirmation_required,
            submitted, manual_review, no_easy_apply, already_applied,
            error), questions (when manual_review), and message.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="easy_apply_submit"
            )
            logger.info(
                "Submitting Easy Apply: job_id=%s confirm_send=%s",
                job_id,
                confirm_send,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Submitting Easy Apply"
            )

            result = await extractor.easy_apply_submit(
                job_id, confirm_send=confirm_send
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "easy_apply_submit")
        except Exception as e:
            raise_tool_error(e, "easy_apply_submit")  # NoReturn
