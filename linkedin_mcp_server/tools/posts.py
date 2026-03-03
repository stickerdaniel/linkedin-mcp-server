"""
LinkedIn posts and comments tools (my recent posts, post comments, unreplied comments).
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
from linkedin_mcp_server.scraping.posts import (
    find_unreplied_comments as scrape_find_unreplied_comments,
    get_my_recent_posts as scrape_get_my_recent_posts,
    get_post_comments as scrape_get_post_comments,
)

logger = logging.getLogger(__name__)


def register_posts_tools(mcp: FastMCP) -> None:
    """Register all posts-related tools with the MCP server."""

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get My Recent Posts",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def get_my_recent_posts(
        ctx: Context,
        limit: int = 20,
    ) -> dict[str, Any]:
        """
        List recent posts from the logged-in user's LinkedIn feed.

        Args:
            ctx: FastMCP context for progress reporting
            limit: Maximum number of posts to return (default 20).

        Returns:
            Dict with posts: list of {post_url, post_id, text_preview, created_at}.
            post_id/urn and created_at are best-effort.
        """
        try:
            await ensure_authenticated()
            logger.info("Scraping my recent posts (limit=%s)", limit)
            browser = await get_or_create_browser()
            await ctx.report_progress(
                progress=0, total=100, message="Fetching your recent posts"
            )
            posts = await scrape_get_my_recent_posts(browser.page, limit=limit)
            await ctx.report_progress(progress=100, total=100, message="Complete")
            return {"posts": posts}
        except Exception as e:
            return handle_tool_error(e, "get_my_recent_posts")

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Post Comments",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def get_post_comments(
        post_url: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """
        Get top-level comments for a LinkedIn post.

        Args:
            post_url: Post URL (e.g. https://www.linkedin.com/feed/update/urn:li:activity:...)
                or post URN/ID (e.g. urn:li:activity:123 or 123).
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with comments: list of {comment_id, author_name, author_url, text, created_at, comment_permalink}.
            created_at and comment_permalink are best-effort.
        """
        try:
            await ensure_authenticated()
            logger.info("Scraping post comments: %s", post_url[:80])
            browser = await get_or_create_browser()
            await ctx.report_progress(
                progress=0, total=100, message="Loading post comments"
            )
            comments = await scrape_get_post_comments(browser.page, post_url)
            await ctx.report_progress(progress=100, total=100, message="Complete")
            return {"comments": comments}
        except Exception as e:
            return handle_tool_error(e, "get_post_comments")

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Find Unreplied Comments",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def find_unreplied_comments(
        ctx: Context,
        since_days: int = 7,
        max_posts: int = 20,
    ) -> dict[str, Any]:
        """
        Find comments on your posts that you have not replied to.

        Uses notifications when possible; otherwise scans your recent posts.
        Results are ordered by most recent first (best-effort).

        Args:
            ctx: FastMCP context for progress reporting
            since_days: Consider activity from the last N days (used to limit scope).
            max_posts: Maximum number of posts to scan in fallback mode.

        Returns:
            Dict with unreplied_comments: list of items with comment_permalink, post_url,
            author_name, text/snippet for each comment pending a reply.
        """
        try:
            await ensure_authenticated()
            logger.info(
                "Finding unreplied comments (since_days=%s, max_posts=%s)",
                since_days,
                max_posts,
            )
            browser = await get_or_create_browser()
            await ctx.report_progress(
                progress=0, total=100, message="Finding unreplied comments"
            )
            unreplied = await scrape_find_unreplied_comments(
                browser.page,
                since_days=since_days,
                max_posts=max_posts,
            )
            await ctx.report_progress(progress=100, total=100, message="Complete")
            return {"unreplied_comments": unreplied}
        except Exception as e:
            return handle_tool_error(e, "find_unreplied_comments")
