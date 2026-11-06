"""
Read published posts, owner analytics and scheduled posts, and search content.

Performs LinkedIn's global content search (the "Posts" results tab) using
innerText extraction, so informal "we're hiring" / "Buscamos ..." posts can
be found before a formal job listing is published. Mirrors search_people:
build a /search/results/content/ URL, scroll to load results, and return the
raw innerText for the LLM to parse, plus post-permalink references.
"""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_ready_extractor, tool_errors
from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode
from linkedin_mcp_server.linkedin.contracts import FilterValidationError
from linkedin_mcp_server.linkedin.identifiers import normalize_post_url
from linkedin_mcp_server.linkedin.post_management import (
    PostQueueReadRefused,
    post_analytics_url,
)

logger = logging.getLogger(__name__)


def register_post_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register post/content-search tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Post",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post"},
    )
    @tool_errors
    async def get_post(
        post_url: str,
        ctx: Context,
        max_scrolls: Annotated[int, Field(ge=0, le=50)] = 3,
    ) -> dict[str, Any]:
        """Read one post's text, visible comments and engagement counters.

        post_url accepts a LinkedIn /feed/update/ or /posts/ permalink, including
        a relative reference returned by this server, or a complete activity,
        share or ugcPost URN. max_scrolls bounds page scrolling (0–50).

        Returns {url, sections: {post: raw_text}} with optional references and
        section_errors. Parse reactions, comments and repost counts only when
        the page displays them; a missing count is not zero. Collapsed replies,
        reaction lists and truncated text are not expanded. A bounded read is
        not a complete comment export. No likes, replies or reposts are sent.
        """
        post_url = normalize_post_url(post_url)
        extractor = await get_ready_extractor(ctx, tool_name="get_post")
        await ctx.report_progress(progress=0, total=100, message="Reading the post")
        result = await extractor.get_post(post_url, max_scrolls=max_scrolls)
        await ctx.report_progress(progress=100, total=100, message="Complete")
        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Get My Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post", "person"},
    )
    @tool_errors
    async def get_my_posts(
        ctx: Context,
        max_scrolls: Annotated[int, Field(ge=0, le=50)] = 5,
    ) -> dict[str, Any]:
        """Read the authenticated user's published posts and visible counters.

        Returns {url, sections: {main_profile: raw_text, posts: raw_text}} through
        the own-profile reader, with optional references and section_errors.
        The posts section is LinkedIn's rendered activity page. max_scrolls (0–50) limits
        loading; the result is a sample rather than the complete post history.
        Open individual permalinks with get_post for their visible discussion,
        or get_post_analytics for any owner-only metrics LinkedIn makes available.
        """
        extractor = await get_ready_extractor(ctx, tool_name="get_my_posts")
        await ctx.report_progress(progress=0, total=100, message="Reading your posts")
        result = await extractor.get_my_posts(max_scrolls=max_scrolls)
        await ctx.report_progress(progress=100, total=100, message="Complete")
        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Post Analytics",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post", "analytics"},
    )
    @tool_errors
    async def get_post_analytics(post_url: str, ctx: Context) -> dict[str, Any]:
        """Read the analytics page LinkedIn exposes for a post you own.

        Accepts an activity permalink or complete urn:li:activity:<id>. Returns raw
        analytics text, which may include impressions, reach and engagement.
        Availability depends on the account, post and current LinkedIn page.
        Missing or restricted metrics are not zero. This tool provides no
        universal ranking score and does not compare posts automatically.
        """
        post_url = normalize_post_url(post_url)
        post_analytics_url(post_url)
        extractor = await get_ready_extractor(ctx, tool_name="get_post_analytics")
        await ctx.report_progress(
            progress=0, total=100, message="Reading post analytics"
        )
        result = await extractor.get_post_analytics(post_url)
        await ctx.report_progress(progress=100, total=100, message="Complete")
        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Scheduled Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post"},
    )
    @tool_errors
    async def get_scheduled_posts(
        ctx: Context,
        max_scrolls: Annotated[int, Field(ge=0, le=20)] = 3,
    ) -> dict[str, Any]:
        """Read the authenticated user's scheduled posts without changing them.

        max_scrolls (0–20) bounds loading of the rendered schedule. The response
        retains LinkedIn's displayed post text, dates and times; it does not
        assume a timezone the page does not state. An unavailable schedule is
        reported as an error, not as evidence that no posts are scheduled.
        The current workflow supports the English composer only.
        This tool does not edit, delete, reschedule or publish a scheduled post.
        """
        extractor = await get_ready_extractor(ctx, tool_name="get_scheduled_posts")
        await ctx.report_progress(
            progress=0, total=100, message="Reading scheduled posts"
        )
        try:
            result = await extractor.get_scheduled_posts(max_scrolls=max_scrolls)
        except PostQueueReadRefused as exc:
            code = (
                ErrorCode.BROWSER_BUSY
                if exc.operator_work
                else ErrorCode.PAGE_UNREADABLE
            )
            raise CodedToolError(str(exc), code) from exc
        await ctx.report_progress(progress=100, total=100, message="Complete")
        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post", "search"},
    )
    @tool_errors
    async def search_posts(
        keywords: str,
        ctx: Context,
        date_posted: str | None = None,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 3,
        content_type: str | None = None,
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
            content_type: Optional "Content type" filter. One of
                "documents" (PDFs and slide decks people upload), "images",
                "videos", "live_videos", "job_posts". Omit for all posts.
            max_pages: Scroll depth as result "pages" of ~5 scrolls each
                (1-10, default 3). Content search is an infinite scroll, so
                this caps how far the page is scrolled rather than fetching
                discrete pages.

        Returns:
            Dict with url, sections (search_results -> raw text), and optional
            references (post authors, companies, linked jobs, and kind
            "feed_post" permalinks read from the page's payload responses —
            /feed/update/<urn>/ or /posts/<slug>, both valid permalinks) and
            section_errors. The DOM carries no per-post permalink anchors;
            captured permalinks are not aligned to result order. The LLM
            should parse the raw text to extract each post's author,
            headline/role, company, body, posted date, and reaction/comment
            counts.
        """
        extractor = await get_ready_extractor(ctx, tool_name="search_posts")
        logger.info(
            "Searching posts: keywords='%s', date_posted='%s', content_type='%s', "
            "max_pages=%d",
            keywords,
            date_posted,
            content_type,
            max_pages,
        )

        await ctx.report_progress(progress=0, total=100, message="Starting post search")

        try:
            result = await extractor.search_posts(
                keywords,
                date_posted=date_posted,
                content_type=content_type,
                max_pages=max_pages,
            )
        except FilterValidationError as e:
            # Validation messages carry actionable detail; surface them as
            # ToolError so mask_error_details doesn't reduce them to a
            # generic "Error calling tool 'search_posts'".
            raise CodedToolError(str(e), ErrorCode.INVALID_ARGUMENT) from e

        await ctx.report_progress(progress=100, total=100, message="Complete")

        return result
