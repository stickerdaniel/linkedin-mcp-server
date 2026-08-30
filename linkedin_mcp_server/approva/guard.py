"""Kill switch and page-state reporting for every Approva write tool.

Spec  3: a write refuses while ``STOP`` exists, and every tool reports the
LinkedIn page state after it acts so the caller's anomaly stop can fire.

The stop file lives in the *bot* repo, not this one, because the founder and
the dispatcher both reach for it there. The path is configurable so this fork
stays usable outside Approva's checkout.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

PageState = Literal["normal", "login", "challenge"]

DEFAULT_STOP_FILE = "~/Approva/linkedin-bot/STOP"

# Substrings LinkedIn puts on an interstitial when it wants a human. Matched
# against lowercased body text, so they must stay lowercase here.
_CHALLENGE_MARKERS = (
    "unusual activity",
    "let's do a quick security check",
    "security verification",
    "verify it's you",
    "we've restricted your account",
    "your account has been restricted",
    "confirm your identity",
    "solve this puzzle",
    "captcha",
)

_LOGIN_MARKERS = (
    "sign in to linkedin",
    "join linkedin",
    "please enter your password",
    "welcome back",
)

_LOGIN_URL_MARKERS = (
    "/uas/login",
    "/login",
    "/checkpoint/lg/login",
    "/authwall",
)

_CHALLENGE_URL_MARKERS = (
    "/checkpoint/challenge",
    "/checkpoint/rp",
)


class StopFileError(RuntimeError):
    """Raised when a write is attempted while the kill switch is set."""


def stop_file_path() -> Path:
    """Resolve the kill-switch path (env override, else the Approva default)."""
    raw = os.environ.get("APPROVA_STOP_FILE", DEFAULT_STOP_FILE)
    return Path(raw).expanduser()


def stop_is_set() -> bool:
    return stop_file_path().exists()


def raise_if_stopped(tool_name: str) -> None:
    """Refuse a write while the kill switch is set.

    Called before the browser is touched, so a stopped system does not even
    load the page. Reads never call this.
    """
    path = stop_file_path()
    if path.exists():
        logger.warning("Refusing %s: kill switch present at %s", tool_name, path)
        raise StopFileError(
            f"Refusing {tool_name}: kill switch is set ({path}). "
            "Remove the file to allow writes again."
        )


def set_stop(reason: str) -> Path:
    """Set the kill switch ourselves after an anomaly, recording why."""
    path = stop_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{reason}\n")
        logger.error("Kill switch SET at %s: %s", path, reason)
    except OSError:
        logger.exception("Could not write kill switch to %s", path)
    return path


async def read_page_state(page: Any) -> PageState:
    """Classify the current page as normal / login / challenge.

    Cheap and defensive: any failure to read the page is reported as
    ``normal`` rather than raising, because this runs *after* an action has
    already happened and must never mask the action's own result. The caller
    treats a non-normal state as an anomaly stop.
    """
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""

    for marker in _CHALLENGE_URL_MARKERS:
        if marker in url:
            return "challenge"
    for marker in _LOGIN_URL_MARKERS:
        if marker in url:
            return "login"

    try:
        body = await page.evaluate(
            "() => (document.body?.innerText || '').slice(0, 4000).toLowerCase()"
        )
    except Exception:
        return "normal"

    if not isinstance(body, str):
        return "normal"

    for marker in _CHALLENGE_MARKERS:
        if marker in body:
            return "challenge"
    for marker in _LOGIN_MARKERS:
        if marker in body:
            return "login"
    return "normal"
