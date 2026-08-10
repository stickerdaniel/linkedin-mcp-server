"""
Interactive setup flows for LinkedIn MCP Server authentication.

Handles session creation through interactive browser login using Patchright
with persistent context. Profile state auto-persists to user_data_dir.
"""

import asyncio
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import functools
from pathlib import Path
import signal
from typing import Any

from linkedin_mcp_server.browser_launch import build_launch_options, describe_launch
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.config.schema import PROFILE_HANDOVER_WAIT_SECONDS
from linkedin_mcp_server.core import (
    BrowserManager,
    goto_reporting_proxy_errors,
    resolve_remember_me_prompt,
    wait_for_manual_login,
)
from linkedin_mcp_server.exceptions import BrowserBusyError
from linkedin_mcp_server.login_viewer import LoginViewer, VIEWER_WALL_SECONDS
from linkedin_mcp_server.profile_lease import ProfileLease, get_profile_lease
from linkedin_mcp_server.session_state import (
    UNGUARDED,
    a_peer_already_signed_in,
    run_deferring_cancels,
    portable_cookie_path,
    restore_source_profile,
    rotate_shielded,
    write_source_state,
)

from linkedin_mcp_server.drivers.browser import close_browser, get_profile_dir


async def interactive_login(
    user_data_dir: Path | None = None,
    *,
    superseded_by: str | None | object = UNGUARDED,
    login_viewer: bool = False,
) -> bool:
    """
    Open browser for manual LinkedIn login with persistent profile.

    Opens a non-headless browser, navigates to LinkedIn login page,
    and waits for user to complete authentication (including 2FA, captcha, etc.).
    Profile state auto-persists to user_data_dir.

    Args:
        user_data_dir: Path to browser profile. Defaults to config's user_data_dir.
        superseded_by: The login generation the caller believes is broken, or
            ``None`` when it observed no session at all. Either way the login
            stands down if a *different* usable session is on disk by the time it
            holds the profile, because somebody else has already done the work.
            Left at :data:`UNGUARDED` by callers with nothing to compare against,
            such as ``--login`` typed at a terminal.

    Returns:
        True if login was successful, or a peer's session is already in place

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
    # Waited for rather than demanded outright, because this is also the path a
    # frontend takes when the shared browser asks it to sign in. A proxy holds no
    # lease of its own to reuse, so a momentary holder anywhere else would leave
    # the user with a login that refuses to open and no way to make it.
    #
    # Bounded by how long someone who just asked to sign in should be left
    # waiting, rather than by any worst case: a holder part way through a tool
    # call will not hand over until it finishes, and that can run to the tool
    # timeout. Past this the user is told the browser is busy, which they can act
    # on, and which beats a silent wait of several minutes.
    if not await lease.acquire(timeout=PROFILE_HANDOVER_WAIT_SECONDS):
        raise BrowserBusyError(
            "Another LinkedIn MCP client is using the browser, so a login "
            "cannot start. Close it and try again."
        )
    # Every exit runs through one finally: a login can fail before the browser
    # opens (rotation errors, cancellation) or after it closed cleanly but with
    # an exception, and each of those must still settle the profile correctly.
    state = _LoginState()
    try:
        # Checked here and nowhere earlier, because here is the first moment it
        # can be trusted: the profile is held, so nothing can change underneath
        # the answer. An earlier check, before the wait, is exactly what does not
        # work. Two clients meeting one dead session both look, both see it dead,
        # and both queue for the profile; the one that waited then holds a stale
        # opinion. It would rotate the session the winner has just created, which
        # was measured: the fresh generation ended up in quarantine and
        # `load_source_state` returned None.
        #
        # Inside the try, so a failure while asking still releases the profile.
        # Outside it, an unreadable profile directory left the lease held until
        # the process exited, which locks out every other client.
        if superseded_by is not UNGUARDED and a_peer_already_signed_in(
            user_data_dir, superseded_by
        ):
            print("   Another client already signed in; using its session.")
            return True

        login_kwargs = {"login_viewer": True} if login_viewer else {}
        return await _login_holding_the_profile(
            user_data_dir, lease, state, **login_kwargs
        )
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
    user_data_dir: Path,
    lease: ProfileLease,
    state: _LoginState,
    *,
    login_viewer: bool = False,
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
        login_kwargs = {"login_viewer": True} if login_viewer else {}
        succeeded = await _login_into_fresh_profile(
            user_data_dir,
            config=get_config(),
            state=state,
            **login_kwargs,
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
    user_data_dir: Path,
    *,
    config: Any,
    state: _LoginState,
    login_viewer: bool = False,
) -> bool:
    """Drive the manual login against a freshly retired profile directory.

    Records on *state* whether the login browser's shutdown was confirmed. The
    caller must not release the profile when it was not: Chromium may still be
    holding it.
    """
    login_timeout_seconds = config.browser.login_timeout_seconds
    login_timeout_ms = int(login_timeout_seconds * 1000)

    effective_timeout = login_timeout_seconds
    if login_viewer and (
        not effective_timeout or VIEWER_WALL_SECONDS < effective_timeout
    ):
        effective_timeout = VIEWER_WALL_SECONDS
    if effective_timeout:
        budget = f"{effective_timeout / 60:.0f} minutes"
    else:
        budget = "no time limit"

    print("Opening browser for LinkedIn login...")
    print(f"   Please log in manually. You have {budget} to complete authentication.")
    print("   (This handles 2FA, captcha, and any security challenges)")

    # The login browser must leave from the same address as later scrapes: a
    # session created on one IP and used from another is what trips LinkedIn's
    # security checkpoint. Shared with the runtime path rather than rebuilt
    # here, so a setting cannot apply to scraping but not to the login that
    # created the session.
    launch_options, viewport = build_launch_options(config.browser)
    describe_launch(launch_options)

    manager = BrowserManager(
        user_data_dir=user_data_dir,
        headless=False,
        slow_mo=config.browser.slow_mo,
        viewport=viewport,
        **launch_options,
    )
    viewer = LoginViewer() if login_viewer else None
    failure: BaseException | None = None
    try:
        if viewer is not None:
            viewer.start_window_manager()
        result = await _run_login(
            manager, user_data_dir, config, login_timeout_ms, viewer=viewer
        )
        if not result:
            failure = RuntimeError("login did not produce a portable session")
        return result
    except BaseException as exc:
        failure = exc
        raise
    finally:
        # In a finally so a login that times out or is cancelled still records a
        # teardown that did complete; otherwise an ordinary timeout would leave
        # the profile held for the rest of this process's life. Defaults to
        # confirmed for a manager that does not report it, so a stand-in cannot
        # wedge the profile.
        state.close_confirmed = bool(getattr(manager, "close_confirmed", True))
        if viewer is not None:
            try:
                viewer.stop_window_manager()
            except Exception as teardown_error:
                if failure is None:
                    raise
                print(f"   Warning: viewer teardown failed: {teardown_error}")


async def _run_login(
    manager: BrowserManager,
    user_data_dir: Path,
    config: Any,
    login_timeout_ms: int,
    *,
    viewer: LoginViewer | None = None,
) -> bool:
    async with manager as browser:
        if viewer is not None:
            url = viewer.start_remote_control()
            print(
                f"Open this loopback URL to control the login browser:\n{url}",
                flush=True,
            )
        failure: BaseException | None = None
        try:
            # Navigate to LinkedIn login
            await goto_reporting_proxy_errors(
                browser.page, "https://www.linkedin.com/login"
            )
            # Let LinkedIn finish rendering the saved-account chooser, then retry
            # the same exact click target before the normal manual-login wait.
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
                print(
                    "   Warning: Session cookie not found. Login may not have persisted."
                )
                print("   Waiting longer for cookie propagation...")
                await asyncio.sleep(5)

            # Export source-session cookies for the one-time foreign-runtime bridge.
            if await browser.export_cookies(portable_cookie_path(user_data_dir)):
                print("   Cookies exported for Docker portability")
                source_state = write_source_state(user_data_dir)
                print(f"   Source session generation: {source_state.login_generation}")
            else:
                print(
                    "   Warning: cookie export failed; Docker bridge may not work. "
                    "Run --login again to retry."
                )
                failure = RuntimeError("cookie export failed")
                return False
            print(f"Profile saved to {user_data_dir}")
            return True
        except BaseException as exc:
            failure = exc
            raise
        finally:
            # Close the public WebSocket and then loopback VNC before Chromium's
            # context manager closes the browser. Openbox follows outside it.
            if viewer is not None:
                try:
                    viewer.stop_remote_control()
                except Exception as teardown_error:
                    if failure is None:
                        raise
                    print(f"   Warning: viewer teardown failed: {teardown_error}")


class _ViewerInterrupted(Exception):
    """A container stop signal received after viewer cleanup completed."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"Docker login viewer interrupted by signal {signum}")


@contextmanager
def _viewer_signal_handlers(notify: Callable[[int], None]) -> Iterator[None]:
    """Schedule viewer cancellation without raising inside asyncio internals."""
    watched = [signal.SIGTERM, signal.SIGINT]
    if sighup := getattr(signal, "SIGHUP", None):
        watched.append(sighup)
    previous = {signum: signal.getsignal(signum) for signum in watched}

    def interrupted(signum: int, _frame: object) -> None:
        notify(signum)

    try:
        for signum in watched:
            signal.signal(signum, interrupted)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


async def _run_bounded_viewer_login(
    login: Coroutine[Any, Any, bool],
) -> bool:
    """Cancel the login cleanly at the hard deadline or on a stop signal."""
    loop = asyncio.get_running_loop()
    interrupted: asyncio.Future[int] = loop.create_future()

    def record_signal(signum: int) -> None:
        if not interrupted.done():
            interrupted.set_result(signum)

    def notify(signum: int) -> None:
        loop.call_soon_threadsafe(record_signal, signum)

    login_task = asyncio.create_task(login)
    cleanup_error: BaseException | None = None
    with _viewer_signal_handlers(notify):
        try:
            done, _pending = await asyncio.wait(
                {login_task, interrupted},
                timeout=VIEWER_WALL_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if login_task in done:
                return await login_task
        finally:
            if not login_task.done():
                login_task.cancel()
                try:
                    await login_task
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    cleanup_error = exc

    if cleanup_error is not None:
        print(f"   Warning: login cleanup failed: {cleanup_error}")
    if interrupted.done():
        raise _ViewerInterrupted(interrupted.result())
    raise TimeoutError


def run_profile_creation(
    user_data_dir: str | None = None, *, login_viewer: bool = False
) -> bool:
    """Create a persistent profile through an interactive browser login."""
    if user_data_dir:
        profile_dir = Path(user_data_dir).expanduser()
    else:
        profile_dir = get_profile_dir()

    print("LinkedIn MCP Server - Profile Creation")
    print(f"   Profile will be saved to: {profile_dir}")

    async def create() -> bool:
        login = interactive_login(profile_dir, login_viewer=login_viewer)
        if login_viewer:
            # This bounds the remote-control exposure even when LOGIN_TIMEOUT=0.
            # Cancellation still awaits profile restoration: abandoning a move on
            # the mounted auth root can leave the previous session split in two.
            return await _run_bounded_viewer_login(login)
        return await login

    try:
        return asyncio.run(create())
    except _ViewerInterrupted as exc:
        raise SystemExit(128 + exc.signum) from None
    except TimeoutError:
        print(
            f"Profile creation failed: Docker login viewer reached its hard "
            f"{VIEWER_WALL_SECONDS:.0f}-second limit"
        )
        return False
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
