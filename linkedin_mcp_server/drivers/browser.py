"""
Patchright browser management for LinkedIn scraping.

This module provides async browser lifecycle management using linkedin_scraper v3's
BrowserManager with persistent context. Implements a singleton pattern for browser
reuse across tool calls with automatic profile persistence.
"""

import logging
from pathlib import Path

from linkedin_scraper import (
    AuthenticationError,
    BrowserManager,
    is_logged_in,
)
from linkedin_scraper.core import detect_rate_limit

from linkedin_mcp_server.config import get_config

logger = logging.getLogger(__name__)


# Default persistent profile directory
DEFAULT_PROFILE_DIR = Path.home() / ".linkedin-mcp" / "profile"

# Global browser instance (singleton)
_browser: BrowserManager | None = None
_headless: bool = True


def _apply_browser_settings(browser: BrowserManager) -> None:
    """Apply configuration settings to browser instance."""
    config = get_config()
    browser.page.set_default_timeout(config.browser.default_timeout)


async def get_or_create_browser(
    headless: bool | None = None,
) -> BrowserManager:
    """
    Get existing browser or create and initialize a new one.

    Uses a singleton pattern to reuse the browser across tool calls.
    Uses persistent context for automatic profile persistence.

    Args:
        headless: Run browser in headless mode. Defaults to config value.

    Returns:
        Initialized BrowserManager instance

    Raises:
        AuthenticationError: If no valid authentication found
    """
    global _browser, _headless

    if headless is not None:
        _headless = headless

    if _browser is not None:
        return _browser

    config = get_config()
    user_data_dir = Path(config.browser.user_data_dir).expanduser()
    viewport = {
        "width": config.browser.viewport_width,
        "height": config.browser.viewport_height,
    }

    # Build launch options for custom browser path
    launch_options: dict[str, str] = {}
    if config.browser.chrome_path:
        launch_options["executable_path"] = config.browser.chrome_path
        logger.info("Using custom Chrome path: %s", config.browser.chrome_path)

    logger.info(
        "Creating new browser (headless=%s, slow_mo=%sms, viewport=%sx%s, profile=%s)",
        _headless,
        config.browser.slow_mo,
        viewport["width"],
        viewport["height"],
        user_data_dir,
    )
    browser = BrowserManager(
        user_data_dir=user_data_dir,
        headless=_headless,
        slow_mo=config.browser.slow_mo,
        user_agent=config.browser.user_agent,
        viewport=viewport,
        **launch_options,
    )
    await browser.start()

    # Navigate to LinkedIn to check authentication
    await browser.page.goto("https://www.linkedin.com/feed/")
    if await is_logged_in(browser.page):
        _apply_browser_settings(browser)
        _browser = browser  # Assign only after auth succeeds
        return _browser

    # Auth failed — try importing portable cookies (cross-platform support)
    logger.info("Native auth failed, attempting portable cookie import...")
    if await browser.import_cookies():
        await browser.page.goto("https://www.linkedin.com/feed/")
        if await is_logged_in(browser.page):
            logger.info("Authentication recovered via portable cookies")
            _apply_browser_settings(browser)
            _browser = browser
            return _browser

    # Auth failed — clean up and fail fast
    await browser.close()
    raise AuthenticationError(
        "No authentication found. Run with --get-session to create a profile."
    )


async def close_browser() -> None:
    """Close the browser and cleanup resources."""
    global _browser

    if _browser is not None:
        logger.info("Closing browser...")
        # Export cookies before closing to keep portable file fresh
        try:
            await _browser.export_cookies()
        except Exception:
            logger.debug("Cookie export on close skipped", exc_info=True)
        await _browser.close()
        _browser = None
        logger.info("Browser closed")


def get_profile_dir() -> Path:
    """Get the resolved profile directory from config."""
    config = get_config()
    return Path(config.browser.user_data_dir).expanduser()


def profile_exists(profile_dir: Path | None = None) -> bool:
    """Check if a persistent browser profile exists and is non-empty."""
    if profile_dir is None:
        profile_dir = get_profile_dir()
    return profile_dir.is_dir() and any(profile_dir.iterdir())


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
