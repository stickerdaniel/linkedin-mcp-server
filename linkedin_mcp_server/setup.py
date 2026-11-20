"""
Interactive setup flows for LinkedIn MCP Server authentication.

Handles session creation through interactive browser login using Patchright
with persistent context. Each login runs in its own attempt directory and its
session becomes the source profile once the browser has closed.
"""

import asyncio
from collections.abc import Callable, Coroutine, Generator
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
from linkedin_mcp_server.core.auth import PeerSignedInError
from linkedin_mcp_server.exceptions import BrowserBusyError
from linkedin_mcp_server.login_attempt import PendingLogin, open_pending_login, promote
from linkedin_mcp_server.login_viewer import LoginViewer, VIEWER_WALL_SECONDS
from linkedin_mcp_server.profile_lease import get_profile_lease
from linkedin_mcp_server.session_state import (
    UNGUARDED,
    a_peer_already_signed_in,
    load_source_state,
    run_deferring_cancels,
)

from linkedin_mcp_server.drivers.browser import close_browser, get_profile_dir

_PEER_SIGNED_IN = "   Another client already signed in; using its session."
_SIGNED_OUT_MEANWHILE = "   Signed out while this login was open; sign in again."


async def interactive_login(
    user_data_dir: Path | None = None,
    *,
    superseded_by: str | None | object = UNGUARDED,
    login_viewer: bool = False,
) -> bool:
    """
    Open browser for manual LinkedIn login in a fresh attempt directory.

    Opens a non-headless browser, navigates to LinkedIn login page,
    and waits for user to complete authentication (including 2FA, captcha, etc.).
    No profile lease is held meanwhile: the session is installed at
    *user_data_dir* under a short lease once the browser has closed.

    Args:
        user_data_dir: Path to browser profile. Defaults to config's user_data_dir.
        superseded_by: The login generation the caller believes is broken, or
            ``None`` when it observed no session at all. Either way the login
            stands down once a *different* usable session is on disk, because
            somebody else has already done the work. Left at
            :data:`UNGUARDED` by callers with nothing to compare against, such
            as ``--login`` typed at a terminal, which replaces what it finds.

    Returns:
        True if login was successful, or a peer's session is already in place

    Raises:
        Exception: If login fails or times out
    """
    if user_data_dir is None:
        user_data_dir = get_profile_dir()

    # Closing first so a later teardown cannot export the retired session's
    # cookies over the new ones (drivers.browser exports on close).
    await close_browser()

    guarded = superseded_by is not UNGUARDED

    def a_peer_published() -> bool:
        return guarded and a_peer_already_signed_in(user_data_dir, superseded_by)

    if a_peer_published():
        print(_PEER_SIGNED_IN)
        return True

    pending = open_pending_login(user_data_dir)
    state = _LoginState()
    try:
        try:
            signed_in = await _login_into_fresh_profile(
                pending,
                config=get_config(),
                state=state,
                stop_when=a_peer_published if guarded else None,
                login_viewer=login_viewer,
            )
        except PeerSignedInError:
            print(_PEER_SIGNED_IN)
            return True
        if not state.close_confirmed:
            print("   The login browser did not close; sign in again.")
            return False
        if not signed_in:
            return False
        # A loser that got its cookie after a peer published goes no further:
        # waiting for the lease would only find the peer's session there.
        if a_peer_published():
            print(_PEER_SIGNED_IN)
            return True
        return await _publish(pending, user_data_dir, superseded_by=superseded_by)
    finally:
        if state.close_confirmed:
            pending.discard()
        else:
            # The attempt's lock stays held, by this process and its guardian,
            # so no sweep deletes the directory under a Chromium that may
            # still be running. Freed when this process exits.
            print(
                "   Warning: the login browser did not shut down cleanly. "
                f"Its directory is kept at {pending.directory}."
            )


async def _publish(
    pending: PendingLogin,
    user_data_dir: Path,
    *,
    superseded_by: str | None | object,
) -> bool:
    """Install the attempt's session under the source lease."""
    # Again, because a tool call may have reopened it on the old session while
    # the login ran, and its close would export those cookies over the new ones.
    await close_browser()
    lease = get_profile_lease(user_data_dir)
    if not await lease.acquire(timeout=PROFILE_HANDOVER_WAIT_SECONDS):
        if superseded_by is not UNGUARDED and a_peer_already_signed_in(
            user_data_dir, superseded_by
        ):
            print(_PEER_SIGNED_IN)
            return True
        raise BrowserBusyError(
            "Another LinkedIn MCP client is using the browser, so the new "
            "session could not be saved. Close it and sign in again."
        )
    try:
        # Deferred rather than abandoned: the worker is already moving the
        # session, and dropping its result mid move would split it.
        outcome, cancelled = await run_deferring_cancels(
            functools.partial(promote, pending, superseded_by=superseded_by)
        )
    finally:
        lease.release()
    if cancelled:
        raise asyncio.CancelledError
    if outcome == "peer":
        print(_PEER_SIGNED_IN)
        return True
    if outcome == "fenced":
        print(_SIGNED_OUT_MEANWHILE)
        return False
    source_state = load_source_state(user_data_dir)
    if source_state is not None:
        print(f"   Source session generation: {source_state.login_generation}")
    print(f"Profile saved to {user_data_dir}")
    return True


@dataclass
class _LoginState:
    """Whether the login browser closed cleanly.

    Carried by reference so the caller can settle the attempt on every exit
    path, including the ones where no value is ever returned.
    """

    # Only a proven teardown deletes the attempt, so an exception or
    # cancellation that never reaches the assignment leaves it held rather
    # than claiming a shutdown nobody observed.
    close_confirmed: bool = False


async def _login_into_fresh_profile(
    pending: PendingLogin,
    *,
    config: Any,
    state: _LoginState,
    stop_when: Callable[[], bool] | None = None,
    login_viewer: bool = False,
) -> bool:
    """Drive the manual login in the attempt's empty profile directory.

    Records on *state* whether the login browser's shutdown was confirmed. The
    caller must not delete the attempt when it was not: Chromium may still be
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

    viewer: LoginViewer | None = None
    failure: BaseException | None = None
    handed_over = False
    try:
        # The login browser must leave from the same address as later reads: a session
        # created on one IP and used from another is what trips LinkedIn's security
        # checkpoint. Shared with the runtime path rather than rebuilt here, so a
        # setting cannot apply to page reading but not to the login that created the
        # session.
        launch_options, viewport = build_launch_options(config.browser)
        describe_launch(launch_options)
        manager = BrowserManager(
            user_data_dir=pending.profile_dir,
            headless=False,
            slow_mo=config.browser.slow_mo,
            viewport=viewport,
            **launch_options,
        )
        viewer = LoginViewer() if login_viewer else None
        if viewer is not None:
            viewer.start_window_manager()
        # ``_run_login`` opens the manager on its first line, so from here on
        # the manager's verdict is an answer about this attempt. Before here it
        # is only its pessimistic default, which is not the same thing.
        handed_over = True
        result = await _run_login(
            manager,
            pending.cookie_path,
            login_timeout_ms,
            stop_when=stop_when,
            viewer=viewer,
        )
        if not result:
            failure = RuntimeError("login did not produce a portable session")
        return result
    except BaseException as exc:
        failure = exc
        raise
    finally:
        # In a finally so a login that times out or is cancelled still records a
        # teardown that did complete. Defaults to confirmed for a manager that
        # does not report it, so a stand-in cannot wedge the attempt, and for
        # one never opened, which cannot be holding anything.
        state.close_confirmed = (
            bool(getattr(manager, "close_confirmed", True)) if handed_over else True
        )
        if viewer is not None:
            try:
                viewer.stop_window_manager()
            except Exception as teardown_error:
                if failure is None:
                    raise
                print(f"   Warning: viewer teardown failed: {teardown_error}")


async def _run_login(
    manager: BrowserManager,
    cookie_path: Path,
    login_timeout_ms: int,
    *,
    stop_when: Callable[[], bool] | None = None,
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
            await wait_for_manual_login(
                browser.page, timeout=login_timeout_ms, stop_when=stop_when
            )

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
            if not await browser.export_cookies(cookie_path):
                print(
                    "   Warning: cookie export failed; Docker bridge may not work. "
                    "Run --login again to retry."
                )
                failure = RuntimeError("cookie export failed")
                return False
            print("   Cookies exported for Docker portability")
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
def _viewer_signal_handlers(notify: Callable[[int], None]) -> Generator[None]:
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
