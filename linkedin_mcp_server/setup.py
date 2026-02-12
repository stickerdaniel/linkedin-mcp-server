"""
Interactive setup flows for LinkedIn MCP Server authentication.

Handles session creation through interactive browser login using Patchright
with persistent context. Profile state auto-persists to user_data_dir.
"""

import asyncio
from pathlib import Path

from linkedin_scraper import BrowserManager, wait_for_manual_login
from linkedin_scraper.core import warm_up_browser

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

    async with BrowserManager(user_data_dir=user_data_dir, headless=False) as browser:
        # Warm up browser to appear more human-like and avoid security checkpoints
        if warm_up:
            print("   Warming up browser (visiting normal sites first)...")
            await warm_up_browser(browser.page)

        # Navigate to LinkedIn login
        await browser.page.goto("https://www.linkedin.com/login")

        # Wait for manual login completion
        # 5 minute timeout (300000ms) allows time for 2FA, captcha, security challenges
        await wait_for_manual_login(browser.page, timeout=300000)

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
