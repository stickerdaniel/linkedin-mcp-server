"""Session registry for MCP push notifications.

Stores weak references to active ServerSession objects. When the poller detects
new LinkedIn activity it calls notify_all() to send a ResourceUpdatedNotification
to every subscribed session, prompting the MCP client to re-read the resource.
"""

from __future__ import annotations

import logging
import weakref
from typing import Any

import mcp.types as mt
from mcp import ServerSession
from pydantic import AnyUrl, TypeAdapter

_NOTIFICATION_URI: AnyUrl = TypeAdapter(AnyUrl).validate_python(
    "linkedin://notifications"
)

logger = logging.getLogger(__name__)

# Weak references so dead sessions are GC'd automatically.
_subscribed_sessions: weakref.WeakSet[ServerSession] = weakref.WeakSet()

# Accumulated events waiting to be consumed via the MCP resource.
_pending_events: list[dict[str, Any]] = []


def add_session(session: ServerSession) -> None:
    """Register a session to receive ResourceUpdatedNotifications."""
    _subscribed_sessions.add(session)
    logger.debug(
        "Notification session registered (total=%d)", len(_subscribed_sessions)
    )


def remove_session(session: ServerSession) -> None:
    """Unregister a session."""
    _subscribed_sessions.discard(session)
    logger.debug("Notification session removed (total=%d)", len(_subscribed_sessions))


def add_events(events: list[dict[str, Any]]) -> None:
    """Append new events to the pending queue."""
    _pending_events.extend(events)


def drain_events() -> list[dict[str, Any]]:
    """Return all pending events and clear the queue."""
    events = list(_pending_events)
    _pending_events.clear()
    return events


async def notify_all() -> None:
    """Send ResourceUpdatedNotification to every subscribed session."""
    notification = mt.ResourceUpdatedNotification(
        params=mt.ResourceUpdatedNotificationParams(uri=_NOTIFICATION_URI)
    )
    server_notification = mt.ServerNotification(root=notification)

    dead: list[ServerSession] = []
    for session in list(_subscribed_sessions):
        try:
            await session.send_notification(server_notification)
        except Exception:
            logger.debug("Notification send failed; dropping session", exc_info=True)
            dead.append(session)
    for session in dead:
        _subscribed_sessions.discard(session)
