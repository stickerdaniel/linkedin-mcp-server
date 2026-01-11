"""
Interactive setup flows for LinkedIn MCP Server authentication.

Handles session creation through interactive browser login using Playwright.
Uses linkedin_scraper v3's wait_for_manual_login for authentication.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from linkedin_scraper import BrowserManager, wait_for_manual_login

from linkedin_mcp_server.drivers.browser import DEFAULT_SESSION_PATH

logger = logging.getLogger(__name__)


async def interactive_login_and_save(session_path: Optional[Path] = None) -> bool:
    """
    Open browser for manual LinkedIn login and save session.

    Opens a non-headless browser, navigates to LinkedIn login page,
    and waits for user to complete authentication (including 2FA, captcha, etc.).

    Args:
        session_path: Path to save session. Defaults to ~/.linkedin-mcp/session.json

    Returns:
        True if login was successful and session was saved

    Raises:
        Exception: If login fails or times out
    """
    if session_path is None:
        session_path = DEFAULT_SESSION_PATH

    print("🔗 Opening browser for LinkedIn login...")
    print("   Please log in manually. You have 5 minutes to complete authentication.")
    print("   (This handles 2FA, captcha, and any security challenges)")

    async with BrowserManager(headless=False) as browser:
        # Navigate to LinkedIn login
        await browser.page.goto("https://www.linkedin.com/login")

        # Wait for manual login completion
        # 5 minute timeout (300000ms) allows time for 2FA, captcha, security challenges
        await wait_for_manual_login(browser.page, timeout=300000)

        # Save session for future use
        session_path.parent.mkdir(parents=True, exist_ok=True)
        await browser.save_session(str(session_path))

        print(f"✅ Session saved to {session_path}")
        return True


def run_session_creation(output_path: Optional[str] = None) -> bool:
    """
    Create session via interactive login and save to file.

    Args:
        output_path: Path to save session file. Defaults to ~/.linkedin-mcp/session.json

    Returns:
        True if session was created successfully
    """
    # Expand ~ in path
    if output_path:
        session_path = Path(output_path).expanduser()
    else:
        session_path = DEFAULT_SESSION_PATH

    print("🔗 LinkedIn MCP Server - Session Creation")
    print(f"   Session will be saved to: {session_path}")

    try:
        success = asyncio.run(interactive_login_and_save(session_path))
        return success
    except Exception as e:
        print(f"❌ Session creation failed: {e}")
        return False


def run_interactive_setup() -> bool:
    """
    Run interactive setup - browser login only.

    Returns:
        True if setup completed successfully
    """
    print("🔗 LinkedIn MCP Server Setup")
    print("   Opening browser for manual login...")

    try:
        return asyncio.run(interactive_login_and_save())
    except Exception as e:
        print(f"❌ Login failed: {e}")
        return False
