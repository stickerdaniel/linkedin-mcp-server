"""
LinkedIn group scraping tools.

Uses innerText extraction for resilient group member listing capture.
"""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error

logger = logging.getLogger(__name__)


def register_group_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register all group-related tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Group Members",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"group", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_group_members(
        group_id: str,
        ctx: Context,
        keywords: str | None = None,
        max_scrolls: Annotated[int, Field(ge=1, le=2000)] | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List members of a LinkedIn group from its /members/ page.

        The full member list is only visible when the logged-in account is a
        member of the group. For groups the account has not joined, LinkedIn
        typically redirects to the group landing page or shows only a
        restricted preview; the returned text reflects whatever the page
        actually served, so an unexpectedly short members section usually
        means the account is not in that group.

        group_id is the numeric id from the group URL — the path segment
        after /groups/ (e.g. "12345" for linkedin.com/groups/12345/).

        LinkedIn serves at most ~500 member rows per listing no matter how
        far the page is scrolled. To enumerate a larger group, make multiple
        calls with different keywords values (e.g. common first names or
        role terms) — each filtered slice gets its own ~500-row budget —
        and merge/dedupe the results by profile URL.

        Args:
            group_id: Numeric LinkedIn group id (e.g., "12345")
            ctx: FastMCP context for progress reporting
            keywords: Optional server-side member filter (name, headline
                term). Filters via the page's member search box.
            max_scrolls: Maximum scroll-to-bottom iterations to load more
                members. The listing is an infinite-scroll list (no page
                URLs), so a fresh call always restarts from the top — the
                only way to reach deeper members within one slice is a
                larger budget, not repeated calls. Each scroll loads roughly
                5 more members and takes about a second. Default (None) uses
                5. ~100 scrolls reaches the ~500-row serving cap; budgets
                beyond that only add time. Raise the server's --tool-timeout
                for deep pulls (the default 180s supports roughly 150
                scrolls).

        Returns:
            Dict with url, sections (members -> raw text), and optional
            references. References include /in/ profile paths for listed
            members. The LLM should parse the raw text to extract member
            names, headlines, and locations.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_group_members"
            )
            logger.info(
                "Scraping group members: %s (keywords=%s, max_scrolls=%s)",
                group_id,
                keywords,
                max_scrolls,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Loading group members"
            )

            result = await extractor.get_group_members(
                group_id, keywords=keywords, max_scrolls=max_scrolls
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_group_members")
        except Exception as e:
            raise_tool_error(e, "get_group_members")  # NoReturn
