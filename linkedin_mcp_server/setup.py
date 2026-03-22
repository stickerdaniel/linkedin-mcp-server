"""
Interactive setup flows for LinkedIn MCP Server authentication.

Handles session creation through interactive browser login using Patchright
with persistent context. Profile state auto-persists to user_data_dir.
"""

import asyncio
from pathlib import Path
from typing import Any

from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.core import (
    BrowserManager,
    resolve_remember_me_prompt,
    wait_for_manual_login,
    warm_up_browser,
)
from linkedin_mcp_server.session_state import portable_cookie_path, write_source_state

from linkedin_mcp_server.drivers.browser import get_profile_dir


async def interactive_login(
    user_data_dir: Path | None = None, warm_up: bool = True
) -> bool:
    """
    Open browser for manual LinkedIn login with persistent profile.

    Opens a non-headless browser, navigates to LinkedIn login page,
    and waits for user to complete authentication (including 2FA, captcha, etc.).
    Profile state auto-persists to user_data_dir.

    Args:
        user_data_dir: Path to browser profile. Defaults to config's user_data_dir.
        warm_up: Visit normal sites first to appear more human-like (default: True)

    Returns:
        True if login was successful

    Raises:
        Exception: If login fails or times out
    """
    if user_data_dir is None:
        user_data_dir = get_profile_dir()

    print("Opening browser for LinkedIn login...")
    print("   Please log in manually. You have 5 minutes to complete authentication.")
    print("   (This handles 2FA, captcha, and any security challenges)")

    launch_options: dict[str, Any] = {}
    config = get_config()
    if config.browser.chrome_path:
        launch_options["executable_path"] = config.browser.chrome_path

    async with BrowserManager(
        user_data_dir=user_data_dir, headless=False, **launch_options
    ) as browser:
        # Warm up browser to appear more human-like and avoid security checkpoints
        if warm_up:
            print("   Warming up browser (visiting normal sites first)...")
            await warm_up_browser(browser.page)

        # Navigate to LinkedIn login
        await browser.page.goto("https://www.linkedin.com/login")
        # Let LinkedIn finish rendering the saved-account chooser, then retry the
        # same exact click target a few times before falling back to the normal
        # manual-login wait loop.
        for _ in range(3):
            await asyncio.sleep(2)
            if await resolve_remember_me_prompt(browser.page):
                break

        # Wait for manual login completion
        # 5 minute timeout (300000ms) allows time for 2FA, captcha, security challenges
        await wait_for_manual_login(browser.page, timeout=300000)

        # Wait for persistent context to flush cookies to disk
        await asyncio.sleep(2)

        # Verify session cookie was persisted
        cookies = await browser.context.cookies()
        li_at = [c for c in cookies if c["name"] == "li_at"]
        if not li_at:
            print("   Warning: Session cookie not found. Login may not have persisted.")
            print("   Waiting longer for cookie propagation...")
            await asyncio.sleep(5)

        # Export source-session cookies for the one-time foreign-runtime bridge.
        # Docker now checkpoint-commits its own derived runtime profile after the
        # first successful /feed/ recovery instead of relying on browser teardown.
        if await browser.export_cookies(portable_cookie_path(user_data_dir)):
            print("   Cookies exported for Docker portability")
            source_state = write_source_state(user_data_dir)
            print(f"   Source session generation: {source_state.login_generation}")
        else:
            print(
                "   Warning: cookie export failed; Docker bridge may not work. "
                "Run --login again to retry."
            )
            return False
        print(f"Profile saved to {user_data_dir}")
        return True


def run_profile_creation(user_data_dir: str | None = None) -> bool:
    """
    Create profile via interactive login with persistent context.

    Args:
        user_data_dir: Path to profile directory. Defaults to config's user_data_dir.

    Returns:
        True if profile was created successfully
    """
    if user_data_dir:
        profile_dir = Path(user_data_dir).expanduser()
    else:
        profile_dir = get_profile_dir()

    print("LinkedIn MCP Server - Profile Creation")
    print(f"   Profile will be saved to: {profile_dir}")

    try:
        success = asyncio.run(interactive_login(profile_dir))
        return success
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
