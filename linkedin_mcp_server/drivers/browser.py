"""
Patchright browser management for LinkedIn scraping.

Provides async browser lifecycle management using BrowserManager with persistent
context. Implements a singleton pattern for browser reuse across tool calls with
automatic profile persistence.
"""

import logging
import os
from pathlib import Path

from linkedin_mcp_server.core import (
    AuthenticationError,
    BrowserManager,
    detect_auth_barrier_quick,
    detect_rate_limit,
    is_logged_in,
    resolve_remember_me_prompt,
)

from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.debug_trace import record_page_trace
from linkedin_mcp_server.debug_utils import stabilize_navigation
from linkedin_mcp_server.session_state import (
    SourceState,
    clear_runtime_profile,
    get_runtime_id,
    get_source_profile_dir,
    load_runtime_state,
    load_source_state,
    portable_cookie_path,
    profile_exists as session_profile_exists,
    runtime_profile_dir,
    runtime_storage_state_path,
    write_runtime_state,
)

logger = logging.getLogger(__name__)


# Default persistent profile directory
DEFAULT_PROFILE_DIR = Path.home() / ".linkedin-mcp" / "profile"
# Global browser instance (singleton)
_browser: BrowserManager | None = None
_browser_cookie_export_path: Path | None = None
_headless: bool = True


def _debug_skip_checkpoint_restart() -> bool:
    """Return whether to keep the fresh bridged browser alive for this run."""
    return os.getenv("LINKEDIN_DEBUG_SKIP_CHECKPOINT_RESTART", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _debug_bridge_every_startup() -> bool:
    """Return whether to force a fresh bridge on every foreign-runtime startup."""
    return os.getenv("LINKEDIN_DEBUG_BRIDGE_EVERY_STARTUP", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def experimental_persist_derived_runtime() -> bool:
    """Return whether Docker-style foreign runtimes should reuse derived profiles."""
    return os.getenv(
        "LINKEDIN_EXPERIMENTAL_PERSIST_DERIVED_SESSION", ""
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _apply_browser_settings(browser: BrowserManager) -> None:
    """Apply configuration settings to browser instance."""
    config = get_config()
    browser.page.set_default_timeout(config.browser.default_timeout)


async def _log_feed_failure_context(
    browser: BrowserManager,
    reason: str,
    exc: Exception | None = None,
) -> None:
    """Log the page state when /feed/ validation fails."""
    page = browser.page

    try:
        title = await page.title()
    except Exception:
        title = ""

    try:
        remember_me = (await page.locator("#rememberme-div").count()) > 0
    except Exception:
        remember_me = False

    try:
        body_text = await page.evaluate("() => document.body?.innerText || ''")
    except Exception:
        body_text = ""

    if not isinstance(body_text, str):
        body_text = ""

    logger.warning(
        "Feed auth check failed on %s: %s title=%r remember_me=%s body_marker=%r",
        page.url,
        reason,
        title,
        remember_me,
        " ".join(body_text.split())[:200],
        exc_info=exc,
    )


async def _feed_auth_succeeds(
    browser: BrowserManager,
    *,
    allow_remember_me: bool = True,
) -> bool:
    """Validate that /feed/ loads without an auth barrier."""
    try:
        await browser.page.goto(
            "https://www.linkedin.com/feed/",
            wait_until="domcontentloaded",
        )
        await stabilize_navigation("feed navigation", logger)
        await record_page_trace(
            browser.page,
            "feed-after-goto",
            extra={"allow_remember_me": allow_remember_me},
        )
        if allow_remember_me:
            if await resolve_remember_me_prompt(browser.page):
                await stabilize_navigation("remember-me resolution", logger)
                await record_page_trace(
                    browser.page,
                    "feed-after-remember-me",
                    extra={"allow_remember_me": allow_remember_me},
                )
        barrier = await detect_auth_barrier_quick(browser.page)
        if barrier is not None:
            await record_page_trace(
                browser.page,
                "feed-auth-barrier",
                extra={"barrier": barrier},
            )
            await _log_feed_failure_context(browser, barrier)
            return False
        return True
    except Exception as exc:
        if allow_remember_me and await resolve_remember_me_prompt(browser.page):
            await stabilize_navigation(
                "remember-me resolution after feed failure", logger
            )
            await record_page_trace(
                browser.page,
                "feed-after-remember-me-error-recovery",
                extra={"error": f"{type(exc).__name__}: {exc}"},
            )
            barrier = await detect_auth_barrier_quick(browser.page)
            if barrier is None:
                return True
        await record_page_trace(
            browser.page,
            "feed-navigation-error",
            extra={"error": f"{type(exc).__name__}: {exc}"},
        )
        await _log_feed_failure_context(browser, str(exc), exc)
        return False


def _launch_options() -> tuple[dict[str, str], dict[str, int], object]:
    config = get_config()
    viewport = {
        "width": config.browser.viewport_width,
        "height": config.browser.viewport_height,
    }
    launch_options: dict[str, str] = {}
    if config.browser.chrome_path:
        launch_options["executable_path"] = config.browser.chrome_path
        logger.info("Using custom Chrome path: %s", config.browser.chrome_path)
    return launch_options, viewport, config


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


async def _authenticate_existing_profile(
    profile_dir: Path,
    *,
    launch_options: dict[str, str],
    viewport: dict[str, int],
) -> BrowserManager:
    browser = _make_browser(
        profile_dir, launch_options=launch_options, viewport=viewport
    )
    await browser.start()
    try:
        if not await _feed_auth_succeeds(browser):
            raise AuthenticationError(
                f"Stored runtime profile is invalid: {profile_dir}. Run with --login to refresh the source session."
            )
        browser.is_authenticated = True
        return browser
    except Exception:
        await browser.close()
        raise


async def _bridge_runtime_profile(
    profile_dir: Path,
    *,
    cookie_path: Path,
    source_state: SourceState,
    runtime_id: str,
    launch_options: dict[str, str],
    viewport: dict[str, int],
    persist_runtime: bool,
    cookie_preset: str = "auth_minimal",
) -> BrowserManager:
    clear_runtime_profile(runtime_id, get_source_profile_dir())
    profile_dir.parent.mkdir(parents=True, exist_ok=True)
    storage_state_path = runtime_storage_state_path(
        runtime_id, get_source_profile_dir()
    )
    browser = _make_browser(
        profile_dir, launch_options=launch_options, viewport=viewport
    )
    await browser.start()
    await record_page_trace(
        browser.page,
        "bridge-browser-started",
        extra={"profile_dir": str(profile_dir)},
    )
    try:
        await browser.page.goto(
            "https://www.linkedin.com/feed/", wait_until="domcontentloaded"
        )
        await stabilize_navigation("pre-import feed navigation", logger)
        await record_page_trace(browser.page, "bridge-after-pre-import-feed")
        if not await browser.import_cookies(cookie_path, preset_name=cookie_preset):
            raise AuthenticationError(
                "Portable authentication could not be imported. Run with --login to create a fresh source session."
            )
        await stabilize_navigation("bridge cookie import", logger)
        await record_page_trace(
            browser.page,
            "bridge-after-cookie-import",
            extra={"cookie_path": str(cookie_path)},
        )
        if not await _feed_auth_succeeds(browser):
            raise AuthenticationError(
                "No authentication found. Run with --login to create a profile."
            )
        await stabilize_navigation("post-import feed validation", logger)
        await record_page_trace(browser.page, "bridge-after-feed-validation")
        if not persist_runtime:
            logger.info(
                "Foreign runtime %s authenticated via fresh bridge "
                "(derived runtime persistence disabled)",
                runtime_id,
            )
            browser.is_authenticated = True
            return browser
        if _debug_skip_checkpoint_restart():
            logger.warning(
                "Skipping checkpoint restart for derived runtime profile %s "
                "(LINKEDIN_DEBUG_SKIP_CHECKPOINT_RESTART enabled)",
                profile_dir,
            )
            browser.is_authenticated = True
            return browser
        if not await browser.export_storage_state(storage_state_path, indexed_db=True):
            raise AuthenticationError(
                "Derived runtime session could not be checkpointed. Run with --login to create a fresh source session."
            )
        await stabilize_navigation("runtime storage-state export", logger)
        logger.info("Checkpoint-restarting derived runtime profile %s", profile_dir)
        await browser.close()
        reopened = _make_browser(
            profile_dir,
            launch_options=launch_options,
            viewport=viewport,
        )
        await reopened.start()
        await stabilize_navigation("derived profile reopen", logger)
        await record_page_trace(
            reopened.page,
            "bridge-after-profile-reopen",
            extra={"profile_dir": str(profile_dir)},
        )
        try:
            if not await _feed_auth_succeeds(reopened):
                logger.warning(
                    "Stored derived runtime profile failed post-commit validation"
                )
                raise AuthenticationError(
                    "Derived runtime validation failed; no automatic re-bridge will be attempted. Run with --login to create a fresh source session."
                )
            await stabilize_navigation("post-reopen feed validation", logger)
            await record_page_trace(reopened.page, "bridge-after-reopen-validation")
            write_runtime_state(
                runtime_id,
                source_state,
                storage_state_path,
                get_source_profile_dir(),
            )
            logger.info("Derived runtime profile committed for %s", runtime_id)
            reopened.is_authenticated = True
            return reopened
        except Exception:
            await reopened.close()
            raise
    except Exception:
        await browser.close()
        clear_runtime_profile(runtime_id, get_source_profile_dir())
        raise


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
    global _browser, _browser_cookie_export_path, _headless

    if headless is not None:
        _headless = headless

    if _browser is not None:
        return _browser

    launch_options, viewport, config = _launch_options()
    source_profile_dir = get_profile_dir()
    cookie_path = portable_cookie_path(source_profile_dir)
    source_state = load_source_state(source_profile_dir)
    if (
        not source_state
        or not profile_exists(source_profile_dir)
        or not cookie_path.exists()
    ):
        raise AuthenticationError(
            "No source authentication found. Run with --login to create a profile."
        )

    current_runtime_id = get_runtime_id()

    if current_runtime_id == source_state.source_runtime_id:
        logger.info(
            "Using source profile for runtime %s (profile=%s)",
            current_runtime_id,
            source_profile_dir,
        )
        browser = await _authenticate_existing_profile(
            source_profile_dir,
            launch_options=launch_options,
            viewport=viewport,
        )
        _apply_browser_settings(browser)
        _browser = browser
        _browser_cookie_export_path = cookie_path
        return _browser

    persist_runtime = experimental_persist_derived_runtime()
    force_bridge = _debug_bridge_every_startup()

    if not persist_runtime:
        logger.info(
            "Using fresh bridge for foreign runtime %s "
            "(derived runtime persistence disabled by default)",
            current_runtime_id,
        )
        browser = await _bridge_runtime_profile(
            runtime_profile_dir(current_runtime_id, source_profile_dir),
            cookie_path=cookie_path,
            source_state=source_state,
            runtime_id=current_runtime_id,
            launch_options=launch_options,
            viewport=viewport,
            persist_runtime=False,
            cookie_preset="auth_minimal",
        )
        _apply_browser_settings(browser)
        _browser = browser
        _browser_cookie_export_path = None
        return _browser

    runtime_state = load_runtime_state(current_runtime_id, source_profile_dir)
    derived_profile_dir = runtime_profile_dir(current_runtime_id, source_profile_dir)
    storage_state_path = runtime_storage_state_path(
        current_runtime_id, source_profile_dir
    )
    generation_matches = (
        runtime_state is not None
        and runtime_state.source_login_generation == source_state.login_generation
    )
    if (
        not force_bridge
        and generation_matches
        and profile_exists(derived_profile_dir)
        and storage_state_path.exists()
    ):
        logger.info(
            "Using derived runtime profile for %s (profile=%s)",
            current_runtime_id,
            derived_profile_dir,
        )
        browser = await _authenticate_existing_profile(
            derived_profile_dir,
            launch_options=launch_options,
            viewport=viewport,
        )
        _apply_browser_settings(browser)
        _browser = browser
        _browser_cookie_export_path = None
        return _browser

    if force_bridge:
        logger.warning(
            "Forcing a fresh bridge for %s on every startup "
            "(LINKEDIN_DEBUG_BRIDGE_EVERY_STARTUP enabled)",
            current_runtime_id,
        )
    logger.info(
        "Deriving runtime profile for %s from source generation %s",
        current_runtime_id,
        source_state.login_generation,
    )
    browser = await _bridge_runtime_profile(
        derived_profile_dir,
        cookie_path=cookie_path,
        source_state=source_state,
        runtime_id=current_runtime_id,
        launch_options=launch_options,
        viewport=viewport,
        persist_runtime=True,
        cookie_preset="auth_minimal",
    )
    _apply_browser_settings(browser)
    _browser = browser
    _browser_cookie_export_path = None
    return _browser


async def close_browser() -> None:
    """Close the browser and cleanup resources."""
    global _browser, _browser_cookie_export_path

    browser = _browser
    cookie_export_path = _browser_cookie_export_path
    _browser = None
    _browser_cookie_export_path = None

    if browser is None:
        return

    logger.info("Closing browser...")
    if cookie_export_path is not None:
        try:
            await browser.export_cookies(cookie_export_path)
        except Exception:
            logger.debug("Cookie export on close skipped", exc_info=True)
    await browser.close()
    logger.info("Browser closed")


def get_profile_dir() -> Path:
    """Get the resolved profile directory from config."""
    return get_source_profile_dir()


def profile_exists(profile_dir: Path | None = None) -> bool:
    """Check if a persistent browser profile exists and is non-empty."""
    return session_profile_exists(profile_dir or get_profile_dir())


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
    if browser.is_authenticated:
        return True
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
    global _browser, _browser_cookie_export_path, _headless
    _browser = None
    _browser_cookie_export_path = None
    _headless = True
