"""How the profile lease is wired into the tool path and destructive operations.

`test_profile_lease.py` covers the lock itself. These tests cover the promises
that make it safe to rely on: contention never looks like an authentication
failure, and nothing deletes or moves the profile while a process is using it.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.exceptions import BrowserBusyError
from linkedin_mcp_server.profile_lease import get_profile_lease
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.session_state import clear_auth_state, rotate_source_profile

_WORKER = Path(__file__).parent / "helpers" / "profile_lease_worker.py"


def _await_line(process: subprocess.Popen[str], expected: str) -> None:
    """Block until *process* prints a line containing *expected*.

    Fails rather than hangs when the worker dies early: ``readline`` returns
    ``""`` forever at EOF, so a loop that only checked its content would spin
    until the CI job's own timeout with no explanation.
    """
    assert process.stdout is not None
    while True:
        line = process.stdout.readline()
        if expected in line:
            return
        if line == "":  # EOF: the worker will never say anything more
            break
    stderr = process.stderr.read() if process.stderr else ""
    raise AssertionError(
        f"worker exited (status {process.poll()}) without reporting "
        f"{expected!r}: {stderr}"
    )


def _hold_profile(auth_root: Path, seconds: float) -> subprocess.Popen[str]:
    """Spawn a process that owns *auth_root*'s lease, and wait until it does."""
    process = subprocess.Popen(
        [sys.executable, str(_WORKER), "hold", str(auth_root), str(seconds)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _await_line(process, "HELD")
    except AssertionError:
        process.kill()
        raise
    return process


def _call_context(tool_name: str = "get_person_profile") -> MagicMock:
    context = MagicMock()
    context.message.name = tool_name
    context.fastmcp_context = None
    return context


class TestBusyIsNotAnAuthFailure:
    """The single most important guarantee in this change.

    `AuthenticationError` is routed into `invalidate_auth_and_trigger_relogin`,
    which force-retires the shared profile. If contention were classified that
    way, a process that merely lost a race would destroy every other process's
    session — the exact opposite of what the lease is for.
    """

    def test_busy_error_is_not_an_authentication_error(self) -> None:
        from linkedin_mcp_server.core.exceptions import AuthenticationError
        from linkedin_mcp_server.exceptions import LinkedInMCPError

        assert issubclass(BrowserBusyError, LinkedInMCPError)
        assert not issubclass(BrowserBusyError, AuthenticationError)

    def test_error_handler_reports_it_without_issue_diagnostics(self) -> None:
        from linkedin_mcp_server.error_handler import raise_tool_error

        with pytest.raises(ToolError) as excinfo:
            raise_tool_error(BrowserBusyError(), "get_person_profile")

        message = str(excinfo.value)
        assert "using the browser" in message
        assert "session was not changed" in message
        # A bug report would carry diagnostics; contention is not a bug.
        assert "issue" not in message.lower()

    async def test_middleware_reports_busy_without_triggering_relogin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # argparse otherwise reads pytest's own command line.
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
        monkeypatch.setenv("BROWSER_WAIT", "0.5")

        holder = _hold_profile(tmp_path, 30)
        try:
            lease = get_profile_lease(tmp_path / "profile")
            with (
                patch(
                    "linkedin_mcp_server.sequential_tool_middleware.get_profile_lease",
                    return_value=lease,
                ),
                patch(
                    "linkedin_mcp_server.bootstrap.invalidate_auth_and_trigger_relogin",
                    new_callable=AsyncMock,
                ) as relogin,
            ):
                middleware = SequentialToolExecutionMiddleware()
                call_next = AsyncMock()

                with pytest.raises(ToolError, match="using the browser"):
                    await middleware.on_call_tool(_call_context(), call_next)

                call_next.assert_not_awaited()
                relogin.assert_not_awaited()
        finally:
            holder.kill()
            holder.wait(timeout=10)


class TestDestructiveOperationsRefuse:
    def test_rotate_refuses_while_another_process_owns_the_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir(parents=True)
        (profile / "Default").mkdir()
        (profile / "Default" / "Cookies").write_text("x")

        holder = _hold_profile(tmp_path, 30)
        try:
            with pytest.raises(RuntimeError, match="in use by another process"):
                rotate_source_profile(profile)
            assert (profile / "Default" / "Cookies").exists(), (
                "the profile was moved despite another process using it"
            )
        finally:
            holder.kill()
            holder.wait(timeout=10)

    def test_clear_refuses_while_another_process_owns_the_profile(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir(parents=True)
        (profile / "Default").mkdir()
        (profile / "Default" / "Cookies").write_text("x")

        holder = _hold_profile(tmp_path, 30)
        try:
            with pytest.raises(RuntimeError, match="in use by another process"):
                clear_auth_state(profile)
            assert (profile / "Default" / "Cookies").exists(), (
                "the profile was deleted out from under a live browser"
            )
        finally:
            holder.kill()
            holder.wait(timeout=10)

    def test_rotate_refuses_while_this_process_has_a_browser_open(
        self, tmp_path: Path
    ) -> None:
        """The reference count alone cannot answer "is our own Chromium live?".

        A destructive helper asking our own lease for a reference simply gets
        one, so without an explicit flag rotation would move the profile out
        from under this process's running browser. That matters most after a
        close whose shutdown could not be confirmed, where the lease is kept
        precisely because Chromium may still be alive.
        """
        profile = tmp_path / "profile"
        profile.mkdir(parents=True)
        (profile / "Default").mkdir()
        (profile / "Default" / "Cookies").write_text("x")

        lease = get_profile_lease(profile)
        assert lease.try_acquire()
        lease.mark_browser_open()
        try:
            with pytest.raises(RuntimeError, match="browser open on the profile"):
                rotate_source_profile(profile)
            with pytest.raises(RuntimeError, match="browser open on the profile"):
                clear_auth_state(profile)
            assert (profile / "Default" / "Cookies").exists()
        finally:
            lease.mark_browser_closed()
            lease.release()

    def test_rotate_proceeds_once_our_browser_is_confirmed_closed(
        self, tmp_path: Path
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir(parents=True)
        (profile / "Default").mkdir()
        (profile / "Default" / "Cookies").write_text("x")

        lease = get_profile_lease(profile)
        assert lease.try_acquire()
        lease.mark_browser_open()
        lease.mark_browser_closed()
        lease.release()

        assert rotate_source_profile(profile) is not None

    def test_rotate_succeeds_once_the_profile_is_free(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir(parents=True)
        (profile / "Default").mkdir()
        (profile / "Default" / "Cookies").write_text("x")

        backup = rotate_source_profile(profile)
        assert backup is not None
        assert (backup / "profile" / "Default" / "Cookies").exists()


class TestMutationHoldsTheProfile:
    """The lease is held *through* the mutation, not merely checked before it.

    Checking and releasing first leaves a window in which another process
    launches Chromium against the very files being moved.
    """

    def test_rotation_holds_the_lease_while_it_moves_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = tmp_path / "profile"
        profile.mkdir(parents=True)
        (profile / "Default").mkdir()
        (profile / "Default" / "Cookies").write_text("x")

        import shutil as shutil_module

        from linkedin_mcp_server import session_state

        observed: list[bool] = []
        real_move = shutil_module.move

        def spy(src: str, dst: str):  # type: ignore[no-untyped-def]
            # Ask a *separate* lease object, so this reflects what another
            # process would see rather than our own reference count.
            from linkedin_mcp_server.profile_lease import ProfileLease

            probe = ProfileLease(tmp_path)
            free = probe.try_acquire()
            if free:
                probe.release()
            observed.append(free)
            return real_move(src, dst)

        monkeypatch.setattr(session_state.shutil, "move", spy)
        assert rotate_source_profile(profile) is not None

        assert observed, "rotation moved nothing, so the test proved nothing"
        assert not any(observed), (
            "the profile was acquirable mid-rotation: another process could have "
            "launched Chromium against the files being moved"
        )


class TestIdleOwnerHandsOver:
    """An owner that has gone idle must still notice a waiter.

    Probing only after each tool call is not enough: a process that announces
    itself once the owner has stopped working would wait out its whole budget
    and get a busy error while the owner sat doing nothing.
    """

    async def test_poller_releases_the_profile_to_a_waiter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from linkedin_mcp_server.drivers import browser as browser_module

        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
        monkeypatch.setattr(browser_module, "_HANDOFF_POLL_INTERVAL_SECONDS", 0.05)

        lease = get_profile_lease(tmp_path / "profile")
        assert lease.try_acquire()
        lease.mark_browser_open()

        fake_browser = MagicMock()
        fake_browser.close = AsyncMock(return_value=True)

        with (
            patch.multiple(
                browser_module,
                _browser=fake_browser,
                _browser_cookie_export_path=None,
                _browser_holds_lease=True,
            ),
            patch.object(browser_module, "get_profile_lease", return_value=lease),
        ):
            watcher = asyncio.create_task(browser_module.watch_for_handoff_requests())
            waiter = subprocess.Popen(
                [
                    sys.executable,
                    str(_WORKER),
                    "announce",
                    # The auth root is the profile's *parent*, which is tmp_path
                    # itself. Deriving it by trimming the "/profile" suffix off
                    # the string worked only on POSIX separators.
                    str(tmp_path),
                    "5",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                _await_line(waiter, "ANNOUNCED")

                # No tool call happens here: only the poller can notice.
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and lease.held:
                    await asyncio.sleep(0.05)

                assert not lease.held, (
                    "an idle owner never handed the profile to a waiting process"
                )
                fake_browser.close.assert_awaited()
            finally:
                watcher.cancel()
                waiter.kill()
                waiter.wait(timeout=10)


class TestPollerDoesNotInterruptWork:
    """The poller runs outside the tool-call lock, so it must not close a
    browser a call is currently using: the tool holds a Page from it and would
    fail with a closed-target error."""

    async def test_poller_leaves_an_in_flight_call_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from linkedin_mcp_server.drivers import browser as browser_module

        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])

        lease = get_profile_lease(tmp_path / "profile")
        assert lease.try_acquire()
        lease.mark_browser_open()

        fake_browser = MagicMock()
        fake_browser.close = AsyncMock(return_value=True)

        waiter = subprocess.Popen(
            [sys.executable, str(_WORKER), "announce", str(tmp_path), "5"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _await_line(waiter, "ANNOUNCED")

            with (
                patch.multiple(
                    browser_module,
                    _browser=fake_browser,
                    _browser_cookie_export_path=None,
                    _browser_holds_lease=True,
                ),
                patch.object(browser_module, "get_profile_lease", return_value=lease),
            ):
                browser_module.note_call_started()
                closed = await browser_module.release_profile_if_idle_or_requested()
                assert not closed, "the poller closed a browser mid tool call"
                fake_browser.close.assert_not_awaited()

                # Once the call finishes and the hold window has passed, the
                # handoff goes through.
                browser_module.note_activity()
                monkeypatch.setenv("BROWSER_MIN_HOLD", "0")
                from linkedin_mcp_server.config import reset_config

                reset_config()
                assert await browser_module.release_profile_if_idle_or_requested()
                fake_browser.close.assert_awaited()
        finally:
            waiter.kill()
            waiter.wait(timeout=10)


class TestMinimumHoldWindow:
    """The hold window bounds how often ownership can move.

    Every handoff costs a browser reopen, and a reopen re-validates `/feed/`, so
    two busy clients trading the browser on every call would multiply LinkedIn
    requests. The window is measured from when the profile was taken, not from
    idleness: by the time the check runs the call has already finished, so an
    idle-based test would always pass and the window would silently never apply.
    """

    async def test_recent_owner_keeps_the_browser(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from linkedin_mcp_server.drivers import browser as browser_module

        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
        monkeypatch.setenv("BROWSER_MIN_HOLD", "300")

        lease = get_profile_lease(tmp_path / "profile")
        assert lease.try_acquire()
        lease.mark_browser_open()

        fake_browser = MagicMock()
        fake_browser.close = AsyncMock(return_value=True)

        waiter = subprocess.Popen(
            [sys.executable, str(_WORKER), "announce", str(tmp_path), "5"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _await_line(waiter, "ANNOUNCED")

            with (
                patch.multiple(
                    browser_module,
                    _browser=fake_browser,
                    _browser_cookie_export_path=None,
                    _browser_holds_lease=True,
                ),
                patch.object(browser_module, "get_profile_lease", return_value=lease),
            ):
                browser_module.note_activity()  # a call just finished
                closed = await browser_module.release_profile_if_idle_or_requested()

            assert not closed, (
                "handed the browser over inside the hold window, so busy clients "
                "would trade it on every call and multiply feed validations"
            )
            fake_browser.close.assert_not_awaited()
        finally:
            waiter.kill()
            waiter.wait(timeout=10)
            lease.mark_browser_closed()
            lease.release()


class TestFailedLoginStillRestores:
    """A failed login must put the previous session back.

    Restore takes the profile exclusively, and the login flow marks its own
    browser live, so the liveness flag has to be cleared first on a confirmed
    close — otherwise every failed login refuses to restore and the user is left
    logged out of a session that was working.
    """

    async def test_restore_runs_after_a_failed_login(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from linkedin_mcp_server import setup as setup_module

        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
        profile = tmp_path / "profile"
        profile.mkdir(parents=True)
        (profile / "Default").mkdir()
        (profile / "Default" / "Cookies").write_text("previous session")

        async def failing_login(user_data_dir, *, config, state):  # type: ignore[no-untyped-def]
            state.close_confirmed = True  # the browser did close cleanly
            return False

        monkeypatch.setattr(setup_module, "_login_into_fresh_profile", failing_login)
        monkeypatch.setattr(setup_module, "close_browser", AsyncMock(return_value=None))

        assert await setup_module.interactive_login(profile) is False
        assert (profile / "Default" / "Cookies").read_text() == "previous session", (
            "the previous session was not restored after a failed login"
        )


class TestConfirmedClose:
    """The lease is released only when Chromium is confirmed gone.

    `BrowserManager.close()` bounds its cleanup steps and swallows their
    failures, so it can return while Chromium is still running. Releasing the
    profile then would hand it to another process mid-shutdown.
    """

    async def test_unconfirmed_close_keeps_the_lease(self, tmp_path: Path) -> None:
        from linkedin_mcp_server.drivers import browser as browser_module

        lease = get_profile_lease(tmp_path / "profile")
        assert lease.try_acquire()

        fake_browser = MagicMock()
        fake_browser.close = AsyncMock(return_value=False)  # shutdown not confirmed

        monkey = patch.multiple(
            browser_module,
            _browser=fake_browser,
            _browser_cookie_export_path=None,
            _browser_holds_lease=True,
        )
        with monkey:
            with patch.object(browser_module, "get_profile_lease", return_value=lease):
                await browser_module.close_browser()

        assert lease.held, "the lease was released on an unconfirmed close"
        lease.release()

    async def test_confirmed_close_releases_the_lease(self, tmp_path: Path) -> None:
        from linkedin_mcp_server.drivers import browser as browser_module

        lease = get_profile_lease(tmp_path / "profile")
        assert lease.try_acquire()

        fake_browser = MagicMock()
        fake_browser.close = AsyncMock(return_value=True)

        with patch.multiple(
            browser_module,
            _browser=fake_browser,
            _browser_cookie_export_path=None,
            _browser_holds_lease=True,
        ):
            with patch.object(browser_module, "get_profile_lease", return_value=lease):
                await browser_module.close_browser()

        assert not lease.held
