"""
LinkedIn post/content search tool.

Performs LinkedIn's global content search (the "Posts" results tab) using
innerText extraction, so informal "we're hiring" / "Buscamos ..." posts can
be found before a formal job listing is published. Mirrors search_people:
build a /search/results/content/ URL, scroll to load results, and return the
raw innerText for the LLM to parse, plus post-permalink references.
"""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_ready_extractor, tool_errors
from linkedin_mcp_server.linkedin.contracts import FilterValidationError
from linkedin_mcp_server.linkedin.post_publisher import refuse_an_invalid_post

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
    )
    @tool_errors
    async def search_posts(
        keywords: str,
        ctx: Context,
        date_posted: str | None = None,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 3,
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
            "Searching posts: keywords='%s', date_posted='%s', max_pages=%d",
            keywords,
            date_posted,
            max_pages,
        )

        await ctx.report_progress(progress=0, total=100, message="Starting post search")

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

    @mcp.tool(
        timeout=tool_timeout,
        title="Create Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"post", "actions"},
    )
    @tool_errors
    async def create_post(
        text: str,
        confirm_post: bool,
        ctx: Context,
    ) -> dict[str, Any]:
        """
        Publish a text post to the authenticated user's LinkedIn feed.

        This is a write operation. With confirm_post False it opens the post
        composer, types the text, verifies it, then empties and closes the
        composer without posting (a dry run). Show the user the exact text and
        call again with confirm_post True only after they approve it. If the
        composer already holds a saved draft, nothing is typed and the status
        is composer_occupied.

        Args:
            text: Post body, up to 3000 characters. Line breaks become
                paragraphs; control characters are refused.
            confirm_post: Must be True to actually publish.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status, message, published and retry_safe, plus
            text_matches, post_button_found and post_button_enabled once the
            text was typed. status is one of dry_run, published, unconfirmed,
            composer_unavailable, composer_occupied, text_mismatch,
            post_button_unavailable or invalid. published is true only when the
            composer was seen closing after Post was clicked. retry_safe is
            false from the moment Post is clicked: on unconfirmed, check the
            user's profile activity before posting again. composer_closed is
            false only when the composer could not be emptied and closed
            after a call that did not post; check LinkedIn for leftover text.
        """
        # Refused before a session exists so a bad request never starts a
        # browser or a login attempt.
        refusal = refuse_an_invalid_post(text)
        if refusal is not None:
            return refusal
        extractor = await get_ready_extractor(ctx, tool_name="create_post")
        logger.info(
            "Creating post (%d characters, confirm_post=%s)", len(text), confirm_post
        )

        await ctx.report_progress(progress=0, total=100, message="Opening composer")

        result = await extractor.create_post(text, confirm_post=confirm_post)

        await ctx.report_progress(progress=100, total=100, message="Complete")

        return result
