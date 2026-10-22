"""Opt-in limits on how often each tool may run against one account.

``tool_calls_per_minute`` and ``tool_calls_per_day`` (0, the default, is no
limit) refuse a call of a tool that already made that many in the last 60
seconds or 24 hours. Counted per tool, as the Live Request Limits in
``AGENTS.md`` count them, and only for tools that load a LinkedIn page.

The count is kept in the auth root, so a restart keeps it and every process
serving the profile shares one. It has no lock of its own: it is read and
written only by a caller that holds the profile lease, which is exclusive
across processes on one auth root (``SequentialToolExecutionMiddleware``).
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from linkedin_mcp_server.common_utils import secure_write_text
from linkedin_mcp_server.exceptions import ToolCallLimitError

if TYPE_CHECKING:
    from linkedin_mcp_server.profile_lease import ProfileLease

logger = logging.getLogger(__name__)

STATE_FILE = "tool-calls.json"
#: Tools that never load a LinkedIn page, so they spend nothing.
LOCAL_TOOLS = frozenset({"close_session"})


def admit_tool_call(
    tool: str,
    lease: ProfileLease,
    *,
    per_minute: int,
    per_day: int,
    now: float | None = None,
) -> None:
    """Count one call of *tool*, or raise :class:`ToolCallLimitError`.

    The caller holds *lease*. With both limits off nothing is read or written,
    not even the lease's auth root, so the call path is what it was.
    """
    windows = [
        (limit, variable, seconds, span)
        for limit, variable, seconds, span in (
            (per_minute, "TOOL_CALLS_PER_MINUTE", 60.0, "minute"),
            (per_day, "TOOL_CALLS_PER_DAY", 86400.0, "24 hours"),
        )
        if limit > 0
    ]
    if tool in LOCAL_TOOLS or not windows:
        return
    now = time.time() if now is None else now
    kept_for = max(seconds for _, _, seconds, _ in windows)
    path = lease.auth_root / STATE_FILE
    # Wall-clock stamps, because they have to outlive the process. One from
    # the future means the clock went back: it is stored again as made now,
    # so it holds its place for one window rather than until the clock
    # catches up.
    calls = {
        name: sorted(min(stamp, now) for stamp in stamps if now - stamp < kept_for)
        for name, stamps in _read(path).items()
    }
    made = calls.get(tool, [])
    refusal = None
    latest = now
    for limit, variable, seconds, span in windows:
        inside = [stamp for stamp in made if now - stamp < seconds]
        # Allowed again once the oldest call that keeps it at the limit leaves
        # the window. With both limits reached, the later of the two decides.
        allowed_at = inside[-limit] + seconds if len(inside) >= limit else now
        if allowed_at > latest:
            latest = allowed_at
            refusal = ToolCallLimitError(
                f"{tool} reached its limit of {limit} calls in the last {span} "
                f"({variable}), so this call was refused and nothing was sent "
                f"to LinkedIn. It is allowed again at {_utc(allowed_at)}, in "
                f"{_duration(allowed_at - now)}. Other tools are counted "
                "separately."
            )
    if refusal is None:
        calls[tool] = [*made, now]
    payload = {"version": 1, "calls": {name: v for name, v in calls.items() if v}}
    try:
        secure_write_text(path, json.dumps(payload, sort_keys=True) + "\n")
    except OSError:
        # Fails open, like an unreadable count below: an opt-in limit that
        # cannot be recorded is no reason to stop the call.
        logger.warning("Could not record tool calls in %s", path, exc_info=True)
    if refusal is not None:
        raise refusal


def _read(path: Path) -> dict[str, list[float]]:
    """The recorded calls, or none when the file is missing or unreadable."""
    try:
        calls = json.loads(path.read_text(encoding="utf-8"))["calls"]
        return {
            str(name): [
                float(stamp)
                for stamp in stamps
                if isinstance(stamp, (int, float))
                and not isinstance(stamp, bool)
                and math.isfinite(stamp)
            ]
            for name, stamps in calls.items()
            if isinstance(stamps, list)
        }
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Not knowing is not evidence: counted from zero and rewritten by this
        # call, rather than refusing every call until somebody deletes it.
        logger.warning("Ignoring unreadable %s; counting from zero", path)
        return {}


def _utc(stamp: float) -> str:
    when = datetime.fromtimestamp(stamp, UTC).isoformat(timespec="seconds")
    return when.replace("+00:00", "Z")


def _duration(seconds: float) -> str:
    seconds = max(1, math.ceil(seconds))
    if seconds < 120:
        return f"{seconds} seconds"
    minutes = math.ceil(seconds / 60)
    if minutes < 120:
        return f"{minutes} minutes"
    return f"{minutes // 60} hours {minutes % 60} minutes"
