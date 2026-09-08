"""
LinkedIn post/content tools: global post search and immediate post creation.

search_posts performs LinkedIn's global content search (the "Posts" results
tab) using innerText extraction, so informal "we're hiring" / "Buscamos ..."
posts can be found before a formal job listing is published. Mirrors
search_people: build a /search/results/content/ URL, scroll to load results,
and return the raw innerText for the LLM to parse, plus post-permalink
references.

create_post drives LinkedIn's native share composer to publish a feed post
immediately (optionally as an organization page this account administers).
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
        title="Create Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"post", "actions"},
        exclude_args=["extractor"],
    )
    async def create_post(
        text: str,
        confirm_post: bool,
        ctx: Context,
        post_as: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Publish a LinkedIn feed post immediately via the native share composer.

        This is a write operation when confirm_post is True; with False it
        performs the full flow as a dry run — opening the composer, optionally
        switching the author, and typing the text — then discards the draft
        without publishing.

        Args:
            text: The post body text.
            confirm_post: Must be True to actually publish the post.
            ctx: FastMCP context for progress reporting
            post_as: Optional author to post as (e.g. "Peacock Labs"), matched
                against the composer's author-switch control for organization
                pages this account administers. Omit to post as the
                signed-in profile. If given but no matching option is found,
                the post is refused rather than publishing under the wrong
                identity.

        Returns:
            Dict with url, status, message, posted (bool), and — once the
            flow reaches the composer — author plus composer_text, the
            composer dialog's raw text, for verification.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="create_post"
            )
            logger.info(
                "Creating post: post_as=%s, confirm_post=%s, text_len=%d",
                post_as,
                confirm_post,
                len(text),
            )

            await ctx.report_progress(
                progress=0, total=100, message="Opening share composer"
            )

            result = await extractor.create_post(
                text,
                confirm_post=confirm_post,
                post_as=post_as,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except ToolError:
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "create_post")
        except Exception as e:
            raise_tool_error(e, "create_post")  # NoReturn
