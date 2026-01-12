"""
Playwright browser management for LinkedIn scraping.

This module provides async browser lifecycle management using linkedin_scraper v3's
BrowserManager. Implements a singleton pattern for browser reuse across tool calls
with session persistence via JSON files.
"""

import logging
from pathlib import Path
from typing import cast

from linkedin_scraper import (
    AuthenticationError,
    BrowserManager,
    is_logged_in,
    login_with_cookie,
)
from linkedin_scraper.core import detect_rate_limit

from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.utils import get_linkedin_cookie

logger = logging.getLogger(__name__)


# Default session file location
DEFAULT_SESSION_PATH = Path.home() / ".linkedin-mcp" / "session.json"

# Global browser instance (singleton)
_browser: BrowserManager | None = None
_headless: bool = True


def _apply_browser_settings(browser: BrowserManager) -> None:
    """Apply configuration settings to browser instance."""
    config = get_config()
    browser.page.set_default_timeout(config.browser.default_timeout)


async def get_or_create_browser(
    headless: bool | None = None,
    session_path: Path | None = None,
) -> BrowserManager:
    """
    Get existing browser or create and initialize a new one.

    Uses a singleton pattern to reuse the browser across tool calls.
    Loads session from file if available.

    Args:
        headless: Run browser in headless mode. Defaults to config value.
        session_path: Path to session file. Defaults to ~/.linkedin-mcp/session.json

    Returns:
        Initialized BrowserManager instance
    """
    global _browser, _headless

    if headless is not None:
        _headless = headless

    if session_path is None:
        session_path = DEFAULT_SESSION_PATH

    if _browser is not None:
        return cast(BrowserManager, _browser)

    config = get_config()
    viewport = {
        "width": config.browser.viewport_width,
        "height": config.browser.viewport_height,
    }
    logger.info(f"Creating new browser (headless={_headless})")
    _browser = BrowserManager(
        headless=_headless,
        slow_mo=config.browser.slow_mo,
        user_agent=config.browser.user_agent,
        viewport=viewport,
    )
    await _browser.start()

    # Priority 1: Load session file if available
    if session_path.exists():
        try:
            await _browser.load_session(str(session_path))
            logger.info(f"Loaded session from {session_path}")
            # Navigate to LinkedIn to validate session
            await _browser.page.goto("https://www.linkedin.com/feed/")
            if await is_logged_in(_browser.page):
                _apply_browser_settings(_browser)
                return _browser
            logger.warning(
                "Session loaded but expired, trying to create session from cookie"
            )
        except Exception as e:
            logger.warning(f"Failed to load session: {e}")

    # Priority 2: Use cookie from environment
    if cookie := get_linkedin_cookie():
        try:
            await login_with_cookie(_browser.page, cookie)
            logger.info("Authenticated using LINKEDIN_COOKIE")
            _apply_browser_settings(_browser)
            return _browser
        except Exception as e:
            logger.warning(f"Cookie authentication failed: {e}")

    # No auth available - fail fast with clear error
    raise AuthenticationError(
        "No authentication found. Run with --get-session to create a session."
    )


async def close_browser() -> None:
    """Close the browser and cleanup resources."""
    global _browser

    if _browser is not None:
        browser = cast(BrowserManager, _browser)
        logger.info("Closing browser...")
        await browser.close()
        _browser = None
        logger.info("Browser closed")


def session_exists(session_path: Path | None = None) -> bool:
    """Check if a session file exists."""
    if session_path is None:
        session_path = DEFAULT_SESSION_PATH
    return session_path.exists()


def set_headless(headless: bool) -> None:
    """Set headless mode for future browser creation."""
    global _headless
    _headless = headless


async def validate_session() -> bool:
    """
    Check if the current session is still valid (logged in).

    Returns:
        True if session is valid and user is logged in
    """
    browser = await get_or_create_browser()
    return await is_logged_in(browser.page)


async def ensure_authenticated() -> None:
    """
    Validate session and raise if expired.

    Raises:
        AuthenticationError: If session is expired or invalid
    """
    if not await validate_session():
        raise AuthenticationError("Session expired or invalid.")


async def check_rate_limit() -> None:
    """
    Proactively check for rate limiting.

    Should be called after navigation to detect if LinkedIn is blocking requests.

    Raises:
        RateLimitError: If rate limiting is detected
    """
    browser = await get_or_create_browser()
    await detect_rate_limit(browser.page)


def reset_browser_for_testing() -> None:
    """Reset global browser state for test isolation."""
    global _browser, _headless
    _browser = None
    _headless = True
