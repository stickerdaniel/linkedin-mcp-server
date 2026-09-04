"""Cross-process minimum interval between MCP tool-call starts.

Complements ``SequentialToolExecutionMiddleware``: the in-process lock serializes
calls inside one server, and this module paces starts across every process that
shares the same LinkedIn auth root. The profile lease is deliberately not used
here — waiting for the interval must not hold the browser.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from linkedin_mcp_server.common_utils import secure_write_text
from linkedin_mcp_server.profile_lease import (
    _release_locked_fd,
    acquire_locked_fd,
)

logger = logging.getLogger(__name__)

_TIMESTAMP_FILE = "tool-interval.json"
_LOCK_FILE = "tool-interval.lock"
# A stamp this far ahead of wall time is treated as corrupt (or from a
# wildly desynchronized clock) and reclaimed. Smaller steps backwards —
# including multi-minute NTP corrections — must not grant an immediate
# start; they count as zero elapsed time so the full interval is waited.
_CORRUPT_FUTURE_SECONDS = 3600.0


def interval_paths(auth_root: Path) -> tuple[Path, Path]:
    """Return ``(timestamp_path, lock_path)`` under *auth_root*."""
    root = auth_root.expanduser().resolve()
    return root / _TIMESTAMP_FILE, root / _LOCK_FILE


def read_last_start_wall(auth_root: Path) -> float | None:
    """Return the last tool-start wall time, or ``None`` if unset/unusable."""
    path, _ = interval_paths(auth_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.debug("Could not read tool-interval stamp at %s: %s", path, exc)
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("Ignoring malformed tool-interval stamp at %s", path)
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("last_start_wall")
    if isinstance(value, (int, float)) and value == value:  # not NaN
        return float(value)
    return None


def write_last_start_wall(auth_root: Path, when: float) -> None:
    """Persist *when* as the last tool-start wall time under *auth_root*."""
    path, _ = interval_paths(auth_root)
    secure_write_text(
        path,
        json.dumps({"last_start_wall": when}, indent=2, sort_keys=True) + "\n",
    )


def remaining_wait_seconds(
    *,
    interval: float,
    last_start_wall: float | None,
    now_wall: float,
) -> float:
    """How long to wait before the next start may claim the interval slot."""
    if interval <= 0 or last_start_wall is None:
        return 0.0
    skew = last_start_wall - now_wall
    # Only reclaim stamps that are absurdly in the future.
    if skew > _CORRUPT_FUTURE_SECONDS:
        return 0.0
    if skew > 0:
        # Peer clock ahead / local rollback: waiting the full interval on
        # every retry never advances (elapsed stays 0) and burns the wait
        # budget. Catch up toward the stamp in interval-sized steps instead.
        return min(interval, skew)
    return max(0.0, interval - (now_wall - last_start_wall))


def try_claim_start(auth_root: Path, interval: float) -> float:
    """Claim a tool-start slot under *auth_root*, or return seconds still to wait.

    Holds the interval lock only for the read/decide/write. A positive return
    means the caller should sleep that long (without holding any lock) and try
    again. ``0.0`` means this process owns the start and the stamp was updated.
    """
    if interval <= 0:
        return 0.0

    _, lock_path = interval_paths(auth_root)
    fd = acquire_locked_fd(lock_path, exclusive=True)
    if fd is None:
        # Another process is deciding; a short wait and retry is enough.
        return min(0.05, interval)

    try:
        now = time.time()
        last = read_last_start_wall(auth_root)
        wait = remaining_wait_seconds(
            interval=interval, last_start_wall=last, now_wall=now
        )
        if wait > 0:
            return wait
        write_last_start_wall(auth_root, now)
        return 0.0
    finally:
        _release_locked_fd(fd)
        try:
            os.close(fd)
        except OSError:
            logger.debug("Closing tool-interval lock fd failed", exc_info=True)
