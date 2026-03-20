"""
LinkedIn posts, comments, notifications, and feed tools.

Provides tools for scraping user posts, post comments, notifications,
person posts, feed posts, and unreplied comment detection.
"""

import logging
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.dependencies import Depends

from linkedin_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_extractor
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping import LinkedInExtractor
from linkedin_mcp_server.scraping.posts import (
    find_unreplied_comments as scrape_find_unreplied_comments,
    get_feed_posts as scrape_get_feed_posts,
    get_my_recent_posts as scrape_get_my_recent_posts,
    get_notifications as scrape_get_notifications,
    get_post_comments as scrape_get_post_comments,
    get_post_content as scrape_get_post_content,
    get_profile_recent_posts as scrape_get_profile_recent_posts,
)

logger = logging.getLogger(__name__)


def register_posts_tools(mcp: FastMCP) -> None:
    """Register all posts-related tools with the MCP server."""

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get My Recent Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"posts", "scraping"},
    )
    async def get_my_recent_posts(
        ctx: Context,
        limit: int = 20,
        extractor: LinkedInExtractor = Depends(get_extractor),
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
            logger.info("Scraping my recent posts (limit=%s)", limit)
            await ctx.report_progress(
                progress=0, total=100, message="Fetching your recent posts"
            )
            posts = await scrape_get_my_recent_posts(extractor._page, limit=limit)
            await ctx.report_progress(progress=100, total=100, message="Complete")
            return {"posts": posts}
        except Exception as e:
            raise_tool_error(e, "get_my_recent_posts")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Post Comments",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"posts", "scraping"},
    )
    async def get_post_comments(
        post_url: str,
        ctx: Context,
        extractor: LinkedInExtractor = Depends(get_extractor),
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
            logger.info("Scraping post comments: %s", post_url[:80])
            await ctx.report_progress(
                progress=0, total=100, message="Loading post comments"
            )
            comments = await scrape_get_post_comments(extractor._page, post_url)
            await ctx.report_progress(progress=100, total=100, message="Complete")
            return {"comments": comments}
        except Exception as e:
            raise_tool_error(e, "get_post_comments")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Post Content",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"posts", "scraping"},
    )
    async def get_post_content(
        post_url: str,
        ctx: Context,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Get the text content of a specific LinkedIn post.

        Args:
            post_url: Post URL (e.g. https://www.linkedin.com/feed/update/urn:li:activity:...)
                or post URN/ID (e.g. urn:li:activity:123 or 123).
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, sections: {"post_content": raw_text}, pages_visited,
            sections_requested, engagement, post_type, author.
        """
        try:
            logger.info("Scraping post content: %s", post_url[:80])
            await ctx.report_progress(
                progress=0, total=100, message="Loading post content"
            )
            result = await scrape_get_post_content(extractor._page, post_url)
            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result
        except Exception as e:
            raise_tool_error(e, "get_post_content")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Notifications",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"posts", "scraping"},
    )
    async def get_notifications(
        ctx: Context,
        limit: int = 20,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Get recent notifications from your LinkedIn notifications page.

        Scrapes the notifications page and returns structured items including
        comments, reactions, connection requests, mentions, endorsements,
        job alerts, and more.

        Args:
            ctx: FastMCP context for progress reporting
            limit: Maximum number of notifications to return (default 20).

        Returns:
            Dict with notifications: list of {text, link, type, created_at}.
            type is best-effort classification (comment, reaction, connection,
            mention, endorsement, job, post, birthday, work_anniversary, view, other).
            created_at is best-effort.
        """
        try:
            logger.info("Scraping notifications (limit=%s)", limit)
            await ctx.report_progress(
                progress=0, total=100, message="Fetching your notifications"
            )
            notifications = await scrape_get_notifications(
                extractor._page, limit=limit
            )
            await ctx.report_progress(progress=100, total=100, message="Complete")
            return {"notifications": notifications}
        except Exception as e:
            raise_tool_error(e, "get_notifications")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Person Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"posts", "person", "scraping"},
    )
    async def get_person_posts(
        linkedin_username: str,
        ctx: Context,
        limit: int = 20,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Get recent posts from a specific person's LinkedIn profile.

        Navigates to the person's profile page, scrolls to load activity,
        and extracts post links with text previews.

        Args:
            linkedin_username: LinkedIn username (e.g., "williamhgates", "satyanadella")
            ctx: FastMCP context for progress reporting
            limit: Maximum number of posts to return (default 20).

        Returns:
            Dict with posts: list of {post_url, post_id, text_preview, created_at}.
            post_id and created_at are best-effort.
        """
        try:
            logger.info(
                "Scraping person posts: %s (limit=%s)", linkedin_username, limit
            )
            await ctx.report_progress(
                progress=0,
                total=100,
                message=f"Fetching posts for {linkedin_username}",
            )
            posts = await scrape_get_profile_recent_posts(
                extractor._page, linkedin_username, limit=limit
            )
            await ctx.report_progress(progress=100, total=100, message="Complete")
            return {"posts": posts}
        except Exception as e:
            raise_tool_error(e, "get_person_posts")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Feed Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"posts", "scraping"},
    )
    async def get_feed_posts(
        ctx: Context,
        limit: int = 20,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Get posts from the logged-in user's LinkedIn home feed.

        Scrapes the main feed to capture algorithmically-surfaced posts,
        including posts from connections and suggested content.

        Args:
            ctx: FastMCP context for progress reporting
            limit: Maximum number of posts to return (default 20).

        Returns:
            Dict with posts: list of {post_url, post_id, text_preview,
            author_name, author_url, created_at}.
            All fields are best-effort.
        """
        try:
            logger.info("Scraping feed posts (limit=%s)", limit)
            await ctx.report_progress(
                progress=0, total=100, message="Fetching feed posts"
            )
            posts = await scrape_get_feed_posts(extractor._page, limit=limit)
            await ctx.report_progress(progress=100, total=100, message="Complete")
            return {"posts": posts}
        except Exception as e:
            raise_tool_error(e, "get_feed_posts")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Find Unreplied Comments",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"posts", "scraping"},
    )
    async def find_unreplied_comments(
        ctx: Context,
        since_days: int = 7,
        max_posts: int = 20,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Find comments on your posts that you have not replied to.

        Uses notifications when possible; otherwise scans your recent posts.
        Results are ordered by most recent first (best-effort).

        Note: when using the notifications fast path, since_days filtering is
        best-effort only (LinkedIn notifications lack reliable timestamps).
        The fallback path (post scanning) enforces since_days strictly.

        Args:
            ctx: FastMCP context for progress reporting
            since_days: Consider activity from the last N days (best-effort on notifications path).
            max_posts: Maximum number of posts to scan in fallback mode.

        Returns:
            Dict with unreplied_comments: list of items with comment_permalink, post_url,
            author_name, text/snippet for each comment pending a reply.
        """
        try:
            logger.info(
                "Finding unreplied comments (since_days=%s, max_posts=%s)",
                since_days,
                max_posts,
            )
            await ctx.report_progress(
                progress=0, total=100, message="Finding unreplied comments"
            )
            unreplied = await scrape_find_unreplied_comments(
                extractor._page,
                since_days=since_days,
                max_posts=max_posts,
            )
            await ctx.report_progress(progress=100, total=100, message="Complete")
            return {"unreplied_comments": unreplied}
        except Exception as e:
            raise_tool_error(e, "find_unreplied_comments")
