"""
LinkedIn network tools.

Provides read-only access to pending network invitations (received or
sent) from ``/mynetwork/invitation-manager/``. Accept, ignore, and
withdraw actions are intentionally not exposed.
"""

import logging
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error

logger = logging.getLogger(__name__)


def register_network_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register all network-related tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Pending Invitations",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"network", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_pending_invitations(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        kind: Literal["received", "sent"] = "received",
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List pending LinkedIn network invitations (received or sent).

        Reads ``/mynetwork/invitation-manager/{received|sent}/`` and returns
        the page's visible text plus references to inviter/invitee profiles.
        Read-only — accepting, ignoring, or withdrawing invitations is not
        exposed.

        Args:
            ctx: FastMCP context for progress reporting
            limit: Maximum number of invitations to load (1-100, default 20).
                Invitations load in batches of ~10 as the list scrolls, so
                the actual count may slightly exceed the target.
            kind: "received" (default) for incoming invitations, "sent" for
                outgoing ones awaiting the recipient's response.

        Returns:
            Dict with url, sections (invitations -> raw text), and optional
            references.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_pending_invitations"
            )
            logger.info("Fetching pending invitations (kind=%s, limit=%d)", kind, limit)

            await ctx.report_progress(
                progress=0, total=100, message=f"Loading {kind} invitations"
            )

            result = await extractor.get_pending_invitations(limit=limit, kind=kind)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_pending_invitations")
        except Exception as e:
            raise_tool_error(e, "get_pending_invitations")  # NoReturn
