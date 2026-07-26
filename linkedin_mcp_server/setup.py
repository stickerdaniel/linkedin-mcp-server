"""
Interactive setup flows for LinkedIn MCP Server authentication.

Handles session creation through interactive browser login using Patchright
with persistent context. Profile state auto-persists to user_data_dir.
"""

import asyncio
import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.core import (
    BrowserManager,
    goto_reporting_proxy_errors,
    resolve_remember_me_prompt,
    wait_for_manual_login,
)
from linkedin_mcp_server.exceptions import BrowserBusyError
from linkedin_mcp_server.profile_lease import ProfileLease, get_profile_lease
from linkedin_mcp_server.session_state import (
    run_deferring_cancels,
    portable_cookie_path,
    restore_source_profile,
    rotate_shielded,
    write_source_state,
)

from linkedin_mcp_server.drivers.browser import close_browser, get_profile_dir


async def interactive_login(user_data_dir: Path | None = None) -> bool:
    """
    Open browser for manual LinkedIn login with persistent profile.

    Opens a non-headless browser, navigates to LinkedIn login page,
    and waits for user to complete authentication (including 2FA, captcha, etc.).
    Profile state auto-persists to user_data_dir.

    Args:
        user_data_dir: Path to browser profile. Defaults to config's user_data_dir.

    Returns:
        True if login was successful

    Raises:
        Exception: If login fails or times out
    """
    if user_data_dir is None:
        user_data_dir = get_profile_dir()

    # A manual login means a new session, possibly for a different account, so
    # the previous profile must not be reused: Chromium would keep its existing
    # machine_id and friends and hand LinkedIn the same device identity twice.
    # Closing first so a later teardown cannot export the retired session's
    # cookies over the new ones (drivers.browser exports on close).
    await close_browser()

    # The login browser owns the profile for its whole run, which can be the
    # full 30-minute login timeout. Without this the profile looks free the
    # moment cookies are written, and another process could launch against it
    # while this browser is still open.
    lease = get_profile_lease(user_data_dir)
    if not lease.try_acquire():
        raise BrowserBusyError(
            "Another LinkedIn MCP client is using the browser, so a login "
            "cannot start. Close it and try again."
        )
    # Every exit runs through one finally: a login can fail before the browser
    # opens (rotation errors, cancellation) or after it closed cleanly but with
    # an exception, and each of those must still settle the profile correctly.
    state = _LoginState()
    try:
        return await _login_holding_the_profile(user_data_dir, lease, state)
    finally:
        if state.close_confirmed:
            # Only clear liveness this login actually set. A pre-launch failure
            # can happen because a *previous* unconfirmed browser still holds
            # the profile, and clearing that flag here would erase the warning
            # someone else raised.
            if state.browser_opened:
                lease.mark_browser_closed()
            lease.release()
        else:
            # Keep the kernel lock as well as the process-local flag: other
            # processes cannot see the flag and would launch on top of a
            # Chromium that may still be running. Freed when this process exits.
            print(
                "   Warning: the login browser did not shut down cleanly. "
                "Restart the server before using this profile again."
            )


@dataclass
class _LoginState:
    """Whether the login browser was opened, and whether it closed cleanly.

    Carried by reference so the caller can settle the profile on every exit
    path, including the ones where no value is ever returned.
    """

    browser_opened: bool = False
    # Only a proven teardown clears the profile, so an exception or cancellation
    # that never reaches the assignment leaves it held rather than claiming a
    # shutdown nobody observed.
    close_confirmed: bool = True


async def _login_holding_the_profile(
    user_data_dir: Path, lease: ProfileLease, state: _LoginState
) -> bool:
    """Rotate and log in; the caller owns the profile for the whole flow."""
    # Rotation must happen before the browser is marked open: the exclusivity
    # check treats an open browser as a reason to refuse, so marking first would
    # make every re-login fail to retire the profile it is replacing.
    retired = await rotate_shielded(user_data_dir)

    succeeded = False
    # The lease reference alone stops other processes; this additionally stops
    # rotation and logout inside *this* one, which the reference cannot express.
    lease.mark_browser_open()
    state.browser_opened = True
    state.close_confirmed = False
    try:
        succeeded = await _login_into_fresh_profile(
            user_data_dir, config=get_config(), state=state
        )
        return succeeded
    finally:
        # The retirement happens before the replacement exists, so a login that
        # is cancelled, times out, or fails to export its cookies would
        # otherwise leave the user logged out of a session that was working.
        #
        # Not on an unconfirmed close: the login browser may still be running,
        # so moving the old session back underneath it would corrupt both. The
        # backup stays in quarantine, which is recoverable; writing over a live
        # profile is not. Restore would also raise from this finally and mask
        # whatever actually failed.
        if state.close_confirmed:
            # Cleared before restore, which now takes the profile exclusively
            # and would otherwise refuse against our own liveness flag. The
            # lease reference is still held, so no other process can slip in.
            lease.mark_browser_closed()
        if retired is not None and not succeeded:
            if not state.close_confirmed:
                print(
                    f"   The previous session was not restored because the "
                    f"login browser did not shut down cleanly. It is kept at "
                    f"{retired}."
                )
            else:
                # Cancellation is deferred rather than abandoning the worker: it
                # is already moving the session back, and dropping its result
                # mid move would leave the user logged out with the pieces
                # split. It is re-raised afterwards so the caller still sees it.
                restored, cancelled = await run_deferring_cancels(
                    functools.partial(restore_source_profile, retired, user_data_dir)
                )
                if not restored:
                    print(
                        f"   Warning: the previous session could not be restored. "
                        f"It is kept at {retired}."
                    )
                if cancelled:
                    raise asyncio.CancelledError


async def _login_into_fresh_profile(
    user_data_dir: Path, *, config: Any, state: _LoginState
) -> bool:
    """Drive the manual login against a freshly retired profile directory.

    Records on *state* whether the login browser's shutdown was confirmed. The
    caller must not release the profile when it was not: Chromium may still be
    holding it.
    """
    login_timeout_ms = int(config.browser.login_timeout_seconds * 1000)

    if config.browser.login_timeout_seconds:
        budget = f"{config.browser.login_timeout_seconds / 60:.0f} minutes"
    else:
        budget = "no time limit"

    print("Opening browser for LinkedIn login...")
    print(f"   Please log in manually. You have {budget} to complete authentication.")
    print("   (This handles 2FA, captcha, and any security challenges)")

    launch_options: dict[str, Any] = {}
    if config.browser.chrome_path:
        launch_options["executable_path"] = config.browser.chrome_path

    # The login browser must leave from the same address as later scrapes: a
    # session created on one IP and used from another is what trips LinkedIn's
    # security checkpoint.
    proxy = config.browser.proxy_settings()
    if proxy:
        launch_options["proxy"] = proxy

    viewport = {
        "width": config.browser.viewport_width,
        "height": config.browser.viewport_height,
    }

    manager = BrowserManager(
        user_data_dir=user_data_dir,
        headless=False,
        slow_mo=config.browser.slow_mo,
        user_agent=config.browser.user_agent,
        viewport=viewport,
        **launch_options,
    )
    try:
        return await _run_login(manager, user_data_dir, config, login_timeout_ms)
    finally:
        # In a finally so a login that times out or is cancelled still records a
        # teardown that did complete; otherwise an ordinary timeout would leave
        # the profile held for the rest of this process's life. Defaults to
        # confirmed for a manager that does not report it, so a stand-in cannot
        # wedge the profile.
        state.close_confirmed = bool(getattr(manager, "close_confirmed", True))


async def _run_login(
    manager: BrowserManager,
    user_data_dir: Path,
    config: Any,
    login_timeout_ms: int,
) -> bool:
    async with manager as browser:
        # Navigate to LinkedIn login
        await goto_reporting_proxy_errors(
            browser.page, "https://www.linkedin.com/login"
        )
        # Let LinkedIn finish rendering the saved-account chooser, then retry the
        # same exact click target a few times before falling back to the normal
        # manual-login wait loop.
        for _ in range(3):
            await asyncio.sleep(2)
            if await resolve_remember_me_prompt(browser.page):
                break

        # Wait for manual login completion. The budget comes from
        # LOGIN_TIMEOUT (config.browser.login_timeout_seconds); 0 = unlimited.
        await wait_for_manual_login(browser.page, timeout=login_timeout_ms)

        # Wait for persistent context to flush cookies to disk
        await asyncio.sleep(2)

        # Verify session cookie was persisted
        cookies = await browser.context.cookies()
        li_at = [c for c in cookies if c["name"] == "li_at"]
        if not li_at:
            print("   Warning: Session cookie not found. Login may not have persisted.")
            print("   Waiting longer for cookie propagation...")
            await asyncio.sleep(5)

        # Export source-session cookies for the one-time foreign-runtime bridge.
        # Docker now checkpoint-commits its own derived runtime profile after the
        # first successful /feed/ recovery instead of relying on browser teardown.
        if await browser.export_cookies(portable_cookie_path(user_data_dir)):
            print("   Cookies exported for Docker portability")
            # Record the override UA the cookie was minted under (the login
            # browser ran with config.browser.user_agent). Without this a later
            # replay from a runtime that lacks the override would fall back to
            # its default UA, a fingerprint mismatch. None when no override is
            # set (the runtime default is stable across replays on that runtime).
            source_state = write_source_state(
                user_data_dir, user_agent=config.browser.user_agent
            )
            print(f"   Source session generation: {source_state.login_generation}")
        else:
            print(
                "   Warning: cookie export failed; Docker bridge may not work. "
                "Run --login again to retry."
            )
            return False
        print(f"Profile saved to {user_data_dir}")
        return True


def run_profile_creation(user_data_dir: str | None = None) -> bool:
    """
    Create profile via interactive login with persistent context.

    Args:
        user_data_dir: Path to profile directory. Defaults to config's user_data_dir.

    Returns:
        True if profile was created successfully
    """
    if user_data_dir:
        profile_dir = Path(user_data_dir).expanduser()
    else:
        profile_dir = get_profile_dir()

    print("LinkedIn MCP Server - Profile Creation")
    print(f"   Profile will be saved to: {profile_dir}")

    try:
        success = asyncio.run(interactive_login(profile_dir))
        return success
    except Exception as e:
        print(f"Profile creation failed: {e}")
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
        return asyncio.run(interactive_login())
    except Exception as e:
        print(f"Login failed: {e}")
        return False
