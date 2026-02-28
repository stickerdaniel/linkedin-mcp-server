"""
LinkedIn connections bulk export tools.

Provides tools for collecting connection usernames via infinite scroll
and enriching profiles with contact details in chunked batches.
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
from linkedin_mcp_server.scraping import LinkedInExtractor

logger = logging.getLogger(__name__)


def register_connections_tools(mcp: FastMCP) -> None:
    """Register all connections-related tools with the MCP server."""

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get My Connections",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def get_my_connections(
        ctx: Context,
        limit: int = 0,
        max_scrolls: int = 50,
    ) -> dict[str, Any]:
        """
        Collect the authenticated user's LinkedIn connections via infinite scroll.

        Navigates to the connections page and scrolls to load all connection cards,
        then extracts username, name, and headline from each.

        Args:
            ctx: FastMCP context for progress reporting
            limit: Maximum connections to return (0 = unlimited, default 0)
            max_scrolls: Maximum scroll iterations, ~1s pause each (default 50)

        Returns:
            Dict with connections (list of {username, name, headline}), total count,
            url visited, and pages_visited list.
        """
        try:
            await ensure_authenticated()

            logger.info("Collecting connections (limit=%d, max_scrolls=%d)", limit, max_scrolls)

            browser = await get_or_create_browser()
            extractor = LinkedInExtractor(browser.page)

            await ctx.report_progress(
                progress=0, total=100, message="Loading connections page"
            )

            result = await extractor.scrape_connections_list(
                limit=limit, max_scrolls=max_scrolls
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except Exception as e:
            return handle_tool_error(e, "get_my_connections")

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Extract Contact Details",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def extract_contact_details(
        usernames: str,
        ctx: Context,
        chunk_size: int = 5,
        chunk_delay: int = 30,
    ) -> dict[str, Any]:
        """
        Enrich LinkedIn profiles with contact details (email, phone, etc.) in chunked batches.

        For each username, scrapes the main profile page and the contact info overlay.
        Processes profiles in chunks with configurable delays to avoid rate limiting.

        Args:
            usernames: Comma-separated LinkedIn usernames (e.g. "johndoe,janedoe,bobsmith")
            ctx: FastMCP context for progress reporting
            chunk_size: Number of profiles per chunk before pausing (default 5)
            chunk_delay: Seconds to pause between chunks (default 30)

        Returns:
            Dict with contacts (list of {username, profile, contact_info}),
            total enriched, failed usernames, rate_limited flag, and pages_visited.
        """
        try:
            await ensure_authenticated()

            username_list = [u.strip() for u in usernames.split(",") if u.strip()]

            if not username_list:
                return {
                    "error": "invalid_input",
                    "message": "No valid usernames provided. Pass comma-separated usernames.",
                }

            logger.info(
                "Enriching %d profiles (chunk_size=%d, chunk_delay=%ds)",
                len(username_list),
                chunk_size,
                chunk_delay,
            )

            browser = await get_or_create_browser()
            extractor = LinkedInExtractor(browser.page)

            total = len(username_list)

            await ctx.report_progress(
                progress=0, total=total, message=f"Starting enrichment of {total} profiles"
            )

            async def on_progress(completed: int, total: int) -> None:
                await ctx.report_progress(
                    progress=completed,
                    total=total,
                    message=f"Enriched {completed}/{total} profiles",
                )

            result = await extractor.scrape_contact_batch(
                usernames=username_list,
                chunk_size=chunk_size,
                chunk_delay=float(chunk_delay),
                progress_cb=on_progress,
            )

            await ctx.report_progress(
                progress=total, total=total, message="Complete"
            )

            return result

        except Exception as e:
            return handle_tool_error(e, "extract_contact_details")
