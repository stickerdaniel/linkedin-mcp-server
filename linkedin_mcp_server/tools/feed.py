"""
LinkedIn feed and post scraping tools.

Fetches posts from the authenticated user's LinkedIn home feed using
innerText extraction. Scrolls until the requested number of post
permalinks have been observed in SDUI pagination responses — a
locale-independent progress signal, since the feed DOM exposes no
stable per-post container selector.

Also reads a single post's permalink page (get_post_comments), paginating
its comment thread so replies on the post are captured.
"""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping.extractor import _RATE_LIMITED_MSG, normalize_post_url
from linkedin_mcp_server.scraping.link_metadata import Reference

logger = logging.getLogger(__name__)


def register_feed_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register feed-related tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Feed",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"feed", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_feed(
        ctx: Context,
        num_posts: Annotated[int, Field(ge=1, le=50)] = 10,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get posts from the authenticated user's LinkedIn feed.

        Args:
            ctx: FastMCP context for progress reporting
            num_posts: Number of posts to fetch (1-50, default 10).
                       Posts are loaded in batches of ~5 as the page scrolls,
                       so the actual count may slightly exceed the target.

        Returns:
            Dict with url, sections (name -> raw text), and optional keys:
            - references["feed"]: list of {kind: "feed_post", url, ...}
              entries. URLs are relative paths and may carry either
              ``/feed/update/<urn>/`` (DOM-anchor-derived) or
              ``/posts/<slug>`` (SDUI-derived) shape — both are valid
              LinkedIn permalinks.
            - section_errors: present when the feed is rate-limited or
              extraction fails.

            Truncated posts are not auto-expanded; full text for any post
            is reachable via its permalink in references["feed"]. The LLM
            should parse sections["feed"] for post bodies.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_feed"
            )
            logger.info("Scraping feed (num_posts=%d)", num_posts)

            await ctx.report_progress(
                progress=0, total=100, message="Starting feed scrape"
            )

            extracted = await extractor.extract_feed(num_posts=num_posts)

            url = "https://www.linkedin.com/feed/"
            sections: dict[str, str] = {}
            references: dict[str, list[Reference]] = {}
            section_errors: dict[str, dict[str, Any]] = {}
            if extracted.text and extracted.text != _RATE_LIMITED_MSG:
                sections["feed"] = extracted.text
                if extracted.references:
                    references["feed"] = extracted.references
            elif extracted.text == _RATE_LIMITED_MSG:
                section_errors["feed"] = {
                    "error_type": "rate_limit",
                    "error_message": extracted.text,
                }
            elif extracted.error:
                section_errors["feed"] = extracted.error

            await ctx.report_progress(progress=100, total=100, message="Complete")

            result: dict[str, Any] = {"url": url, "sections": sections}
            if references:
                result["references"] = references
            if section_errors:
                result["section_errors"] = section_errors
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_feed")
        except Exception as e:
            raise_tool_error(e, "get_feed")

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Post Comments",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"feed", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_post_comments(
        post_url: str,
        ctx: Context,
        max_scrolls: Annotated[int, Field(ge=1, le=50)] | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get a single LinkedIn post with its full comment thread.

        Use this to read the comments (and nested replies) other people
        left on a post — e.g. on the authenticated user's own posts. Obtain
        post URLs from references["feed"] (get_feed), the posts/comments
        sections of get_person_profile / get_my_profile, or get_company_posts.

        Args:
            post_url: Post permalink. Accepts a full or relative URL in
                either ``/feed/update/<urn>/`` or ``/posts/<slug>`` form, or
                a bare URN like ``urn:li:activity:7203847...``.
            ctx: FastMCP context for progress reporting
            max_scrolls: Maximum "load more comments" / "see previous
                replies" pagination clicks (default 5). Increase for posts
                with long comment threads.

        Returns:
            Dict with url and sections["post"] containing the post body and
            comment thread as raw text. references["post"] lists commenter
            profiles and linked posts. section_errors is present when the
            page is rate-limited or extraction fails. The LLM should parse
            the raw text; the comment thread follows the post body.
        """
        try:
            url = normalize_post_url(post_url)
            if url is None:
                raise ToolError(
                    "post_url must be a LinkedIn post permalink "
                    "(/feed/update/<urn>/ or /posts/<slug>, full or relative "
                    "URL) or a bare urn:li:activity:<id>."
                )

            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_post_comments"
            )
            logger.info("Scraping post comments: %s", url)

            await ctx.report_progress(
                progress=0, total=100, message="Loading post and comments"
            )

            extracted = await extractor.get_post_comments(url, max_scrolls=max_scrolls)

            sections: dict[str, str] = {}
            references: dict[str, list[Reference]] = {}
            section_errors: dict[str, dict[str, Any]] = {}
            if extracted.text and extracted.text != _RATE_LIMITED_MSG:
                sections["post"] = extracted.text
                if extracted.references:
                    references["post"] = extracted.references
            elif extracted.text == _RATE_LIMITED_MSG:
                section_errors["post"] = {
                    "error_type": "rate_limit",
                    "error_message": extracted.text,
                }
            elif extracted.error:
                section_errors["post"] = extracted.error

            await ctx.report_progress(progress=100, total=100, message="Complete")

            result: dict[str, Any] = {"url": url, "sections": sections}
            if references:
                result["references"] = references
            if section_errors:
                result["section_errors"] = section_errors
            return result

        except ToolError:
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_post_comments")
        except Exception as e:
            raise_tool_error(e, "get_post_comments")
