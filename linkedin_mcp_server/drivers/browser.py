"""
Playwright browser management for LinkedIn scraping.

This module provides async browser lifecycle management using linkedin_scraper v3's
BrowserManager. Implements a singleton pattern for browser reuse across tool calls
with session persistence via JSON files.
"""

import logging
from pathlib import Path

from linkedin_scraper import (
    AuthenticationError,
    is_logged_in,
    login_with_cookie,
)
from linkedin_scraper.core import detect_rate_limit

from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.drivers.persistent_browser import PersistentBrowserManager
from linkedin_mcp_server.utils import get_linkedin_cookie

logger = logging.getLogger(__name__)


# Default browser profile directory for persistent context
DEFAULT_USER_DATA_DIR = Path.home() / ".linkedin-mcp" / "browser-profile"

# Legacy session file location (for migration)
LEGACY_SESSION_PATH = Path.home() / ".linkedin-mcp" / "session.json"

# Global browser instance (singleton)
_browser: PersistentBrowserManager | None = None
_headless: bool = True


def _apply_browser_settings(browser: PersistentBrowserManager) -> None:
    """Apply configuration settings to browser instance."""
    config = get_config()
    browser.page.set_default_timeout(config.browser.default_timeout)


async def get_or_create_browser(
    headless: bool | None = None,
    user_data_dir: Path | None = None,
) -> PersistentBrowserManager:
    """
    Get existing browser or create and initialize a new one.

    Uses a singleton pattern to reuse the browser across tool calls.
    Session is automatically maintained in user_data_dir.

    Args:
        headless: Run browser in headless mode. Defaults to config value.
        user_data_dir: Browser profile directory. Defaults to ~/.linkedin-mcp/browser-profile

    Returns:
        Initialized PersistentBrowserManager instance
    """
    global _browser, _headless

    if headless is not None:
        _headless = headless

    if user_data_dir is None:
        config = get_config()
        user_data_dir = Path(config.browser.user_data_dir).expanduser()

    if _browser is not None:
        return _browser

    config = get_config()
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
        "Creating persistent browser context (headless=%s, slow_mo=%sms, user_data_dir=%s)",
        _headless,
        config.browser.slow_mo,
        user_data_dir,
    )
    _browser = PersistentBrowserManager(
        user_data_dir=user_data_dir,
        headless=_headless,
        slow_mo=config.browser.slow_mo,
        user_agent=config.browser.user_agent,
        viewport=viewport,
        **launch_options,
    )
    await _browser.start()

    # Navigate to LinkedIn to check if already logged in
    await _browser.page.goto("https://www.linkedin.com/feed/")

    if await is_logged_in(_browser.page):
        logger.info("Already authenticated from persistent context")
        _apply_browser_settings(_browser)
        return _browser

    # Not logged in - try cookie authentication
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
        logger.info("Closing browser...")
        await _browser.close()
        _browser = None
        logger.info("Browser closed")


def profile_exists(user_data_dir: Path | None = None) -> bool:
    """Check if browser profile directory exists and has data."""
    if user_data_dir is None:
        config = get_config()
        user_data_dir = Path(config.browser.user_data_dir).expanduser()

    # Check if directory exists
    if not user_data_dir.exists():
        return False

    # Check for common Chromium profile files indicating it has been used
    profile_indicators = [
        "Preferences",
        "Local State",
        "Cookies",
        "Default/Preferences",
    ]

    return any((user_data_dir / indicator).exists() for indicator in profile_indicators)


def session_exists(session_path: Path | None = None) -> bool:
    """
    Legacy function for backward compatibility.

    Deprecated: Use profile_exists() instead.
    """
    if session_path is None:
        session_path = LEGACY_SESSION_PATH
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


def needs_migration() -> bool:
    """Check if migration from legacy session.json is needed."""
    return LEGACY_SESSION_PATH.exists() and not profile_exists()


async def migrate_from_legacy_session() -> bool:
    """
    Migrate from legacy session.json to persistent browser context.

    Attempts to load the old session file, extract cookies, and save them
    to the new persistent context. Backs up the old session file on success.

    Returns:
        True if migration successful, False otherwise
    """
    if not LEGACY_SESSION_PATH.exists():
        logger.info("No legacy session file to migrate")
        return False

    logger.info("Migrating from legacy session.json to persistent browser profile...")

    try:
        # Import BrowserManager only for migration
        from linkedin_scraper import BrowserManager

        # Create temporary browser with old session.json
        logger.info("Loading legacy session file...")
        temp_browser = BrowserManager(headless=True)
        await temp_browser.start()
        await temp_browser.load_session(str(LEGACY_SESSION_PATH))

        # Check if session is still valid
        if not await is_logged_in(temp_browser.page):
            logger.warning("Legacy session expired - migration not possible")
            await temp_browser.close()
            return False

        logger.info("Legacy session is valid, migrating to persistent context...")

        # Get user data directory from config
        config = get_config()
        user_data_dir = Path(config.browser.user_data_dir).expanduser()
        user_data_dir.parent.mkdir(parents=True, exist_ok=True)

        # Create persistent context
        persistent = PersistentBrowserManager(
            user_data_dir=user_data_dir, headless=True
        )
        await persistent.start()

        # Copy cookies from old session to new persistent context
        storage_state = await temp_browser.context.storage_state()
        # Type ignore: storage_state cookies are compatible with add_cookies
        await persistent.context.add_cookies(storage_state["cookies"])  # type: ignore[arg-type]

        # Verify migration by checking login status
        await persistent.page.goto("https://www.linkedin.com/feed/")
        if await is_logged_in(persistent.page):
            logger.info("Migration successful!")

            # Cleanup
            await persistent.close()
            await temp_browser.close()

            # Backup old session file
            import shutil

            backup_path = LEGACY_SESSION_PATH.with_suffix(".json.backup")
            shutil.move(str(LEGACY_SESSION_PATH), str(backup_path))
            logger.info(f"Legacy session backed up to {backup_path}")

            return True
        else:
            logger.error("Migration failed - session not valid in new profile")
            await persistent.close()
            await temp_browser.close()
            return False

    except Exception as e:
        logger.error(f"Migration error: {e}")
        return False
