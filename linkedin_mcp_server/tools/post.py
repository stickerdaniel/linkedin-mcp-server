"""LinkedIn feed post engagement tools."""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error

logger = logging.getLogger(__name__)


def register_post_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register feed-post engagement tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Comment on Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"feed", "actions"},
        exclude_args=["extractor"],
    )
    async def comment_on_post(
        post_url: str,
        comment_text: Annotated[str, Field(min_length=1, max_length=1500)],
        confirm_post: bool,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Post a top-level comment on a LinkedIn feed post.

        This is a write operation when confirm_post is True. With
        confirm_post=False the composer is located but no comment is
        submitted, returning status="confirmation_required" — useful for
        dry-run pre-flight checks before issuing the real call.

        Args:
            post_url: A feed post URL or activity reference. Accepts the
                full /feed/update/urn:li:activity:{id}/ URL, the
                /posts/<slug>-activity-{id}-<sig>/ permalink, a bare
                urn:li:activity:{id} URN, or the bare numeric id.
            comment_text: The comment body (1-1500 characters, plain text).
            confirm_post: Must be True to actually submit the comment.
            ctx: FastMCP context for progress reporting.

        Returns:
            Dict with url, status, message, posted, comment_visible.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="comment_on_post"
            )
            logger.info(
                "Posting comment on %s (confirm_post=%s, length=%d)",
                post_url,
                confirm_post,
                len(comment_text),
            )

            await ctx.report_progress(progress=0, total=100, message="Posting comment")

            result = await extractor.post_comment(
                post_url,
                comment_text,
                confirm_post=confirm_post,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "comment_on_post")
        except Exception as e:
            raise_tool_error(e, "comment_on_post")  # NoReturn
