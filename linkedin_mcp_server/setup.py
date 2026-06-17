"""
Interactive setup flows for LinkedIn MCP Server authentication.

Handles session creation through interactive browser login using Patchright
with persistent context. Profile state auto-persists to user_data_dir.
"""

import asyncio
from pathlib import Path
from typing import Any

from linkedin_mcp_server.authentication import clear_auth_state
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.constants import MANUAL_LOGIN_TIMEOUT_MINUTES, MANUAL_LOGIN_TIMEOUT_MS
from linkedin_mcp_server.core import (
    BrowserManager,
    resolve_remember_me_prompt,
    wait_for_manual_login,
    warm_up_browser,
)
from linkedin_mcp_server.drivers.browser import get_profile_dir
from linkedin_mcp_server.session_state import (
    portable_cookie_path,
    profile_exists,
    source_state_path,
    write_source_state,
)


async def _perform_login(
    browser: BrowserManager,
    user_data_dir: Path,
    warm_up: bool = True,
) -> bool:
    """Drive the manual login flow on an already-started browser.

    Returns True if login succeeded and cookies were exported.
    """
    if warm_up:
        print("   Warming up browser (visiting normal sites first)...")
        await warm_up_browser(browser.page)

    await browser.page.goto("https://www.linkedin.com/login")
    for _ in range(3):
        await asyncio.sleep(2)
        if await resolve_remember_me_prompt(browser.page):
            break

    await wait_for_manual_login(browser.page, timeout=MANUAL_LOGIN_TIMEOUT_MS)
    await asyncio.sleep(2)

    cookies = await browser.context.cookies()
    if not any(c["name"] == "li_at" for c in cookies):
        print("   Warning: Session cookie not found. Waiting longer...")
        await asyncio.sleep(5)

    if await browser.export_cookies(portable_cookie_path(user_data_dir)):
        source_state = write_source_state(user_data_dir)
        print(f"   Source session generation: {source_state.login_generation}")
    else:
        print("   Warning: cookie export failed. Run --login again to retry.")
        return False

    print(f"Profile saved to {user_data_dir}")
    return True


def _should_retry_with_fresh_auth_state(user_data_dir: Path, error: Exception) -> bool:
    """Retry once when a stale LinkedIn profile causes a redirect loop."""
    if "ERR_TOO_MANY_REDIRECTS" not in str(error):
        return False

    return (
        profile_exists(user_data_dir)
        or portable_cookie_path(user_data_dir).exists()
        or source_state_path(user_data_dir).exists()
    )


def _login_launch_options() -> dict[str, Any]:
    """Launch options shared by interactive login flows."""
    launch_options: dict[str, Any] = {}
    config = get_config()
    if config.browser.chrome_path:
        launch_options["executable_path"] = config.browser.chrome_path
    return launch_options


async def interactive_login(user_data_dir: Path | None = None, warm_up: bool = True) -> bool:
    """Open browser for manual LinkedIn login, then close it.

    Standard flow for ``--login``: authenticate and exit.
    """
    if user_data_dir is None:
        user_data_dir = get_profile_dir()

    print("Opening browser for LinkedIn login...")
    print(
        f"   Please log in manually. You have {MANUAL_LOGIN_TIMEOUT_MINUTES} minutes "
        "to complete authentication."
    )
    print("   (This handles 2FA, captcha, and any security challenges)")

    launch_options = _login_launch_options()

    for attempt in range(2):
        try:
            async with BrowserManager(
                user_data_dir=user_data_dir, headless=False, **launch_options
            ) as browser:
                return await _perform_login(browser, user_data_dir, warm_up)
        except Exception as error:
            if attempt == 0 and _should_retry_with_fresh_auth_state(user_data_dir, error):
                print("   Existing LinkedIn auth state appears stale. Clearing it and retrying...")
                clear_auth_state(user_data_dir)
                continue
            raise
    raise RuntimeError("Interactive login retry loop exhausted")


async def interactive_login_keep_alive(
    user_data_dir: Path | None = None, warm_up: bool = True
) -> BrowserManager:
    """Login and return the LIVE browser (caller owns lifecycle).

    Used by ``--login-serve`` to keep the same browser session alive
    for MCP tool execution, avoiding the cookie-bridge fingerprint mismatch.

    Raises:
        RuntimeError: If login fails.
    """
    if user_data_dir is None:
        user_data_dir = get_profile_dir()

    print("Opening browser for LinkedIn login (login-serve mode)...")
    print(
        f"   Please log in manually. You have {MANUAL_LOGIN_TIMEOUT_MINUTES} minutes "
        "to complete authentication."
    )
    print("   (This handles 2FA, captcha, and any security challenges)")

    launch_options = _login_launch_options()

    for attempt in range(2):
        browser = BrowserManager(user_data_dir=user_data_dir, headless=False, **launch_options)
        await browser.start()
        try:
            ok = await _perform_login(browser, user_data_dir, warm_up)
            if not ok:
                raise RuntimeError("Login failed — cookie export unsuccessful")
            browser.is_authenticated = True
            return browser
        except Exception as error:
            await browser.close()
            if attempt == 0 and _should_retry_with_fresh_auth_state(user_data_dir, error):
                print("   Existing LinkedIn auth state appears stale. Clearing it and retrying...")
                clear_auth_state(user_data_dir)
                continue
            raise
    raise RuntimeError("Interactive login retry loop exhausted")


def run_profile_creation(user_data_dir: str | None = None) -> bool:
    """
    Create profile via interactive login with persistent context.

    Args:
        user_data_dir: Path to profile directory. Defaults to config's user_data_dir.

    Returns:
        True if profile was created successfully
    """
    profile_dir = Path(user_data_dir).expanduser() if user_data_dir else get_profile_dir()

    print("LinkedIn MCP Server - Profile Creation")
    print(f"   Profile will be saved to: {profile_dir}")

    try:
        return asyncio.run(interactive_login(profile_dir))
    except Exception as e:
        print(f"Profile creation failed: {e}")
        return False


def run_interactive_setup() -> bool:
    """
    Run interactive setup - browser login only.

    Returns:
        True if setup completed successfully
    """
    print("LinkedIn MCP Server Setup")
    print("   Opening browser for manual login...")

    try:
        return asyncio.run(interactive_login())
    except Exception as e:
        print(f"Login failed: {e}")
        return False
