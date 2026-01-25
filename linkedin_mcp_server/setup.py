"""
Interactive setup flows for LinkedIn MCP Server authentication.

Handles session creation through interactive browser login using Playwright.
Uses linkedin_scraper v3's wait_for_manual_login for authentication.
"""

import asyncio
import logging
from pathlib import Path

from linkedin_scraper import BrowserManager, wait_for_manual_login
from linkedin_scraper.core import warm_up_browser

from linkedin_mcp_server.drivers.browser import DEFAULT_SESSION_PATH

logger = logging.getLogger(__name__)


async def interactive_login_and_save(
    session_path: Path | None = None, warm_up: bool = True
) -> bool:
    """
    Open browser for manual LinkedIn login and save session.

    Opens a non-headless browser, navigates to LinkedIn login page,
    and waits for user to complete authentication (including 2FA, captcha, etc.).

    Args:
        session_path: Path to save session. Defaults to ~/.linkedin-mcp/session.json
        warm_up: Visit normal sites first to appear more human-like (default: True)

    Returns:
        True if login was successful and session was saved

    Raises:
        Exception: If login fails or times out
    """
    if session_path is None:
        session_path = DEFAULT_SESSION_PATH

    logger.info(f"Starting session creation - target path: {session_path}")
    logger.info(f"Session path (absolute): {session_path.resolve()}")
    logger.info(f"Parent directory: {session_path.parent.resolve()}")

    print("Opening browser for LinkedIn login...")
    print("   Please log in manually. You have 5 minutes to complete authentication.")
    print("   (This handles 2FA, captcha, and any security challenges)")

    async with BrowserManager(headless=False) as browser:
        # Warm up browser to appear more human-like and avoid security checkpoints
        if warm_up:
            print("   Warming up browser (visiting normal sites first)...")
            logger.debug("Starting browser warm-up")
            await warm_up_browser(browser.page)
            logger.debug("Browser warm-up completed")

        # Navigate to LinkedIn login
        logger.info("Navigating to LinkedIn login page")
        await browser.page.goto("https://www.linkedin.com/login")
        logger.info(f"Loaded page: {browser.page.url}")

        # Wait for manual login completion
        # 5 minute timeout (300000ms) allows time for 2FA, captcha, security challenges
        logger.info("Waiting for manual login (timeout: 5 minutes)")
        print("   ⏳ Waiting for you to log in...")

        try:
            await wait_for_manual_login(browser.page, timeout=300000)
            logger.info(f"✓ Login detected! Current URL: {browser.page.url}")
            print("   ✓ Login successful")
        except Exception as e:
            # Enhanced error info for debugging
            current_url = browser.page.url
            try:
                page_title = await browser.page.title()
            except Exception:
                page_title = "(could not get title)"

            logger.error(f"Login failed at URL: {current_url}")
            logger.error(f"Page title: {page_title}")
            logger.error(f"Error: {e}")

            print("   ❌ Login failed")
            print(f"   Current URL: {current_url}")
            print(f"   Page title: {page_title}")
            raise

        # Save session for future use
        logger.info(f"Creating parent directory: {session_path.parent}")
        session_path.parent.mkdir(parents=True, exist_ok=True)

        if not session_path.parent.exists():
            raise RuntimeError(f"Failed to create directory: {session_path.parent}")

        logger.info(f"Saving session to: {session_path}")
        logger.debug(f"Session path as string: {str(session_path)}")

        try:
            await browser.save_session(str(session_path))
            logger.info("Session save completed")
        except Exception as e:
            logger.error(f"Failed to save session: {e}")
            raise

        # Verify session file was created
        if not session_path.exists():
            raise RuntimeError(
                f"Session file was not created at {session_path}. "
                f"Parent directory exists: {session_path.parent.exists()}"
            )

        file_size = session_path.stat().st_size
        logger.info(f"Session file created successfully ({file_size} bytes)")
        print(f"   ✓ Session saved to {session_path} ({file_size} bytes)")

        return True


def run_session_creation(output_path: str | None = None) -> bool:
    """
    Create session via interactive login and save to file.

    Args:
        output_path: Path to save session file. Defaults to ~/.linkedin-mcp/session.json

    Returns:
        True if session was created successfully
    """
    # Expand ~ in path
    if output_path:
        logger.debug(f"Expanding output_path: {output_path}")
        session_path = Path(output_path).expanduser()
        logger.debug(f"Expanded to: {session_path}")
    else:
        session_path = DEFAULT_SESSION_PATH
        logger.debug(f"Using DEFAULT_SESSION_PATH: {session_path}")

    print("🔗 LinkedIn MCP Server - Session Creation")
    print(f"   Session will be saved to: {session_path}")
    logger.info(f"Session creation requested - output_path: {output_path}")
    logger.info(
        f"Final session_path: {session_path} (absolute: {session_path.resolve()})"
    )

    try:
        success = asyncio.run(interactive_login_and_save(session_path))
        if success:
            print("✅ Session creation completed successfully!")
        return success
    except KeyboardInterrupt:
        print("\n\n❌ Session creation cancelled by user")
        logger.info("Session creation cancelled by user")
        return False
    except Exception as e:
        logger.error(f"Session creation failed with exception: {e}", exc_info=True)
        print(f"\n❌ Session creation failed: {e}")

        # Provide helpful diagnostic info
        if "timeout" in str(e).lower():
            print("\nTroubleshooting:")
            print("  - Make sure you complete the LinkedIn login within 5 minutes")
            print("  - If you see a security checkpoint, complete it before timeout")
            print("  - Try running with --log-level DEBUG for more details")
        elif "permission" in str(e).lower() or "denied" in str(e).lower():
            print("\nTroubleshooting:")
            print(
                f"  - Check that you have write permissions to: {session_path.parent}"
            )
            print("  - Try creating the directory manually")
        else:
            print("\nFor detailed logs, run with: --log-level DEBUG")

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
