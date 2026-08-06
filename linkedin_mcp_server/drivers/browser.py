"""
Patchright browser management for LinkedIn scraping.

Provides async browser lifecycle management using BrowserManager with persistent
context. Implements a singleton pattern for browser reuse across tool calls with
automatic profile persistence.
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from collections.abc import Coroutine
from typing import Any, TypeVar

from linkedin_mcp_server.common_utils import harden_linkedin_tree, secure_mkdir
from linkedin_mcp_server.core import (
    AuthenticationError,
    BrowserManager,
    detect_auth_barrier_quick,
    detect_rate_limit,
    goto_reporting_proxy_errors,
    is_logged_in,
    proxy_hint,
    raise_if_proxy_configured,
    redact_proxy_credentials,
    raise_if_proxy_error,
    resolve_remember_me_prompt,
)


from linkedin_mcp_server.browser_launch import build_launch_options, describe_launch
from linkedin_mcp_server.common_utils import utcnow_iso
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.debug_trace import record_page_trace
from linkedin_mcp_server.debug_utils import stabilize_navigation
from linkedin_mcp_server.exceptions import (
    BrowserBusyError,
    BrowserDowngradeError,
    BrowserShutdownUnconfirmedError,
    ProfileRootRefusedError,
)
from linkedin_mcp_server.profile_lease import ProfileLease, get_profile_lease
from linkedin_mcp_server.server_role import a_held_profile_means_this_owner_must_go
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
# Serializes singleton creation: tool calls are serialized by the tool-call
# middleware, but the background login flow started at startup can resume into
# this path and race the first tool call, and an unguarded check-then-create
# would launch two browsers against the same profile.
_browser_create_lock = asyncio.Lock()
# The lease the singleton took, held onto rather than merely noted, so the close
# settles the object it acquired instead of looking one up again.
#
# It used to be a bool, and the difference cost six review rounds. Recording only
# *that* a lease existed left the close to reconstruct it through
# ``get_profile_lease()``, which resolves a path and can fail — while ``_browser``
# is already cleared and every line that keeps the profile safe sits below that
# call. Ownership was therefore inferred from two globals that could disagree,
# settled at the one moment one of them was already gone. Each round closed one
# more way in; keeping the object closes the shape.
#
# Always the object the registry handed out (``get_profile_lease``), never one
# constructed here: ``profile_lease`` clears inherited leases after a fork by
# walking that registry, and an instance built outside it would silently keep a
# parent's kernel lock alive in a child.
_browser_lease: ProfileLease | None = None
# Monotonic timestamp of the last completed tool call, for the idle timer.
_last_activity: float | None = None
# Tool calls currently driving the browser. The background handoff poll must not
# close a browser out from under a running call: the tool holds a Page from it.
_calls_in_flight: int = 0
# Serializes close against create. close_browser() clears _browser and then
# awaits the cookie export and Chromium teardown; without this a tool call
# arriving in that window would see no browser and launch a second Chromium on
# the same profile, which is the very corruption this module prevents.
_browser_lifecycle_lock = asyncio.Lock()

T = TypeVar("T")


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
) -> None:
    """Log the page state when /feed/ validation fails.

    *reason* must already be redacted. The exception itself is deliberately not
    logged: a driver error can quote the proxy URL, and this log is what users
    paste into issue reports.
    """
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
    )


async def _feed_auth_succeeds(
    browser: BrowserManager,
    *,
    allow_remember_me: bool = True,
) -> bool:
    """Validate that /feed/ loads without an auth barrier."""
    try:
        await goto_reporting_proxy_errors(
            browser.page,
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
        return True
    except Exception as exc:
        # Before anything else: a proxy fault is not a dead session. Returning
        # False here would have the caller retire a valid profile and tell the
        # user to log in again, which cannot fix an unreachable proxy. Checked
        # first because no page loaded, so there is no remember-me prompt to
        # resolve, and it also catches a ProxyConnectionError raised by the
        # recursive retries above, which run inside this try.
        raise_if_proxy_error(exc)
        if allow_remember_me and await resolve_remember_me_prompt(browser.page):
            await stabilize_navigation(
                "remember-me resolution after feed failure", logger
            )
            await record_page_trace(
                browser.page,
                "feed-after-remember-me-error-recovery",
                extra={
                    "error": redact_proxy_credentials(f"{type(exc).__name__}: {exc}")
                },
            )
            return await _feed_auth_succeeds(browser, allow_remember_me=False)
        # A failed navigation still leaves a URL and a title behind, and the
        # quick check reads only those. LinkedIn may have committed a redirect
        # to /login and merely missed the load event, which is real evidence
        # about the session and must outrank the proxy explanation below.
        barrier = await detect_auth_barrier_quick(browser.page)
        detail = redact_proxy_credentials(f"{type(exc).__name__}: {exc}")
        await record_page_trace(
            browser.page,
            "feed-navigation-error",
            extra={"error": detail, "barrier": barrier},
        )
        # Redacted, and without exc_info: driver errors can quote the proxy URL,
        # and both destinations here outlive the call -- the trace is written to
        # disk and the log is what users paste into issue reports.
        await _log_feed_failure_context(browser, detail)
        if barrier is None:
            # Nothing loaded and no barrier, so nothing proves the session is
            # dead -- and with a proxy in front, the most likely cause is the
            # proxy. Wrong credentials in particular produce no proxy error code
            # at all: Chromium retries the 407 challenge until the navigation
            # times out (verified against a local authenticating relay), so the
            # marker check above cannot catch it. Reporting False would hand the
            # caller an AuthenticationError, whose recovery moves the stored
            # profile aside and starts a login through the same broken proxy.
            raise_if_proxy_configured(exc)
        return False


def _launch_options() -> tuple[dict[str, Any], dict[str, int]]:
    launch_options, viewport = build_launch_options(get_config().browser)
    describe_launch(launch_options)
    return launch_options, viewport


def _make_browser(
    profile_dir: Path,
    *,
    launch_options: dict[str, Any],
    viewport: dict[str, int],
) -> BrowserManager:
    """Build a BrowserManager on *profile_dir*."""
    config = get_config()
    return BrowserManager(
        user_data_dir=profile_dir,
        headless=_headless,
        slow_mo=config.browser.slow_mo,
        viewport=viewport,
        **launch_options,
    )


async def _authenticate_existing_profile(
    profile_dir: Path,
    *,
    launch_options: dict[str, Any],
    viewport: dict[str, int],
) -> BrowserManager:
    browser = _make_browser(
        profile_dir,
        launch_options=launch_options,
        viewport=viewport,
    )
    try:
        await browser.start()
        if not await _feed_auth_succeeds(browser):
            raise AuthenticationError(
                f"Stored runtime profile is invalid: {profile_dir}. "
                f"Run with --login to refresh the source session.{proxy_hint()}"
            )
        browser.is_authenticated = True
        return browser
    except BaseException as exc:
        # BaseException so a cancelled startup still tears Chromium down. Left
        # running it would hold the profile that the caller is about to release.
        if not await browser.close():
            # The original failure is replaced deliberately: the caller's
            # recovery for it releases the profile, which is unsafe while this
            # browser may still be on it. Chained so the cause is not lost.
            raise BrowserShutdownUnconfirmedError(
                "The browser did not shut down cleanly after a failed startup, "
                "so the profile is kept. Restart the server to recover."
            ) from exc
        raise


async def validate_imported_cookies(cookie_path: Path, profile_dir: Path) -> bool:
    """Validate freshly imported cookies against /feed/ before persisting.

    Starts a headless browser on *profile_dir*, injects the LinkedIn cookies
    from *cookie_path*, and proves /feed/ with the same validator login and the
    Docker bridge use (``_feed_auth_succeeds``: remember-me resolution plus
    auth-barrier detection). Used only by the browser-import CLI path.

    A local :class:`BrowserManager` is used (never the singleton), so
    ``close_browser()``'s export-on-close is not involved and cannot shrink
    ``cookies.json``. Injection routes through the existing ``import_cookies``
    with ``preset_name="bridge_core"`` (the largest existing preset); the
    on-disk ``cookies.json`` still holds the full superset for the Docker
    bridge. Always closes the browser in ``finally``.
    """
    launch_options, viewport = _launch_options()
    secure_mkdir(profile_dir)
    harden_linkedin_tree(profile_dir)
    browser = _make_browser(
        profile_dir,
        launch_options=launch_options,
        viewport=viewport,
    )
    try:
        await browser.start()
        await goto_reporting_proxy_errors(
            browser.page,
            "https://www.linkedin.com/feed/",
            wait_until="domcontentloaded",
        )
        await stabilize_navigation("import pre-validate feed navigation", logger)
        if not await browser.import_cookies(cookie_path, preset_name="bridge_core"):
            accepted = False
        else:
            await stabilize_navigation("import cookie injection", logger)
            accepted = await _feed_auth_succeeds(browser)
    except BaseException as exc:
        # The confirmation has to be checked on this path too. A plain finally
        # would re-raise before it ran, and the caller would then treat an
        # unconfirmed close as an ordinary failure: wipe the profile, try the
        # next candidate, restore over it.
        if not await browser.close():
            raise BrowserShutdownUnconfirmedError(
                "The validation browser did not shut down cleanly, so the "
                "profile is kept. Restart the server to retry."
            ) from exc
        raise

    # Raised rather than returned False: a rejected cookie makes the caller wipe
    # the profile and try the next candidate, and doing that over a Chromium
    # that may still be running is the corruption we are avoiding.
    if not await browser.close():
        raise BrowserShutdownUnconfirmedError(
            "The validation browser did not shut down cleanly, so the imported "
            "session cannot be committed. Restart the server to retry."
        )
    return accepted


async def _bridge_runtime_profile(
    profile_dir: Path,
    *,
    cookie_path: Path,
    source_state: SourceState,
    runtime_id: str,
    launch_options: dict[str, Any],
    viewport: dict[str, int],
    persist_runtime: bool,
) -> BrowserManager:
    source_profile_dir = get_source_profile_dir()
    bridge_started_at = utcnow_iso()
    clear_runtime_profile(runtime_id, source_profile_dir)
    secure_mkdir(profile_dir.parent)
    storage_state_path = runtime_storage_state_path(runtime_id, source_profile_dir)
    browser = _make_browser(
        profile_dir,
        launch_options=launch_options,
        viewport=viewport,
    )
    try:
        await browser.start()
        await record_page_trace(
            browser.page,
            "bridge-browser-started",
            extra={"profile_dir": str(profile_dir)},
        )
        await goto_reporting_proxy_errors(
            browser.page,
            "https://www.linkedin.com/feed/",
            wait_until="domcontentloaded",
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
                "No authentication found. "
                f"Run with --login to create a profile.{proxy_hint()}"
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
        if not await browser.close():
            # Reopening the same directory while the first Chromium may still be
            # running is the concurrent-profile corruption in miniature.
            raise BrowserShutdownUnconfirmedError(
                "The bridge browser did not shut down cleanly, so its profile "
                "cannot be reopened. Restart the server to retry."
            )
        reopened = _make_browser(
            profile_dir,
            launch_options=launch_options,
            viewport=viewport,
        )
        try:
            await reopened.start()
            await stabilize_navigation("derived profile reopen", logger)
            await record_page_trace(
                reopened.page,
                "bridge-after-profile-reopen",
                extra={"profile_dir": str(profile_dir)},
            )
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
                source_profile_dir,
                created_at=bridge_started_at,
            )
            logger.info("Derived runtime profile committed for %s", runtime_id)
            reopened.is_authenticated = True
            return reopened
        except BaseException as exc:
            if not await reopened.close():
                raise BrowserShutdownUnconfirmedError(
                    "The reopened bridge browser did not shut down cleanly, so "
                    "its profile is kept. Restart the server to recover."
                ) from exc
            raise
    except BrowserShutdownUnconfirmedError:
        # Chromium may still be running on this runtime profile. Closing again
        # would report success — the manager has already dropped its handles —
        # and deleting the directory underneath a live browser is exactly what
        # this guard exists to prevent. Leave everything for the operator.
        raise
    except BaseException as exc:
        # BaseException so a cancelled bridge still closes Chromium before the
        # caller releases the profile, and before the runtime dir is removed.
        if not await browser.close():
            # Deleting the runtime directory under a browser that may still be
            # running is the corruption this guard exists for, so stop instead.
            raise BrowserShutdownUnconfirmedError(
                "The bridge browser did not shut down cleanly, so its runtime "
                "profile is kept. Restart the server to recover."
            ) from exc
        try:
            clear_runtime_profile(runtime_id, source_profile_dir)
        except ProfileRootRefusedError as refusal:
            # Cleanup, not a precondition. The bridge has already failed and
            # that failure is what the caller needs to see; a complaint about
            # who owns the directory would replace it with something nobody can
            # act on. The clear at the top of this function is the opposite case
            # and deliberately lets the refusal through.
            logger.warning("Leaving the runtime profile in place: %s", refusal)
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
    global _headless

    if headless is not None:
        _headless = headless

    if _browser is not None:
        return _browser

    # Double-checked: only one concurrent caller may create the singleton. The
    # lifecycle lock additionally keeps creation out of an in-progress close,
    # which clears _browser before it has finished tearing Chromium down.
    async with _browser_create_lock, _browser_lifecycle_lock:
        if _browser is not None:
            return _browser
        return await _create_browser()


async def _create_browser() -> BrowserManager:
    """Create and initialize the singleton (caller holds _browser_create_lock)."""
    global _browser, _browser_cookie_export_path, _browser_lease

    lease = get_profile_lease()

    # A previous close could not confirm Chromium had exited, so it may still be
    # running on this profile. Launching a second one now is exactly the
    # corruption this module exists to prevent, and the operator has to clear it.
    if lease.browser_open:
        raise BrowserBusyError(
            "A previous browser on this profile did not shut down cleanly and "
            "may still be running. Restart the server to recover."
        )

    # Own the profile before Chromium touches it. The middleware normally holds a
    # reference already, so this is usually a cheap second reference; taking it
    # here as well keeps every launch path covered, including the CLI ones.
    took_lease = False
    if _browser_lease is None:
        if not lease.try_acquire():
            raise BrowserBusyError()
        took_lease = True

    try:
        browser = await _create_browser_locked()
    except BrowserShutdownUnconfirmedError:
        # A browser from this attempt may still be running on the profile. Keep
        # the lease so nobody launches on top of it. The reference taken here is
        # deliberately not released, and one extra is taken so the caller's own
        # release cannot drop the last one; the kernel frees the lock when this
        # process exits.
        lease.mark_browser_open()
        lease.try_acquire()
        # Recorded even though no browser survives, because the profile is held
        # and something has to say by whom. There is no ``_browser`` to close, so
        # nothing will ever settle this lease: that is the point of keeping it.
        _browser_lease = lease
        # After the marker, not before, and that ordering is the whole reason
        # this call is here rather than only in the exception's constructor. The
        # constructor runs while `_create_browser_locked` is still raising, when
        # `browser_open` is false, so the helper reads "nothing is held" and
        # returns; the two lines above then hold the profile with nobody having
        # asked for a replacement. Measured through `_create_browser` in
        # production order: `browser_open=True`, `stand_down=None`, and live, a
        # real owner that three consecutive fresh clients each attached to and
        # got the same refusal from.
        a_held_profile_means_this_owner_must_go(lease)
        raise
    except BaseException:
        # BaseException, not Exception: a cancelled startup would otherwise
        # leave the reference held with nothing tracking it, wedging every other
        # process until this one exits.
        if took_lease:
            lease.release()
        raise

    if took_lease:
        _browser_lease = lease
    # Records that Chromium is live on the profile, which the reference count
    # cannot express: destructive helpers ask for a reference and would simply
    # get one from our own lease.
    lease.mark_browser_open()
    return browser


async def _create_browser_locked() -> BrowserManager:
    """Build the singleton browser; the profile lease is already held."""
    global _browser, _browser_cookie_export_path

    launch_options, viewport = _launch_options()
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
        try:
            browser = await _authenticate_existing_profile(
                derived_profile_dir,
                launch_options=launch_options,
                viewport=viewport,
            )
            _apply_browser_settings(browser)
            _browser = browser
            _browser_cookie_export_path = None
            return _browser
        except (AuthenticationError, BrowserDowngradeError) as exc:
            # A downgrade belongs here and nowhere else. On the *source* profile
            # it has to reach the user, because that directory holds the session
            # and throwing it away to satisfy an old browser is the damage, not
            # the repair. A derived runtime profile is the opposite: it is
            # rebuilt from the source cookies on demand, and the re-bridge below
            # deletes it first, so the older browser gets a directory it wrote
            # itself. Reachable whenever a container image moves backwards with
            # EXPERIMENTAL_PERSIST_DERIVED_RUNTIME set.
            logger.warning(
                "Derived runtime profile is unusable for %s (%s); re-bridging "
                "from source cookies",
                current_runtime_id,
                type(exc).__name__,
            )

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
    )
    _apply_browser_settings(browser)
    _browser = browser
    _browser_cookie_export_path = None
    return _browser


async def close_browser() -> None:
    """Close the browser, releasing the profile once Chromium is confirmed gone.

    Cancellation is held back until teardown finishes, mirroring
    ``session_state.run_deferring_cancels``. Interrupting half way leaves a
    Chromium nobody owns: ``_browser`` is already cleared, so a later call cannot
    retry, and the lease can be neither confirmed nor released. Shielding alone
    would not do: it protects the child but re-raises to the caller immediately,
    releasing the lifecycle lock while teardown is still running, which is
    exactly when a new launch must not start.
    """
    async with _browser_lifecycle_lock:
        await _run_deferring_cancels(_close_browser_locked())


async def _run_deferring_cancels(coroutine: Coroutine[Any, Any, T]) -> T:
    """Run *coroutine* to completion, holding cancellation back until it ends.

    The caller holds the lifecycle lock. Splitting this out of ``close_browser``
    lets the conditional close run its own coroutine under the same protection,
    rather than duplicating the shield loop.
    """
    task = asyncio.create_task(coroutine)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


def _settle_the_profile(*, confirmed: bool) -> None:
    """Hand the profile back, or keep it and ask for a replacement.

    Every way a browser stops arrives here, and there is nothing to look up: the
    lease is the object this module took, kept for exactly this moment. That is
    the whole of the refactor. The close used to re-derive it from a path while
    ``_browser`` was already cleared, so a lookup that failed skipped every line
    below it — the marker, the release, the warning and the request for a
    successor, all at once.

    *confirmed* says whether Chromium is provably gone, which is the only thing
    that decides between the two outcomes:

    * proven gone, so the profile is free: clear the marker, release the
      reference, and forget the lease.
    * not proven, so Chromium may still be on the profile: keep both, and say so.
      Handing it over now is the corruption the lease exists to prevent, and the
      kernel frees the lock when this process exits.

    Never raises. Callers reach it mid-teardown, sometimes with an exception of
    their own already on the way out.
    """
    global _browser_lease

    lease, _browser_lease = _browser_lease, None
    if lease is None:
        # Nothing was ever taken, so nothing is owed. A browser created while the
        # middleware already held a reference is the ordinary case.
        return

    if confirmed:
        # Only now is Chromium provably gone, so only now may auth state move.
        lease.mark_browser_closed()
        lease.release()
        return

    # close() bounds its cleanup steps and reports failure rather than hanging,
    # so it can return while Chromium is still running.
    _browser_lease = lease
    logger.warning(
        "Browser shutdown could not be confirmed; keeping the profile "
        "lease until this process exits."
    )
    # And for a shared owner that sentence is the whole problem: it outlives the
    # client that started it, so nobody is coming to restart it and every later
    # call meets `BrowserBusyError`. Asked from wherever the lease is *kept*
    # rather than wherever a failure is reported, because this path reports
    # nothing at all — a handoff, the idle timeout and `close_session` all return
    # normally from here, and no exception is ever constructed.
    a_held_profile_means_this_owner_must_go(lease)


async def _close_browser_locked() -> None:
    """Tear the browser down; the caller holds the lifecycle lock."""
    global _browser, _browser_cookie_export_path, _last_activity

    browser = _browser
    cookie_export_path = _browser_cookie_export_path
    _browser = None
    _browser_cookie_export_path = None
    _last_activity = None

    if browser is None:
        return

    logger.info("Closing browser...")
    if cookie_export_path is not None:
        try:
            await browser.export_cookies(cookie_export_path)
        except Exception:
            logger.debug("Cookie export on close skipped", exc_info=True)

    # In a `finally`, so the profile is settled however `close()` ends. It is
    # bounded and swallows its own failures today, but the ordering here is what
    # the whole class of wedge turned on: anything raised between clearing
    # `_browser` and settling the lease used to leave the profile held by a
    # process nobody had asked to give way.
    confirmed = False
    try:
        confirmed = await browser.close()
    finally:
        _settle_the_profile(confirmed=confirmed)
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


def note_call_started() -> None:
    """Record that a tool call is now driving the browser."""
    global _calls_in_flight
    _calls_in_flight += 1


def note_activity() -> None:
    """Record that a tool call just finished, for the idle timer."""
    global _last_activity, _calls_in_flight
    _last_activity = time.monotonic()
    _calls_in_flight = max(0, _calls_in_flight - 1)


# Interval of the background handoff poll. A probe costs about 40 microseconds,
# so a one-second cadence is free and makes a handover feel immediate.
_HANDOFF_POLL_INTERVAL_SECONDS = 1.0


async def watch_for_handoff_requests() -> None:
    """Release the profile promptly once another process asks for it.

    Checking only after each tool call is not enough: an owner that finishes its
    last call, probes, and then goes idle never probes again. A process that
    announces itself a moment later would wait out its whole budget and get a
    busy error while the owner sat doing nothing. This poll closes that window,
    and also drives the idle timeout, which likewise has nothing to trigger it
    once calls stop arriving.
    """
    while True:
        try:
            await asyncio.sleep(_HANDOFF_POLL_INTERVAL_SECONDS)
            await release_profile_if_idle_or_requested()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A poll failure must never take down the server; the next tool call
            # checks again anyway.
            logger.debug("Handoff poll failed", exc_info=True)


async def release_profile_if_idle_or_requested() -> bool:
    """Close the browser when another process wants it, or when we are idle.

    Called after each tool call and from a background poller. Polling matters:
    if the owner only checked between calls, a waiter that announces *after* the
    owner's last call would block until the idle timeout while the owner sits
    doing nothing.

    Returns whether the browser was closed.
    """
    if _browser is None or _browser_lease is None:
        return False

    # A tool call is using this browser's Page right now. Closing it here would
    # fail that call with a closed-target error, which is worse than making the
    # waiting process wait a few seconds longer; the caller's own post-call check
    # hands over as soon as the call finishes.
    if _calls_in_flight > 0:
        return False

    config = get_config().browser
    # The lease this browser holds, not one the registry resolves now. Both
    # questions asked below are about *this* profile: `handoff_requested()`
    # probes a file under the lease's own auth root, and `held_seconds` counts
    # from the moment that lease was acquired. A freshly resolved object answers
    # about whichever path the config points at now and reports a hold of zero,
    # so a waiter would be either ignored or refused forever. `config` still
    # comes from settings: the hold window and the idle timeout are not lease
    # state. Bound to a local because the close below can clear the global, and
    # the lines under it must keep describing the lease this decision was about.
    lease = _browser_lease
    # None means no tool call has ever run, which is idle in the strongest sense.
    idle_for = time.monotonic() - _last_activity if _last_activity is not None else None

    if lease.handoff_requested():
        # Every handoff costs a reopen, and a reopen re-validates /feed/, so a
        # busy pair of clients trading the browser on every call would multiply
        # LinkedIn requests. The hold window bounds how often ownership can move.
        #
        # It is measured from when we took the profile, not from idleness: by
        # the time this runs the current call has already finished, so any
        # idle-based test would always pass and the window would never apply.
        held = lease.held_seconds
        never_worked = idle_for is None
        if never_worked or held >= config.browser_min_hold_seconds:
            logger.info(
                "Another process is waiting for the browser; handing over (held %.1fs)",
                held,
            )
            return await _close_unless_a_call_arrived()
        logger.debug(
            "Handoff requested but held for only %.1fs of %.1fs; keeping the "
            "browser to avoid a reopen",
            held,
            config.browser_min_hold_seconds,
        )
        return False

    timeout = config.browser_idle_timeout_seconds
    if timeout > 0 and idle_for is not None and idle_for >= timeout:
        logger.info(
            "Closing idle browser after %.0fs and releasing the profile", idle_for
        )
        return await _close_unless_a_call_arrived()

    return False


async def _close_unless_a_call_arrived() -> bool:
    """Close the browser, unless a tool call started while we were deciding.

    The guard in the caller ran outside the lifecycle lock, and taking that lock
    can wait: a close in progress holds it across the cookie export and the
    Chromium teardown. A call that starts inside that wait is invisible to a
    check that already happened. Measured, with the lock held to open the
    window: the decision was taken with nothing in flight, and Chromium was
    closed and the profile released with one call running. That call's Page is
    gone, which is exactly the closed-target error the guard exists to prevent.

    Here rather than in ``close_browser``: that is the general teardown entry
    point, and ``close_session``, shutdown, login and import all mean "close it"
    unconditionally. A counter check there would make a deliberate close
    silently do nothing. This is the one caller whose close is conditional.

    Returns whether the browser was closed.
    """
    async with _browser_lifecycle_lock:
        return await _run_deferring_cancels(_close_browser_if_still_idle())


async def _close_browser_if_still_idle() -> bool:
    """Tear the browser down unless a call claimed it; the lock is held.

    The check has to sit *here*, in the coroutine the teardown task runs, and
    that placement is the whole point rather than a detail. Checking in the
    caller and then starting this as a task leaves a scheduling point between
    the two: ``asyncio.create_task`` does not begin the coroutine, so the loop
    gets to run whatever else is ready before the first line below executes.
    Measured with the counter read in the caller instead: the re-check saw zero,
    a tool call ran next and took the live browser through the fast path in
    ``get_or_create_browser``, and the teardown then closed that same browser
    with one call in flight.

    From here there is no such gap. This body runs to its first ``await``
    without yielding, and ``_close_browser_locked`` clears ``_browser`` before
    *its* first await, so a call arriving afterwards finds no browser and waits
    on the lock for a fresh one rather than losing the one it holds.

    Returns whether the browser was closed.
    """
    if _calls_in_flight > 0:
        logger.debug(
            "A tool call started while the browser was being handed over; "
            "keeping it, and the caller's post-call check hands over next"
        )
        return False
    # Another close may have run while we waited, and reporting a close we did
    # not perform would tell the middleware a handover happened.
    if _browser is None:
        return False
    await _close_browser_locked()
    return True


def reset_browser_for_testing() -> None:
    """Reset global browser state for test isolation.

    The lease is dropped rather than released, deliberately. ``conftest`` resets
    this module before ``profile_lease`` precisely so a lease the browser still
    held is settled by its own bookkeeping first; releasing a reference here as
    well would drop one the test never took, and the next test would find a lease
    that reports itself free while the kernel lock is still open.
    """
    global _browser, _browser_cookie_export_path, _headless
    global _browser_lease, _last_activity, _calls_in_flight
    _browser = None
    _browser_cookie_export_path = None
    _headless = True
    _browser_lease = None
    _last_activity = None
    _calls_in_flight = 0
