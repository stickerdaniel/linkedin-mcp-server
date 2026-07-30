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


class TestALoginWaitsForTheProfile:
    """A login asks for the profile and waits, rather than demanding it at once.

    This is also the path a frontend takes when the shared browser asks it to sign
    in, and a proxy holds no lease of its own to reuse. Left non-blocking, any
    momentary holder anywhere on the machine leaves the user with a login that
    refuses to open and nothing they can do about it.
    """

    async def test_it_waits_for_a_real_holder_to_let_go(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server.setup import interactive_login

        auth_root = isolate_profile_dir.parent
        # A real second process, because the lease is a kernel lock: an in-process
        # stand-in would be reference-counted and simply succeed.
        holder = _hold_profile(auth_root, seconds=1.5)

        opened: list[bool] = []

        async def login_once_we_have_the_profile(user_data_dir, lease, state):
            opened.append(True)
            return True

        monkeypatch.setattr(
            "linkedin_mcp_server.setup._login_holding_the_profile",
            login_once_we_have_the_profile,
        )
        monkeypatch.setattr("linkedin_mcp_server.setup.close_browser", AsyncMock())

        try:
            assert await interactive_login(isolate_profile_dir) is True
        finally:
            holder.wait(timeout=10)

        assert opened == [True]

    async def test_it_still_gives_up_on_a_holder_that_never_releases(
        self, isolate_profile_dir, monkeypatch
    ):
        # Bounded, not indefinite. Past the handover window the holder is a process
        # that is not going to release, and hanging would be worse than saying so.
        from linkedin_mcp_server.exceptions import BrowserBusyError
        from linkedin_mcp_server.setup import interactive_login

        auth_root = isolate_profile_dir.parent
        holder = _hold_profile(auth_root, seconds=30)

        monkeypatch.setattr(
            "linkedin_mcp_server.setup.PROFILE_HANDOVER_WAIT_SECONDS", 0.3
        )
        monkeypatch.setattr("linkedin_mcp_server.setup.close_browser", AsyncMock())
        never = MagicMock()
        monkeypatch.setattr(
            "linkedin_mcp_server.setup._login_holding_the_profile", never
        )

        try:
            with pytest.raises(BrowserBusyError):
                await interactive_login(isolate_profile_dir)
        finally:
            holder.kill()
            holder.wait(timeout=10)

        never.assert_not_called()


class TestALateLoginDoesNotUndoAnEarlierOne:
    """The window the generation check in `interactive_login` exists for.

    Two clients told to sign in for one dead session both look, both see it dead,
    and both queue for the profile. The one that waited then holds an opinion
    formed before the winner finished. Its login rotates the profile as soon as it
    has the lease, and rotation does not ask whose session it is retiring.

    So the check cannot live where the decision to log in is made. It has to be
    where the profile is actually held, which is the first moment the answer
    cannot change underneath it.
    """

    def _write_session(self, profile_dir) -> str:
        """Put a usable session on disk and return its generation."""
        from linkedin_mcp_server.session_state import (
            portable_cookie_path,
            write_source_state,
        )

        (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
        (profile_dir / "Default" / "Cookies").write_text("placeholder")
        portable_cookie_path(profile_dir).write_text("[]")
        return write_source_state(profile_dir).login_generation

    def _login_without_a_browser(self, monkeypatch) -> list[bool]:
        """Stub only the browser half, so the real rotation still runs.

        Returns a list that gains an entry each time a login would have opened,
        which is how the tests below tell "stood down" from "signed in".
        """
        import linkedin_mcp_server.setup as setup
        from linkedin_mcp_server.config import set_config
        from linkedin_mcp_server.config.schema import AppConfig

        # Installed rather than loaded: `_login_holding_the_profile` calls
        # get_config(), which parses sys.argv, and under pytest that is pytest's
        # own command line.
        set_config(AppConfig())

        opened: list[bool] = []

        async def signed_in(user_data_dir, config, state):
            opened.append(True)
            state.browser_opened = True
            state.close_confirmed = True
            return True

        monkeypatch.setattr(setup, "_login_into_fresh_profile", signed_in)
        monkeypatch.setattr(setup, "close_browser", AsyncMock())
        return opened

    async def test_without_the_generation_the_later_login_destroys_the_earlier(
        self, isolate_profile_dir, monkeypatch
    ):
        # The damage, reproduced, so the guarded case below is not asserting thin
        # air. This is what the old call shape did.
        from linkedin_mcp_server.session_state import load_source_state
        from linkedin_mcp_server.setup import interactive_login

        self._write_session(isolate_profile_dir)
        self._login_without_a_browser(monkeypatch)

        # The winner finishes and writes a fresh generation.
        fresh = self._write_session(isolate_profile_dir)

        # The loser's login proceeds anyway, knowing nothing.
        assert await interactive_login(isolate_profile_dir) is True

        surviving = load_source_state(isolate_profile_dir)
        assert surviving is None or surviving.login_generation != fresh

    async def test_the_generation_makes_the_later_login_stand_down(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server.session_state import load_source_state
        from linkedin_mcp_server.setup import interactive_login

        stale = self._write_session(isolate_profile_dir)
        self._login_without_a_browser(monkeypatch)

        fresh = self._write_session(isolate_profile_dir)
        assert fresh != stale

        # Reports success, because from the caller's point of view there is now a
        # usable session, which is what it asked for.
        assert await interactive_login(isolate_profile_dir, superseded_by=stale) is True

        surviving = load_source_state(isolate_profile_dir)
        assert surviving is not None
        assert surviving.login_generation == fresh

    async def test_it_still_logs_in_when_the_session_really_is_the_dead_one(
        self, isolate_profile_dir, monkeypatch
    ):
        # The guard must not stop the client that is right, which is every
        # ordinary case: same generation, so nobody has repaired anything.
        from linkedin_mcp_server.setup import interactive_login

        stale = self._write_session(isolate_profile_dir)

        opened = self._login_without_a_browser(monkeypatch)

        assert await interactive_login(isolate_profile_dir, superseded_by=stale) is True

        assert opened == [True]

    async def test_an_abandoned_peer_attempt_does_not_stop_the_next_login(
        self, isolate_profile_dir, monkeypatch
    ):
        # A rotated profile has no generation, which also differs from the stale
        # one. Standing down there would mean nobody ever signs in.
        from linkedin_mcp_server.session_state import (
            load_source_state,
            rotate_source_profile,
        )
        from linkedin_mcp_server.setup import interactive_login

        stale = self._write_session(isolate_profile_dir)

        opened = self._login_without_a_browser(monkeypatch)

        rotate_source_profile(isolate_profile_dir)
        assert load_source_state(isolate_profile_dir) is None

        assert await interactive_login(isolate_profile_dir, superseded_by=stale) is True

        assert opened == [True]

    async def test_a_new_generation_without_a_usable_session_does_not_stop_it(
        self, isolate_profile_dir, monkeypatch
    ):
        """A different generation is not on its own proof that anyone succeeded.

        The abandoned case above never reaches this: rotation removes the state
        file entirely, so the check stops at "there is no generation". This is the
        other shape, where a generation *is* written and the session still is not
        usable, and it is what the readiness half of the condition is for.
        Measured: without it, the login stands down while nothing on disk works,
        and the user is left with no session and no way to get one.
        """
        from linkedin_mcp_server.session_state import (
            portable_cookie_path,
        )
        from linkedin_mcp_server.setup import interactive_login

        stale = self._write_session(isolate_profile_dir)
        opened = self._login_without_a_browser(monkeypatch)

        # A newer generation, but the cookies that make it usable are gone.
        assert self._write_session(isolate_profile_dir) != stale
        portable_cookie_path(isolate_profile_dir).unlink()

        assert await interactive_login(isolate_profile_dir, superseded_by=stale) is True

        assert opened == [True]

    async def test_an_observed_empty_profile_is_guarded_too(
        self, isolate_profile_dir, monkeypatch
    ):
        """Two clients meeting an empty profile need the same protection.

        A profile with no session reads as no generation at all, so the observed
        value and "nothing asked for a guard" would be the same thing if both were
        spelled None. Measured with them conflated: the second client rotated away
        the session the first had just created, on a profile that started empty.
        """
        from linkedin_mcp_server.session_state import load_source_state
        from linkedin_mcp_server.setup import interactive_login

        opened = self._login_without_a_browser(monkeypatch)
        assert load_source_state(isolate_profile_dir) is None

        # The winner signs in where there was nothing.
        fresh = self._write_session(isolate_profile_dir)

        # The loser decided when the profile was empty, so it observed None.
        assert await interactive_login(isolate_profile_dir, superseded_by=None) is True

        surviving = load_source_state(isolate_profile_dir)
        assert surviving is not None
        assert surviving.login_generation == fresh
        assert opened == []

    async def test_a_login_with_nothing_to_compare_is_left_alone(
        self, isolate_profile_dir, monkeypatch
    ):
        # `--login` typed at a terminal has no peer to defer to and must always
        # sign in, however new the session on disk looks.
        from linkedin_mcp_server.setup import interactive_login

        self._write_session(isolate_profile_dir)
        opened = self._login_without_a_browser(monkeypatch)
        self._write_session(isolate_profile_dir)

        assert await interactive_login(isolate_profile_dir) is True

        assert opened == [True]

    async def test_the_guard_releases_the_profile_when_it_fails(
        self, isolate_profile_dir, monkeypatch
    ):
        # The check runs with the profile held, so a failure inside it has to
        # release. Left outside the try, an unreadable profile directory kept the
        # lease until the process exited, locking out every other client.
        import linkedin_mcp_server.setup as setup
        from linkedin_mcp_server.profile_lease import get_profile_lease
        from linkedin_mcp_server.setup import interactive_login

        self._login_without_a_browser(monkeypatch)
        monkeypatch.setattr(
            setup,
            "a_peer_already_signed_in",
            MagicMock(side_effect=PermissionError("cannot read the profile")),
        )

        with pytest.raises(PermissionError):
            await interactive_login(isolate_profile_dir, superseded_by="g0")

        assert get_profile_lease(isolate_profile_dir).held is False


class TestALateImportDoesNotUndoALogin:
    """The import rotates the profile too, so it needs the same guard.

    `start_login_if_needed` tries an auto-import before it opens a login window,
    and the import retires the current session as soon as it holds the profile.
    A client whose keychain read or profile discovery ran long is exactly the late
    arrival this protects against.
    """

    def _write_session(self, profile_dir) -> str:
        from linkedin_mcp_server.session_state import (
            portable_cookie_path,
            write_source_state,
        )

        (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
        (profile_dir / "Default" / "Cookies").write_text("placeholder")
        portable_cookie_path(profile_dir).write_text("[]")
        return write_source_state(profile_dir).login_generation

    def _import_without_a_browser(self, monkeypatch) -> list[bool]:
        """Stub discovery and validation; the real rotation still runs."""
        import linkedin_mcp_server.browser_import.orchestrate as orchestrate
        from linkedin_mcp_server.config import set_config
        from linkedin_mcp_server.config.schema import AppConfig

        set_config(AppConfig())
        committed: list[bool] = []

        async def imported(live, cookie_path, user_data_dir):
            committed.append(True)
            return True

        monkeypatch.setattr(orchestrate, "_import_first_accepted", imported)
        # Imported inside the function, so patched at the source module.
        monkeypatch.setattr(
            "linkedin_mcp_server.drivers.browser.close_browser", AsyncMock()
        )
        monkeypatch.setattr(
            orchestrate,
            "_discover_and_rank",
            MagicMock(return_value=([(MagicMock(), MagicMock())], [])),
        )
        return committed

    async def test_a_late_import_stands_down(self, isolate_profile_dir, monkeypatch):
        from linkedin_mcp_server.browser_import.orchestrate import (
            import_session_from_browser,
        )
        from linkedin_mcp_server.session_state import load_source_state

        stale = self._write_session(isolate_profile_dir)
        committed = self._import_without_a_browser(monkeypatch)

        # A peer signs in while this import is still reading the keychain.
        fresh = self._write_session(isolate_profile_dir)

        assert (
            await import_session_from_browser(
                None, user_data_dir=isolate_profile_dir, superseded_by=stale
            )
            is True
        )

        surviving = load_source_state(isolate_profile_dir)
        assert surviving is not None
        assert surviving.login_generation == fresh
        assert committed == []

    async def test_an_import_with_nothing_to_compare_still_runs(
        self, isolate_profile_dir, monkeypatch
    ):
        # `--import-from-browser` typed at a terminal has no peer to defer to.
        from linkedin_mcp_server.browser_import.orchestrate import (
            import_session_from_browser,
        )

        self._write_session(isolate_profile_dir)
        committed = self._import_without_a_browser(monkeypatch)

        assert (
            await import_session_from_browser(None, user_data_dir=isolate_profile_dir)
            is True
        )

        assert committed == [True]
