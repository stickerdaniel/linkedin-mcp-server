"""
LinkedIn connections tools.

Provides tools for listing connections, extracting contact details,
searching connections at a specific company, and viewing notifications.
"""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error

logger = logging.getLogger(__name__)


def register_connections_tools(mcp: FastMCP) -> None:
    """Register all connections-related tools with the MCP server."""

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS * 3,
        title="Get My Connections",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"connections", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_my_connections(
        ctx: Context,
        limit: Annotated[int, Field(ge=0)] = 0,
        max_scrolls: Annotated[int, Field(ge=1, le=200)] = 50,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List the authenticated user's LinkedIn connections.

        Scrolls through the connections page to collect usernames,
        names, and headlines.

        Args:
            ctx: FastMCP context for progress reporting
            limit: Maximum connections to return (0 = unlimited, default 0)
            max_scrolls: Maximum scroll iterations, ~1s each (default 50)

        Returns:
            Dict with connections list [{username, name, headline}], total count, and url.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_my_connections"
            )
            logger.info("Collecting connections (limit=%d)", limit)

            await ctx.report_progress(
                progress=0, total=100, message="Loading connections"
            )

            result = await extractor.scrape_connections_list(
                limit=limit, max_scrolls=max_scrolls
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_my_connections")
        except Exception as e:
            raise_tool_error(e, "get_my_connections")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS * 5,
        title="Extract Contact Details",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"connections", "scraping"},
        exclude_args=["extractor"],
    )
    async def extract_contact_details(
        usernames: str,
        ctx: Context,
        chunk_size: Annotated[int, Field(ge=1, le=20)] = 5,
        chunk_delay: float = 30.0,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Enrich LinkedIn profiles with contact details in chunked batches.

        For each username, scrapes the main profile page and the contact
        info overlay to extract structured fields.

        Args:
            usernames: Comma-separated LinkedIn usernames (e.g., "johndoe,janedoe")
            ctx: FastMCP context for progress reporting
            chunk_size: Profiles per chunk before pausing (default 5)
            chunk_delay: Seconds to pause between chunks (default 30)

        Returns:
            Dict with contacts list [{username, first_name, last_name, email,
            phone, headline, location, company, website, birthday}],
            total, failed list, and rate_limited flag.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="extract_contact_details"
            )

            username_list = list(
                dict.fromkeys(u.strip() for u in usernames.split(",") if u.strip())
            )
            if not username_list:
                return {
                    "error": "invalid_input",
                    "message": "No valid usernames provided.",
                }

            logger.info("Enriching %d profiles", len(username_list))
            total = len(username_list)

            await ctx.report_progress(
                progress=0, total=total, message=f"Enriching {total} profiles"
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
                chunk_delay=chunk_delay,
                progress_cb=on_progress,
            )

            await ctx.report_progress(
                progress=result["total"], total=total, message="Complete"
            )
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "extract_contact_details")
        except Exception as e:
            raise_tool_error(e, "extract_contact_details")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Connections at Company",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"connections", "search"},
        exclude_args=["extractor"],
    )
    async def get_connections_at_company(
        company: str,
        ctx: Context,
        keywords: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Find your 1st-degree connections who work at a specific company.

        Searches LinkedIn people with network filter set to 1st-degree
        and company filter set to the provided company name.

        Args:
            company: Company name (e.g., "Google", "Microsoft")
            ctx: FastMCP context for progress reporting
            keywords: Optional additional keywords to filter by (e.g., "engineer")

        Returns:
            Dict with url, sections (search_results -> text), and references.
            The LLM should parse the text to extract individual people.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_connections_at_company"
            )
            logger.info("Searching connections at %s", company)

            await ctx.report_progress(
                progress=0, total=100, message=f"Searching connections at {company}"
            )

            result = await extractor.search_connections_at_company(
                company, keywords=keywords
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_connections_at_company")
        except Exception as e:
            raise_tool_error(e, "get_connections_at_company")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Notifications",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"notifications", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_notifications(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get your LinkedIn notifications.

        Scrapes the notifications page for connection requests, post
        reactions, mentions, endorsements, job alerts, and other activity.

        Args:
            ctx: FastMCP context for progress reporting
            limit: Maximum notifications to load (1-50, default 20)

        Returns:
            Dict with url, sections (notifications -> raw text), and references.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_notifications"
            )
            logger.info("Fetching notifications (limit=%d)", limit)

            await ctx.report_progress(
                progress=0, total=100, message="Loading notifications"
            )

            result = await extractor.get_notifications(limit=limit)

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_notifications")
        except Exception as e:
            raise_tool_error(e, "get_notifications")
