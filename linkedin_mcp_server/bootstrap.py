"""Browser environment bootstrap for LinkedIn MCP Server."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
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
_BROWSER_INSTALL_TIMEOUT_S = 600
_initialized = False


def _browser_dir_populated(browser_dir: Path) -> bool:
    return browser_dir.exists() and any(browser_dir.iterdir())


def ensure_browser_installed(browser_dir: Path) -> bool:
    """Install Patchright's managed Chromium into the shared cache if missing.

    Self-heals the "Executable doesn't exist" failure that occurs when the
    shared cache path (``~/.linkedin-mcp/patchright-browsers``) hasn't been
    provisioned yet -- e.g. after a fresh clone, a cleared cache, or a
    patchright version bump that changed the pinned build.

    Returns:
        True if the browser is present after this call (already installed or
        freshly installed), False if installation was attempted and failed.
    """
    if _browser_dir_populated(browser_dir):
        return True

    logger.info("Patchright Chromium not found at %s -- installing", browser_dir)
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_dir)
    try:
        # Fixed argv (sys.executable + literal strings), no untrusted input reaches this call.
        subprocess.run(  # noqa: S603
            [sys.executable, "-m", "patchright", "install", "chromium"],
            env=env,
            check=True,
            timeout=_BROWSER_INSTALL_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.error("Automatic Patchright Chromium install failed: %s", exc)
        return False

    return _browser_dir_populated(browser_dir)


def browsers_path() -> Path:
    """Return the shared user-level Patchright browser cache path."""
    return auth_root_dir(get_profile_dir()) / _BROWSER_DIR


def configure_browser_environment() -> Path:
    """Ensure the shared browser cache path is configured."""
    browser_dir = browsers_path()
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_dir))
    return browser_dir


def initialize_bootstrap(_runtime_policy: str | None = None) -> None:
    """Configure the shared browser cache on startup."""
    global _initialized
    if _initialized:
        return
    configure_browser_environment()
    _initialized = True


async def start_background_browser_setup_if_needed() -> None:
    """Self-heal the managed Chromium install on startup, before any tool call needs it."""
    initialize_bootstrap()
    browser_dir = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(browsers_path())))
    await asyncio.to_thread(ensure_browser_installed, browser_dir)


async def ensure_tool_ready_or_raise(_tool_name: str, _ctx: object | None = None) -> None:
    """Gate tools on browser installed + valid auth profile."""
    initialize_bootstrap()

    browser_dir = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(browsers_path())))
    if not _browser_dir_populated(browser_dir) and not await asyncio.to_thread(
        ensure_browser_installed, browser_dir
    ):
        raise AuthenticationError(
            "Patchright Chromium browser is not installed and automatic "
            "installation failed. Run: uv run python -m patchright install chromium"
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
