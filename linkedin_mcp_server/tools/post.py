"""
LinkedIn post tools: global content search and the saved-items list.

search_posts performs LinkedIn's global content search (the "Posts" results
tab) using innerText extraction, so informal "we're hiring" / "Buscamos ..."
posts can be found before a formal job listing is published.
get_saved_posts lists the authenticated user's saved posts and articles
from /my-items/saved-posts/, scrolling until enough item anchors are
present — a URL-pattern count signal, because ?start= offsets are a no-op
on this surface and text-based counting would not survive a non-English UI.
Its enrich levels and read_post share one primitive: the post-detail page,
which is where a body LinkedIn cut in a list is rendered whole.
"""

import logging
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.callbacks import MCPContextProgressCallback
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping.contracts import FilterValidationError

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
        title="Get Saved Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post", "saved"},
        exclude_args=["extractor"],
    )
    async def get_saved_posts(
        ctx: Context,
        num_posts: Annotated[int, Field(ge=1, le=50)] = 10,
        enrich: Annotated[Literal["none", "truncated", "all"], Field()] = "none",
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List the authenticated user's saved posts and articles.

        Reads /my-items/saved-posts/ and scrolls until at least num_posts
        saved-item anchors are present (1-50, default 10). Each item is
        returned on its own, keyed by its permalink: /feed/update/<urn>/ for
        posts (kind "feed_post"), /pulse/<slug>/ for articles (kind
        "article").

        The listing cuts long bodies. enrich re-reads the post-detail page,
        which renders the body in full, at the cost of one navigation per
        item (seconds each — a batch operation, not an interactive one):
        - "none" (default): listing only.
        - "truncated": re-read only the items whose text was cut.
        - "all": re-read every item; the only level that reports images for
          an item whose listing text was already complete.

        Args:
            ctx: FastMCP context for progress reporting
            num_posts: How many saved items to scroll to (1-50, default 10)
            enrich: Detail-page re-read level: none, truncated, or all

        Returns:
            Dict with url and saved_posts: a list of items carrying kind,
            permalink, text, truncated, and — where the card offers them —
            urn, author and preview {title, domain}. preview.domain means the
            post points at content hosted elsewhere. Enriched items carry the
            full text with truncated false, plus images (signed media URLs
            that expire, so fetch them promptly) and links (non-LinkedIn URLs
            as LinkedIn serves them, lnkd.in shortlinks unresolved). An item
            whose re-read failed carries error instead. section_errors
            reports a page-level failure or a rate limit.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_saved_posts"
            )
            logger.info(
                "Fetching saved posts (num_posts=%d, enrich=%s)", num_posts, enrich
            )

            await ctx.report_progress(
                progress=0, total=100, message="Starting saved posts"
            )

            result = await extractor.get_saved_posts(
                num_posts=num_posts,
                enrich=enrich,
                callbacks=MCPContextProgressCallback(ctx),
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except FilterValidationError as e:
            raise ToolError(str(e)) from e
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_saved_posts")
        except Exception as e:
            raise_tool_error(e, "get_saved_posts")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Read Post",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post"},
        exclude_args=["extractor"],
    )
    async def read_post(
        ctx: Context,
        urn: Annotated[str, Field(min_length=1)],
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Read one post or article in full from its permalink page.

        Takes an activity URN (urn:li:activity:123), a permalink path
        (/feed/update/<urn>/ or /pulse/<slug>/) or the LinkedIn URL of
        either — the permalink and urn that get_saved_posts and get_feed
        return are both accepted directly.

        Args:
            ctx: FastMCP context for progress reporting
            urn: Activity URN, permalink path, or full LinkedIn post URL

        Returns:
            Dict with url, text (the untruncated body), images (the post's
            own media URLs, signed and expiring) and links (the non-LinkedIn
            URLs the post points at, unresolved).
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="read_post"
            )
            logger.info("Reading post %s", urn)

            await ctx.report_progress(progress=0, total=100, message="Starting post")

            result = await extractor.read_post(urn=urn)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "read_post")
        except Exception as e:
            raise_tool_error(e, "read_post")  # NoReturn
