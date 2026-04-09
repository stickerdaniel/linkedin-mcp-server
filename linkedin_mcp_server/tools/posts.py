"""
LinkedIn post and comment tools (official REST API).

create_post / delete_post   → /v2/ugcPosts        (UGC Posts API)
create_comment / reply / delete → /v2/socialActions  (Social Actions API)

Both endpoints require w_member_social scope (Share on LinkedIn product, instant approval).
Run `--linkedin-auth` once to obtain and store the OAuth token.
"""

import logging
from typing import Annotated, Any
from urllib.parse import quote

from fastmcp import FastMCP
from pydantic import Field

from linkedin_mcp_server.api.client import get_api_client
from linkedin_mcp_server.constants import TOOL_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


def _encode_urn(urn: str) -> str:
    """URL-encode a LinkedIn URN for use in a path segment."""
    return quote(urn, safe="")


def register_post_tools(mcp: FastMCP) -> None:
    """Register LinkedIn post and comment tools with the MCP server."""

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Create Post",
        annotations={"destructiveHint": False, "openWorldHint": True},
        tags={"posts", "api"},
    )
    def create_post(
        text: Annotated[str, Field(description="Post text / commentary")],
        url: Annotated[
            str | None,
            Field(description="Optional URL to share as a link post"),
        ] = None,
        visibility: Annotated[
            str,
            Field(
                description="Audience: PUBLIC or CONNECTIONS",
                pattern="^(PUBLIC|CONNECTIONS)$",
            ),
        ] = "PUBLIC",
    ) -> dict[str, Any]:
        """
        Publish a text post (or link share) to the authenticated member's LinkedIn feed.

        Uses the UGC Posts API — requires w_member_social scope
        (Share on LinkedIn product, instant approval).

        Returns the post URN (e.g. urn:li:ugcPost:123456) which can be used
        with create_comment or delete_post.
        """
        client = get_api_client()

        if url:
            share_content: dict[str, Any] = {
                "shareCommentary": {"text": text},
                "shareMediaCategory": "ARTICLE",
                "media": [{"status": "READY", "originalUrl": url}],
            }
        else:
            share_content = {
                "shareCommentary": {"text": text},
                "shareMediaCategory": "NONE",
            }

        body = {
            "author": client.person_id(),
            "lifecycleState": "PUBLISHED",
            "specificContent": {"com.linkedin.ugc.ShareContent": share_content},
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": visibility},
        }
        resp = client.post("/v2/ugcPosts", body)
        post_urn = resp.headers.get("x-restli-id", "")
        logger.info("Post created: %s", post_urn)
        return {"post_urn": post_urn, "status": "published"}

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Delete Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"posts", "api"},
    )
    def delete_post(
        post_urn: Annotated[
            str,
            Field(description="URN of the post to delete, e.g. urn:li:ugcPost:123456"),
        ],
    ) -> dict[str, Any]:
        """
        Delete a LinkedIn post by its URN.

        Only posts created by the authenticated member can be deleted.
        """
        client = get_api_client()
        client.delete(f"/v2/ugcPosts/{_encode_urn(post_urn)}")
        logger.info("Post deleted: %s", post_urn)
        return {"post_urn": post_urn, "status": "deleted"}

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Create Comment",
        annotations={"destructiveHint": False, "openWorldHint": True},
        tags={"posts", "api"},
    )
    def create_comment(
        post_urn: Annotated[
            str,
            Field(
                description="URN of the post to comment on, e.g. urn:li:ugcPost:123456"
            ),
        ],
        text: Annotated[str, Field(description="Comment text")],
    ) -> dict[str, Any]:
        """
        Add a comment to a LinkedIn post.

        Returns the comment URN which can be passed to reply_to_comment or delete_comment.
        """
        client = get_api_client()
        body = {
            "actor": client.person_id(),
            "message": {"text": text},
        }
        resp = client.post(f"/v2/socialActions/{_encode_urn(post_urn)}/comments", body)
        comment_urn = resp.json().get("$URN", resp.headers.get("x-restli-id", ""))
        logger.info("Comment created on %s: %s", post_urn, comment_urn)
        return {"comment_urn": comment_urn, "post_urn": post_urn, "status": "created"}

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Reply to Comment",
        annotations={"destructiveHint": False, "openWorldHint": True},
        tags={"posts", "api"},
    )
    def reply_to_comment(
        comment_urn: Annotated[
            str,
            Field(
                description=(
                    "URN of the comment to reply to, "
                    "e.g. urn:li:comment:(urn:li:activity:123,456)"
                )
            ),
        ],
        text: Annotated[str, Field(description="Reply text")],
    ) -> dict[str, Any]:
        """
        Reply to an existing LinkedIn comment (nested comment).

        Returns the URN of the new reply comment.
        """
        client = get_api_client()
        body = {
            "actor": client.person_id(),
            "message": {"text": text},
        }
        resp = client.post(
            f"/v2/socialActions/{_encode_urn(comment_urn)}/comments", body
        )
        reply_urn = resp.json().get("$URN", resp.headers.get("x-restli-id", ""))
        logger.info("Reply created on %s: %s", comment_urn, reply_urn)
        return {
            "reply_urn": reply_urn,
            "parent_comment_urn": comment_urn,
            "status": "created",
        }

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Delete Comment",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"posts", "api"},
    )
    def delete_comment(
        post_urn: Annotated[
            str,
            Field(description="URN of the post the comment belongs to"),
        ],
        comment_id: Annotated[
            str,
            Field(
                description=(
                    "Numeric comment ID — the second element in the comment URN. "
                    "For urn:li:comment:(urn:li:activity:123,456) use '456'."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Delete a comment from a LinkedIn post by its numeric comment ID."""
        client = get_api_client()
        actor = _encode_urn(client.person_id())
        client.delete(
            f"/v2/socialActions/{_encode_urn(post_urn)}/comments/{comment_id}?actor={actor}"
        )
        logger.info("Comment %s deleted from %s", comment_id, post_urn)
        return {"comment_id": comment_id, "post_urn": post_urn, "status": "deleted"}
