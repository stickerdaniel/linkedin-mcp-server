"""
Interactive setup flows for LinkedIn MCP Server authentication.

Handles session creation through interactive browser login using Playwright.
Uses linkedin_scraper v3's wait_for_manual_login for authentication.
"""

import asyncio
import logging
from pathlib import Path

from linkedin_scraper import wait_for_manual_login
from linkedin_scraper.core import warm_up_browser

from linkedin_mcp_server.drivers.browser import DEFAULT_USER_DATA_DIR
from linkedin_mcp_server.drivers.persistent_browser import PersistentBrowserManager

logger = logging.getLogger(__name__)


async def interactive_login_and_save(
    user_data_dir: Path | None = None, warm_up: bool = True
) -> bool:
    """
    Open browser for manual LinkedIn login using persistent context.

    Opens a non-headless browser, navigates to LinkedIn login page,
    and waits for user to complete authentication (including 2FA, captcha, etc.).
    Session is automatically saved to user_data_dir by Playwright.

    Args:
        user_data_dir: Browser profile directory. Defaults to ~/.linkedin-mcp/browser-profile
        warm_up: Visit normal sites first to appear more human-like (default: True)

    Returns:
        True if login was successful

    Raises:
        Exception: If login fails or times out
    """
    if user_data_dir is None:
        user_data_dir = DEFAULT_USER_DATA_DIR

    print("Opening browser for LinkedIn login...")
    print("   Please log in manually. You have 5 minutes to complete authentication.")
    print("   (This handles 2FA, captcha, and any security challenges)")

    # Ensure directory parent exists
    user_data_dir.parent.mkdir(parents=True, exist_ok=True)

    # Create persistent browser manager
    browser = PersistentBrowserManager(
        user_data_dir=user_data_dir,
        headless=False,  # Always non-headless for interactive login
    )

    try:
        await browser.start()

        # Warm up browser to appear more human-like and avoid security checkpoints
        if warm_up:
            print("   Warming up browser (visiting normal sites first)...")
            await warm_up_browser(browser.page)

        # Navigate to LinkedIn login
        await browser.page.goto("https://www.linkedin.com/login")

        # Wait for manual login completion
        # 5 minute timeout (300000ms) allows time for 2FA, captcha, security challenges
        await wait_for_manual_login(browser.page, timeout=300000)

        # Session automatically persisted to user_data_dir
        print(f"Session saved to {user_data_dir}")
        return True

    finally:
        await browser.close()


def run_session_creation(output_path: str | None = None) -> bool:
    """
    Create session via interactive login using persistent browser context.

    Args:
        output_path: Path to browser profile directory. Defaults to ~/.linkedin-mcp/browser-profile

    Returns:
        True if session was created successfully
    """
    # Expand ~ in path
    if output_path:
        user_data_dir = Path(output_path).expanduser()
    else:
        user_data_dir = DEFAULT_USER_DATA_DIR

    print("LinkedIn MCP Server - Session Creation")
    print(f"   Browser profile will be saved to: {user_data_dir}")

    try:
        success = asyncio.run(interactive_login_and_save(user_data_dir))
        return success
    except Exception as e:
        print(f"Session creation failed: {e}")
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
        return asyncio.run(interactive_login_and_save())
    except Exception as e:
        print(f"Login failed: {e}")
        return False
