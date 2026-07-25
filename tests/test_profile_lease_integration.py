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


def _hold_profile(auth_root: Path, seconds: float) -> subprocess.Popen[str]:
    """Spawn a process that owns *auth_root*'s lease, and wait until it does."""
    process = subprocess.Popen(
        [sys.executable, str(_WORKER), "hold", str(auth_root), str(seconds)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if "HELD" in process.stdout.readline():
            return process
    process.kill()
    raise AssertionError("helper never acquired the lease")


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

    def test_rotate_succeeds_once_the_profile_is_free(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        profile.mkdir(parents=True)
        (profile / "Default").mkdir()
        (profile / "Default" / "Cookies").write_text("x")

        backup = rotate_source_profile(profile)
        assert backup is not None
        assert (backup / "profile" / "Default" / "Cookies").exists()


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
