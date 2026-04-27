"""
LinkedIn network management tools.

Provides read-only access to the LinkedIn invitation manager — both outgoing
connection requests still awaiting acceptance, and incoming requests awaiting
your action.
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


def register_network_tools(mcp: FastMCP) -> None:
    """Register all network management tools with the MCP server."""

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Sent Invitations",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"network", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_sent_invitations(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List outgoing LinkedIn connection requests still awaiting acceptance.

        Args:
            ctx: FastMCP context for progress reporting
            limit: Maximum number of invitations to load (1-100, default 50)

        Returns:
            Dict with url, sections (sent_invitations -> raw text), and
            optional references.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_sent_invitations"
            )
            logger.info("Fetching sent invitations (limit=%d)", limit)

            await ctx.report_progress(
                progress=0, total=100, message="Loading sent invitations"
            )

            result = await extractor.get_sent_invitations(limit=limit)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_sent_invitations")
        except Exception as e:
            raise_tool_error(e, "get_sent_invitations")  # NoReturn

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Received Invitations",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"network", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_received_invitations(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List incoming LinkedIn connection requests awaiting your action.

        Args:
            ctx: FastMCP context for progress reporting
            limit: Maximum number of invitations to load (1-100, default 50)

        Returns:
            Dict with url, sections (received_invitations -> raw text), and
            optional references.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_received_invitations"
            )
            logger.info("Fetching received invitations (limit=%d)", limit)

            await ctx.report_progress(
                progress=0, total=100, message="Loading received invitations"
            )

            result = await extractor.get_received_invitations(limit=limit)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_received_invitations")
        except Exception as e:
            raise_tool_error(e, "get_received_invitations")  # NoReturn
