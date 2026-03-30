"""Patchright browser lifecycle for LinkedIn scraping."""

import logging
from pathlib import Path

from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.core import (
    AuthenticationError,
    BrowserManager,
    detect_auth_barrier_quick,
    detect_rate_limit,
    is_logged_in,
    resolve_remember_me_prompt,
)
from linkedin_mcp_server.debug_trace import record_page_trace
from linkedin_mcp_server.debug_utils import stabilize_navigation
from linkedin_mcp_server.session_state import (
    get_source_profile_dir,
    load_source_state,
    portable_cookie_path,
)
from linkedin_mcp_server.session_state import (
    profile_exists as session_profile_exists,
)

logger = logging.getLogger(__name__)

DEFAULT_PROFILE_DIR = Path.home() / ".linkedin-mcp" / "profile"
_browser: BrowserManager | None = None
_headless: bool = True


def _apply_browser_settings(browser: BrowserManager) -> None:
    config = get_config()
    browser.page.set_default_timeout(config.browser.default_timeout)


def _launch_options() -> tuple[dict[str, str], dict[str, int]]:
    config = get_config()
    viewport = {
        "width": config.browser.viewport_width,
        "height": config.browser.viewport_height,
    }
    launch_options: dict[str, str] = {}
    if config.browser.chrome_path:
        launch_options["executable_path"] = config.browser.chrome_path
        logger.info("Using custom Chrome path: %s", config.browser.chrome_path)
    return launch_options, viewport


def _make_browser(
    profile_dir: Path,
    *,
    launch_options: dict[str, str],
    viewport: dict[str, int],
) -> BrowserManager:
    config = get_config()
    return BrowserManager(
        user_data_dir=profile_dir,
        headless=_headless,
        slow_mo=config.browser.slow_mo,
        user_agent=config.browser.user_agent,
        viewport=viewport,
        **launch_options,
    )


async def _feed_auth_succeeds(browser: BrowserManager) -> bool:
    try:
        await browser.page.goto(
            "https://www.linkedin.com/feed/",
            wait_until="domcontentloaded",
        )
        await stabilize_navigation("feed navigation", logger)
        if await resolve_remember_me_prompt(browser.page):
            await stabilize_navigation("remember-me resolution", logger)
            await browser.page.goto(
                "https://www.linkedin.com/feed/",
                wait_until="domcontentloaded",
            )
            await stabilize_navigation("post-remember-me feed", logger)
        barrier = await detect_auth_barrier_quick(browser.page)
        if barrier is not None:
            await record_page_trace(browser.page, "feed-auth-barrier", extra={"barrier": barrier})
            logger.warning("Feed auth check failed: %s url=%s", barrier, browser.page.url)
            return False
        return True
    except Exception as exc:
        if await resolve_remember_me_prompt(browser.page):
            await stabilize_navigation("remember-me after error", logger)
            await record_page_trace(
                browser.page,
                "feed-after-remember-me-error-recovery",
                extra={"error": f"{type(exc).__name__}: {exc}"},
            )
            await browser.page.goto(
                "https://www.linkedin.com/feed/",
                wait_until="domcontentloaded",
            )
            await stabilize_navigation("post-recovery feed", logger)
            barrier = await detect_auth_barrier_quick(browser.page)
            if barrier is None:
                return True
        logger.warning("Feed navigation failed: %s", exc, exc_info=exc)
        return False


async def get_or_create_browser(headless: bool | None = None) -> BrowserManager:
    global _browser, _headless
    if headless is not None:
        _headless = headless
    if _browser is not None:
        return _browser

    launch_options, viewport = _launch_options()
    profile_dir = get_profile_dir()
    cookie_path = portable_cookie_path(profile_dir)
    source_state = load_source_state(profile_dir)
    if not source_state or not profile_exists(profile_dir) or not cookie_path.exists():
        raise AuthenticationError(
            "No source authentication found. Run with --login to create a profile."
        )
    browser = _make_browser(profile_dir, launch_options=launch_options, viewport=viewport)
    try:
        await browser.start()
        await browser.import_cookies(cookie_path)
        if not await _feed_auth_succeeds(browser):
            raise AuthenticationError(
                "Stored profile is invalid. Run with --login to refresh the session."
            )
        browser.is_authenticated = True
    except Exception:
        await browser.close()
        raise
    _apply_browser_settings(browser)
    _browser = browser
    return _browser


async def adopt_browser(browser: BrowserManager) -> None:
    """Adopt a live browser as the singleton.

    Used after interactive login to keep the same browser session alive
    for tool execution, avoiding fingerprint mismatch from cookie replay.
    """
    global _browser
    if _browser is not None:
        await _browser.close()
    _browser = browser
    logger.info("Adopted live browser into singleton")


async def close_browser() -> None:
    global _browser
    browser = _browser
    _browser = None
    if browser is not None:
        logger.info("Closing browser...")
        await browser.close()
        logger.info("Browser closed")


def get_profile_dir() -> Path:
    return get_source_profile_dir()


def profile_exists(profile_dir: Path | None = None) -> bool:
    return session_profile_exists(profile_dir or get_profile_dir())


def set_headless(headless: bool) -> None:
    global _headless
    _headless = headless


async def validate_session() -> bool:
    browser = await get_or_create_browser()
    if browser.is_authenticated:
        return True
    return await is_logged_in(browser.page)


async def recover_session() -> bool:
    """Re-import bridge cookies and verify the session is alive.

    Called when an auth barrier appears mid-session. Re-injects cookies
    from the portable cookies.json (which preserves session-only cookies
    that Chromium's profile DB may have dropped) then re-checks the feed.
    """
    global _browser
    if _browser is None:
        return False

    cookie_path = portable_cookie_path(get_profile_dir())
    if not cookie_path.exists():
        logger.warning("No cookie file for session recovery at %s", cookie_path)
        return False

    imported = await _browser.import_cookies(cookie_path)
    if not imported:
        logger.warning("Cookie bridge import failed during session recovery")
        return False

    if await _feed_auth_succeeds(_browser):
        _browser.is_authenticated = True
        logger.info("Session recovered via cookie bridge")
        return True

    _browser.is_authenticated = False
    logger.warning("Session recovery failed after cookie import")
    return False


async def ensure_authenticated() -> None:
    if not await validate_session():
        raise AuthenticationError("Session expired or invalid.")


async def check_rate_limit() -> None:
    browser = await get_or_create_browser()
    await detect_rate_limit(browser.page)


def reset_browser_for_testing() -> None:
    global _browser, _headless
    _browser = None
    _headless = True
