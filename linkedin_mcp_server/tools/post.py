"""
LinkedIn post/content tools: global post search and post scheduling.

search_posts performs LinkedIn's global content search (the "Posts" results
tab) using innerText extraction, so informal "we're hiring" / "Buscamos ..."
posts can be found before a formal job listing is published. Mirrors
search_people: build a /search/results/content/ URL, scroll to load results,
and return the raw innerText for the LLM to parse, plus post-permalink
references.

schedule_post drives LinkedIn's native Schedule-for-later flow in the share
composer, so the scheduled post lives on LinkedIn's side and publishes
without this server running.
"""

import datetime
import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping.extractor import FilterValidationError

logger = logging.getLogger(__name__)


def register_post_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register post/content-search tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post", "search"},
        exclude_args=["extractor"],
    )
    async def search_posts(
        keywords: str,
        ctx: Context,
        date_posted: str | None = None,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 3,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Search LinkedIn posts/content globally by keyword (the "Posts" tab).

        Use this to catch informal hiring posts ("we're hiring", "Buscamos
        ...", "estamos contratando", "join our team") that often appear before
        a formal job listing exists. This is global content search, distinct
        from get_feed (your own home feed) and get_company_posts (one
        company's page).

        Args:
            keywords: Search keywords (e.g., "Buscamos Unity", "AI automation hiring")
            ctx: FastMCP context for progress reporting
            date_posted: Optional recency filter. One of "past-24h",
                "past-week", "past-month"; the "past_24_hours" / "past_week" /
                "past_month" spellings used by search_jobs are accepted too.
                Omit for any time.
            max_pages: Scroll depth as result "pages" of ~5 scrolls each
                (1-10, default 3). Content search is an infinite scroll, so
                this caps how far the page is scrolled rather than fetching
                discrete pages.

        Returns:
            Dict with url, sections (search_results -> raw text), and optional
            references (post authors, companies, linked jobs) and
            section_errors. The results page carries no per-post permalinks,
            so reach a post through its author. The LLM should parse the raw
            text to extract each post's author, headline/role, company, body,
            posted date, and reaction/comment counts.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="search_posts"
            )
            logger.info(
                "Searching posts: keywords='%s', date_posted='%s', max_pages=%d",
                keywords,
                date_posted,
                max_pages,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Starting post search"
            )

            try:
                result = await extractor.search_posts(
                    keywords,
                    date_posted=date_posted,
                    max_pages=max_pages,
                )
            except FilterValidationError as e:
                # Validation messages carry actionable detail; surface them as
                # ToolError so mask_error_details doesn't reduce them to a
                # generic "Error calling tool 'search_posts'".
                raise ToolError(str(e)) from e

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except ToolError:
            # Already a properly formatted client-facing error; do not log it
            # as "Unexpected error" via raise_tool_error.
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "search_posts")
        except Exception as e:
            raise_tool_error(e, "search_posts")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Schedule Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"post", "actions"},
        exclude_args=["extractor"],
    )
    async def schedule_post(
        text: str,
        schedule_date: str,
        schedule_time: str,
        confirm_schedule: bool,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Schedule a LinkedIn feed post via LinkedIn's native Schedule-for-later.

        The post is stored by LinkedIn itself and publishes at the scheduled
        moment whether or not this server is running. LinkedIn interprets the
        date and time in the profile's own timezone and accepts roughly the
        next three months. This is a write operation when confirm_schedule is
        True; with False it performs the full flow as a dry run — filling the
        schedule and the text — then discards the draft.

        Args:
            text: The post body text.
            schedule_date: Date to publish, as YYYY-MM-DD (e.g. "2026-08-15").
            schedule_time: Time to publish, as 24-hour HH:MM (e.g. "17:30").
            confirm_schedule: Must be True to actually schedule the post.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status, message, scheduled (bool), and — once the
            flow reaches the composer — scheduled_for plus composer_text, the
            composer dialog's raw text, which carries LinkedIn's own rendering
            of the scheduled moment for verification.
        """
        try:
            try:
                when = datetime.datetime.strptime(
                    f"{schedule_date} {schedule_time}", "%Y-%m-%d %H:%M"
                )
            except ValueError as e:
                raise ToolError(
                    f"Invalid schedule_date/schedule_time: {e}. "
                    "Use YYYY-MM-DD and 24-hour HH:MM."
                ) from e
            # LinkedIn interprets the moment in the *profile's* timezone,
            # which this server cannot know, so the server clock only bounds
            # the check: a wall time is already past in every timezone once it
            # trails UTC by more than the westernmost offset (UTC-12). Only
            # that much is rejected here; anything nearer is LinkedIn's call,
            # and the composer flow reports schedule_rejected when LinkedIn
            # refuses the values.
            now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            if when <= now_utc - datetime.timedelta(hours=12):
                raise ToolError(
                    f"The scheduled moment {when:%Y-%m-%d %H:%M} is already "
                    "past in every timezone. Pick a later time."
                )

            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="schedule_post"
            )
            logger.info(
                "Scheduling post for %s %s (confirm_schedule=%s)",
                schedule_date,
                schedule_time,
                confirm_schedule,
            )

            await ctx.report_progress(progress=0, total=100, message="Scheduling post")

            result = await extractor.schedule_post(
                text,
                year=when.year,
                month=when.month,
                day=when.day,
                hour=when.hour,
                minute=when.minute,
                confirm_schedule=confirm_schedule,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except ToolError:
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "schedule_post")
        except Exception as e:
            raise_tool_error(e, "schedule_post")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Scheduled Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_scheduled_posts(
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List the authenticated user's scheduled posts.

        Reads LinkedIn's "Scheduled posts" view (the share composer's clock
        button -> "View all scheduled posts"). Read-only in effect: the
        composer is opened to reach the list, never typed into, and dismissed
        empty. Entries appear in the returned text in LinkedIn's own order
        (soonest first), each as a "Posting <day>, <date> at <time>" line
        followed by a body preview.

        Args:
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url and sections (scheduled_posts -> raw text). When the
            list cannot be reached, a dict with url, status and message
            instead. The LLM should parse the raw text to extract each entry's
            scheduled moment and body preview.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_scheduled_posts"
            )
            logger.info("Listing scheduled posts")

            await ctx.report_progress(
                progress=0, total=100, message="Opening scheduled posts"
            )

            result = await extractor.get_scheduled_posts()

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_scheduled_posts")
        except Exception as e:
            raise_tool_error(e, "get_scheduled_posts")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Edit Scheduled Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"post", "actions"},
        exclude_args=["extractor"],
    )
    async def edit_scheduled_post(
        match_text: str,
        confirm_edit: bool,
        ctx: Context,
        new_text: str | None = None,
        new_date: str | None = None,
        new_time: str | None = None,
        occurrence: Annotated[int, Field(ge=0)] | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Edit a scheduled post's body text and/or scheduled moment in place.

        The post to edit is identified by match_text, a snippet of its current
        body, resolved against the live scheduled-posts list at action time
        (get_scheduled_posts shows the entries to pick a snippet from). With
        occurrence omitted the snippet must match exactly one entry — several
        matches are an error, never a silent first pick; pass occurrence only
        after seeing the list, to choose among known duplicates in list
        order. This is a write
        operation when confirm_edit is True; with False it applies the changes
        in the composer as a dry run, returns LinkedIn's rendering of the
        result, and abandons them unsaved.

        Args:
            match_text: Snippet of the scheduled post's current body text.
            confirm_edit: Must be True to actually save the changes.
            ctx: FastMCP context for progress reporting
            new_text: Replacement body text. Omit to keep the current body.
            new_date: New date as YYYY-MM-DD. Omit to keep the current date.
            new_time: New 24-hour time as HH:MM. Omit to keep the current time.
            occurrence: 0-based selector among entries matching match_text.
                Omit to require a unique match.

        Returns:
            Dict with url, status, message, done (bool), entry_preview, and —
            once the composer is reached — composer_text carrying LinkedIn's
            own rendering of the (proposed) scheduled moment and body.
        """
        try:
            if not match_text.strip():
                raise ToolError(
                    "match_text is empty; pass a snippet of the post's body text."
                )
            if new_text is None and new_date is None and new_time is None:
                raise ToolError(
                    "Nothing to change: pass new_text, new_date and/or new_time."
                )
            parsed_date: datetime.date | None = None
            parsed_time: datetime.time | None = None
            try:
                if new_date is not None:
                    parsed_date = datetime.datetime.strptime(
                        new_date, "%Y-%m-%d"
                    ).date()
                if new_time is not None:
                    parsed_time = datetime.datetime.strptime(new_time, "%H:%M").time()
            except ValueError as e:
                raise ToolError(
                    f"Invalid new_date/new_time: {e}. Use YYYY-MM-DD and 24-hour HH:MM."
                ) from e
            # Same bound as schedule_post: LinkedIn judges the moment in the
            # profile's timezone, so only reject what is past in *every*
            # timezone (more than UTC-12 behind). With only a date given the
            # kept time is unknown, so the date is checked at its most
            # generous end-of-day reading; with only a time given no server
            # clock statement is possible at all and LinkedIn decides.
            now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            checked = (
                datetime.datetime.combine(
                    parsed_date, parsed_time or datetime.time(23, 59)
                )
                if parsed_date is not None
                else None
            )
            if checked is not None and checked <= now_utc - datetime.timedelta(
                hours=12
            ):
                raise ToolError(
                    f"The new moment {checked:%Y-%m-%d %H:%M} is already past "
                    "in every timezone. Pick a later date."
                )

            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="edit_scheduled_post"
            )
            logger.info(
                "Editing scheduled post matching %r (confirm_edit=%s)",
                match_text[:60],
                confirm_edit,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Editing scheduled post"
            )

            result = await extractor.edit_scheduled_post(
                match_text,
                new_text=new_text,
                new_year=parsed_date.year if parsed_date else None,
                new_month=parsed_date.month if parsed_date else None,
                new_day=parsed_date.day if parsed_date else None,
                new_hour=parsed_time.hour if parsed_time else None,
                new_minute=parsed_time.minute if parsed_time else None,
                occurrence=occurrence,
                confirm_edit=confirm_edit,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except ToolError:
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "edit_scheduled_post")
        except Exception as e:
            raise_tool_error(e, "edit_scheduled_post")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Delete Scheduled Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"post", "actions"},
        exclude_args=["extractor"],
    )
    async def delete_scheduled_post(
        match_text: str,
        confirm_delete: bool,
        ctx: Context,
        occurrence: Annotated[int, Field(ge=0)] = 0,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Delete a scheduled post. LinkedIn cannot recover a deleted one.

        The post is identified by match_text, a snippet of its body, resolved
        against the live scheduled-posts list at action time (same selection
        as edit_scheduled_post; use get_scheduled_posts to pick a snippet).
        With confirm_delete False the tool only resolves the entry and reports
        what would be deleted — LinkedIn's confirmation dialog is not opened.

        Args:
            match_text: Snippet of the scheduled post's body text.
            confirm_delete: Must be True to actually delete the post.
            ctx: FastMCP context for progress reporting
            occurrence: 0-based selector among entries matching match_text.

        Returns:
            Dict with url, status, message, done (bool), and entry_preview
            (the matched entry's scheduled moment and body head). Deletion is
            verified by the entry's absence from the list afterwards.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="delete_scheduled_post"
            )
            logger.info(
                "Deleting scheduled post matching %r (confirm_delete=%s)",
                match_text[:60],
                confirm_delete,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Deleting scheduled post"
            )

            result = await extractor.delete_scheduled_post(
                match_text,
                occurrence=occurrence,
                confirm_delete=confirm_delete,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "delete_scheduled_post")
        except Exception as e:
            raise_tool_error(e, "delete_scheduled_post")  # NoReturn
