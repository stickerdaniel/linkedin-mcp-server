"""
LinkedIn posting tools.

Provides write-action tools for posting comments on LinkedIn posts.
Mirrors the send_message pattern in messaging.py: destructiveHint
annotation, confirm_send-style gating, JS-based DOM interaction to
work around Patchright actionability checks.
"""

import logging
from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
)
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error

logger = logging.getLogger(__name__)


def register_posting_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register all posting-related tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Post Comment",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"posting", "actions"},
        exclude_args=["extractor"],
    )
    async def post_comment(
        post_url: str,
        comment_text: str,
        confirm_send: bool,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Post a comment on a LinkedIn post.

        Navigates to the target post URL, opens the comment composer,
        types the comment, and submits. Write operation when confirm_send
        is True; dry-run (confirms composer is reachable) when False.

        Args:
            post_url: Full LinkedIn activity URL of the target post
                (e.g. https://www.linkedin.com/feed/update/urn:li:activity:NNN/
                or https://www.linkedin.com/posts/<slug>-NNN-XXXX).
            comment_text: The comment body to post.
            confirm_send: Must be True to actually submit.
            ctx: FastMCP context for progress reporting.

        Returns:
            Dict with url, status, message, composer_resolved, and posted.
            Status values: "posted" (success), "confirmation_required"
            (dry run, composer reached), "composer_unavailable",
            "post_not_found", "submit_unavailable", "post_unconfirmed".
        """
        if not post_url:
            raise_tool_error(
                LinkedInScraperException("post_url is required"),
                "post_comment",
            )
        if not comment_text or not comment_text.strip():
            raise_tool_error(
                LinkedInScraperException("comment_text is required and non-empty"),
                "post_comment",
            )

        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="post_comment"
            )
            logger.info(
                "Posting comment on %s (confirm_send=%s, length=%d)",
                post_url,
                confirm_send,
                len(comment_text),
            )

            await ctx.report_progress(
                progress=0, total=100, message="Loading post page"
            )

            result = await extractor.post_comment(
                post_url=post_url,
                comment_text=comment_text,
                confirm_send=confirm_send,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "post_comment")
        except Exception as e:
            raise_tool_error(e, "post_comment")  # NoReturn
