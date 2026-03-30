"""Browser environment bootstrap for LinkedIn MCP Server."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from linkedin_mcp_server.authentication import get_authentication_source
from linkedin_mcp_server.drivers.browser import get_profile_dir
from linkedin_mcp_server.exceptions import AuthenticationError
from linkedin_mcp_server.session_state import (
    auth_root_dir,
    portable_cookie_path,
    profile_exists,
    source_state_path,
)

logger = logging.getLogger(__name__)

_BROWSER_DIR = "patchright-browsers"
_initialized = False


def browsers_path() -> Path:
    """Return the shared user-level Patchright browser cache path."""
    return auth_root_dir(get_profile_dir()) / _BROWSER_DIR


def configure_browser_environment() -> Path:
    """Ensure the shared browser cache path is configured."""
    browser_dir = browsers_path()
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_dir))
    return browser_dir


def initialize_bootstrap(runtime_policy: str | None = None) -> None:
    """Configure the shared browser cache on startup."""
    global _initialized
    if _initialized:
        return
    configure_browser_environment()
    _initialized = True


async def start_background_browser_setup_if_needed() -> None:
    """No-op -- browser install is handled by `uv run patchright install chromium`."""
    initialize_bootstrap()


async def ensure_tool_ready_or_raise(tool_name: str, ctx: object | None = None) -> None:
    """Gate tools on browser installed + valid auth profile."""
    initialize_bootstrap()

    browser_dir = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(browsers_path())))
    if not browser_dir.exists() or not any(browser_dir.iterdir()):
        raise AuthenticationError(
            "Patchright Chromium browser is not installed. Run: uv run patchright install chromium"
        )

    profile_dir = get_profile_dir()
    if not (
        profile_exists(profile_dir)
        and portable_cookie_path(profile_dir).exists()
        and source_state_path(profile_dir).exists()
    ):
        raise AuthenticationError(
            "No valid LinkedIn session found. Run with --login to create a browser profile."
        )

    try:
        get_authentication_source()
    except Exception as exc:
        raise AuthenticationError(
            "LinkedIn session metadata is incomplete. Run with --login to re-authenticate."
        ) from exc


def get_runtime_policy() -> str:
    """Return 'managed' -- Docker gating removed."""
    return "managed"


def reset_bootstrap_for_testing() -> None:
    """Reset bootstrap state for test isolation."""
    global _initialized
    _initialized = False
    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
