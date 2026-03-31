"""Shared asyncio lock for serializing all browser operations.

Both SequentialToolExecutionMiddleware and the notification poller import this
module so they contend on the same lock, preventing concurrent browser use.
"""

from __future__ import annotations

import asyncio

_lock: asyncio.Lock | None = None


def get_scraper_lock() -> asyncio.Lock:
    """Return the process-wide scraper lock, creating it on first call."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock
