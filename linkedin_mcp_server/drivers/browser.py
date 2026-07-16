"""
Patchright browser management for LinkedIn scraping.

Provides async browser lifecycle management using BrowserManager with persistent
context. Implements a singleton pattern for browser reuse across tool calls with
automatic profile persistence.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from linkedin_mcp_server.common_utils import harden_linkedin_tree, secure_mkdir
from linkedin_mcp_server.core.camoufox_identity import (
    CamoufoxIdentityError,
    load_camoufox_identity_sha256,
)
from linkedin_mcp_server.core import (
    AuthenticationError,
    BrowserManager,
    BrowserTeardownError,
    NetworkError,
    detect_auth_barrier_quick,
    detect_rate_limit,
    is_logged_in,
    resolve_remember_me_prompt,
    wait_for_session_resume_redirect,
)


from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.debug_trace import record_page_trace
from linkedin_mcp_server.debug_utils import stabilize_navigation
from linkedin_mcp_server.session_state import (
    SourceState,
    camoufox_identity_path as source_camoufox_identity_path,
    clear_runtime_instance,
    clear_runtime_profile,
    get_runtime_id,
    get_runtime_instance_id,
    get_source_profile_dir,
    load_source_state,
    portable_cookie_path,
    portable_cookie_is_valid,
    profile_exists as session_profile_exists,
    rotate_runtime_instance_id,
    runtime_profile_dir,
    source_session_lock,
)

logger = logging.getLogger(__name__)


# Default persistent profile directory
DEFAULT_PROFILE_DIR = Path.home() / ".linkedin-mcp" / "profile"
# Global browser instance (singleton)
_browser: BrowserManager | None = None
_browser_runtime_id: str | None = None
_browser_runtime_instance_id: str | None = None
_browser_lifecycle_lock = asyncio.Lock()
_browser_owner_pid = os.getpid()
_headless: bool = True


def _ensure_browser_process_state() -> None:
    """Discard fork-inherited browser/lock objects without touching the parent."""
    global _browser, _browser_runtime_id, _browser_runtime_instance_id
    global _browser_lifecycle_lock, _browser_owner_pid

    current_pid = os.getpid()
    if current_pid == _browser_owner_pid:
        return
    logger.warning(
        "Detected fork from browser owner PID %s to %s; discarding inherited "
        "driver references",
        _browser_owner_pid,
        current_pid,
    )
    # The inherited transport and asyncio.Lock belong to the parent's event
    # loop/process. Closing either copy could interfere with the live parent.
    _browser = None
    _browser_runtime_id = None
    _browser_runtime_instance_id = None
    _browser_lifecycle_lock = asyncio.Lock()
    _browser_owner_pid = current_pid
    rotate_runtime_instance_id()


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


_TRANSPORT_FAILURE_MARKERS = (
    "connection closed",
    "target closed",
    "target page, context or browser has been closed",
    "browser has been closed",
    "pipe closed",
)


def _is_linkedin_page_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme.lower() == "https"
        and port in {None, 443}
        and hostname in {"linkedin.com", "www.linkedin.com"}
    )


def _is_linkedin_feed_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme.lower() == "https"
        and port in {None, 443}
        and hostname in {"linkedin.com", "www.linkedin.com"}
        and (parsed.path == "/feed" or parsed.path.startswith("/feed/"))
    )


def _is_transport_failure(exc: Exception) -> bool:
    """Return whether *exc* signals a dead browser/driver connection.

    Distinct from a semantic "not logged in" result (a real auth barrier,
    or a benign timeout waiting for a selector): this fires only when the
    underlying browser process or driver connection itself died mid-call
    (e.g. a driver crash, a killed process, a severed pipe). Message-marker
    style, matching the existing classifiers in ``dependencies.py``
    (``_is_linux_browser_dependency_error``, ``_is_browser_binary_missing_error``).

    Callers must NOT treat this signal as "the session/cookies are invalid" —
    it says nothing about auth validity, only that the check was inconclusive.
    """
    message = str(exc).lower()
    return any(marker in message for marker in _TRANSPORT_FAILURE_MARKERS)


async def _feed_auth_succeeds(
    browser: BrowserManager,
    *,
    allow_remember_me: bool = True,
) -> bool:
    """Validate that /feed/ loads without an auth barrier.

    Raises :class:`NetworkError` instead of returning ``False`` when the
    failure is a dead browser/driver connection rather than a real auth
    signal -- callers must not treat that as "cookies rejected" (see
    :func:`_is_transport_failure`).
    """
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
        if not _is_linkedin_page_url(browser.page.url):
            # Never click an off-origin captive-portal/proxy form. It is
            # neither a LinkedIn auth rejection nor proof that /feed loaded.
            raise NetworkError(
                f"Feed validation was redirected outside LinkedIn: {browser.page.url}"
            )
        if allow_remember_me:
            if await resolve_remember_me_prompt(browser.page):
                await stabilize_navigation("remember-me resolution", logger)
                await record_page_trace(
                    browser.page,
                    "feed-after-remember-me",
                    extra={"allow_remember_me": allow_remember_me},
                )
                return await _feed_auth_succeeds(browser, allow_remember_me=False)
            if await wait_for_session_resume_redirect(browser.page):
                await stabilize_navigation("session-resume interstitial", logger)
                await record_page_trace(
                    browser.page,
                    "feed-after-session-resume-wait",
                    extra={"allow_remember_me": allow_remember_me},
                )
                return await _feed_auth_succeeds(browser, allow_remember_me=False)
        barrier = await detect_auth_barrier_quick(browser.page)
        if barrier is not None:
            await record_page_trace(
                browser.page,
                "feed-auth-barrier",
                extra={"barrier": barrier},
            )
            await _log_feed_failure_context(browser, barrier)
            return False
        if not _is_linkedin_feed_url(browser.page.url):
            raise NetworkError(
                "Feed validation did not reach the expected LinkedIn feed: "
                f"{browser.page.url}"
            )
        return True
    except Exception as exc:
        if isinstance(exc, NetworkError):
            raise
        if _is_transport_failure(exc):
            # The connection itself died -- recovery attempts (remember-me
            # click, page.title() etc.) would just hang/fail again. Skip
            # straight to raising so the caller treats this as inconclusive,
            # never as "LinkedIn rejected the session."
            logger.warning(
                "Feed auth check hit a transport failure on %s: %s",
                browser.page.url,
                exc,
            )
            raise NetworkError(f"Feed validation could not complete: {exc}") from exc
        if not _is_linkedin_page_url(browser.page.url):
            # Navigation may fail only after a proxy/captive portal redirected
            # the page. Never run recovery clicks against that foreign origin.
            raise NetworkError(
                f"Feed validation was redirected outside LinkedIn: {browser.page.url}"
            ) from exc
        if allow_remember_me and await resolve_remember_me_prompt(browser.page):
            await stabilize_navigation(
                "remember-me resolution after feed failure", logger
            )
            await record_page_trace(
                browser.page,
                "feed-after-remember-me-error-recovery",
                extra={"error": f"{type(exc).__name__}: {exc}"},
            )
            return await _feed_auth_succeeds(browser, allow_remember_me=False)
        if allow_remember_me and await wait_for_session_resume_redirect(browser.page):
            await stabilize_navigation(
                "session-resume interstitial after feed failure", logger
            )
            await record_page_trace(
                browser.page,
                "feed-after-session-resume-error-recovery",
                extra={"error": f"{type(exc).__name__}: {exc}"},
            )
            return await _feed_auth_succeeds(browser, allow_remember_me=False)
        await record_page_trace(
            browser.page,
            "feed-navigation-error",
            extra={"error": f"{type(exc).__name__}: {exc}"},
        )
        await _log_feed_failure_context(browser, str(exc), exc)
        # A thrown navigation/detector error is not evidence that LinkedIn
        # rejected the cookie. Only an observed auth barrier above may return
        # False; timeouts, DNS/TLS failures, redirect errors, and unexpected
        # page/driver failures are inconclusive infrastructure failures.
        raise NetworkError(f"Feed validation could not complete: {exc}") from exc


def _launch_options() -> tuple[dict[str, Any], dict[str, int]]:
    config = get_config()
    viewport = {
        "width": config.browser.viewport_width,
        "height": config.browser.viewport_height,
    }
    launch_options: dict[str, Any] = {}
    # chrome_path is Chromium-specific; only apply it on the Patchright engine
    # so a stale value doesn't leak into Camoufox's own binary resolution.
    if config.browser.chrome_path and config.browser.browser_engine == "patchright":
        launch_options["executable_path"] = config.browser.chrome_path
        logger.info("Using custom Chrome path: %s", config.browser.chrome_path)
    # Proxy applies identically to either engine (unlike chrome_path).
    if proxy := config.browser.proxy_settings():
        launch_options["proxy"] = proxy
        logger.info(
            "Using proxy: %s", proxy["server"]
        )  # credential-free by construction
    # Camoufox-only, same reason as chrome_path above: "humanize" isn't a
    # recognized Playwright launch_persistent_context() kwarg, so setting it
    # unconditionally would break Patchright. CamoufoxAdapter.launch()
    # already builds camoufox_options["humanize"]=True before spreading
    # **launch_options, so this only needs to act when the profile disables
    # it (NO_STEALTH) -- everything else matches Camoufox's own default.
    if config.browser.browser_engine == "camoufox":
        stealth_profile = config.browser.resolve_stealth_profile()
        if not stealth_profile.enable_fingerprint_masking:
            launch_options["humanize"] = False
    return launch_options, viewport


def _make_browser(
    profile_dir: Path,
    *,
    launch_options: dict[str, Any],
    viewport: dict[str, int],
    user_agent: str | None = None,
    camoufox_identity_path: Path | None = None,
    expected_camoufox_identity_sha256: str | None = None,
) -> BrowserManager:
    """Build a browser without changing the session's browser identity.

    For Patchright, an explicit USER_AGENT (env/CLI) wins over the imported
    source-session UA. Camoufox always keeps its own coherent Firefox identity:
    injecting either value changes only part of its generated fingerprint and
    can cause LinkedIn to invalidate ``li_at``.
    """
    config = get_config()
    resolved_user_agent = config.browser.user_agent or user_agent
    if config.browser.browser_engine == "camoufox":
        if resolved_user_agent:
            logger.warning(
                "Ignoring configured/source user agent for Camoufox; using "
                "the engine-native Firefox identity"
            )
        resolved_user_agent = None
    return BrowserManager(
        user_data_dir=profile_dir,
        headless=_headless,
        slow_mo=config.browser.slow_mo,
        user_agent=resolved_user_agent,
        viewport=viewport,
        engine=config.browser.browser_engine,
        camoufox_identity_path=camoufox_identity_path,
        expected_camoufox_identity_sha256=expected_camoufox_identity_sha256,
        **launch_options,
    )


async def validate_imported_cookies(
    cookie_path: Path,
    profile_dir: Path,
    *,
    user_agent: str | None = None,
) -> bool:
    """Validate freshly imported cookies against /feed/ before persisting.

    Starts a headless browser on *profile_dir*, injects the LinkedIn cookies
    from *cookie_path*, and proves /feed/ with the same validator login and the
    Docker bridge use (``_feed_auth_succeeds``: remember-me resolution plus
    auth-barrier detection). Used only by the browser-import CLI path.
    *user_agent* is the source browser's synthesized UA — validating under the
    same UA the runtime will use keeps the proof representative.

    A local :class:`BrowserManager` is used (never the singleton), so
    ``close_browser()``'s export-on-close is not involved and cannot shrink
    ``cookies.json``. Injection routes through the existing ``import_cookies``
    with ``preset_name="bridge_core"`` (the largest existing preset). After
    the feed proof succeeds, post-navigation rotations/deletions are
    merged back into the staged full snapshot before it can be committed.
    Always closes the browser in ``finally``.
    """
    launch_options, viewport = _launch_options()
    secure_mkdir(profile_dir)
    harden_linkedin_tree(profile_dir)
    browser = _make_browser(
        profile_dir,
        launch_options=launch_options,
        viewport=viewport,
        user_agent=user_agent,
    )
    try:
        await browser.start()
        await browser.page.goto(
            "https://www.linkedin.com/feed/", wait_until="domcontentloaded"
        )
        await stabilize_navigation("import pre-validate feed navigation", logger)
        if not await browser.import_cookies(cookie_path, preset_name="bridge_core"):
            return False
        await stabilize_navigation("import cookie injection", logger)
        if not await _feed_auth_succeeds(browser):
            return False
        if not await browser.refresh_imported_cookie_snapshot(
            cookie_path, preset_name="bridge_core"
        ):
            raise NetworkError(
                "Validated the imported session but could not capture its "
                "post-feed cookie snapshot"
            )
        return True
    finally:
        try:
            teardown_complete = await browser.close()
        except asyncio.CancelledError as exc:
            if not browser.teardown_complete:
                raise BrowserTeardownError(
                    "Imported-session validation was cancelled before browser "
                    "teardown could be confirmed"
                ) from exc
            raise
        if not teardown_complete:
            raise BrowserTeardownError(
                "Imported-session validation could not confirm browser teardown"
            )


async def _bridge_runtime_profile(
    profile_dir: Path,
    *,
    cookie_path: Path,
    source_state: SourceState,
    runtime_id: str,
    launch_options: dict[str, Any],
    viewport: dict[str, int],
) -> BrowserManager:
    source_profile_dir = get_source_profile_dir()
    runtime_instance_id = get_runtime_instance_id()
    config = get_config()
    if config.browser.browser_engine == "camoufox":
        if not source_state.camoufox_identity_sha256:
            raise AuthenticationError(
                "The source session is not bound to a Camoufox identity. "
                "Run with --browser camoufox --login before using Camoufox."
            )
        identity_path = source_camoufox_identity_path(source_profile_dir)
        try:
            identity_sha256 = load_camoufox_identity_sha256(identity_path)
        except (CamoufoxIdentityError, OSError) as exc:
            raise AuthenticationError(
                "The Camoufox identity bound to the source session is missing "
                "or incompatible. Run with --browser camoufox --login."
            ) from exc
        if identity_sha256 != source_state.camoufox_identity_sha256:
            raise AuthenticationError(
                "The Camoufox identity does not match the source session. "
                "Run with --browser camoufox --login."
            )
    elif source_state.camoufox_identity_sha256:
        raise AuthenticationError(
            "The source session was minted under Camoufox and cannot be replayed "
            "under Patchright. Run with --login or --import-from-browser."
        )
    if not clear_runtime_profile(runtime_id, source_profile_dir):
        rotate_runtime_instance_id()
        raise NetworkError(f"Could not prepare isolated runtime profile: {profile_dir}")
    secure_mkdir(profile_dir.parent)
    browser = _make_browser(
        profile_dir,
        launch_options=launch_options,
        viewport=viewport,
        user_agent=source_state.user_agent,
        camoufox_identity_path=source_camoufox_identity_path(source_profile_dir),
        expected_camoufox_identity_sha256=(
            source_state.camoufox_identity_sha256
            if config.browser.browser_engine == "camoufox"
            else None
        ),
    )
    try:
        await browser.start()
        await record_page_trace(
            browser.page,
            "bridge-browser-started",
            extra={"profile_dir": str(profile_dir)},
        )
        await browser.page.goto(
            "https://www.linkedin.com/feed/", wait_until="domcontentloaded"
        )
        await stabilize_navigation("pre-import feed navigation", logger)
        await record_page_trace(browser.page, "bridge-after-pre-import-feed")
        if not await browser.import_cookies(cookie_path):
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
        if not await browser.refresh_imported_cookie_snapshot(cookie_path):
            raise NetworkError(
                "Validated the runtime session but could not capture its "
                "post-feed cookie snapshot"
            )
        logger.info("Runtime %s authenticated via fresh isolated bridge", runtime_id)
        browser.is_authenticated = True
        return browser
    except asyncio.CancelledError:
        # Abandon the namespace before the first await: a second cancellation
        # can interrupt close(), and no later startup may then reopen/delete a
        # directory whose browser lock ownership is uncertain.
        rotate_runtime_instance_id()
        if await browser.close():
            clear_runtime_instance(runtime_id, runtime_instance_id, source_profile_dir)
        else:
            logger.warning(
                "Preserving runtime profile after cancelled bridge because "
                "browser teardown was not confirmed: %s",
                profile_dir,
            )
        raise
    except NetworkError:
        # A network/driver error says nothing about cookie validity, but the
        # bounded close still determines whether this isolated directory is
        # safe to remove. Preserve it only when lock ownership is uncertain.
        rotate_runtime_instance_id()
        if await browser.close():
            clear_runtime_instance(runtime_id, runtime_instance_id, source_profile_dir)
        else:
            logger.warning(
                "Preserving runtime profile after network failure because "
                "browser teardown was not confirmed: %s",
                profile_dir,
            )
        raise
    except Exception:
        rotate_runtime_instance_id()
        if await browser.close():
            clear_runtime_instance(runtime_id, runtime_instance_id, source_profile_dir)
        else:
            logger.warning(
                "Preserving runtime profile after bridge failure because "
                "browser teardown was not confirmed: %s",
                profile_dir,
            )
        raise


async def get_or_create_browser(
    headless: bool | None = None,
) -> BrowserManager:
    """Get/create a runtime browser from one consistent source snapshot."""
    _ensure_browser_process_state()
    async with _browser_lifecycle_lock:
        async with source_session_lock(get_source_profile_dir()):
            return await _get_or_create_browser_locked(headless)


async def _get_or_create_browser_locked(
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
    global _browser, _browser_runtime_id, _browser_runtime_instance_id, _headless

    if headless is not None:
        _headless = headless

    if _browser is not None:
        return _browser

    launch_options, viewport = _launch_options()
    source_profile_dir = get_profile_dir()
    cookie_path = portable_cookie_path(source_profile_dir)
    source_state = load_source_state(source_profile_dir)
    if not source_state or not portable_cookie_is_valid(source_profile_dir):
        raise AuthenticationError(
            "No source authentication found. Run with --login to create a profile."
        )

    current_runtime_id = get_runtime_id()
    current_runtime_instance_id = get_runtime_instance_id()

    logger.info(
        "Using fresh isolated bridge for runtime %s from source generation %s",
        current_runtime_id,
        source_state.login_generation,
    )
    browser = await _bridge_runtime_profile(
        runtime_profile_dir(current_runtime_id, source_profile_dir),
        cookie_path=cookie_path,
        source_state=source_state,
        runtime_id=current_runtime_id,
        launch_options=launch_options,
        viewport=viewport,
    )
    _apply_browser_settings(browser)
    _browser = browser
    _browser_runtime_id = current_runtime_id
    _browser_runtime_instance_id = current_runtime_instance_id
    return _browser


async def close_browser() -> bool:
    """Close the browser and cleanup resources."""
    _ensure_browser_process_state()
    async with _browser_lifecycle_lock:
        return await _close_browser_locked()


async def _close_browser_locked() -> bool:
    """Close the singleton while holding ``_browser_lifecycle_lock``."""
    global _browser, _browser_runtime_id, _browser_runtime_instance_id

    browser = _browser
    runtime_id = _browser_runtime_id
    runtime_instance_id = _browser_runtime_instance_id
    _browser = None
    _browser_runtime_id = None
    _browser_runtime_instance_id = None

    if browser is None:
        return True

    logger.info("Closing browser...")
    # Rotate before the await so cancellation can never leave the next startup
    # pointing at a namespace whose teardown result was lost.
    rotate_runtime_instance_id()
    teardown_complete = await browser.close()
    cleanup_complete = teardown_complete
    if runtime_id is not None and runtime_instance_id is not None:
        if teardown_complete:
            cleanup_complete = clear_runtime_instance(
                runtime_id,
                runtime_instance_id,
                get_source_profile_dir(),
            )
            if not cleanup_complete:
                logger.error(
                    "Browser teardown completed but isolated runtime cleanup "
                    "failed for %s/%s",
                    runtime_id,
                    runtime_instance_id,
                )
        else:
            logger.warning(
                "Preserving isolated runtime profile because browser teardown "
                "was not confirmed"
            )
            rotate_runtime_instance_id()
    logger.info("Browser closed")
    return teardown_complete and cleanup_complete


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


def current_headless() -> bool:
    """Return the headless mode future browser creation will use."""
    return _headless


async def validate_session() -> bool:
    """
    Check whether startup authentication has already succeeded for this browser.

    Mid-session expiry is detected during real LinkedIn navigations and scraper
    auth checks rather than via a fresh login probe on every tool call.

    Returns:
        True if startup authentication succeeded for the current browser
    """
    browser = await get_or_create_browser()
    if browser.is_authenticated:
        return True
    return await is_logged_in(browser.page)


async def ensure_authenticated() -> None:
    """
    Confirm that the shared browser completed startup authentication.

    Raises:
        AuthenticationError: If no authenticated browser session is available
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
    global _browser, _browser_lifecycle_lock, _browser_owner_pid
    global _browser_runtime_id, _browser_runtime_instance_id, _headless
    _browser = None
    _browser_runtime_id = None
    _browser_runtime_instance_id = None
    _browser_lifecycle_lock = asyncio.Lock()
    _browser_owner_pid = os.getpid()
    _headless = True
