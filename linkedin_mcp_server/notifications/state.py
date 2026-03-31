"""Persistent state for the notification poller.

Tracks which messages and connection approvals have already been seen so the
poller can emit events only for genuinely new activity.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_STATE_PATH = Path.home() / ".linkedin-mcp" / "notification-state.json"


@dataclass
class NotificationState:
    last_message_thread_ids: set[str] = field(default_factory=set)
    last_connection_approval_texts: set[str] = field(default_factory=set)


def load_state() -> NotificationState:
    """Load notification state from disk; return empty state on any error."""
    try:
        if _STATE_PATH.exists():
            data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            return NotificationState(
                last_message_thread_ids=set(data.get("last_message_thread_ids", [])),
                last_connection_approval_texts=set(
                    data.get("last_connection_approval_texts", [])
                ),
            )
    except Exception:
        logger.debug("Could not load notification state; starting fresh", exc_info=True)
    return NotificationState()


def save_state(state: NotificationState) -> None:
    """Persist notification state to disk."""
    try:
        _STATE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps(
                {
                    "last_message_thread_ids": sorted(state.last_message_thread_ids),
                    "last_connection_approval_texts": sorted(
                        state.last_connection_approval_texts
                    ),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        logger.warning("Could not save notification state", exc_info=True)
