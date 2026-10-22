"""Refuse LinkedIn tools for a while after LinkedIn pushes back.

AGENTS.md ("Live Request Limits") stops live checks for 24 hours after a
rate-limit page. ``RATE_LIMIT_PAUSE`` holds the server's own tool calls to that
rule: a rate-limit page or an account restriction is recorded in the auth root,
where every process on the profile and every restart finds it. The call that
met the page closes the browser, and until the pause ends each LinkedIn tool is
refused before it asks for one.

The record names the login generation it was made under, so a new sign-in
ends the pause: it is the step a person takes after looking at the account.
The file is only ever written, never moved or deleted; an expired or
superseded record is ignored.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import UTC, datetime

from fastmcp.exceptions import ToolError

from linkedin_mcp_server.common_utils import secure_write_text
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.core.exceptions import AccountRestrictedError
from linkedin_mcp_server.session_state import auth_root_dir, load_source_state

logger = logging.getLogger(__name__)

PAUSE_FILE = "rate-limit-pause.json"


def _login_generation() -> str | None:
    state = load_source_state()
    return None if state is None else state.login_generation


def _utc(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def start_pause(exception: Exception) -> str | None:
    """Record that LinkedIn pushed back, and say until when tools are paused.

    ``None`` when the pause is off or could not be recorded, so the caller
    keeps its own advice.
    """
    seconds = get_config().browser.rate_limit_pause_seconds
    if not seconds:
        return None
    since = time.time()
    record = {
        "since": since,
        "restricted": isinstance(exception, AccountRestrictedError),
        "login_generation": _login_generation(),
    }
    try:
        secure_write_text(auth_root_dir() / PAUSE_FILE, json.dumps(record) + "\n")
    except OSError:
        logger.warning("Could not record the LinkedIn pause", exc_info=True)
        return None
    until = _utc(since + seconds)
    logger.warning("LinkedIn tools paused until %s", until)
    return (
        f"LinkedIn tools are paused until {until} (RATE_LIMIT_PAUSE); "
        "a successful --login ends the pause sooner."
    )


def _active_pause() -> tuple[float, float, bool] | None:
    """When the recorded pause began and ends, and whether it was a restriction.

    ``None`` when the pause is off, over, superseded by a new sign-in, or the
    record cannot be read.
    """
    seconds = get_config().browser.rate_limit_pause_seconds
    if not seconds:
        return None
    path = auth_root_dir() / PAUSE_FILE
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("Ignoring an unreadable %s", path)
        return None
    since = record.get("since") if isinstance(record, dict) else None
    if not isinstance(since, (int, float)) or not math.isfinite(since):
        return None
    if record.get("login_generation") != _login_generation():
        return None
    until = since + seconds
    if time.time() >= until:
        return None
    return since, until, record.get("restricted") is True


def is_paused() -> bool:
    """Whether LinkedIn tools are paused now."""
    return _active_pause() is not None


def raise_if_paused() -> None:
    """Refuse a LinkedIn tool while a recorded pause lasts.

    Raises:
        ToolError: Naming when the pause ends. Nothing was sent to LinkedIn.
    """
    pause = _active_pause()
    if pause is None:
        return
    since, until, restricted = pause
    what = "restricted the account" if restricted else "showed a rate-limit page"
    raise ToolError(
        f"LinkedIn {what} at {_utc(since)}, so LinkedIn tools are paused until "
        f"{_utc(until)} (RATE_LIMIT_PAUSE). Nothing was sent to LinkedIn. "
        "A successful --login ends the pause sooner."
    )
