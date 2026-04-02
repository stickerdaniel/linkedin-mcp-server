"""
LinkedIn notification tools.

Provides a subscribe/unsubscribe pair that lets an MCP client register for
push delivery of LinkedIn events (new messages, connection approvals) via the
MCP resource-subscription protocol. When the background poller detects new
activity it sends a ResourceUpdatedNotification to all subscribed sessions;
the client then re-reads the linkedin://notifications resource to get the
event list.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server.notifications.registry import (
    add_session,
    drain_events,
    remove_session,
)

logger = logging.getLogger(__name__)


def register_notification_tools(mcp: FastMCP) -> None:
    """Register the notifications resource and subscribe/unsubscribe tools."""

    @mcp.resource(
        "linkedin://notifications",
        name="LinkedIn Notifications",
        description=(
            "Pending LinkedIn notification events detected since the last read. "
            "Subscribe to this resource and call subscribe_notifications() to receive "
            "push updates when new messages arrive or connections are approved. "
            "Reading this resource drains the event queue."
        ),
        mime_type="application/json",
        tags={"notifications"},
    )
    async def get_notifications() -> list[dict[str, Any]]:
        """Return and clear all pending LinkedIn notification events."""
        return drain_events()

    @mcp.tool(
        title="Subscribe to Notifications",
        annotations={"readOnlyHint": True},
        tags={"notifications"},
    )
    async def subscribe_notifications(ctx: Context) -> str:
        """
        Subscribe this MCP session to LinkedIn push notifications.

        After calling this tool, also subscribe your MCP client to the
        linkedin://notifications resource. The server will send a
        ResourceUpdatedNotification whenever new messages or connection
        approvals are detected (polled every 5 minutes). Read the resource
        to drain the event queue.

        Returns:
            Confirmation message.
        """
        add_session(ctx.session)
        logger.info("Client subscribed to LinkedIn notifications")
        return (
            "Subscribed. Subscribe your MCP client to the linkedin://notifications "
            "resource to receive push notifications for new messages and connection "
            "approvals. Events are checked every 5 minutes."
        )

    @mcp.tool(
        title="Unsubscribe from Notifications",
        annotations={"readOnlyHint": True},
        tags={"notifications"},
    )
    async def unsubscribe_notifications(ctx: Context) -> str:
        """
        Unsubscribe this MCP session from LinkedIn push notifications.

        Returns:
            Confirmation message.
        """
        remove_session(ctx.session)
        logger.info("Client unsubscribed from LinkedIn notifications")
        return "Unsubscribed from LinkedIn notifications."
