import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import json
import logging
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from typing import IO, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.bootstrap import (
    AuthState,
    _auto_import_allowed,
    _CACHE_REPORT_MARKER,
    _force_move_auth_state_aside,
    _has_install_for,
    _patchright_install_targets,
    _report_retained_browser_revisions,
    _start_login_if_needed,
    browser_setup_ready,
    browsers_path,
    configure_browser_environment,
    ensure_browser_installed,
    ensure_tool_ready_or_raise,
    browser_ready,
    get_bootstrap_state,
    get_runtime_policy,
    initialize_bootstrap,
    install_metadata_path,
    invalidate_auth_and_trigger_relogin,
    invalidate_browser_setup,
    reset_bootstrap_for_testing,
    RuntimePolicy,
    SetupState,
    start_background_browser_setup_if_needed,
)
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.core.exceptions import NetworkError
from linkedin_mcp_server.exceptions import (
    AuthenticationBootstrapFailedError,
    AuthenticationInProgressError,
    AuthenticationStartedError,
    BrowserSetupInProgressError,
    CookieDecryptionError,
    DockerHostLoginRequiredError,
    LinkedInMCPError,
    NoLinkedInSessionFoundError,
)
from linkedin_mcp_server.session_state import (
    PeerSessionInPlaceError,
    portable_cookie_path,
    source_state_path,
)


def _patch_inline_wait(monkeypatch, seconds: float, *, auto_import=False) -> None:
    """Point bootstrap.get_config() at a config with the given inline wait.

    A FULL fake config (server + is_interactive) so _auto_import_allowed() never
    AttributeErrors on the fake regardless of predicate branch ordering.
    auto_import defaults False so existing inline-wait tests skip the import
    branch.
    """
    config = SimpleNamespace(
        browser=SimpleNamespace(
            login_inline_wait_seconds=seconds,
            auto_import_from_browser=auto_import,
            chrome_path=None,
            headless=True,
        ),
        server=SimpleNamespace(transport="stdio", host="127.0.0.1"),
        is_interactive=False,
    )
    monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)


async def _wait_event(event: asyncio.Event) -> None:
    """Await an event, returning None so the wrapping task is a Task[None]."""
    await event.wait()


class TestBootstrap:
    async def test_managed_startup_starts_background_setup(self, monkeypatch):
        async def fake_setup() -> None:
            return None

        _patch_inline_wait(monkeypatch, 0)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_setup_ready", lambda: False
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_browser_setup", fake_setup
        )

        initialize_bootstrap("managed")
        await start_background_browser_setup_if_needed()

        state = get_bootstrap_state()
        assert state.setup_state is SetupState.RUNNING
        assert state.setup_task is not None
        await state.setup_task

    async def test_setup_in_progress_raises(self, monkeypatch):
        _patch_inline_wait(monkeypatch, 0)
        initialize_bootstrap("managed")
        state = get_bootstrap_state()
        state.setup_state = SetupState.RUNNING
        state.setup_task = MagicMock(done=lambda: False)

        with pytest.raises(BrowserSetupInProgressError):
            await ensure_tool_ready_or_raise("search_jobs")

    async def test_missing_auth_starts_login(self, monkeypatch):
        async def fake_start_login(ctx=None, **_kwargs) -> None:
            raise AuthenticationStartedError(
                "No valid LinkedIn session was found. A login browser window has been opened. Sign in with your LinkedIn credentials there, then retry this tool."
            )

        _patch_inline_wait(monkeypatch, 0)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_setup_ready", lambda: True
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: False)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._start_login_if_needed", fake_start_login
        )

        initialize_bootstrap("managed")

        with pytest.raises(AuthenticationStartedError):
            await ensure_tool_ready_or_raise("get_person_profile")

    async def test_login_in_progress_reuses_existing_session(self, monkeypatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_setup_ready", lambda: True
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: False)
        _patch_inline_wait(monkeypatch, 0.05)

        # A real, still-running task so the inline wait can await it without
        # spawning a second login (singleton reuse).
        never_done = asyncio.Event()
        login_task: asyncio.Task[None] = asyncio.ensure_future(_wait_event(never_done))

        initialize_bootstrap("managed")
        state = get_bootstrap_state()
        state.auth_state = AuthState.IN_PROGRESS
        state.login_task = login_task

        try:
            with pytest.raises(AuthenticationInProgressError):
                await ensure_tool_ready_or_raise("get_person_profile")

            # The shared task survived the budget-elapsed wait.
            assert not login_task.cancelled()
            assert not login_task.done()
        finally:
            never_done.set()
            login_task.cancel()

    async def test_docker_requires_host_login(self, monkeypatch):
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: False)
        initialize_bootstrap("docker")
        with pytest.raises(DockerHostLoginRequiredError):
            await ensure_tool_ready_or_raise("search_jobs")

    def test_reset_bootstrap_clears_state(self):
        initialize_bootstrap("managed")
        reset_bootstrap_for_testing()
        state = get_bootstrap_state()
        assert state.runtime_policy is None
        assert state.initialized is False
        assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ

    def test_reset_bootstrap_clears_browser_env_var(self):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/tmp/stale-browser-cache"

        reset_bootstrap_for_testing()

        assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ

    def test_reset_bootstrap_cancels_running_tasks(self):
        setup_task = MagicMock()
        setup_task.done.return_value = False
        cache_report_task = MagicMock()
        cache_report_task.done.return_value = False
        login_task = MagicMock()
        login_task.done.return_value = False

        initialize_bootstrap("managed")
        state = get_bootstrap_state()
        state.setup_task = setup_task
        state.cache_report_task = cache_report_task
        state.login_task = login_task

        reset_bootstrap_for_testing()

        setup_task.cancel.assert_called_once_with()
        cache_report_task.cancel.assert_called_once_with()
        login_task.cancel.assert_called_once_with()

    def test_managed_browser_path_defaults_under_auth_root(self, isolate_profile_dir):
        path = browsers_path()
        assert path == isolate_profile_dir.parent / "patchright-browsers"

    def test_install_metadata_path_defaults_under_auth_root(self, isolate_profile_dir):
        path = install_metadata_path()
        assert path == isolate_profile_dir.parent / "browser-install.json"

    def test_runtime_policy_uses_initialized_value(self):
        initialize_bootstrap("managed")
        assert get_runtime_policy() == "managed"


def _make_auth_ready(profile_dir):
    """Create all files that _auth_ready() checks."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default" / "Cookies").write_text("placeholder")
    cookie_path = portable_cookie_path(profile_dir)
    cookie_path.parent.mkdir(parents=True, exist_ok=True)
    cookie_path.write_text(json.dumps([{"name": "li_at", "domain": ".linkedin.com"}]))
    source_state_path(profile_dir).write_text(
        json.dumps(
            {
                "version": 1,
                "source_runtime_id": "macos-arm64-host",
                "login_generation": "gen-1",
                "created_at": "2026-03-12T17:00:00Z",
                "profile_path": str(profile_dir),
                "cookies_path": str(cookie_path),
            }
        )
    )


class TestInvalidateAuthAndTriggerRelogin:
    async def test_force_moves_files_and_starts_login(
        self, isolate_profile_dir, monkeypatch
    ):
        """Stale-but-present profile files are moved aside and login starts."""
        _make_auth_ready(isolate_profile_dir)

        async def fake_login_flow():
            return None

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_login_flow", fake_login_flow
        )
        initialize_bootstrap("managed")

        with pytest.raises(AuthenticationStartedError, match="Session expired"):
            await invalidate_auth_and_trigger_relogin()

        # Profile files should have been moved aside.
        assert not isolate_profile_dir.exists()
        assert not portable_cookie_path(isolate_profile_dir).exists()
        assert not source_state_path(isolate_profile_dir).exists()

        state = get_bootstrap_state()
        assert state.auth_state is AuthState.STARTING
        assert state.login_task is not None

    async def test_login_in_progress_does_not_move_files(
        self, isolate_profile_dir, monkeypatch
    ):
        """If login is already running, raise InProgress without touching files."""
        _make_auth_ready(isolate_profile_dir)
        initialize_bootstrap("managed")

        state = get_bootstrap_state()
        state.login_task = MagicMock(done=lambda: False)
        state.auth_state = AuthState.IN_PROGRESS

        with pytest.raises(AuthenticationInProgressError):
            await invalidate_auth_and_trigger_relogin()

        # Files must NOT have been moved.
        assert isolate_profile_dir.exists()
        assert portable_cookie_path(isolate_profile_dir).exists()

    def test_force_move_skips_auth_ready_guard(self, isolate_profile_dir):
        """_force_move_auth_state_aside moves files even when _auth_ready() is True."""
        _make_auth_ready(isolate_profile_dir)

        # Confirm _auth_ready() would return True before the move.
        from linkedin_mcp_server.bootstrap import _auth_ready

        assert _auth_ready()

        _force_move_auth_state_aside()

        assert not isolate_profile_dir.exists()
        assert not portable_cookie_path(isolate_profile_dir).exists()
        assert not source_state_path(isolate_profile_dir).exists()


_DEFAULT_TARGETS = {
    "chromium-": "1217",
    "chromium_headless_shell-": "1217",
}
_PATCHRIGHT_VERSION = "1.41.0"


def _materialize_install(browsers_dir: Path, dirs: list[str]) -> None:
    browsers_dir.mkdir(parents=True, exist_ok=True)
    for name in dirs:
        d = browsers_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "INSTALLATION_COMPLETE").write_text("")
        (d / "DEPENDENCIES_VALIDATED").write_text("")


def _write_metadata(path: Path, browsers_dir: Path, **overrides) -> None:
    payload = {
        "version": 3,
        "runtime_id": "test-runtime",
        "installed_at": "2026-01-01T00:00:00Z",
        "browsers_path": str(browsers_dir),
        "browser_name": "chromium",
        "installer_name": "patchright",
        "patchright_version": _PATCHRIGHT_VERSION,
        "installed_targets": {
            "chromium-": True,
            "chromium_headless_shell-": True,
        },
        **overrides,
    }
    path.write_text(json.dumps(payload))


def _set_headless(monkeypatch, headless: bool) -> None:
    """Point bootstrap.get_config() at a config with the given headless mode."""
    config = SimpleNamespace(
        browser=SimpleNamespace(headless=headless, chrome_path=None),
    )
    monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)


def _patch_targets_and_version(
    monkeypatch, *, targets=_DEFAULT_TARGETS, version=_PATCHRIGHT_VERSION
):
    monkeypatch.setattr(
        "linkedin_mcp_server.bootstrap._patchright_install_targets",
        lambda: dict(targets) if targets else None,
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.bootstrap._patchright_pkg_version", lambda: version
    )


class TestBrowserSetupReady:
    """Shared metadata-shape coverage, exercised through browser_setup_ready().

    The default test config is headless, so browser_setup_ready() resolves to
    shell_ready(); the mode-aware split is covered separately below.
    """

    @pytest.fixture(autouse=True)
    def _headless_config(self, monkeypatch):
        _set_headless(monkeypatch, True)

    def test_false_when_metadata_absent(self, isolate_profile_dir, monkeypatch):
        _patch_targets_and_version(monkeypatch)
        assert browser_setup_ready() is False

    def test_false_when_browsers_dir_missing(self, isolate_profile_dir, monkeypatch):
        _patch_targets_and_version(monkeypatch)
        meta_dir = browsers_path()
        _write_metadata(install_metadata_path(), meta_dir)
        assert browser_setup_ready() is False

    def test_true_with_complete_install(self, isolate_profile_dir, monkeypatch):
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium_headless_shell-1217"])
        _write_metadata(install_metadata_path(), bdir)
        assert browser_setup_ready() is True

    def test_false_when_marker_missing(self, isolate_profile_dir, monkeypatch):
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "chromium-1217").mkdir()
        (bdir / "chromium_headless_shell-1217").mkdir()
        # No INSTALLATION_COMPLETE files
        _write_metadata(install_metadata_path(), bdir)
        assert browser_setup_ready() is False

    def test_false_when_required_revision_missing(
        self, isolate_profile_dir, monkeypatch
    ):
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1208", "chromium_headless_shell-1208"])
        _write_metadata(install_metadata_path(), bdir)
        assert browser_setup_ready() is False

    def test_false_on_pkg_version_mismatch(self, isolate_profile_dir, monkeypatch):
        _patch_targets_and_version(monkeypatch, version="1.42.0")
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium_headless_shell-1217"])
        _write_metadata(install_metadata_path(), bdir, patchright_version="1.41.0")
        assert browser_setup_ready() is False

    def test_false_on_browsers_path_mismatch(
        self, isolate_profile_dir, monkeypatch, tmp_path
    ):
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium_headless_shell-1217"])
        _write_metadata(
            install_metadata_path(), bdir, browsers_path=str(tmp_path / "elsewhere")
        )
        assert browser_setup_ready() is False

    def test_false_on_v1_metadata(self, isolate_profile_dir, monkeypatch):
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium_headless_shell-1217"])
        _write_metadata(install_metadata_path(), bdir, version=1)
        assert browser_setup_ready() is False

    def test_false_on_v2_metadata(self, isolate_profile_dir, monkeypatch):
        """A pre-shell-first v2 blob reads as not-ready under v3 (forced reinstall)."""
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium_headless_shell-1217"])
        _write_metadata(install_metadata_path(), bdir, version=2)
        assert browser_setup_ready() is False

    def test_false_on_corrupt_metadata(self, isolate_profile_dir, monkeypatch):
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium_headless_shell-1217"])
        bdir.mkdir(parents=True, exist_ok=True)
        install_metadata_path().write_text("not json {{{")
        assert browser_setup_ready() is False

    def test_false_when_registry_unreadable(self, isolate_profile_dir, monkeypatch):
        _patch_targets_and_version(monkeypatch, targets=None)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium_headless_shell-1217"])
        _write_metadata(install_metadata_path(), bdir)
        assert browser_setup_ready() is False

    def test_true_with_stale_old_revision_alongside_current(
        self, isolate_profile_dir, monkeypatch
    ):
        """Locks in: stale chromium-1208 doesn't break readiness when current 1217 is also present."""
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(
            bdir,
            [
                "chromium-1208",
                "chromium-1217",
                "chromium_headless_shell-1208",
                "chromium_headless_shell-1217",
            ],
        )
        _write_metadata(install_metadata_path(), bdir)
        assert browser_setup_ready() is True

    def test_false_when_only_stale_revision_present(
        self, isolate_profile_dir, monkeypatch
    ):
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1208", "chromium_headless_shell-1208"])
        _write_metadata(install_metadata_path(), bdir)
        assert browser_setup_ready() is False

    def test_true_when_marker_present_but_dir_partially_corrupted(
        self, isolate_profile_dir, monkeypatch
    ):
        """Documents the known gap: marker is set, but executable inside dir was deleted.

        Readiness still passes; the runtime catch-site in dependencies.py is
        the safety net that recovers from the eventual launch failure.
        """
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium_headless_shell-1217"])
        # Simulate partial corruption: marker stays, contents wiped.
        (bdir / "chromium-1217" / "DEPENDENCIES_VALIDATED").unlink()
        _write_metadata(install_metadata_path(), bdir)
        assert browser_setup_ready() is True


class TestBrowserReady:
    """One predicate now: is the browser this server launches installed?

    The old pair asked separately about the headless shell, because the launch
    picked a binary from the headless flag. Nothing launches the shell any more,
    so its presence or absence says nothing about readiness.
    """

    @pytest.fixture(autouse=True)
    def _headless_config(self, monkeypatch):
        _set_headless(monkeypatch, True)

    def test_ready_with_full_only(self, isolate_profile_dir, monkeypatch):
        """The case that used to read as permanently not ready.

        `full_chromium_ready` iterated every install-by-default target, so a
        full-only install failed the check forever. With a launch that demands
        the full browser, that is the install loop this change exists to avoid.
        """
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217"])
        _write_metadata(install_metadata_path(), bdir)
        assert browser_ready() is True

    def test_ready_with_full_and_shell(self, isolate_profile_dir, monkeypatch):
        """An install from before this change still counts."""
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium_headless_shell-1217"])
        _write_metadata(install_metadata_path(), bdir)
        assert browser_ready() is True

    def test_not_ready_with_shell_only(self, isolate_profile_dir, monkeypatch):
        """The shell alone is not a browser this server can launch."""
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium_headless_shell-1217"])
        _write_metadata(install_metadata_path(), bdir)
        assert browser_ready() is False

    def test_not_ready_when_metadata_shape_bad(self, isolate_profile_dir, monkeypatch):
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium_headless_shell-1217"])
        _write_metadata(install_metadata_path(), bdir, version=2)
        assert browser_ready() is False


def _fill(directory: Path, num_bytes: int) -> None:
    """Give *directory* a payload file of a known size."""
    (directory / "payload.bin").write_bytes(b"\0" * num_bytes)


class TestRetainedRevisionReport:
    """The managed cache is inventoried and reported, never pruned (#686).

    Patchright keeps every revision a valid `.links` reference names, and uv
    keeps one readable archive per version anyone has run, so old browsers stay
    on disk for good. Deleting a link or a browser directory would break an
    installation that can still be launched, so the server says what it is
    holding and how to reclaim it by hand.
    """

    @pytest.fixture(autouse=True)
    def _headless_config(self, monkeypatch):
        _set_headless(monkeypatch, True)

    def _report(self, caplog) -> str:
        with caplog.at_level(logging.WARNING, logger="linkedin_mcp_server.bootstrap"):
            _report_retained_browser_revisions()
        return caplog.text

    def test_current_revision_alone_is_silent(
        self, isolate_profile_dir, monkeypatch, caplog
    ):
        """A fresh install holds nothing it will not launch."""
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217"])

        assert self._report(caplog) == ""
        assert not (bdir / _CACHE_REPORT_MARKER).exists()

    def test_old_full_and_every_shell_are_reported(
        self, isolate_profile_dir, monkeypatch, caplog
    ):
        """The running revision is active; the older one and the shells are not."""
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(
            bdir,
            [
                "chromium-1208",
                "chromium-1217",
                "chromium_headless_shell-1208",
                "chromium_headless_shell-1217",
            ],
        )

        text = self._report(caplog)

        assert "chromium-1208" in text
        assert "chromium_headless_shell-1208" in text
        assert "chromium_headless_shell-1217" in text
        assert "chromium-1217" not in text

    def test_unrelated_directories_are_left_out(
        self, isolate_profile_dir, monkeypatch, caplog
    ):
        """Only the two browsers this server installs are the report's business."""
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(
            bdir,
            [
                "chromium-1217",
                "chromium-1208",
                "chromium_tip_of_tree-1208",
                "ffmpeg-1011",
            ],
        )

        text = self._report(caplog)

        assert "chromium-1208" in text
        assert "chromium_tip_of_tree" not in text
        assert "ffmpeg" not in text

    def test_symlinked_revisions_are_neither_named_nor_measured(
        self, isolate_profile_dir, monkeypatch, tmp_path, caplog
    ):
        """A link is not the storage it points at.

        Counting it would attribute another directory's bytes to this cache and
        name something the remedy would not free.
        """
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium_headless_shell-1217"])
        _fill(bdir / "chromium_headless_shell-1217", 2048)

        elsewhere = tmp_path / "elsewhere" / "chromium-1208"
        elsewhere.mkdir(parents=True)
        _fill(elsewhere, 4 * 1024 * 1024)
        (bdir / "chromium-1208").symlink_to(elsewhere, target_is_directory=True)

        text = self._report(caplog)

        assert "chromium_headless_shell-1217" in text
        assert "chromium-1208" not in text
        assert "2.0 KiB" in text

    def test_warning_carries_size_path_and_remedy(
        self, isolate_profile_dir, monkeypatch, caplog
    ):
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium-1208"])
        _fill(bdir / "chromium-1208", 3 * 1024 * 1024)

        text = self._report(caplog)

        assert "3.0 MiB" in text
        assert "chromium-1208" in text
        assert str(bdir) in text
        assert "stop every LinkedIn MCP Server instance" in text

    def test_the_same_cache_state_warns_once(
        self, isolate_profile_dir, monkeypatch, caplog
    ):
        """A restart on an unchanged cache has nothing new to say."""
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium-1208"])

        assert "chromium-1208" in self._report(caplog)
        caplog.clear()
        assert self._report(caplog) == ""
        assert (bdir / _CACHE_REPORT_MARKER).is_file()

    def test_concurrent_process_report_is_suppressed_by_the_cache_lock(
        self, isolate_profile_dir, monkeypatch, caplog
    ):
        """A second process stays quiet while the first records this state."""
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium-1208"])
        scan_started = threading.Event()
        finish_scan = threading.Event()

        def blocked_size(_path: Path) -> int:
            scan_started.set()
            assert finish_scan.wait(timeout=1)
            return 1024

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._directory_size", blocked_size
        )
        with (
            caplog.at_level(logging.WARNING, logger="linkedin_mcp_server.bootstrap"),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(_report_retained_browser_revisions)
            assert scan_started.wait(timeout=1)
            second = pool.submit(_report_retained_browser_revisions)
            second.result(timeout=1)
            finish_scan.set()
            first.result(timeout=1)

        assert caplog.text.count("The managed browser cache") == 1

    def test_a_changed_retained_set_warns_again(
        self, isolate_profile_dir, monkeypatch, caplog
    ):
        """The next patchright bump is exactly what the user needs to hear about."""
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium-1208"])
        self._report(caplog)
        caplog.clear()

        _materialize_install(bdir, ["chromium_headless_shell-1208"])

        assert "chromium_headless_shell-1208" in self._report(caplog)

    def test_a_new_current_revision_warns_again(
        self, isolate_profile_dir, monkeypatch, caplog
    ):
        """Same directories on disk, and the one that runs them has moved on."""
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium-1208"])
        self._report(caplog)
        caplog.clear()

        _patch_targets_and_version(
            monkeypatch,
            targets={"chromium-": "1228", "chromium_headless_shell-": "1228"},
        )

        assert "chromium-1217" in self._report(caplog)

    def test_a_malformed_marker_reports_again(
        self, isolate_profile_dir, monkeypatch, caplog
    ):
        """Silence about a gigabyte is the worse answer to an unparsable file."""
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium-1208"])
        (bdir / _CACHE_REPORT_MARKER).write_text("not json {{{")

        assert "chromium-1208" in self._report(caplog)
        assert json.loads((bdir / _CACHE_REPORT_MARKER).read_text())["retained"] == [
            "chromium-1208"
        ]

    def test_an_invalid_utf8_marker_reports_again(
        self, isolate_profile_dir, monkeypatch, caplog
    ):
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium-1208"])
        (bdir / _CACHE_REPORT_MARKER).write_bytes(b"\xff\xfe")

        assert "chromium-1208" in self._report(caplog)

    def test_a_marker_write_failure_is_survivable(
        self, isolate_profile_dir, monkeypatch, caplog
    ):
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium-1208"])

        def boom(*args, **kwargs):
            raise OSError("read-only cache")

        monkeypatch.setattr("linkedin_mcp_server.bootstrap.secure_write_text", boom)

        assert "chromium-1208" in self._report(caplog)

    def test_files_that_vanish_mid_scan_are_skipped(
        self, isolate_profile_dir, monkeypatch, caplog
    ):
        """Another process may be installing into the same cache."""
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium-1208"])
        _fill(bdir / "chromium-1208", 4096)

        def vanish(_path):
            raise FileNotFoundError(_path)

        monkeypatch.setattr(os, "lstat", vanish)

        text = self._report(caplog)

        assert "chromium-1208" in text
        assert "0.0 B" in text

    def test_an_unreadable_scan_never_raises(
        self, isolate_profile_dir, monkeypatch, caplog
    ):
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium-1208"])

        def denied(*args, **kwargs):
            raise PermissionError("no access")

        monkeypatch.setattr(os, "walk", denied)

        assert self._report(caplog) == ""

    def test_an_unreadable_registry_reports_nothing(
        self, isolate_profile_dir, monkeypatch, caplog
    ):
        """Without the registry, the running browser is indistinguishable from dead weight."""
        _patch_targets_and_version(monkeypatch, targets=None)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium-1208"])

        assert self._report(caplog) == ""

    def test_a_missing_cache_reports_nothing(
        self, isolate_profile_dir, monkeypatch, caplog
    ):
        _patch_targets_and_version(monkeypatch)

        assert self._report(caplog) == ""

    def test_a_custom_browsers_path_is_inventoried(
        self, isolate_profile_dir, monkeypatch, tmp_path, caplog
    ):
        """The report follows the path patchright actually uses."""
        _patch_targets_and_version(monkeypatch)
        custom = tmp_path / "operator-cache"
        _materialize_install(custom, ["chromium-1217", "chromium-1208"])
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(custom))

        text = self._report(caplog)

        assert str(custom) in text
        assert "chromium-1208" in text
        assert (custom / _CACHE_REPORT_MARKER).is_file()
        assert not (browsers_path() / _CACHE_REPORT_MARKER).exists()


class TestRetainedRevisionReportCallSites:
    """Every managed path that concludes the browser is in place reports it."""

    def _record(self, monkeypatch) -> list[int]:
        calls: list[int] = []
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._report_retained_browser_revisions",
            lambda: calls.append(1),
        )
        return calls

    async def test_ready_background_setup_reports(
        self, isolate_profile_dir, monkeypatch
    ):
        _patch_targets_and_version(monkeypatch)
        _set_headless(monkeypatch, True)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217"])
        _write_metadata(install_metadata_path(), bdir)
        calls = self._record(monkeypatch)

        initialize_bootstrap("managed")
        await start_background_browser_setup_if_needed()
        report_task = get_bootstrap_state().cache_report_task
        assert report_task is not None
        await report_task

        assert get_bootstrap_state().setup_state is SetupState.READY
        assert calls == [1]

    async def test_ready_background_setup_does_not_wait_for_the_scan(
        self, isolate_profile_dir, monkeypatch
    ):
        """A slow cache filesystem cannot hold up the server lifespan."""
        _patch_targets_and_version(monkeypatch)
        _set_headless(monkeypatch, True)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217"])
        _write_metadata(install_metadata_path(), bdir)
        scan_started = threading.Event()
        finish_scan = threading.Event()

        def blocked_report() -> None:
            scan_started.set()
            assert finish_scan.wait(timeout=1)

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._report_retained_browser_revisions",
            blocked_report,
        )

        initialize_bootstrap("managed")
        await start_background_browser_setup_if_needed()

        assert get_bootstrap_state().setup_state is SetupState.READY
        assert await asyncio.to_thread(scan_started.wait, 1)
        report_task = get_bootstrap_state().cache_report_task
        assert report_task is not None
        assert report_task.done() is False
        finish_scan.set()
        await report_task

    async def test_a_custom_chrome_skips_the_report(
        self, isolate_profile_dir, monkeypatch
    ):
        """No managed cache is involved, so there is nothing to inventory."""
        config = SimpleNamespace(
            browser=SimpleNamespace(headless=True, chrome_path="/usr/bin/chromium"),
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        calls = self._record(monkeypatch)

        initialize_bootstrap("managed")
        await start_background_browser_setup_if_needed()
        ensure_browser_installed()

        assert calls == []

    async def test_a_finished_install_reports(self, isolate_profile_dir, monkeypatch):
        _patch_targets_and_version(monkeypatch)
        config = SimpleNamespace(
            browser=SimpleNamespace(headless=True, eager_full_chromium=False)
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)

        async def fake_install(extra_arg: str, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_patchright_install", fake_install
        )
        calls = self._record(monkeypatch)

        from linkedin_mcp_server.bootstrap import _run_browser_setup

        await _run_browser_setup()
        report_task = get_bootstrap_state().cache_report_task
        assert report_task is not None
        await report_task

        assert calls == [1]

    def test_a_ready_cli_setup_reports(self, isolate_profile_dir, monkeypatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._uses_custom_chrome", lambda: False
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.browser_ready", lambda: True)
        calls = self._record(monkeypatch)

        ensure_browser_installed()

        assert calls == [1]


class TestSetupGate:
    """ensure_tool_ready_or_raise gates on the browser, not on the mode.

    It used to branch on `headless`, which is what let a shell-only install
    release the gate while the launch demanded the full browser.
    """

    @pytest.mark.parametrize("headless", [True, False])
    async def test_shell_only_blocks_in_either_mode(
        self, isolate_profile_dir, monkeypatch, headless
    ):
        """Shell-only must block, and the mode must not change that.

        Released for a headless server before this change, which is exactly the
        combination that loops: gate opens, launch demands the missing full
        browser, metadata is invalidated, setup reinstalls the shell.
        """
        _patch_targets_and_version(monkeypatch)
        _set_headless(monkeypatch, headless)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium_headless_shell-1217"])
        _write_metadata(install_metadata_path(), bdir)
        configure_browser_environment()

        async def fake_setup() -> None:
            return None

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_browser_setup", fake_setup
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: True)

        initialize_bootstrap("managed")

        with pytest.raises(BrowserSetupInProgressError):
            await ensure_tool_ready_or_raise("get_person_profile")

    @pytest.mark.parametrize("headless", [True, False])
    async def test_full_only_releases_in_either_mode(
        self, isolate_profile_dir, monkeypatch, headless
    ):
        """The browser alone is enough, with no shell beside it."""
        _patch_targets_and_version(monkeypatch)
        _set_headless(monkeypatch, headless)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217"])
        _write_metadata(install_metadata_path(), bdir)
        configure_browser_environment()

        monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: True)

        initialize_bootstrap("managed")

        result = await ensure_tool_ready_or_raise("get_person_profile")
        assert result is None


class TestChromePathShortCircuit:
    """A custom chrome_path skips the managed install in both modes."""

    def _config(self, *, headless: bool, chrome_path: str):
        return SimpleNamespace(
            browser=SimpleNamespace(
                headless=headless,
                chrome_path=chrome_path,
                login_inline_wait_seconds=0,
                auto_import_from_browser=False,
            ),
            server=SimpleNamespace(transport="stdio", host="127.0.0.1"),
            is_interactive=False,
        )

    @pytest.mark.parametrize("headless", [True, False])
    async def test_gate_short_circuits_to_ready(
        self, isolate_profile_dir, monkeypatch, headless
    ):
        config = self._config(headless=headless, chrome_path="/usr/bin/chromium")
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: True)

        # No metadata, no install on disk: a managed gate would block, but the
        # custom executable must short-circuit straight to ready.
        called = {"value": False}

        async def fail_setup() -> None:
            called["value"] = True

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_browser_setup", fail_setup
        )

        initialize_bootstrap("managed")

        result = await ensure_tool_ready_or_raise("get_person_profile")
        assert result is None
        assert called["value"] is False
        assert get_bootstrap_state().setup_state is SetupState.READY

    @pytest.mark.parametrize("headless", [True, False])
    async def test_background_setup_skipped(
        self, isolate_profile_dir, monkeypatch, headless
    ):
        config = self._config(headless=headless, chrome_path="/usr/bin/chromium")
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)

        initialize_bootstrap("managed")
        await start_background_browser_setup_if_needed()

        state = get_bootstrap_state()
        assert state.setup_state is SetupState.READY
        assert state.setup_task is None


class TestTwoStageInstall:
    """_run_browser_setup runs --only-shell then --no-shell and writes v3 metadata."""

    def _stub_install(self, monkeypatch):
        """Replace the patchright subprocess with a recorder of the install flags."""
        calls: list[str] = []

        async def fake_install(extra_arg: str, **_kwargs: object) -> None:
            calls.append(extra_arg)

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_patchright_install", fake_install
        )
        return calls

    @pytest.mark.parametrize("headless", [True, False])
    @pytest.mark.parametrize("eager", [True, False])
    async def test_one_stage_regardless_of_mode_or_eager_knob(
        self, isolate_profile_dir, monkeypatch, headless, eager
    ):
        """Exactly one install, and neither setting moves it.

        The shell stage is gone because nothing launches the shell. The eager
        knob is inert for the same reason: with one browser, "up front" and
        "lazily" name the same install. It stays accepted in configuration so an
        existing command line keeps working, and it stays in the daemon
        fingerprint, but it must not reach the installer.
        """
        _patch_targets_and_version(monkeypatch)
        config = SimpleNamespace(
            browser=SimpleNamespace(headless=headless, eager_full_chromium=eager)
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        calls = self._stub_install(monkeypatch)

        from linkedin_mcp_server.bootstrap import _run_browser_setup

        await _run_browser_setup()

        assert calls == ["--no-shell"]
        payload = json.loads(install_metadata_path().read_text())
        assert payload["version"] == 3
        assert payload["installed_targets"] == {
            "chromium-": True,
            "chromium_headless_shell-": False,
        }

    async def test_failed_install_records_nothing_ready(
        self, isolate_profile_dir, monkeypatch
    ):
        """A failed install must not leave metadata claiming a usable browser."""
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        _patch_targets_and_version(monkeypatch)
        config = SimpleNamespace(
            browser=SimpleNamespace(headless=True, eager_full_chromium=False)
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)

        async def fake_install(extra_arg: str, **_kwargs: object) -> None:
            raise BrowserSetupFailedError("network down")

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_patchright_install", fake_install
        )

        from linkedin_mcp_server.bootstrap import _run_browser_setup

        with pytest.raises(BrowserSetupFailedError):
            await _run_browser_setup()

        assert not install_metadata_path().exists()

    async def test_background_setup_task_runs_the_install(
        self, isolate_profile_dir, monkeypatch
    ):
        """The install runs inside the one background setup task."""
        _patch_targets_and_version(monkeypatch)
        config = SimpleNamespace(
            browser=SimpleNamespace(
                headless=False, eager_full_chromium=False, chrome_path=None
            )
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_setup_ready", lambda: False
        )
        calls = self._stub_install(monkeypatch)

        initialize_bootstrap("managed")
        await start_background_browser_setup_if_needed()

        state = get_bootstrap_state()
        assert state.setup_task is not None
        await state.setup_task
        assert calls == ["--no-shell"]


class _StdoutThatIsGone(io.StringIO):
    """A stdout whose every write meets a reader that has already left.

    What ``--install-browser | head`` leaves behind once head has printed its
    ten lines and exited.
    """

    encoding = "utf-8"

    def isatty(self) -> bool:
        return False

    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")


class _FakeStdout:
    """Serves pre-canned byte chunks through ``read()``, like a real pipe.

    Records ``exhausted`` once it has answered with b"" so the process can
    assert its pipe was fully drained before ``wait()``.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._pending = list(chunks)
        self.exhausted = False

    async def read(self, n: int = -1) -> bytes:
        """At most *n* bytes, like a real StreamReader: never the whole line."""
        while self._pending and not self._pending[0]:
            self._pending.pop(0)
        if not self._pending:
            self.exhausted = True
            return b""
        head = self._pending[0]
        if n is not None and 0 <= n < len(head):
            self._pending[0] = head[n:]
            return head[:n]
        self._pending.pop(0)
        return head


class _FakeProc:
    """A running process: ``returncode`` stays None until ``wait()`` reaps it."""

    def __init__(self, chunks: list[bytes], returncode: int) -> None:
        self.stdout = _FakeStdout(chunks)
        self.returncode: int | None = None
        self._final = returncode
        self.killed = False
        self.waited = False
        self.pid = 424242  # a real Process carries one

    async def wait(self) -> int:
        self.waited = True
        return await self._wait()

    async def _wait(self) -> int:
        # A real process whose output pipe fills while the caller blocks on
        # wait() deadlocks. Draining stdout first is the safe ordering, so this
        # fails loudly if the implementation ever awaits wait() before the pipe
        # is empty.
        assert self.stdout.exhausted, "wait() awaited before stdout was drained"
        self.returncode = self._final
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _Spawned:
    """What the patched ``create_subprocess_exec`` was given, and handed back."""

    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}
        self.proc: _FakeProc | None = None


class TestPatchrightInstallStreaming:
    """The install streams its output as it arrives rather than after it ends."""

    def _patch_proc(
        self, monkeypatch, chunks: list[bytes], returncode: int
    ) -> "_Spawned":
        """Patch the subprocess and record the exec kwargs and the fake process."""
        spawned = _Spawned()

        async def fake_exec(*_args: object, **kwargs: object) -> _FakeProc:
            spawned.kwargs.update(kwargs)
            spawned.proc = _FakeProc(chunks, returncode)
            return spawned.proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        return spawned

    async def test_the_callback_gets_every_line_in_order(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        spawned = self._patch_proc(
            monkeypatch,
            [b"Downloading 10%\n", b"\n", b"Downloading 100%\n"],
            0,
        )
        seen: list[str] = []
        await bootstrap._run_patchright_install("--no-shell", line_callback=seen.append)

        # stderr folds into stdout so the two interleave in write order. The
        # fake process's wait() also asserts the pipe was drained first, so a
        # regression that awaited wait() before streaming would fail here.
        assert spawned.kwargs["stderr"] is asyncio.subprocess.STDOUT
        assert seen == ["Downloading 10%", "Downloading 100%"]  # blanks dropped

    async def test_the_background_path_logs_every_line(self, monkeypatch, caplog):
        """With no callback there is no bar, and the log is the only display."""
        from linkedin_mcp_server import bootstrap

        self._patch_proc(monkeypatch, [b"Downloading 100%\n"], 0)
        with caplog.at_level(logging.DEBUG, logger="linkedin_mcp_server.bootstrap"):
            await bootstrap._run_patchright_install("--no-shell")

        assert any("Downloading 100%" in r.message for r in caplog.records)

    async def test_the_cli_path_does_not_log_what_the_bar_shows(
        self, monkeypatch, caplog
    ):
        """Otherwise every percentage is printed permanently above the bar."""
        from linkedin_mcp_server import bootstrap

        self._patch_proc(monkeypatch, [b"Downloading 100%\n"], 0)
        with caplog.at_level(logging.DEBUG, logger="linkedin_mcp_server.bootstrap"):
            await bootstrap._run_patchright_install(
                "--no-shell", line_callback=lambda _l: None
            )

        assert not [r for r in caplog.records if "Downloading" in r.message]

    async def test_lines_arrive_before_the_process_is_reaped(self, monkeypatch):
        """Streaming, as opposed to replaying a drained buffer afterwards.

        Draining everything and then replaying the callbacks passes an
        assertion on the final list just as well, and restores the silence this
        change removes. So the moment matters: each line must reach the
        callback while the installer is still running.
        """
        from linkedin_mcp_server import bootstrap

        spawned = self._patch_proc(monkeypatch, [b"10%\n", b"20%\n"], 0)
        waited_when_seen: list[bool] = []

        def watch(_line: str) -> None:
            proc = spawned.proc
            assert proc is not None
            waited_when_seen.append(proc.waited)

        await bootstrap._run_patchright_install("--no-shell", line_callback=watch)

        assert waited_when_seen == [False, False]

    async def test_failure_message_is_built_from_streamed_lines(self, monkeypatch):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        self._patch_proc(monkeypatch, [b"resolving...\n", b"error: no network\n"], 1)

        with pytest.raises(BrowserSetupFailedError, match="error: no network"):
            await bootstrap._run_patchright_install("--no-shell")

    async def test_failure_without_output_has_a_default_message(self, monkeypatch):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        self._patch_proc(monkeypatch, [], 1)

        with pytest.raises(BrowserSetupFailedError, match="setup failed"):
            await bootstrap._run_patchright_install("--no-shell")

    async def test_a_line_past_the_reader_limit_still_arrives(self, monkeypatch):
        """One line longer than asyncio's 64 KiB readline limit must not raise.

        Patchright puts a whole non-200 response body into a single error line,
        so a captive portal or a mirror answering with minified HTML produces
        one. Reading through ``readline`` raises ValueError there, mid-download,
        leaving the child running.
        """
        from linkedin_mcp_server import bootstrap

        long_line = "E" * 200_000
        self._patch_proc(monkeypatch, [long_line.encode() + b"\n"], 0)
        seen: list[str] = []

        await bootstrap._run_patchright_install("--no-shell", line_callback=seen.append)

        # Emitted in bounded pieces rather than buffered whole, and nothing of
        # it is lost. Reading through readline would have raised instead.
        assert "".join(seen) == long_line
        assert max(len(piece) for piece in seen) <= bootstrap._MAX_LINE_CHARS

    async def test_whitespace_at_a_forced_cut_survives(self, monkeypatch):
        """A fragment boundary is not the end of a line, so it is not trimmed.

        Trimming every fragment would eat a space the installer wrote, at
        whatever offset the cap happens to fall on.
        """
        from linkedin_mcp_server import bootstrap

        cut = bootstrap._MAX_LINE_CHARS
        payload = "A" * (cut - 1) + " " + "B" * bootstrap._MAX_LINE_CHARS
        seen: list[str] = []

        async def fake_exec(*_a: object, **_k: object):
            return _FakeProc([payload.encode() + b"\n"], 0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        await bootstrap._run_patchright_install("--no-shell", line_callback=seen.append)

        assert "".join(seen) == payload

    async def test_output_that_never_ends_a_line_stays_bounded(self, monkeypatch):
        """Output with no newline at all must not grow the buffer without limit."""
        from linkedin_mcp_server import bootstrap

        self._patch_proc(monkeypatch, [b"N" * 500_000], 0)
        seen: list[str] = []

        await bootstrap._run_patchright_install("--no-shell", line_callback=seen.append)

        assert "".join(seen) == "N" * 500_000
        assert max(len(piece) for piece in seen) <= bootstrap._MAX_LINE_CHARS

    async def test_multibyte_split_across_reads_is_not_corrupted(self, monkeypatch):
        """A UTF-8 character straddling two reads must survive decoding."""
        from linkedin_mcp_server import bootstrap

        encoded = "100% ■ fertig".encode()
        self._patch_proc(monkeypatch, [encoded[:7], encoded[7:] + b"\n"], 0)
        seen: list[str] = []

        await bootstrap._run_patchright_install("--no-shell", line_callback=seen.append)

        assert seen == ["100% ■ fertig"]

    async def test_a_trailing_line_without_a_newline_arrives(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        self._patch_proc(monkeypatch, [b"no trailing newline"], 0)
        seen: list[str] = []

        await bootstrap._run_patchright_install("--no-shell", line_callback=seen.append)

        assert seen == ["no trailing newline"]

    async def test_mirror_credentials_are_not_logged(self, monkeypatch, caplog):
        """Userinfo in a download URL reaches neither the terminal nor the log.

        Userinfo and the query only. A credential an operator put in the *path*
        still prints, on purpose and by a decision `_safe_to_print` records:
        the path is also where the browser build is named, and blanking it
        would take the diagnostic with it.
        """
        from linkedin_mcp_server import bootstrap

        url = "https://ci-bot:s3cr3t@mirror.example/chromium.zip"
        self._patch_proc(monkeypatch, [f"Downloading from {url}\n".encode()], 0)
        seen: list[str] = []

        with caplog.at_level(logging.DEBUG, logger="linkedin_mcp_server.bootstrap"):
            await bootstrap._run_patchright_install(
                "--no-shell", line_callback=seen.append
            )

        everything = " ".join(seen) + " ".join(r.message for r in caplog.records)
        assert "s3cr3t" not in everything
        assert "ci-bot" not in everything
        assert "mirror.example/chromium.zip" in everything

    async def test_the_failure_message_is_bounded(self, monkeypatch):
        """A pathological installer must not be quoted back in full."""
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        chunk = b"".join(f"line {i}\n".encode() for i in range(5_000))
        self._patch_proc(monkeypatch, [chunk], 1)

        with pytest.raises(BrowserSetupFailedError) as excinfo:
            await bootstrap._run_patchright_install("--no-shell")

        reported = str(excinfo.value).splitlines()
        assert len(reported) == bootstrap._MAX_RETAINED_LINES
        # The tail is kept: that is the part that says why it failed.
        assert reported[-1] == "line 4999"

    async def test_a_few_huge_lines_are_bounded_by_characters(self, monkeypatch):
        """The line count alone would allow 200 fragments of 60 KiB each.

        That is 12 MiB in an exception message and in `last_error`, from one
        response body. The character bound is what stops it, and a test built
        from short lines never reaches it.
        """
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        big = b"".join((b"H" * 40_000) + b"\n" for _ in range(20))
        self._patch_proc(monkeypatch, [big], 1)

        with pytest.raises(BrowserSetupFailedError) as excinfo:
            await bootstrap._run_patchright_install("--no-shell")

        # The characters themselves are what `retained` counts; the newlines
        # `join` puts between them are not, and there is one fewer of those
        # than there are lines. Anything looser passes a regression that keeps
        # twice the bound, which is what this test exists to catch.
        assert len(str(excinfo.value)) <= (
            bootstrap._MAX_RETAINED_CHARS + bootstrap._MAX_RETAINED_LINES
        )

    async def test_a_failing_callback_does_not_leave_the_installer_running(
        self, monkeypatch
    ):
        """Any escape from the read loop must stop the child.

        Reaches the Python wrapper. Node keeps running until this process
        exits and its pipe closes, which is the bounded end of the trade
        `_stop_installer` explains.
        """
        from linkedin_mcp_server import bootstrap

        spawned = self._patch_proc(monkeypatch, [b"downloading\n"], 0)

        def explode(_line: str) -> None:
            raise RuntimeError("consumer died")

        with pytest.raises(RuntimeError, match="consumer died"):
            await bootstrap._run_patchright_install("--no-shell", line_callback=explode)

        assert spawned.proc is not None and spawned.proc.killed is True

    async def test_cancellation_stops_the_installer(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        spawned = self._patch_proc(monkeypatch, [b"downloading\n"], 0)

        def cancel(_line: str) -> None:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await bootstrap._run_patchright_install("--no-shell", line_callback=cancel)

        assert spawned.proc is not None and spawned.proc.killed is True


class TestCredentialRedaction:
    """Every userinfo shape a mirror URL can carry, not only user:password."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://ci-bot:s3cr3t@mirror.example/x.zip",
            "https://s3cr3t@mirror.example/x.zip",
            "https://s3cr3t:@mirror.example/x.zip",
            "https://:s3cr3t@mirror.example/x.zip",
            # A password may hold an "@". Stopping at the first one leaves the
            # rest of it in the line.
            "https://ci-bot:s3cr3t@x@mirror.example/x.zip",
            # Node normalises backslashes before authenticating, so this is a
            # working credential too.
            "https:\\\\ci-bot:s3cr3t@mirror.example/x.zip",
            # Not userinfo at all: patchright pastes its path onto whatever the
            # download-host variable names.
            "https://mirror.example/dl?token=s3cr3t&path=/builds/x.zip",
            "https://mirror.example/dl?api_key=s3cr3t",
            "https://mirror.example/dl?a=1&signature=s3cr3t&b=2",
        ],
    )
    def test_the_secret_never_survives(self, url):
        from linkedin_mcp_server.bootstrap import _safe_to_print

        redacted = _safe_to_print(f"Downloading Chromium from {url}")
        assert "s3cr3t" not in redacted
        assert "mirror.example" in redacted

    @pytest.mark.parametrize("straddle", [0, 1, 2, 3])
    async def test_a_url_is_redacted_wherever_the_cut_falls(
        self, monkeypatch, straddle
    ):
        """A credential must not escape by landing on a fragment boundary.

        Redaction runs on the buffer, before it is cut, so a URL that has
        arrived whole is replaced wherever the cut then falls. The offsets
        sweep the boundaries a cut and a read can land on, and a version that
        redacts the fragments instead leaks in at least one of them.
        """
        from linkedin_mcp_server import bootstrap

        secret = "https://ci-bot:s3cr3t@mirror.example/x.zip"
        cap = bootstrap._MAX_LINE_CHARS
        # Place the URL across each boundary a cut could land on.
        offset = [cap, cap * 2, bootstrap._READ_CHUNK, bootstrap._READ_CHUNK * 3][
            straddle
        ] - len(secret) // 2
        payload = "F" * offset + secret + "F" * bootstrap._MAX_LINE_CHARS
        seen: list[str] = []

        async def fake_exec(*_a: object, **_k: object):
            return _FakeProc([payload.encode() + b"\n"], 0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        await bootstrap._run_patchright_install("--no-shell", line_callback=seen.append)

        assert "s3cr3t" not in "".join(seen)


class TestQuotedResponseBodiesAreDropped:
    """The one part of this output a stranger writes is not printed at all.

    Patchright's ``Download failed: server returned code N body '…'. URL: …``
    quotes whatever a refusing mirror sent, verbatim and once per retry. That
    body is where the escape sequences, the reflected credentials, the rich
    markup and the 400-digit sizes all came from, so it is dropped between the
    markers rather than defended against downstream.
    """

    #: Measured, not composed: one error block as patchright 1.61.2 wrote it to a
    #: pipe when a mirror answered 403 with an HTML page. The body carries its
    #: own newlines, which is why the drop has to hold across lines.
    SAMPLE = (
        "Error: Download failed: server returned code 403 body '<html>\n"
        "<head>\x1b]0;PWNED\x07<title>denied</title></head>\n"
        "<body>\x1b[2Jbad token: sup3rs3cr3tvalue\n"
        "split: sup3r\x1b[31ms3cr3tvalue\n"
        "|########| 999% of 12345678901234567890.5 MiB\n"
        "</body>\n</html>\n"
        "'. URL: http://user:sup3rs3cr3tvalue@mirror.example/?token=sup3rs3cr3tvalue"
        "/builds/cft/149.0.7827.55/mac-arm64/chrome-headless-shell-mac-arm64.zip\n"
        "    at IncomingMessage.handleError (/x/coreBundle.js:29308:23)\n"
    )

    async def _lines(self, monkeypatch, *chunks: bytes) -> list[str]:
        from linkedin_mcp_server import bootstrap

        seen: list[str] = []

        async def fake_exec(*_a: object, **_k: object):
            return _FakeProc(list(chunks), 0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        await bootstrap._run_patchright_install("--no-shell", line_callback=seen.append)
        return seen

    async def test_nothing_between_the_markers_is_printed(self, monkeypatch):
        seen = await self._lines(monkeypatch, self.SAMPLE.encode())
        printed = "\n".join(seen)

        assert "<response body omitted>" in printed
        assert "denied" not in printed
        assert "999%" not in printed
        assert "sup3rs3cr3tvalue" not in printed

    async def test_what_patchright_wrote_itself_survives(self, monkeypatch):
        """The diagnosis is the status code and the URL, and both are kept."""
        seen = await self._lines(monkeypatch, self.SAMPLE.encode())
        printed = "\n".join(seen)

        assert "server returned code 403" in printed
        # The mirror is named; its credential and its query are not, and the
        # archive path sits behind the query in a host configured this way.
        assert "URL: http://***@mirror.example/?***" in printed
        assert "coreBundle.js:29308:23" in printed, "output resumes after the body"

    async def test_one_body_does_not_swallow_the_next_download(self, monkeypatch):
        """A failed attempt is followed by four more, and they must show."""
        seen = await self._lines(
            monkeypatch,
            self.SAMPLE.encode(),
            b"Downloading Chrome for Testing 149.0.7827.55 from https://cdn/x.zip\n",
        )

        assert any("Downloading Chrome for Testing" in line for line in seen)

    async def test_a_body_that_never_closes_takes_the_rest_with_it(self, monkeypatch):
        """A truncated response leaves the marker open, and open means dropped.

        Losing the tail of a failed install's output is the cheaper mistake:
        the alternative is guessing where a stranger's bytes stopped.
        """
        seen = await self._lines(
            monkeypatch,
            b"Error: Download failed: server returned code 500 body '<html>\n",
            b"still the body\nand still\n",
        )

        assert seen == [
            "Error: Download failed: server returned code 500 body "
            "'<response body omitted>"
        ]

    async def test_a_body_quoting_the_opener_still_terminates(self, monkeypatch):
        """The marker stays in what is kept, so a rescan must not find it again."""
        seen = await self._lines(
            monkeypatch,
            b"Error: Download failed: server returned code 403 body "
            b"'server returned code 403 body ''. URL: http://cdn/x.zip\n",
        )

        assert len(seen) == 1
        assert seen[0].count("<response body omitted>") == 1

    async def test_two_bodies_on_one_line_are_both_dropped(self, monkeypatch):
        """Patchright writes one message per line, so this shape is the body's.

        Only the closer whose URL ends the line is believed, which here is the
        second one: the whole span from the first opener to it goes, and both
        bodies with it. Dropping more than a body is the safe error to make;
        the alternative is believing a marker a stranger wrote.
        """
        line = (
            "A: server returned code 403 body 'first'. URL: http://a/x.zip "
            "B: server returned code 404 body 'second'. URL: http://b/x.zip\n"
        )
        seen = await self._lines(monkeypatch, line.encode())

        assert "first" not in seen[0] and "second" not in seen[0]
        assert seen[0].count("<response body omitted>") == 1
        assert seen[0].endswith("'. URL: http://b/x.zip")

    async def test_a_body_cannot_close_itself_with_prose_behind_it(self, monkeypatch):
        """The closer is anchored to the end of its line, so a forgery misses.

        Measured against a refusing mirror: patchright's own closer starts a
        line of its own and its URL runs to end of line. A body that writes the
        marker mid-line is therefore still the body talking, and the drop stays
        open rather than printing the rest of the page as if patchright had
        written it.
        """
        stream = (
            "Download failed: server returned code 403 body '<html>\n"
            "'. URL: http://forged/x and then sup3rs3cr3tvalue\n"
            "still inside the page\n"
            "'. URL: http://real/x.zip\n"
            "    at IncomingMessage.handleError (/x/coreBundle.js:1:1)\n"
        )
        seen = await self._lines(monkeypatch, stream.encode())
        printed = "\n".join(seen)

        assert "sup3rs3cr3tvalue" not in printed
        assert "still inside the page" not in printed
        assert "'. URL: http://real/x.zip" in printed
        assert "coreBundle.js" in printed, "and patchright's own trace comes back"

    async def test_a_body_line_ending_in_a_url_does_close_the_drop(self, monkeypatch):
        """The limit the anchor leaves open, pinned so a change to it is visible.

        The closer patchright writes is the marker, a URL, end of line, and
        nothing at the source distinguishes that from a body line of the same
        shape. So a page that ends a line with one closes the drop and the rest
        of the page prints. Recorded rather than defended against: the
        alternative is dropping the real closer too, which takes the URL, the
        stack trace and every later retry with it.

        What the drop is for does not rest on this. The rest of the page is
        still stripped of terminal controls, still redacted, still capped, and
        still rendered without markup, which is what the sibling tests hold.
        """
        stream = (
            "Download failed: server returned code 403 body '<html>\n"
            "'. URL: http://forged/x\n"
            "reflected sup3rs3cr3tvalue\n"
            "'. URL: http://real/x.zip\n"
        )
        seen = await self._lines(monkeypatch, stream.encode())
        printed = "\n".join(seen)

        assert "reflected sup3rs3cr3tvalue" in printed, (
            "a body line of the closer's own shape ends the drop: known limit"
        )
        assert "\x1b" not in printed, "and the rest is still stripped"

    @pytest.mark.parametrize("cuts", [1, 2])
    async def test_a_closer_split_by_the_forced_cut_still_closes(
        self, monkeypatch, cuts
    ):
        """A body over the line cap is cut wherever the cap falls.

        Including inside ``'. URL: ``, which leaves half the marker on each
        side of a fragment boundary. Without a carry across fragments the drop
        would never see a whole marker again and would swallow the URL, the
        stack trace and every later retry along with the page. Two cuts because
        the elision carries in two places: where it opens, and on every
        fragment it stays open across.
        """
        from linkedin_mcp_server import bootstrap

        cap = bootstrap._MAX_LINE_CHARS
        opener = "Download failed: server returned code 403 body '"
        closer = "'. URL: http://h/z.zip"
        # Three characters of the marker land on the near side of the last cut.
        body = "A" * (cuts * cap - 3 - len(opener))
        stream = opener + body + closer + "\nback to normal\nDone\n"
        seen = await self._lines(monkeypatch, stream.encode())
        printed = "\n".join(seen)

        assert "AAAA" not in printed, "the page itself is still dropped"
        assert "back to normal" in printed
        assert "Done" in printed

    def test_the_markers_match_what_patchright_builds(self):
        """Drawn from the installer's own source, not composed here.

        A double written from memory tests this file against itself. So the
        template comes out of the ``coreBundle.js`` that is actually installed
        and the placeholders are filled in; if a release rewords the message,
        the drop stops finding a body and this fails, rather than the body
        appearing on someone's terminal.
        """
        import re

        import patchright

        from linkedin_mcp_server import bootstrap

        # Searched rather than named: where patchright builds this moved
        # between the versions this project accepts. `pyproject.toml` allows
        # 1.55, which has it in `server/registry/oopDownloadBrowserMain.js`,
        # and the locked 1.61 bundles it into `coreBundle.js`. Naming one file
        # fails the suite of a contributor resolving the other, for a reason
        # that has nothing to do with their change. The tree is 31 files.
        lib = Path(patchright.__file__).parent / "driver" / "package" / "lib"
        assert lib.is_dir(), f"the installed driver has no library at {lib}"
        template = re.compile(r"`Download failed: server returned code [^`]*`")
        found = None
        for source in sorted(lib.rglob("*.js")):
            found = template.search(source.read_text(errors="replace"))
            if found is not None:
                break
        assert found is not None, "patchright no longer builds this message"

        built = found.group(0)[1:-1]
        for placeholder, value in (
            (r"${response.statusCode}", "403"),
            (r"${response2.statusCode}", "403"),
            (r"${content}", "X"),
            (r"${options.url}", "http://h/z"),
        ):
            built = built.replace(placeholder, value)
        assert "${" not in built, f"unsubstituted placeholder in {built!r}"

        opened = bootstrap._RESPONSE_BODY_OPENS.search(built)
        assert opened is not None
        assert built[opened.end() :].startswith("X")
        assert built[opened.end() + 1 :].startswith(bootstrap._RESPONSE_BODY_CLOSES)
        assert bootstrap._RESPONSE_BODY_CLOSED.search(built) is not None, (
            "and the closer still runs to the end of its line"
        )


class TestTerminalControlsAreStripped:
    """Installer output is data, and it reaches a terminal and a log."""

    @pytest.mark.parametrize(
        "payload",
        [
            "\x1b[2J",  # clear the screen
            "\x1b]0;owned\x07",  # rename the window
            "\x1b]52;c;cGF5bG9hZA==\x07",  # write the clipboard, where enabled
            "before\x08\x08\x08after",  # backspace over what was printed
            "\x9b2J",  # the same clear, in its eight-bit C1 form
            "\x9d0;owned\x07",  # and the same window rename
        ],
    )
    def test_a_response_body_cannot_drive_the_terminal(self, payload):
        from linkedin_mcp_server.bootstrap import _safe_to_print

        cleaned = _safe_to_print(f"Download failed: body '{payload}'")
        assert "\x1b" not in cleaned
        assert "\x08" not in cleaned
        assert not any("\x80" <= c <= "\x9f" for c in cleaned), "C1 too"
        assert "Download failed" in cleaned


class TestControlsComeOutBeforeCredentials:
    """Order matters: an escape sequence can hide a credential from a pattern.

    Stripping after redacting would leave ``//us<ESC>[0mer:pw@host`` unmatched
    and then hand the stripper the pieces to put back together.
    """

    @pytest.mark.parametrize(
        "line",
        [
            # A vertical tab and a form feed are whitespace to the pattern, so
            # the userinfo stops being one until they are gone.
            "from https://userbot:s3cr\x0b3tpw@mirror.example/x.zip",
            "from https://userbot:s3cr\x0c3tpw@mirror.example/x.zip",
            # And an OSC sequence can carry the other character the pattern
            # refuses, which is the slash it opened on.
            "from https://userbot:s3\x1b]0;/tmp/x\x07cr3tpw@mirror.example/x.zip",
        ],
    )
    def test_a_credential_broken_up_by_controls_is_still_redacted(self, line):
        from linkedin_mcp_server.bootstrap import _safe_to_print

        cleaned = _safe_to_print(line)

        assert "s3cr3tpw" not in cleaned
        assert "mirror.example" in cleaned, "the mirror is the diagnosis"


class TestPlainLinePrinting:
    """The fallback used when no bar is drawn. It must never abort an install."""

    def test_a_glyph_the_stream_cannot_encode_is_replaced(self, monkeypatch):
        """Patchright draws its bar with U+25A0 and an ascii stream refuses it.

        No ``capsys`` here, deliberately. It installs its own ``CaptureIO`` as
        ``sys.stdout``, which is then what ``monkeypatch`` records as the value
        to put back; ``capsys`` tears down first and closes that object, so the
        undo reinstalls a *closed* stream and every later test in the process
        prints into it. Under pytest's own capturing each test gets a fresh
        ``sys.stdout`` and the wreckage is invisible, so this only surfaces
        under ``-s``, which is how CI runs.
        """
        from linkedin_mcp_server.bootstrap import _print_whatever_the_stream_takes

        class _Ascii(io.StringIO):
            encoding = "ascii"

            def write(self, s: str) -> int:
                s.encode("ascii")  # raises exactly as a real ascii stream does
                return super().write(s)

        stream = _Ascii()
        monkeypatch.setattr(sys, "stdout", stream)
        _print_whatever_the_stream_takes("|■■■■   |  30% of 171.0 MiB")
        assert "30% of 171.0 MiB" in stream.getvalue()

    def test_each_line_is_flushed_as_it_is_written(self, monkeypatch):
        """Progress that sits in a block buffer is not progress.

        Redirected stdout is block-buffered, so without a flush per line the
        download reports nothing until the buffer fills or the process ends,
        which is the silence this change removes.
        """
        import builtins

        from linkedin_mcp_server.bootstrap import _print_whatever_the_stream_takes

        flushes: list[object] = []
        monkeypatch.setattr(
            builtins, "print", lambda *a, **k: flushes.append(k.get("flush"))
        )
        _print_whatever_the_stream_takes("|\u25a0\u25a0| 30% of 171.0 MiB")

        assert flushes == [True]

    def test_a_closed_stream_does_not_abort_the_install(self, monkeypatch):
        """The replacement attempt can meet the same broken pipe as the first."""
        from linkedin_mcp_server.bootstrap import _print_whatever_the_stream_takes

        class _Broken(io.StringIO):
            encoding = "ascii"

            def write(self, s: str) -> int:
                raise BrokenPipeError(32, "Broken pipe")

        monkeypatch.setattr(sys, "stdout", _Broken())
        _print_whatever_the_stream_takes("|■■■■   |  30%")  # must not raise

    def test_a_stream_the_host_already_closed_does_not_abort_it_either(
        self, monkeypatch
    ):
        """A closed ``TextIOBase`` answers ``ValueError``, not ``BrokenPipeError``.

        Only a real pipe raises the latter. A host that closed the server's
        stdout before starting it leaves the other one, and catching just the
        pipe would turn a cosmetic write into a failed install.
        """
        from linkedin_mcp_server.bootstrap import _print_whatever_the_stream_takes

        closed = io.StringIO()
        closed.close()
        monkeypatch.setattr(sys, "stdout", closed)
        _print_whatever_the_stream_takes("|■■■■   |  30%")  # must not raise


class TestRendererSurvivesOddOutput:
    """The render callback runs inside patchright's retry loop; it must not raise."""

    def _terminal(self, monkeypatch) -> list:
        """Terminal branch, capturing both the output and the Progress built.

        The Progress is captured by wrapping the class the module looked up,
        so nothing in the production code exists only for this test.
        """
        import io as _io

        from rich.progress import Progress

        from linkedin_mcp_server import bootstrap

        buffer = _io.StringIO()
        built: list = [buffer]
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        quiet = bootstrap._ConsoleThatGoesQuiet
        monkeypatch.setattr(
            bootstrap,
            "_ConsoleThatGoesQuiet",
            lambda *a, **k: quiet(
                file=buffer,
                force_terminal=True,
                width=100,
                # Stated rather than inherited: a CI runner setting TERM=dumb
                # would otherwise send these tests down the plain-line path and
                # quietly stop exercising the bar at all.
                _environ={"TERM": "xterm-256color"},
                **k,
            ),
        )

        def record(*args: Any, **kwargs: Any) -> Progress:
            progress = Progress(*args, **kwargs)
            built.append(progress)
            return progress

        monkeypatch.setattr(bootstrap, "Progress", record)
        return built

    @pytest.mark.parametrize(
        "line",
        [
            "error: access [/red] denied",  # rich markup in an error body
            "|  10% of 171.0 MB",  # a unit the table does not know
            "|  10% of . MiB",  # a number that will not parse
            "|  10% of 171.0 EiB",  # a unit nobody has
        ],
    )
    def test_it_prints_instead_of_raising(self, monkeypatch, line):
        from linkedin_mcp_server import bootstrap

        buffer = self._terminal(monkeypatch)[0]
        with bootstrap._cli_progress() as report:
            report(line)  # must not raise
        assert buffer.getvalue().strip()

    def test_markup_in_the_artifact_name_does_not_raise(self, monkeypatch):
        """Measured against real patchright: a non-200 body whose text lands on
        its own output line reaches the description, five times over. Without
        escaping, the callback raises MarkupError and the retries are lost.
        """
        from linkedin_mcp_server import bootstrap

        self._terminal(monkeypatch)
        with bootstrap._cli_progress() as report:
            report("Downloading [/red] from https://example/x")
            report("|  10% of 1.0 MiB")  # creating the task is what renders it

    @pytest.mark.parametrize(
        "line",
        [
            "|  10% of " + "9" * 400 + ".0 MiB",  # OverflowError on conversion
            "|  " + "1" * 5000 + "% of 171.0 MiB",  # past Python's digit limit
        ],
    )
    def test_absurd_numbers_never_reach_a_conversion(self, monkeypatch, line):
        """The digit counts in the pattern are the guard, so they are asserted.

        Without them the conversions raise from inside the render callback,
        which kills the installer and reports arithmetic in place of a
        download failure.
        """
        from linkedin_mcp_server import bootstrap

        assert bootstrap._PATCHRIGHT_PERCENT.search(line) is None
        self._terminal(monkeypatch)
        with bootstrap._cli_progress() as report:
            report(line)

    def test_a_retry_after_a_finished_bar_replaces_it_too(self, monkeypatch):
        """Patchright reports 100% before it unpacks, and a corrupt archive
        fails after that. It then retries the same artifact, up to five times.
        Keeping the finished bar would leave four of them for one browser.
        """
        from linkedin_mcp_server import bootstrap

        built = self._terminal(monkeypatch)
        with bootstrap._cli_progress() as report:
            report("Downloading Chromium (v1) from https://a/b.zip")
            report("| 100% of 171.0 MiB")
            report("Downloading Chromium (v1) from https://mirror/b.zip")
            report("|  10% of 171.0 MiB")
            bars = list(built[1].tasks)
            # Read inside: leaving drives whatever is still running to 100%,
            # and these are live objects.
            done = [t.finished for t in bars]

        assert len(bars) == 1, "one artifact, one bar"
        assert done == [False]

    def test_a_silent_retry_reuses_its_bar(self, monkeypatch):
        """A raised npm log level suppresses the retry announcement.

        Patchright retries five times, so a renderer that starts a bar on every
        backwards step leaves five of them for one download.
        """
        from linkedin_mcp_server import bootstrap

        built = self._terminal(monkeypatch)
        with bootstrap._cli_progress() as report:
            report("Downloading Chromium (v1) from https://a/b.zip")
            report("|  40% of 171.0 MiB")
            report("|   0% of 171.0 MiB")  # the retry, unannounced
            report("|  10% of 171.0 MiB")
            bars = list(built[1].tasks)

        assert len(bars) == 1, "one artifact, one bar"
        assert bars[0].description == "Chromium"

    def test_a_silent_retry_after_a_full_bar_reuses_it(self, monkeypatch):
        """Patchright reports 100% before it unpacks, and then retries.

        A corrupt archive downloads to the end, fails to extract, and is
        fetched again from the top: five times, with the announcements
        suppressed. Read as five artifacts, the failure arrives under five
        completed bars for one browser. The total is what tells a retry from
        the next artifact, which is a different archive.
        """
        from linkedin_mcp_server import bootstrap

        built = self._terminal(monkeypatch)
        with bootstrap._cli_progress() as report:
            report("Downloading Chromium (v1) from https://a/b.zip")
            report("| 100% of 171.0 MiB")
            for _ in range(4):  # patchright allows five attempts in all
                report("|   0% of 171.0 MiB")
                report("| 100% of 171.0 MiB")
            bars = list(built[1].tasks)

        assert len(bars) == 1, "one archive, one bar"

    def test_a_second_artifact_without_an_announcement_gets_its_own_bar(
        self, monkeypatch
    ):
        """A raised npm log level suppresses the announcements and keeps the
        percentages. Feeding ffmpeg's ten percent to the finished browser bar
        leaves it finished at ten percent.
        """
        from linkedin_mcp_server import bootstrap

        built = self._terminal(monkeypatch)
        with bootstrap._cli_progress() as report:
            report("| 100% of 171.0 MiB")
            report("|  10% of 1.6 MiB")
            bars = list(built[1].tasks)
            done = [t.finished for t in bars]

        assert len(bars) == 2
        assert done == [True, False]

    def test_a_retry_replaces_its_unfinished_bar(self, monkeypatch):
        """Patchright retries a failed download, announcing it the same way."""
        from linkedin_mcp_server import bootstrap

        built = self._terminal(monkeypatch)
        with bootstrap._cli_progress() as report:
            report("Downloading Chromium (v1) from https://a/b.zip")
            report("|  40% of 171.0 MiB")
            report("Downloading Chromium (v1) from https://mirror/b.zip")
            report("|  10% of 171.0 MiB")
            bars = len(built[1].tasks)
        assert bars == 1, "the abandoned attempt should not keep spinning"

    def test_a_finished_bar_stays_when_the_next_artifact_starts(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        built = self._terminal(monkeypatch)
        with bootstrap._cli_progress() as report:
            report("Downloading Chromium (v1) from https://a/b.zip")
            report("| 100% of 171.0 MiB")
            report("Downloading FFmpeg (v2) from https://a/c.zip")
            report("|  10% of 1.6 MiB")
            bars = len(built[1].tasks)
        assert bars == 2, "the completed browser should stay on screen"


class TestCliProgress:
    """The terminal bar, and the plain lines everywhere else."""

    def _terminal(self, monkeypatch) -> list:
        """Make the CLI path believe it is on a terminal, and capture it.

        Returns the buffer and the live ``Progress``. The bar refreshes on
        rich's own schedule, so a test that wants to see an intermediate frame
        has to ask for one; on the way out every bar is driven to completion.
        """
        from rich.progress import Progress

        from linkedin_mcp_server import bootstrap

        buffer = io.StringIO()
        built: list = [buffer]
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        quiet = bootstrap._ConsoleThatGoesQuiet
        monkeypatch.setattr(
            bootstrap,
            "_ConsoleThatGoesQuiet",
            lambda *a, **k: quiet(file=buffer, force_terminal=True, width=100, **k),
        )

        def record(*args: Any, **kwargs: Any) -> Progress:
            progress = Progress(*args, **kwargs)
            built.append(progress)
            return progress

        monkeypatch.setattr(bootstrap, "Progress", record)
        return built

    def test_richs_own_console_exits_the_process_on_a_broken_pipe(self):
        """The upstream behaviour this subclass exists to replace.

        Measured rather than read, and in a subprocess because the default
        implementation points the caller's ``sys.stdout`` at ``os.devnull``
        before it raises. If a rich release stops exiting here, the subclass
        stops being needed and this says so.
        """
        import subprocess

        script = (
            "import io, os, sys\n"
            "from rich.console import Console\n"
            "class Pipe(io.StringIO):\n"
            "    def write(self, s):\n"
            "        raise BrokenPipeError(32, 'Broken pipe')\n"
            "try:\n"
            "    Console(file=Pipe(), force_terminal=True).print('x')\n"
            "except SystemExit:\n"
            "    os._exit(7)\n"
            "os._exit(0)\n"
        )
        run = subprocess.run([sys.executable, "-c", script], capture_output=True)

        assert run.returncode == 7, "rich no longer exits; drop the subclass"

    def test_a_broken_pipe_silences_the_bar_instead_of_killing_the_install(
        self, monkeypatch
    ):
        """``--install-browser | head`` closes the pipe mid-download.

        Which reaches rich whenever the bar is drawn at all, and
        ``FORCE_COLOR=1`` arranges for that on a redirected stdout. Measured
        before the subclass: exit code 1, empty stderr, the archive half
        unpacked. Going quiet leaves the install running, which is the answer
        the plain-line path already gives a stream that will not take a line.
        """
        from linkedin_mcp_server import bootstrap

        class _Pipe(io.StringIO):
            def isatty(self) -> bool:
                return True

            def write(self, s: str) -> int:
                raise BrokenPipeError(32, "Broken pipe")

        pipe = _Pipe()
        monkeypatch.setattr(sys, "stdout", pipe)
        quiet = bootstrap._ConsoleThatGoesQuiet
        consoles: list = []

        def build(*a: Any, **k: Any):
            console = quiet(file=pipe, force_terminal=True, width=100, **k)
            consoles.append(console)
            return console

        monkeypatch.setattr(bootstrap, "_ConsoleThatGoesQuiet", build)

        with bootstrap._cli_progress() as report:  # must not raise SystemExit
            report("Downloading Chromium (v1) from https://a/b.zip")
            report("|\u25a0\u25a0\u25a0| 40% of 171.0 MiB")

        assert consoles and consoles[0].quiet, "and stops trying after the first"

    def test_a_closed_stream_silences_the_bar_too(self, monkeypatch):
        """A pipe is not the only way a terminal stops taking bytes.

        rich catches ``BrokenPipeError`` and nothing else, so a stdout the host
        has closed answers ``ValueError`` and an IDE terminal whose other end
        detached answers ``OSError`` EIO, both straight out of the bar's own
        refresh. From there it leaves the read loop and reports a failed
        install for a browser already on disk.
        """
        from rich.console import Console

        from linkedin_mcp_server import bootstrap

        for error in (
            ValueError("I/O operation on closed file"),
            OSError(5, "Input/output error"),
        ):

            class _Gone(io.StringIO):
                def isatty(self) -> bool:
                    return True

                def write(self, s: str) -> int:
                    raise error

                def flush(self) -> None:
                    raise error

            gone = _Gone()
            with pytest.raises(type(error)):
                Console(file=gone, force_terminal=True, width=80).print("contract")

            console = bootstrap._ConsoleThatGoesQuiet(
                file=gone, force_terminal=True, width=80
            )
            console.print("| 40% of 171.0 MiB")  # must not raise

            assert console.quiet, f"and stops trying after {error!r}"

    def test_a_failed_install_leaves_its_bar_where_it_died(self, monkeypatch):
        """The completion on the way out is for a success, and only that.

        Patchright can succeed in silence, so the bar is finished when the
        caller leaves without raising. A caller that raises is a failed
        install, and filling its bar to 100% would report the opposite.
        """
        from linkedin_mcp_server import bootstrap

        built = self._terminal(monkeypatch)
        with pytest.raises(RuntimeError):
            with bootstrap._cli_progress() as report:
                report("Downloading Chromium (v1) from https://a/b.zip")
                report("|\u25a0\u25a0\u25a0| 40% of 171.0 MiB")
                raise RuntimeError("the mirror refused")

        assert not built[1].tasks[0].finished

    def test_a_retried_download_reuses_its_bar(self, monkeypatch):
        """Patchright restarts a failed download at zero, up to five times.

        Adding a bar per attempt would leave four abandoned ones spinning at
        the percentage where each died.
        """
        from rich.progress import Progress

        from linkedin_mcp_server import bootstrap

        self._terminal(monkeypatch)
        created: list[str] = []
        real_add = Progress.add_task

        def counting(self, description: str, **kwargs):  # noqa: ANN003
            created.append(description)
            return real_add(self, description, **kwargs)

        monkeypatch.setattr(Progress, "add_task", counting)

        with bootstrap._cli_progress() as report:
            report("Downloading Chrome for Testing 149.0.7827.55 from https://c/x")
            report("|\u25a0\u25a0\u25a0| 40% of 171.0 MiB")
            report("|\u25a0| 10% of 171.0 MiB")  # the retry, back at the start

        # One bar for the whole thing: the placeholder the context opens with,
        # renamed by the announcement and rewound by the retry.
        assert len(created) == 1, created

    def test_a_terminal_that_cannot_encode_the_spinner_gets_lines(self, monkeypatch):
        """``PYTHONIOENCODING=ascii`` on a real terminal, which rich walks into.

        Measured: rich downgrades the bar itself when the console encoding is
        not utf, and does not downgrade the spinner, so the braille reaches
        ``write`` and raises. Inside the live region that leaves the read loop,
        kills the installer and reports an encoding error in place of a browser.
        """
        from linkedin_mcp_server import bootstrap

        class _AsciiTty(io.StringIO):
            encoding = "ascii"

            def isatty(self) -> bool:
                return True

            def write(self, s: str) -> int:
                s.encode("ascii")  # exactly what a real ascii stream does
                return super().write(s)

        monkeypatch.setattr(sys, "stdout", _AsciiTty())

        with bootstrap._cli_progress() as report:
            assert report is bootstrap._print_whatever_the_stream_takes

    def test_rich_draws_nothing_outside_the_glyphs_that_were_checked(self, monkeypatch):
        """The encodability check is only as good as its list of glyphs.

        Renders the shipped column set and looks for any non-ASCII character
        that ``_BAR_GLYPHS`` does not name. A rich release that adds one to the
        bar, the spinner or a column fails here rather than on the terminal of
        whoever has an ascii stdout.
        """
        from linkedin_mcp_server import bootstrap

        buffer = self._terminal(monkeypatch)[0]

        with bootstrap._cli_progress() as report:
            report(
                "Downloading Chrome for Testing 149.0.7827.55 "
                "(playwright chromium v1228) from https://cdn.example/x.zip"
            )
            for percent in (0, 10, 40, 100):
                report(f"|\u25a0\u25a0| {percent}% of 171.0 MiB")

        drawn = {c for c in buffer.getvalue() if ord(c) > 127}
        # The bar's three, which rich swaps for "-" and " " on an ascii
        # console, plus whatever the spinner is drawing.
        assert drawn <= set(bootstrap._BAR_GLYPHS + "\u2501\u2578\u257a"), drawn

    def test_without_a_terminal_the_lines_are_printed(self, monkeypatch):
        """Redirected output keeps the installer's own lines."""
        from linkedin_mcp_server import bootstrap

        monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)

        with bootstrap._cli_progress() as report:
            assert report is bootstrap._print_whatever_the_stream_takes

    def test_a_terminal_gets_a_bar_carrying_the_artifact_name(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        built = self._terminal(monkeypatch)
        buffer = built[0]

        with bootstrap._cli_progress() as report:
            report(
                "Downloading Chrome for Testing 149.0.7827.55 "
                "(playwright chromium v1228) from https://cdn.example/x.zip"
            )
            report("|■■■■     |  40% of 171.0 MiB")
            # rich refreshes on its own schedule and leaving completes the bar,
            # so the frame carrying 40% has to be asked for here.
            built[1].refresh()
            rendered = buffer.getvalue()

        # The name without patchright's parenthetical, and the percentage it
        # reported. The bar itself is rich's business, not this test's.
        assert "Chrome for Testing 149.0.7827.55" in rendered
        assert "(playwright" not in rendered
        assert "40%" in rendered

    def test_lines_that_are_not_progress_survive(self, monkeypatch):
        """A format change must cost the bar, never the message."""
        from linkedin_mcp_server import bootstrap

        buffer = self._terminal(monkeypatch)[0]

        with bootstrap._cli_progress() as report:
            report("Host system is missing dependencies to run browsers.")

        assert "missing dependencies" in buffer.getvalue()

    def test_a_second_artifact_gets_its_own_bar(self, monkeypatch):
        """ffmpeg follows the browser; it must not rewind the finished bar."""
        from linkedin_mcp_server import bootstrap

        buffer = self._terminal(monkeypatch)[0]

        with bootstrap._cli_progress() as report:
            report("Downloading Chrome for Testing 149.0 (x) from https://a/b.zip")
            report("|■■■■■■■■■|  100% of 171.0 MiB")
            report("Downloading FFmpeg (playwright ffmpeg v1011) from https://a/c.zip")
            report("|■        |  10% of 1.6 MiB")

        rendered = buffer.getvalue()
        assert "Chrome for Testing 149.0" in rendered
        assert "FFmpeg" in rendered


class _StdoutThatClaimsATerminal(io.StringIO):
    """The screen the bar is drawn on, as far as the moving rule can tell.

    A plain ``StringIO`` would stand in for a terminal that answers False to
    ``isatty``, which no terminal does, and the rule would then read the double
    rather than the case.
    """

    def isatty(self) -> bool:
        return True


class _StreamThatClaimsATerminal(io.TextIOWrapper):
    """A real descriptor behind an ``isatty`` that answers True.

    A pty in every respect the destination rule can read, without needing one:
    it can name where it writes *and* it claims a terminal, which is the pair
    that separates a proven destination from a guessed one.
    """

    def __init__(self, file):
        super().__init__(open(file.fileno(), "wb", closefd=False))

    def isatty(self) -> bool:
        return True


class TestWhereAStreamWrites:
    """Two streams share a destination, or they demonstrably do not."""

    def test_one_known_device_and_one_unknown_is_not_a_match(self, tmp_path):
        """A wrapper that hides its descriptor may be a second pane.

        Guessing yes there moves a handler's records out of the destination
        the operator chose. Guessing no draws one record through the bar. Only
        the first is unrecoverable, so an unknown is never a match.
        """
        from linkedin_mcp_server import bootstrap

        class _TerminalWithoutADescriptor(io.StringIO):
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                raise io.UnsupportedOperation("no descriptor here")

        # Both claim a terminal, so the guess would say yes; one of them can
        # prove where it writes and the other cannot, which is the pty and the
        # second pane. Only the pair that proves it counts as a match.
        with (tmp_path / "out.log").open("w") as file:
            known = _StreamThatClaimsATerminal(file)
            assert known.isatty() and known.fileno() >= 0
            assert not bootstrap._same_destination(known, _TerminalWithoutADescriptor())

    def test_two_streams_that_cannot_name_a_device_still_use_the_terminal_guess(self):
        """Both unknown is where the old answer is still the best one."""
        from linkedin_mcp_server import bootstrap

        assert bootstrap._same_destination(
            _StdoutThatClaimsATerminal(), _StdoutThatClaimsATerminal()
        )
        assert not bootstrap._same_destination(io.StringIO(), io.StringIO())

    def test_a_windows_pipe_reports_no_device_at_all(self, monkeypatch, tmp_path):
        """``(0, 0)`` is Windows saying nothing, not a device two pipes share.

        Measured in ``Python/fileutils.c``: ``_Py_fstat_noraise`` zeroes the
        whole struct and then fills in ``st_mode`` alone for ``FILE_TYPE_PIPE``
        and ``FILE_TYPE_CHAR``. Read as an identity, every pipe on Windows is
        the same pipe, and a console and a pipe are the same destination: which
        is exactly the pairing ``2>errors.log`` produces. Patched here because
        the platform that answers this way cannot run the suite.
        """
        from linkedin_mcp_server import bootstrap

        zeroed = os.stat_result((0o010600, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        monkeypatch.setattr(bootstrap.os, "fstat", lambda fd: zeroed)

        with (tmp_path / "a").open("w") as one, (tmp_path / "b").open("w") as other:
            assert bootstrap._device_of(one) is None
            # Neither claims a terminal, so the fallback cannot rescue it
            # either, which is the answer a pipe and a console need.
            assert not bootstrap._same_destination(one, other)


class TestLogHandlersDuringTheBar:
    """Log records must not be drawn through the live region."""

    def _terminal(self, monkeypatch, file: IO[str] | None = None) -> None:
        from linkedin_mcp_server import bootstrap

        drawn_on = _StdoutThatClaimsATerminal() if file is None else file
        quiet = bootstrap._ConsoleThatGoesQuiet
        monkeypatch.setattr(
            bootstrap,
            "_ConsoleThatGoesQuiet",
            lambda *a, **k: quiet(
                file=drawn_on,
                force_terminal=True,
                width=100,
                _environ={"TERM": "xterm-256color"},
                **k,
            ),
        )

    def test_a_stream_handler_is_moved_and_restored(self, monkeypatch):
        """rich swaps the streams; a handler built earlier holds the old one.

        Measured in a pty: without this, a DEBUG record emitted while the bar
        is up ends up on the same physical line as the bar.
        """
        from linkedin_mcp_server import bootstrap

        self._terminal(monkeypatch)

        # A stream that claims to be a terminal, because only those collide
        # with the bar and only those are moved. Under pytest the real stderr
        # is captured and reports False, which is the behaviour the sibling
        # test relies on.
        class _TerminalStream(io.StringIO):
            def isatty(self) -> bool:
                return True

        terminal_stderr = _TerminalStream()
        monkeypatch.setattr(sys, "stderr", terminal_stderr)
        handler = logging.StreamHandler()  # binds sys.stderr, as logging does
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            bound = handler.stream
            with bootstrap._cli_progress():
                assert handler.stream is not bound, (
                    "the handler must follow rich's redirection"
                )
            assert handler.stream is bound, "and be put back"
        finally:
            root.removeHandler(handler)

    def test_a_redirected_stderr_keeps_its_destination(self, monkeypatch):
        """Moving it would take records out of the file and onto the screen.

        rich's stderr proxy writes through the progress console, which is on
        stdout. A handler whose stderr is a file cannot collide with the bar,
        so it has nothing to gain and a destination to lose.
        """
        from linkedin_mcp_server import bootstrap

        self._terminal(monkeypatch)
        redirected = io.StringIO()  # isatty() is False, as a file's is
        monkeypatch.setattr(sys, "stderr", redirected)
        handler = logging.StreamHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            with bootstrap._cli_progress():
                assert handler.stream is redirected
        finally:
            root.removeHandler(handler)

    def test_a_forced_terminal_moves_a_handler_that_shares_the_file(
        self, monkeypatch, tmp_path
    ):
        """``FORCE_COLOR=1 ... >build.log 2>&1``: rich draws into the file.

        ``Console.is_terminal`` returns True on ``FORCE_COLOR`` or
        ``TTY_COMPATIBLE=1`` without consulting the stream, which is how a CI
        runner keeps colour through a redirect. Both streams then answer False
        to ``isatty`` while the bar is live, and measured against that: the
        record was written into the middle of a rendered frame.
        """
        from linkedin_mcp_server import bootstrap

        log = tmp_path / "build.log"
        with log.open("w") as out, log.open("a") as err:
            self._terminal(monkeypatch, file=out)
            assert not err.isatty(), "the case is a redirect, not a terminal"
            monkeypatch.setattr(sys, "stderr", err)
            handler = logging.StreamHandler()
            root = logging.getLogger()
            root.addHandler(handler)
            try:
                with bootstrap._cli_progress():
                    assert handler.stream is not err, (
                        "a handler on the file the bar is drawn into must follow it"
                    )
                assert handler.stream is err, "and be put back"
            finally:
                root.removeHandler(handler)

    def test_a_forced_terminal_leaves_a_handler_on_another_file_alone(
        self, monkeypatch, tmp_path
    ):
        """The other half of the same rule: ``FORCE_COLOR=1 ... 2>errors.log``.

        Forcing a terminal must not start moving handlers that write somewhere
        else. Both proxies write through the progress console, so this one's
        records would leave the file the operator named.
        """
        from linkedin_mcp_server import bootstrap

        with (tmp_path / "build.log").open("w") as out:
            with (tmp_path / "errors.log").open("w") as err:
                self._terminal(monkeypatch, file=out)
                monkeypatch.setattr(sys, "stderr", err)
                handler = logging.StreamHandler()
                root = logging.getLogger()
                root.addHandler(handler)
                try:
                    with bootstrap._cli_progress():
                        assert handler.stream is err, (
                            "a separate file keeps its records"
                        )
                finally:
                    root.removeHandler(handler)

    def test_a_separate_stderr_is_left_unredirected(self, monkeypatch, tmp_path):
        """rich redirects both streams by default, wherever they point.

        ``Progress`` defaults to ``redirect_stderr=True`` and ``Live`` acts on
        it whenever the console claims a terminal, so under ``2>errors.log`` a
        direct ``sys.stderr.write`` would come out on the bar's screen and the
        file the operator named would stay empty. Measured against rich 15.0.0.
        """
        from linkedin_mcp_server import bootstrap

        with (tmp_path / "build.log").open("w") as out:
            with (tmp_path / "errors.log").open("w") as err:
                self._terminal(monkeypatch, file=out)
                monkeypatch.setattr(sys, "stderr", err)
                with bootstrap._cli_progress():
                    assert sys.stderr is err, (
                        "a stderr going somewhere else keeps going there"
                    )

    def test_a_shared_stderr_is_still_redirected(self, monkeypatch, tmp_path):
        """The other half: ``>build.log 2>&1`` must keep rich's redirection.

        Turning it off wholesale would put every direct write back through the
        rendered frame, which is what the redirection exists to prevent.
        """
        from rich.file_proxy import FileProxy

        from linkedin_mcp_server import bootstrap

        log = tmp_path / "build.log"
        with log.open("w") as out, log.open("a") as err:
            self._terminal(monkeypatch, file=out)
            monkeypatch.setattr(sys, "stderr", err)
            with bootstrap._cli_progress():
                assert isinstance(sys.stderr, FileProxy), (
                    "writes to the bar's own file must go above it"
                )

    def test_a_handler_reconfigured_during_the_download_keeps_its_new_stream(
        self, monkeypatch
    ):
        """Restoring is putting back what was taken, not overwriting a choice.

        A host that calls ``setStream`` while the bar is up has named a newer
        destination, and the stream captured on the way in may have been closed
        along with the configuration that held it.
        """
        from linkedin_mcp_server import bootstrap

        self._terminal(monkeypatch)

        class _TerminalStream(io.StringIO):
            def isatty(self) -> bool:
                return True

        monkeypatch.setattr(sys, "stderr", _TerminalStream())
        handler = logging.StreamHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        chosen_later = io.StringIO()
        try:
            with bootstrap._cli_progress():
                assert handler.stream is not sys.__stderr__
                handler.setStream(chosen_later)
            assert handler.stream is chosen_later, (
                "the newer configuration outlives the bar"
            )
        finally:
            root.removeHandler(handler)

    def test_an_isatty_that_raises_falls_back_to_plain_lines(self, monkeypatch):
        """A detached pty answers ``EIO`` rather than True or False.

        rich guards that call for ``ValueError`` alone (measured in 15.0.0), so
        an ``OSError`` comes back out of the console. Not knowing whether there
        is a terminal is the plain-line case; raising would take the whole
        browser install with it.
        """
        from linkedin_mcp_server import bootstrap

        class _DetachedTerminal(io.StringIO):
            def isatty(self) -> bool:
                raise OSError(5, "Input/output error")

        quiet = bootstrap._ConsoleThatGoesQuiet
        monkeypatch.setattr(
            bootstrap,
            "_ConsoleThatGoesQuiet",
            lambda *a, **k: quiet(file=_DetachedTerminal(), _environ={}, **k),
        )
        with bootstrap._cli_progress() as report:
            assert report is bootstrap._print_whatever_the_stream_takes

    def test_a_handler_on_stdout_stays_when_the_bar_is_drawn_elsewhere(
        self, monkeypatch, tmp_path
    ):
        """The rule is the destination, and it is asked of both streams.

        rich redirects stdout unconditionally while the bar is up, so this one
        is decided here and nowhere else: a handler writing where the bar is
        not must keep its own file, whichever of the two streams it holds.
        """
        from linkedin_mcp_server import bootstrap

        with (tmp_path / "bar.log").open("w") as drawn_on:
            with (tmp_path / "records.log").open("w") as elsewhere:
                self._terminal(monkeypatch, file=drawn_on)
                monkeypatch.setattr(sys, "stdout", elsewhere)
                handler = logging.StreamHandler(elsewhere)
                root = logging.getLogger()
                root.addHandler(handler)
                try:
                    with bootstrap._cli_progress():
                        assert handler.stream is elsewhere, (
                            "records keep the file they were pointed at"
                        )
                finally:
                    root.removeHandler(handler)

    def test_a_file_handler_is_left_alone(self, monkeypatch, tmp_path):
        """Only handlers on the real streams move; the trace file keeps its own."""
        from linkedin_mcp_server import bootstrap

        self._terminal(monkeypatch)
        handler = logging.FileHandler(tmp_path / "server.log")
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            with bootstrap._cli_progress():
                assert handler.stream is not sys.stderr
        finally:
            root.removeHandler(handler)
            handler.close()


class TestProgressWithoutPercentages:
    """A mirror can suppress patchright's progress entirely."""

    def _terminal(self, monkeypatch, term: str = "xterm-256color") -> list:
        import io as _io

        from rich.progress import Progress

        from linkedin_mcp_server import bootstrap

        buffer = _io.StringIO()
        built: list = [buffer]
        quiet = bootstrap._ConsoleThatGoesQuiet
        monkeypatch.setattr(
            bootstrap,
            "_ConsoleThatGoesQuiet",
            lambda *a, **k: quiet(
                file=buffer,
                force_terminal=True,
                width=100,
                _environ={"TERM": term},
                **k,
            ),
        )

        def record(*args: Any, **kwargs: Any) -> Progress:
            progress = Progress(*args, **kwargs)
            built.append(progress)
            return progress

        monkeypatch.setattr(bootstrap, "Progress", record)
        return built

    def test_a_bar_exists_before_any_output_arrives(self, monkeypatch):
        """Patchright has configurations where it says nothing at all.

        `npm_config_loglevel=warn` suppresses its announcements, a chunked
        response suppresses its percentages, and it waits on the registry lock
        for up to ten minutes in silence. A bar created on the first
        recognised line leaves every one of those blank.
        """
        from linkedin_mcp_server import bootstrap

        built = self._terminal(monkeypatch)
        with bootstrap._cli_progress():
            bars = list(built[1].tasks)
            pulsing = [t.total for t in bars]

        assert len(bars) == 1
        assert pulsing == [None], "an unknown size pulses"
        assert bars[0].finished, "and does not pulse on past the install"

    def test_the_first_announcement_names_the_waiting_bar(self, monkeypatch):
        """Rather than leaving an unnamed one behind and starting a second."""
        from linkedin_mcp_server import bootstrap

        built = self._terminal(monkeypatch)
        with bootstrap._cli_progress() as report:
            report("Downloading Chromium (v1) from https://a/b.zip")
            bars = list(built[1].tasks)

        assert len(bars) == 1
        assert bars[0].description == "Chromium"

    def test_a_chunked_download_still_gets_a_bar(self, monkeypatch):
        """`Transfer-Encoding: chunked` makes patchright report no percentage.

        Its reporter is guarded by `if (!chunked && reportProgress)`, so a bar
        created on the first percentage would never be created at all, and the
        download would run behind exactly the silence #533 is about.
        """
        from linkedin_mcp_server import bootstrap

        built = self._terminal(monkeypatch)
        with bootstrap._cli_progress() as report:
            report("Downloading Chromium (v1) from https://mirror/b.zip")
            bars = list(built[1].tasks)
            assert len(bars) == 1, "the bar must exist before any percentage"
            assert bars[0].total is None, "an unknown size pulses"
            report("Chromium (v1) downloaded to /tmp/chromium-1228")
            assert built[1].tasks[0].finished, "the archive is in place"

    def test_a_percentage_fills_in_the_size_afterwards(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        built = self._terminal(monkeypatch)
        with bootstrap._cli_progress() as report:
            report("Downloading Chromium (v1) from https://a/b.zip")
            report("|  40% of 171.0 MiB")
            task = built[1].tasks[0]
            assert task.total == int(171.0 * 1024**2)
            assert task.completed == int(task.total * 0.4)

    @pytest.mark.parametrize("term", ["dumb", "unknown"])
    def test_a_dumb_terminal_gets_the_lines_instead(self, monkeypatch, term):
        """rich refuses to draw live there, and does not fall back either.

        `Live.refresh` renders through `elif not self._started`, which is once,
        on the way out. Sending a dumb terminal down the bar would show nothing
        for the whole download.
        """
        from linkedin_mcp_server import bootstrap

        self._terminal(monkeypatch, term=term)
        with bootstrap._cli_progress() as report:
            assert report is bootstrap._print_whatever_the_stream_takes


class TestInstallerLinesAgainstARealPipe:
    """The reader, against a real ``StreamReader`` rather than the fake above.

    ``_FakeStdout`` is a claim about asyncio: that ``read(n)`` returns at most
    *n* bytes and b"" at EOF. These run the same scenarios through an actual
    subprocess pipe, so the fake cannot quietly drift from what asyncio does and
    take the tests that rely on it along.
    """

    async def _collect(self, script: str) -> list[str]:
        from linkedin_mcp_server.bootstrap import _installer_lines

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        pieces = [line async for line in _installer_lines(proc.stdout)]
        await proc.wait()
        return pieces

    async def test_a_line_past_the_readline_limit_survives(self):
        """The shape that raises ValueError when read through readline."""
        pieces = await self._collect(
            "import sys; sys.stdout.write('x' * 200_000 + chr(10))"
        )
        assert "".join(pieces) == "x" * 200_000

    async def test_output_without_any_newline_survives(self):
        pieces = await self._collect("import sys; sys.stdout.write('z' * 200_000)")
        assert "".join(pieces) == "z" * 200_000

    async def test_multibyte_characters_survive_the_read_boundaries(self):
        """40 000 three-byte characters cannot align with 64 KiB reads."""
        pieces = await self._collect(
            "import sys; sys.stdout.write('■' * 40_000 + chr(10))"
        )
        assert "".join(pieces) == "■" * 40_000

    async def test_ordinary_lines_come_out_one_by_one(self):
        pieces = await self._collect("import sys; sys.stdout.write('a\\nb\\n\\nc\\n')")
        assert pieces == ["a", "b", "", "c"]

    async def test_a_lines_own_leading_whitespace_survives(self):
        """Only the end of a line is trimmed. Patchright indents its output."""
        pieces = await self._collect("import sys; sys.stdout.write('   indented  \\n')")
        assert pieces == ["   indented"]


class TestInstallCallbackThreading:
    """Only the CLI path prints; the background path stays silent."""

    def _capture_install(self, monkeypatch) -> dict[str, object]:
        from linkedin_mcp_server import bootstrap

        seen: dict[str, object] = {}

        async def fake_install(extra_arg: str, *, line_callback=None) -> None:
            seen["extra_arg"] = extra_arg
            seen["line_callback"] = line_callback

        monkeypatch.setattr(bootstrap, "_run_patchright_install", fake_install)
        return seen

    async def test_background_setup_forwards_no_callback(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        _patch_targets_and_version(monkeypatch)
        seen = self._capture_install(monkeypatch)

        await bootstrap._run_browser_setup()

        assert seen["line_callback"] is None

    async def test_setup_forwards_an_explicit_callback(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        _patch_targets_and_version(monkeypatch)
        seen = self._capture_install(monkeypatch)

        def sink(line: str) -> None:
            pass

        await bootstrap._run_browser_setup(line_callback=sink)

        assert seen["line_callback"] is sink

    def test_cli_install_passes_a_flushing_print(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        _patch_targets_and_version(monkeypatch)
        monkeypatch.setattr(bootstrap, "_uses_custom_chrome", lambda: False)
        monkeypatch.setattr(bootstrap, "browser_ready", lambda: False)

        captured: dict[str, object] = {}

        async def fake_ensure(*, line_callback=None) -> None:
            captured["line_callback"] = line_callback

        monkeypatch.setattr(bootstrap, "_ensure_browser_installed", fake_ensure)

        bootstrap.ensure_browser_installed()

        # Flushing, because block buffering of a redirected stdout would hold
        # every line until exit, and tolerant of a stream that cannot encode
        # patchright's bar glyph.
        assert captured["line_callback"] is bootstrap._print_whatever_the_stream_takes

    async def test_setup_logs_the_download_duration(
        self, isolate_profile_dir, monkeypatch, caplog
    ):
        from linkedin_mcp_server import bootstrap

        _patch_targets_and_version(monkeypatch)
        self._capture_install(monkeypatch)

        with caplog.at_level(logging.INFO, logger="linkedin_mcp_server.bootstrap"):
            await bootstrap._run_browser_setup()

        assert any(
            "browser setup completed in" in record.message for record in caplog.records
        )


class TestEnsureBrowserInstalledSkipsCustomChrome:
    """A custom executable must not trigger a managed download.

    The gap this closes was survivable while two of the three CLI modes needed
    only the much smaller shell. Now they all want the full browser, so it is
    the whole download fetched for something that is never launched -- and for
    an operator whose network cannot reach the CDN, it is the difference
    between signing in and not. `_uses_custom_chrome()` already existed and
    said so in its docstring; only this caller never asked.
    """

    def test_custom_chrome_skips_the_download(self, isolate_profile_dir, monkeypatch):
        _patch_targets_and_version(monkeypatch)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._uses_custom_chrome", lambda: True
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_ready", lambda: False
        )

        called = {"value": 0}

        async def fake_install(**_kwargs: object) -> None:
            called["value"] += 1

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._ensure_browser_installed", fake_install
        )

        ensure_browser_installed()

        assert called["value"] == 0

    def test_managed_chrome_still_downloads(self, isolate_profile_dir, monkeypatch):
        """The short circuit must be about the custom path, not about skipping."""
        _patch_targets_and_version(monkeypatch)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._uses_custom_chrome", lambda: False
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_ready", lambda: False
        )

        called = {"value": 0}

        async def fake_install(**_kwargs: object) -> None:
            called["value"] += 1

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._ensure_browser_installed", fake_install
        )

        ensure_browser_installed()

        assert called["value"] == 1


class TestEnsureBrowserInstalled:
    """The CLI installer: one browser, the same one for every mode."""

    @pytest.fixture(autouse=True)
    def _managed_browser(self, monkeypatch):
        """No custom executable, stated rather than assumed.

        `ensure_browser_installed` now asks whether CHROME_PATH is set, and
        answering that reads the configuration, which pytest cannot resolve on
        its own.
        """
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._uses_custom_chrome", lambda: False
        )

    def _stub(self, monkeypatch):
        calls = {"value": 0}

        async def fake_install(**_kwargs: object) -> None:
            calls["value"] += 1

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._ensure_browser_installed", fake_install
        )
        return calls

    def test_installs_when_absent(self, isolate_profile_dir, monkeypatch):
        _patch_targets_and_version(monkeypatch)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_ready", lambda: False
        )
        calls = self._stub(monkeypatch)

        ensure_browser_installed()

        assert calls["value"] == 1

    def test_noop_when_present(self, isolate_profile_dir, monkeypatch):
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.browser_ready", lambda: True)
        calls = self._stub(monkeypatch)

        ensure_browser_installed()

        assert calls["value"] == 0

    def test_shell_only_is_not_enough(self, isolate_profile_dir, monkeypatch):
        """A pre-existing shell-only install must still trigger the download.

        This is the upgrade path: someone who only ever ran headless has the
        shell and nothing else, and the browser they are about to launch is not
        there.
        """
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium_headless_shell-1217"])
        _write_metadata(install_metadata_path(), bdir)
        calls = self._stub(monkeypatch)

        ensure_browser_installed()

        assert calls["value"] == 1

    def test_a_gone_stdout_does_not_take_a_successful_install_with_it(
        self, isolate_profile_dir, monkeypatch
    ):
        """``--install-browser | head`` must not turn a success into a traceback.

        The bar's own console goes quiet on a broken pipe, but the three lines
        around it are printed directly, and an unguarded one raises straight
        out of the installer: measured, a browser that installed cleanly
        reported ``BrokenPipeError`` instead of "Browser installed."
        """
        _patch_targets_and_version(monkeypatch)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_ready", lambda: False
        )
        calls = self._stub(monkeypatch)
        monkeypatch.setattr("sys.stdout", _StdoutThatIsGone())

        ensure_browser_installed()

        assert calls["value"] == 1

    def test_a_gone_stdout_does_not_mask_why_the_install_failed(
        self, isolate_profile_dir, monkeypatch
    ):
        """The failure line is on the same pipe, and it is printed first.

        Raising from it would replace the error that says what went wrong with
        one that says only that nobody was listening.
        """
        _patch_targets_and_version(monkeypatch)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_ready", lambda: False
        )

        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        async def fake_install(**_kwargs: object) -> None:
            raise BrowserSetupFailedError("the mirror refused")

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._ensure_browser_installed", fake_install
        )
        monkeypatch.setattr("sys.stdout", _StdoutThatIsGone())

        with pytest.raises(BrowserSetupFailedError, match="the mirror refused"):
            ensure_browser_installed()


class TestLoginInstallBackstop:
    """The headed manual-login fallback installs full chromium before launching."""

    def _stub(self, monkeypatch, *, custom_chrome: bool):
        order: list[str] = []

        async def fake_full(**_kwargs: object) -> None:
            order.append("full")

        async def fake_login(_profile_dir, *, superseded_by=None) -> bool:
            order.append("login")
            return True

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._uses_custom_chrome", lambda: custom_chrome
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._ensure_browser_installed", fake_full
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.interactive_login", fake_login
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.get_profile_dir",
            lambda: Path("/tmp/profile"),
        )
        return order

    async def test_installs_the_browser_before_the_login_launch(self, monkeypatch):
        order = self._stub(monkeypatch, custom_chrome=False)

        from linkedin_mcp_server.bootstrap import _run_login_flow

        await _run_login_flow()

        assert order == ["full", "login"]

    async def test_skips_the_install_for_a_custom_chrome(self, monkeypatch):
        order = self._stub(monkeypatch, custom_chrome=True)

        from linkedin_mcp_server.bootstrap import _run_login_flow

        await _run_login_flow()

        assert order == ["login"]


class TestPatchrightInstallTargets:
    def _stub_registry(self, monkeypatch, payload, tmp_path):
        registry = tmp_path / "browsers.json"
        registry.write_text(json.dumps(payload))
        fake_pkg_dir = tmp_path / "patchright_pkg"
        (fake_pkg_dir / "driver" / "package").mkdir(parents=True)
        (fake_pkg_dir / "driver" / "package" / "browsers.json").write_text(
            json.dumps(payload)
        )
        # Make `Path(patchright.__file__).parent` resolve to fake_pkg_dir.
        fake_module = MagicMock()
        fake_module.__file__ = str(fake_pkg_dir / "__init__.py")
        monkeypatch.setitem(__import__("sys").modules, "patchright", fake_module)

    def test_resolves_chromium_pair(self, monkeypatch, tmp_path):
        self._stub_registry(
            monkeypatch,
            {
                "browsers": [
                    {
                        "name": "chromium",
                        "revision": "1217",
                        "installByDefault": True,
                    },
                    {
                        "name": "chromium-headless-shell",
                        "revision": "1217",
                        "installByDefault": True,
                    },
                ]
            },
            tmp_path,
        )
        assert _patchright_install_targets() == {
            "chromium-": "1217",
            "chromium_headless_shell-": "1217",
        }

    def test_skips_unrelated_browsers(self, monkeypatch, tmp_path):
        self._stub_registry(
            monkeypatch,
            {
                "browsers": [
                    {
                        "name": "chromium",
                        "revision": "1217",
                        "installByDefault": True,
                    },
                    {
                        "name": "chromium-headless-shell",
                        "revision": "1217",
                        "installByDefault": True,
                    },
                    {
                        "name": "firefox",
                        "revision": "1465",
                        "installByDefault": True,
                    },
                    {
                        "name": "webkit",
                        "revision": "2150",
                        "installByDefault": True,
                    },
                    {
                        "name": "ffmpeg",
                        "revision": "1011",
                        "installByDefault": True,
                    },
                    {
                        "name": "android",
                        "revision": "1001",
                        "installByDefault": False,
                    },
                ]
            },
            tmp_path,
        )
        assert _patchright_install_targets() == {
            "chromium-": "1217",
            "chromium_headless_shell-": "1217",
        }

    def test_returns_none_on_non_dict_payload(self, monkeypatch, tmp_path):
        self._stub_registry(monkeypatch, ["not", "a", "dict"], tmp_path)
        assert _patchright_install_targets() is None

    def test_returns_none_on_missing_registry(self, monkeypatch, tmp_path):
        fake_pkg_dir = tmp_path / "patchright_pkg"
        fake_pkg_dir.mkdir()
        # No driver/package/browsers.json → OSError
        fake_module = MagicMock()
        fake_module.__file__ = str(fake_pkg_dir / "__init__.py")
        monkeypatch.setitem(__import__("sys").modules, "patchright", fake_module)
        assert _patchright_install_targets() is None

    def test_skips_install_by_default_false(self, monkeypatch, tmp_path):
        self._stub_registry(
            monkeypatch,
            {
                "browsers": [
                    {
                        "name": "chromium",
                        "revision": "1217",
                        "installByDefault": False,
                    },
                ]
            },
            tmp_path,
        )
        assert _patchright_install_targets() is None


class TestInvalidateBrowserSetup:
    def test_drops_metadata_and_resets_ready_state(self, isolate_profile_dir):
        bdir = browsers_path()
        bdir.mkdir(parents=True, exist_ok=True)
        _write_metadata(install_metadata_path(), bdir)

        initialize_bootstrap("managed")
        state = get_bootstrap_state()
        state.setup_state = SetupState.READY
        state.setup_completed_at = "2026-01-01T00:00:00Z"

        invalidate_browser_setup()

        assert not install_metadata_path().exists()
        assert state.setup_state is SetupState.IDLE
        assert state.setup_completed_at is None

    @pytest.mark.parametrize(
        "leave_state",
        [SetupState.IDLE, SetupState.RUNNING, SetupState.FAILED],
    )
    def test_leaves_non_ready_state_alone(self, isolate_profile_dir, leave_state):
        bdir = browsers_path()
        bdir.mkdir(parents=True, exist_ok=True)
        _write_metadata(install_metadata_path(), bdir)

        initialize_bootstrap("managed")
        state = get_bootstrap_state()
        state.setup_state = leave_state

        invalidate_browser_setup()

        assert state.setup_state is leave_state


class TestEnsureToolReadyInvalidatesStaleReady:
    async def test_invalidates_when_ready_state_disagrees_with_disk(
        self, isolate_profile_dir, monkeypatch
    ):
        async def fake_setup() -> None:
            return None

        _patch_inline_wait(monkeypatch, 0)
        # Disk says not-ready, in-memory state cached READY.
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_setup_ready", lambda: False
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_browser_setup", fake_setup
        )

        # Pre-existing stale metadata file the invalidator should drop.
        bdir = browsers_path()
        bdir.mkdir(parents=True, exist_ok=True)
        _write_metadata(install_metadata_path(), bdir)

        initialize_bootstrap("managed")
        state = get_bootstrap_state()
        state.setup_state = SetupState.READY
        state.setup_completed_at = "2026-01-01T00:00:00Z"

        with pytest.raises(BrowserSetupInProgressError):
            await ensure_tool_ready_or_raise("get_person_profile")

        # Invalidator must have run — metadata gone, state reset, install task spawned.
        assert not install_metadata_path().exists()
        assert state.setup_state is SetupState.RUNNING
        assert state.setup_task is not None
        await state.setup_task


class TestConfigureBrowserEnvironment:
    def test_honors_existing_env_var(self, isolate_profile_dir, monkeypatch, tmp_path):
        custom = tmp_path / "shared-cache"
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(custom))

        result = configure_browser_environment()

        assert result == custom
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(custom)

    def test_defaults_when_env_unset(self, isolate_profile_dir, monkeypatch):
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

        result = configure_browser_environment()

        assert result == browsers_path()
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers_path())

    def test_expands_tilde_in_env_var(self, isolate_profile_dir, monkeypatch):
        """A pre-set ``~``-prefixed path is expanded so readiness/metadata stay consistent."""
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "~/some-custom-browsers-cache")

        result = configure_browser_environment()

        assert "~" not in str(result)
        assert result.is_absolute()
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(result)

    def test_absolutizes_relative_env_var(
        self, isolate_profile_dir, monkeypatch, tmp_path
    ):
        """A relative path env var is made absolute so subsequent readiness checks don't depend on cwd."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "relative-cache")

        result = configure_browser_environment()

        assert result.is_absolute()
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(result)


class TestHasInstallFor:
    def test_true_when_marker_present(self, isolate_profile_dir):
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217"])
        assert _has_install_for(bdir, "chromium-", "1217") is True

    def test_false_when_dir_missing(self, isolate_profile_dir):
        bdir = browsers_path()
        bdir.mkdir(parents=True, exist_ok=True)
        assert _has_install_for(bdir, "chromium-", "1217") is False

    def test_false_when_marker_missing(self, isolate_profile_dir):
        bdir = browsers_path()
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "chromium-1217").mkdir()
        assert _has_install_for(bdir, "chromium-", "1217") is False


class TestInlineLoginWait:
    async def test_inline_wait_resumes_on_success(
        self, isolate_profile_dir, monkeypatch
    ):
        """A login that finishes within the budget resumes the same call (ready)."""
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_setup_ready", lambda: True
        )

        # _auth_ready() flips True only after the fake login flow materializes
        # the profile files on disk.
        async def fake_login_flow() -> None:
            _make_auth_ready(isolate_profile_dir)

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_login_flow", fake_login_flow
        )
        _patch_inline_wait(monkeypatch, 0.5)

        initialize_bootstrap("managed")

        # No raise: ensure_tool_ready_or_raise returns normally so the caller
        # falls through to the scrape path.
        result = await ensure_tool_ready_or_raise("get_person_profile")
        assert result is None

        state = get_bootstrap_state()
        assert state.auth_state is AuthState.READY

    async def test_inline_wait_elapses_returns_pending(
        self, isolate_profile_dir, monkeypatch
    ):
        """Budget elapses with login still pending -> poll-friendly raise.

        Regression guard for the asyncio.wait_for footgun: the login task must
        still be running (not cancelled, not done) after the wait elapses.
        """
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_setup_ready", lambda: True
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: False)

        never_done = asyncio.Event()

        async def fake_login_flow() -> None:
            await never_done.wait()

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_login_flow", fake_login_flow
        )
        _patch_inline_wait(monkeypatch, 0.05)

        initialize_bootstrap("managed")

        try:
            with pytest.raises(AuthenticationInProgressError) as exc_info:
                await ensure_tool_ready_or_raise("get_person_profile")

            message = str(exc_info.value)
            assert "not a failure" in message
            assert "call this exact tool again" in message

            login_task = get_bootstrap_state().login_task
            assert login_task is not None
            assert not login_task.cancelled()
            assert not login_task.done()
        finally:
            never_done.set()
            login_task = get_bootstrap_state().login_task
            if login_task is not None:
                login_task.cancel()

    async def test_inline_wait_zero_returns_immediately(
        self, isolate_profile_dir, monkeypatch
    ):
        """login_inline_wait_seconds == 0 raises without awaiting the task."""
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_setup_ready", lambda: True
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: False)

        never_done = asyncio.Event()
        wait_called = {"value": False}

        async def fake_login_flow() -> None:
            await never_done.wait()

        real_wait = asyncio.wait

        async def tracking_wait(*args, **kwargs):
            wait_called["value"] = True
            return await real_wait(*args, **kwargs)

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_login_flow", fake_login_flow
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.asyncio.wait", tracking_wait)
        _patch_inline_wait(monkeypatch, 0)

        initialize_bootstrap("managed")

        try:
            with pytest.raises(AuthenticationInProgressError):
                await ensure_tool_ready_or_raise("get_person_profile")

            assert wait_called["value"] is False
            login_task = get_bootstrap_state().login_task
            assert login_task is not None
            assert not login_task.done()
        finally:
            never_done.set()
            login_task = get_bootstrap_state().login_task
            if login_task is not None:
                login_task.cancel()

    async def test_inline_wait_prior_failure_surfaced(
        self, isolate_profile_dir, monkeypatch
    ):
        """A prior failed attempt is mentioned when a fresh login is spawned."""
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_setup_ready", lambda: True
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: False)

        never_done = asyncio.Event()

        async def fake_login_flow() -> None:
            await never_done.wait()

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_login_flow", fake_login_flow
        )
        _patch_inline_wait(monkeypatch, 0.05)

        initialize_bootstrap("managed")
        state = get_bootstrap_state()
        # Prior attempt finished failed: FAILED + last_error, no running task.
        state.auth_state = AuthState.FAILED
        state.last_error = (
            "Manual login timeout: login was not completed within 30 minutes."
        )
        state.login_task = None

        try:
            with pytest.raises(AuthenticationInProgressError) as exc_info:
                await _start_login_if_needed()

            message = str(exc_info.value)
            assert "previous login attempt did not finish" in message
            assert "Manual login timeout" in message
        finally:
            never_done.set()
            login_task = get_bootstrap_state().login_task
            if login_task is not None:
                login_task.cancel()

    async def test_inline_wait_single_task_under_concurrency(
        self, isolate_profile_dir, monkeypatch
    ):
        """Concurrent callers share ONE login task; the flow spawns once."""
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_setup_ready", lambda: True
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: False)

        never_done = asyncio.Event()
        spawn_count = {"value": 0}

        async def fake_login_flow() -> None:
            spawn_count["value"] += 1
            await never_done.wait()

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_login_flow", fake_login_flow
        )
        _patch_inline_wait(monkeypatch, 0.05)

        initialize_bootstrap("managed")

        try:
            results = await asyncio.gather(
                ensure_tool_ready_or_raise("get_person_profile"),
                ensure_tool_ready_or_raise("get_person_profile"),
                return_exceptions=True,
            )
            assert all(isinstance(r, AuthenticationInProgressError) for r in results)
            assert spawn_count["value"] == 1
        finally:
            never_done.set()
            login_task = get_bootstrap_state().login_task
            if login_task is not None:
                login_task.cancel()

    async def test_inline_wait_bypassed_in_docker(
        self, isolate_profile_dir, monkeypatch
    ):
        """Docker raises host-login required without ever entering the wait."""
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: False)

        async def fail_if_called(*args, **kwargs):
            raise AssertionError("asyncio.wait must not run under Docker")

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.asyncio.wait", fail_if_called
        )
        # A large budget would matter only if the wait were reachable.
        _patch_inline_wait(monkeypatch, 30)

        initialize_bootstrap("docker")

        with pytest.raises(DockerHostLoginRequiredError):
            await ensure_tool_ready_or_raise("search_jobs")


_IMPORT_TARGET = (
    "linkedin_mcp_server.browser_import.orchestrate.import_session_from_browser"
)


@pytest.fixture
def _stub_import_env(monkeypatch):
    """Stub the import side-effects and force the gate open for auto-login tests."""
    monkeypatch.setattr(
        "linkedin_mcp_server.bootstrap.browser_setup_ready", lambda: True
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.bootstrap.close_browser", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("linkedin_mcp_server.bootstrap.set_headless", lambda _x: None)
    monkeypatch.setattr("linkedin_mcp_server.bootstrap.current_headless", lambda: True)
    monkeypatch.setattr(
        "linkedin_mcp_server.bootstrap._auto_import_allowed", lambda: True
    )
    monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: False)


def _auto_import_config(
    *,
    flag,
    transport="stdio",
    host="127.0.0.1",
    is_interactive=False,
) -> AppConfig:
    config = AppConfig()
    config.browser.auto_import_from_browser = flag
    config.server.transport = transport
    config.server.host = host
    config.is_interactive = is_interactive
    return config


class TestAutoLogin:
    async def test_import_success_skips_manual_login(
        self, isolate_profile_dir, monkeypatch, _stub_import_env
    ):
        """A successful import seeds a session; no manual login is ever spawned."""
        spawn_count = {"value": 0}

        async def fake_run_login_flow() -> None:
            spawn_count["value"] += 1

        async def fake_import(_browser, *, user_data_dir, **_kwargs):
            _make_auth_ready(isolate_profile_dir)
            return True

        # _auth_ready flips True once the import materializes the files on disk.
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._auth_ready",
            lambda: portable_cookie_path(isolate_profile_dir).exists(),
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_login_flow", fake_run_login_flow
        )
        import_mock = AsyncMock(side_effect=fake_import)
        monkeypatch.setattr(_IMPORT_TARGET, import_mock)
        _patch_inline_wait(monkeypatch, 0.5, auto_import=True)

        initialize_bootstrap("managed")

        result = await ensure_tool_ready_or_raise("get_person_profile")
        assert result is None

        state = get_bootstrap_state()
        assert state.auth_state is AuthState.READY
        assert import_mock.await_count == 1
        assert spawn_count["value"] == 0
        assert state.login_task is None

    @pytest.mark.parametrize(
        "import_outcome",
        [
            AsyncMock(side_effect=NoLinkedInSessionFoundError("none")),
            AsyncMock(side_effect=CookieDecryptionError("app-bound")),
            AsyncMock(side_effect=NetworkError("launch wedged")),
            AsyncMock(side_effect=LinkedInMCPError("import error")),
            AsyncMock(return_value=False),
        ],
    )
    async def test_no_live_session_falls_back_to_inline_wait(
        self, isolate_profile_dir, monkeypatch, _stub_import_env, import_outcome
    ):
        """Each 'nothing to import' outcome falls through to the manual login."""
        never_done = asyncio.Event()

        async def fake_login_flow() -> None:
            await never_done.wait()

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_login_flow", fake_login_flow
        )
        monkeypatch.setattr(_IMPORT_TARGET, import_outcome)
        _patch_inline_wait(monkeypatch, 0.05, auto_import=True)

        initialize_bootstrap("managed")

        try:
            with pytest.raises(AuthenticationInProgressError) as exc_info:
                await ensure_tool_ready_or_raise("get_person_profile")

            message = str(exc_info.value)
            assert "not a failure" in message
            assert "call this exact tool again" in message

            login_task = get_bootstrap_state().login_task
            assert login_task is not None
            assert not login_task.cancelled()
            assert not login_task.done()
        finally:
            never_done.set()
            login_task = get_bootstrap_state().login_task
            if login_task is not None:
                login_task.cancel()

    async def test_import_runs_once_under_concurrency(
        self, isolate_profile_dir, monkeypatch, _stub_import_env
    ):
        """Concurrent pollers share ONE import; only one headed login follows."""
        release_import = asyncio.Event()
        never_done = asyncio.Event()
        spawn_count = {"value": 0}

        async def fake_import(_browser, *, user_data_dir, **_kwargs):
            await release_import.wait()
            return False

        async def fake_login_flow() -> None:
            spawn_count["value"] += 1
            await never_done.wait()

        import_mock = AsyncMock(side_effect=fake_import)
        monkeypatch.setattr(_IMPORT_TARGET, import_mock)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_login_flow", fake_login_flow
        )
        _patch_inline_wait(monkeypatch, 0.05, auto_import=True)

        initialize_bootstrap("managed")

        async def call_then_release():
            results = await asyncio.gather(
                ensure_tool_ready_or_raise("get_person_profile"),
                ensure_tool_ready_or_raise("get_person_profile"),
                return_exceptions=True,
            )
            return results

        try:
            gather_task = asyncio.create_task(call_then_release())
            # Let both pollers enter and one claim the import before releasing it.
            await asyncio.sleep(0.05)
            release_import.set()
            results = await gather_task
            assert all(isinstance(r, AuthenticationInProgressError) for r in results)
            assert import_mock.await_count == 1
            assert spawn_count["value"] == 1
        finally:
            release_import.set()
            never_done.set()
            login_task = get_bootstrap_state().login_task
            if login_task is not None:
                login_task.cancel()

    async def test_docker_never_imports(self, isolate_profile_dir, monkeypatch):
        """Docker raises host-login required without ever attempting an import."""
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: False)
        import_mock = AsyncMock(return_value=False)
        monkeypatch.setattr(_IMPORT_TARGET, import_mock)
        _patch_inline_wait(monkeypatch, 30, auto_import=True)

        initialize_bootstrap("docker")

        with pytest.raises(DockerHostLoginRequiredError):
            await ensure_tool_ready_or_raise("get_person_profile")
        assert import_mock.await_count == 0

    async def test_config_disabled_skips_import(self, isolate_profile_dir, monkeypatch):
        """auto_import False -> the real predicate gates it off, manual login only."""
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_setup_ready", lambda: True
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: False)

        never_done = asyncio.Event()

        async def fake_login_flow() -> None:
            await never_done.wait()

        import_mock = AsyncMock(return_value=False)
        monkeypatch.setattr(_IMPORT_TARGET, import_mock)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_login_flow", fake_login_flow
        )
        # Do NOT patch _auto_import_allowed: let the real predicate see the flag.
        _patch_inline_wait(monkeypatch, 0.05, auto_import=False)

        initialize_bootstrap("managed")

        try:
            with pytest.raises(AuthenticationInProgressError):
                await ensure_tool_ready_or_raise("get_person_profile")
            assert import_mock.await_count == 0
            assert get_bootstrap_state().login_task is not None
        finally:
            never_done.set()
            login_task = get_bootstrap_state().login_task
            if login_task is not None:
                login_task.cancel()

    def test_predicate_flag_false(self, monkeypatch):
        config = _auto_import_config(flag=False)
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        assert _auto_import_allowed() is False

    def test_predicate_docker(self, monkeypatch):
        config = _auto_import_config(flag=True, is_interactive=True)
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.get_runtime_policy",
            lambda: RuntimePolicy.DOCKER,
        )
        assert _auto_import_allowed() is False

    def test_predicate_remote_bind_skipped(self, monkeypatch):
        # Default (flag=None) on a non-loopback streamable-http bind -> OFF.
        # is_interactive=True is now irrelevant to the gate; the only thing
        # keeping the predicate False is the remote-bind gate. If that gate were
        # deleted the predicate would return True on any host, catching the
        # regression even on a non-GUI CI host. Paired with
        # test_predicate_explicit_true_remote_bind_still_skipped this pins that
        # the remote-bind gate fires for BOTH the auto (None) and force-on (True)
        # resolutions.
        config = _auto_import_config(
            flag=None,
            transport="streamable-http",
            host="0.0.0.0",
            is_interactive=True,
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.get_runtime_policy",
            lambda: RuntimePolicy.MANAGED,
        )
        assert _auto_import_allowed() is False

    def test_predicate_explicit_true_remote_bind_still_skipped(self, monkeypatch):
        # Force-on must NOT override the network-bind gate: flag True +
        # non-loopback streamable-http host -> still OFF. The Docker and
        # remote-bind gates sit BEFORE the final return True, so an explicit
        # opt-in cannot reach a network-exposed cookie read.
        config = _auto_import_config(
            flag=True,
            transport="streamable-http",
            host="0.0.0.0",
            is_interactive=True,
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.get_runtime_policy",
            lambda: RuntimePolicy.MANAGED,
        )
        assert _auto_import_allowed() is False

    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
    def test_predicate_loopback_streamable_http_allowed(self, monkeypatch, host):
        config = _auto_import_config(
            flag=True,
            transport="streamable-http",
            host=host,
            is_interactive=True,
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.get_runtime_policy",
            lambda: RuntimePolicy.MANAGED,
        )
        assert _auto_import_allowed() is True

    def test_predicate_auto_interactive_allowed(self, monkeypatch):
        config = _auto_import_config(flag=None, is_interactive=True)
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.get_runtime_policy",
            lambda: RuntimePolicy.MANAGED,
        )
        assert _auto_import_allowed() is True

    def test_predicate_auto_non_tty_default_on(self, monkeypatch):
        # Default-on headline case: flag None (auto), non-interactive stdio
        # Desktop child -> ON. No TTY or GUI signal gates it any more.
        config = _auto_import_config(flag=None, is_interactive=False)
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.get_runtime_policy",
            lambda: RuntimePolicy.MANAGED,
        )
        assert _auto_import_allowed() is True

    def test_predicate_explicit_opt_in_non_tty(self, monkeypatch):
        # flag True, non-interactive -> ON regardless of platform/GUI. Together
        # with test_predicate_auto_non_tty_default_on and
        # test_predicate_auto_interactive_allowed this pins that neither `flag`
        # (None vs True) nor `is_interactive` changes the answer any more.
        config = _auto_import_config(flag=True, is_interactive=False)
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.get_runtime_policy",
            lambda: RuntimePolicy.MANAGED,
        )
        assert _auto_import_allowed() is True

    async def test_relogin_resets_import_latch(self, isolate_profile_dir, monkeypatch):
        """A relogin force-move resets the one-shot import latch for the next episode."""
        _make_auth_ready(isolate_profile_dir)

        never_done = asyncio.Event()

        async def fake_login_flow() -> None:
            await never_done.wait()

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_login_flow", fake_login_flow
        )

        state = get_bootstrap_state()
        state.import_attempted = True
        state.import_task = None

        try:
            with pytest.raises(AuthenticationStartedError):
                await invalidate_auth_and_trigger_relogin()
            assert get_bootstrap_state().import_attempted is False
        finally:
            never_done.set()
            login_task = get_bootstrap_state().login_task
            if login_task is not None:
                login_task.cancel()

    async def test_closes_browser_before_import_and_restores_headless(
        self, isolate_profile_dir, monkeypatch
    ):
        """close_browser() runs before the import; the prior headless mode is restored."""
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_setup_ready", lambda: True
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._auto_import_allowed", lambda: True
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: False)

        order: list[str] = []
        headless_calls: list[bool] = []

        async def spy_close_browser() -> None:
            order.append("close")

        async def fake_import(_browser, *, user_data_dir, **_kwargs):
            order.append("import")
            _make_auth_ready(isolate_profile_dir)
            return True

        # current_headless() reports the operator's --no-headless scrape mode; the
        # restore in finally must put exactly that value back.
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.close_browser", spy_close_browser
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.current_headless", lambda: False
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.set_headless", headless_calls.append
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._auth_ready",
            lambda: portable_cookie_path(isolate_profile_dir).exists(),
        )
        monkeypatch.setattr(_IMPORT_TARGET, AsyncMock(side_effect=fake_import))
        _patch_inline_wait(monkeypatch, 0.5, auto_import=True)

        initialize_bootstrap("managed")

        await ensure_tool_ready_or_raise("get_person_profile")

        assert order == ["close", "import"]
        # Forced headless True for the probe, then restored the original False.
        assert headless_calls == [True, False]

    async def test_announce_fires_once_and_import_survives_ctx_failure(
        self, isolate_profile_dir, monkeypatch, _stub_import_env
    ):
        """ctx.info notice fires at most once per process; a ctx.info failure never blocks the import."""

        async def fake_import(_browser, *, user_data_dir, **_kwargs):
            return False  # nothing to import; falls through to manual login

        async def fake_login_flow() -> None:
            await asyncio.Event().wait()

        import_mock = AsyncMock(side_effect=fake_import)
        monkeypatch.setattr(_IMPORT_TARGET, import_mock)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_login_flow", fake_login_flow
        )
        _patch_inline_wait(monkeypatch, 0.01, auto_import=True)

        initialize_bootstrap("managed")

        ctx = MagicMock()
        ctx.info = AsyncMock(side_effect=RuntimeError("transport gone"))
        ctx.report_progress = AsyncMock()

        # First episode: ctx.info is invoked (and raises) but the import still runs.
        with pytest.raises(AuthenticationInProgressError):
            await ensure_tool_ready_or_raise("get_person_profile", ctx)
        assert import_mock.await_count == 1
        assert ctx.info.await_count == 1

        # Clear the in-flight login + import state to simulate a fresh no-session
        # episode so the second call genuinely re-enters the import branch.
        state = get_bootstrap_state()
        if state.login_task is not None:
            state.login_task.cancel()
            state.login_task = None
        state.import_attempted = False
        state.import_task = None

        # A second import attempt in the SAME process must NOT re-announce.
        with pytest.raises(AuthenticationInProgressError):
            await ensure_tool_ready_or_raise("get_person_profile", ctx)
        assert import_mock.await_count == 2
        assert ctx.info.await_count == 1

        login_task = get_bootstrap_state().login_task
        if login_task is not None:
            login_task.cancel()

    async def test_no_session_logs_fallback_reason(
        self, isolate_profile_dir, monkeypatch, _stub_import_env, caplog
    ):
        """A decrypted-but-rejected session logs why it fell back, not silently."""
        never_done = asyncio.Event()

        async def fake_login_flow() -> None:
            await never_done.wait()

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_login_flow", fake_login_flow
        )
        monkeypatch.setattr(_IMPORT_TARGET, AsyncMock(return_value=False))
        _patch_inline_wait(monkeypatch, 0.05, auto_import=True)

        initialize_bootstrap("managed")

        try:
            with caplog.at_level(logging.INFO, logger="linkedin_mcp_server.bootstrap"):
                with pytest.raises(AuthenticationInProgressError):
                    await ensure_tool_ready_or_raise("get_person_profile")
            assert "found no usable browser session" in caplog.text
        finally:
            never_done.set()
            login_task = get_bootstrap_state().login_task
            if login_task is not None:
                login_task.cancel()

    async def test_import_timeout_degrades_to_manual_login(
        self, isolate_profile_dir, monkeypatch, _stub_import_env, caplog
    ):
        """A wedged import times out, logs it, and falls back instead of hanging."""
        never_done = asyncio.Event()

        async def fake_login_flow() -> None:
            await never_done.wait()

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_login_flow", fake_login_flow
        )
        # Exercise the except TimeoutError branch deterministically: no real wait.
        monkeypatch.setattr(_IMPORT_TARGET, AsyncMock(side_effect=TimeoutError()))
        _patch_inline_wait(monkeypatch, 0.05, auto_import=True)

        initialize_bootstrap("managed")

        try:
            with caplog.at_level(logging.INFO, logger="linkedin_mcp_server.bootstrap"):
                with pytest.raises(AuthenticationInProgressError):
                    await ensure_tool_ready_or_raise("get_person_profile")
            assert "Auto-import timed out" in caplog.text
            assert get_bootstrap_state().login_task is not None
        finally:
            never_done.set()
            login_task = get_bootstrap_state().login_task
            if login_task is not None:
                login_task.cancel()


def test_move_auth_state_aside_reports_a_held_profile(monkeypatch):
    """Swallowing this would open a login browser whose own rotation fails at
    the same point, after telling the user the browser had opened."""
    from linkedin_mcp_server import bootstrap

    monkeypatch.setattr(
        bootstrap,
        "rotate_source_profile",
        MagicMock(side_effect=RuntimeError("in use by another process")),
    )

    with pytest.raises(
        AuthenticationBootstrapFailedError, match="No login was started"
    ):
        bootstrap._move_auth_state_aside(force=True)


class TestAutoImportSkippedForProxy:
    """A configured proxy turns auto-import off by default.

    The imported session was created in a local browser on the real address.
    Driving it through the proxy afterwards is exactly the IP change that
    triggers a LinkedIn checkpoint, so the automatic path defers to --login.
    """

    def test_predicate_skips_when_a_proxy_is_configured(self, monkeypatch):
        config = _auto_import_config(flag=True, is_interactive=True)
        config.browser.proxy_server = "http://gate.example:7000"
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        assert _auto_import_allowed() is False

    def test_predicate_still_allows_it_without_a_proxy(self, monkeypatch):
        config = _auto_import_config(flag=True, is_interactive=True)
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        assert _auto_import_allowed() is True


class TestProxyErrorSurvivesTheImportTask:
    """The auto-import task must not swallow a proxy fault.

    _try_auto_import_session re-raises ProxyConnectionError instead of
    reporting "no session found". The outer await has to honour that: catching
    it with the broad handler would send the user into a manual login that has
    to fail through the same proxy, hiding the real cause.
    """

    async def test_proxy_error_propagates_out_of_the_awaited_task(
        self, isolate_profile_dir, monkeypatch, _stub_import_env
    ):
        from linkedin_mcp_server.core.exceptions import ProxyConnectionError

        config = _auto_import_config(flag=True, is_interactive=True)
        config.browser.proxy_server = "http://gate.example:7000"
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)

        async def failing_import(_ctx=None):
            raise ProxyConnectionError("proxy gate.example:7000 is unreachable")

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._try_auto_import_session", failing_import
        )

        with pytest.raises(ProxyConnectionError, match="gate.example"):
            await ensure_tool_ready_or_raise("get_person_profile")

        # No login task started: the proxy has to be fixed first.
        assert get_bootstrap_state().login_task is None


class TestAnOwnerNeverSignsInItself:
    """A detached owner reports bad auth instead of acting on it.

    It has no terminal and no desktop session, so a login window has nowhere to
    appear. It must also leave the profile alone: the frontend that will log in
    needs to find the session state as the owner saw it, and rotating from here
    would race that login for the same files.
    """

    def _as_owner(self, monkeypatch):
        from linkedin_mcp_server.server_role import ServerRole, set_process_role

        set_process_role(ServerRole.OWNER)

    async def test_a_missing_session_is_reported_not_repaired(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server.exceptions import AuthMissingOnOwnerError

        self._as_owner(monkeypatch)
        # Both of the things the owner must not do, watched rather than assumed.
        # Patched on `bootstrap`, not on `setup`: bootstrap imported the name
        # directly (`bootstrap.py:49`), so patching the source module would watch
        # a reference nothing calls and pass however the gate behaved.
        started_login = MagicMock()
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.interactive_login", started_login
        )
        imported = MagicMock()
        monkeypatch.setattr(
            "linkedin_mcp_server.browser_import.orchestrate.import_session_from_browser",
            imported,
        )

        with pytest.raises(AuthMissingOnOwnerError, match="cannot sign in by itself"):
            await _start_login_if_needed()

        started_login.assert_not_called()
        imported.assert_not_called()
        # No login task either: one would open a window on the next poll.
        assert get_bootstrap_state().login_task is None

    async def test_a_stale_session_is_reported_without_rotating_the_profile(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server.exceptions import AuthStaleOnOwnerError

        self._as_owner(monkeypatch)
        rotated = MagicMock()
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.rotate_source_profile", rotated
        )
        started_login = MagicMock()
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.interactive_login", started_login
        )

        with pytest.raises(AuthStaleOnOwnerError, match="cannot sign in by itself"):
            await invalidate_auth_and_trigger_relogin()

        # The rotation is the destructive half: it retires the session the
        # frontend is about to replace, and it cannot be undone from there.
        rotated.assert_not_called()
        started_login.assert_not_called()

    async def test_auto_import_is_refused(self, isolate_profile_dir, monkeypatch):
        # As a predicate test beside the Docker and proxy ones, because gating
        # only the call site would leave the next caller free to reopen the hole.
        config = _auto_import_config(flag=True, is_interactive=True)
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)

        assert _auto_import_allowed() is True  # the same config, as a DIRECT server
        self._as_owner(monkeypatch)
        assert _auto_import_allowed() is False

    async def test_a_direct_server_still_signs_in(
        self, isolate_profile_dir, monkeypatch
    ):
        # The gates key off the role, so the historical single-process behaviour
        # has to be untouched. Without this, a gate written against the wrong
        # predicate would look correct in every owner test and break every user.

        config = _auto_import_config(flag=False, is_interactive=True)
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._move_invalid_auth_state_aside",
            MagicMock(),
        )
        login_flow = AsyncMock()
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._run_login_flow", login_flow)

        # Reaches the login path rather than the owner refusal, and actually
        # starts one: asserting only that an AuthenticationInProgressError came
        # back proves a task-shaped path, not a login. Verified by mutation, that
        # weaker form passed with _run_login_flow replaced by a bare sleep.
        with pytest.raises(AuthenticationInProgressError):
            await _start_login_if_needed()

        login_flow.assert_called_once()


class TestTheOwnerStaysQuiescentUntilANewSessionLands:
    """Closing the browser is not enough to stop the next call reopening it.

    ``get_or_create_browser`` opens one whenever the singleton is None, and
    readiness tests whether the auth files exist rather than whether they work.
    Measured before the latch existed: the call after a confirmed close created a
    second browser, which is one opened into the middle of the frontend's login.
    """

    def _quiescent_on(self, generation):
        import linkedin_mcp_server.bootstrap as bootstrap

        bootstrap.go_auth_quiescent(generation)

    def _still_latched(self) -> bool:
        import linkedin_mcp_server.bootstrap as bootstrap
        from linkedin_mcp_server.exceptions import AuthStaleOnOwnerError

        try:
            bootstrap._raise_if_auth_quiescent()
        except AuthStaleOnOwnerError:
            return True
        return False

    def _write_session(self, profile_dir):
        from linkedin_mcp_server.session_state import (
            portable_cookie_path,
            write_source_state,
        )

        (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
        (profile_dir / "Default" / "Cookies").write_text("placeholder")
        portable_cookie_path(profile_dir).write_text("[]")
        write_source_state(profile_dir)

    async def test_the_broken_session_alone_never_lifts_it(
        self, isolate_profile_dir, monkeypatch
    ):
        import linkedin_mcp_server.bootstrap as bootstrap

        self._write_session(isolate_profile_dir)
        self._quiescent_on(bootstrap.current_login_generation())

        # `_auth_ready()` is already true of these very files, so a latch that
        # only asked whether a session exists would lift immediately.
        assert bootstrap._auth_ready() is True
        assert self._still_latched() is True

    async def test_an_abandoned_login_leaves_it_latched(
        self, isolate_profile_dir, monkeypatch
    ):
        import linkedin_mcp_server.bootstrap as bootstrap
        from linkedin_mcp_server.session_state import rotate_source_profile

        self._write_session(isolate_profile_dir)
        self._quiescent_on(bootstrap.current_login_generation())

        # What the frontend does first, and all it does if the user closes the
        # window: the session is retired and nothing replaces it.
        rotate_source_profile(isolate_profile_dir)

        # The generation now reads as None, which *differs* from the observed
        # one, so a latch keyed on inequality alone would lift here and open
        # Chromium on a profile with no session at all.
        assert bootstrap.current_login_generation() is None
        assert self._still_latched() is True

    async def test_a_real_new_session_lifts_it(self, isolate_profile_dir, monkeypatch):
        import linkedin_mcp_server.bootstrap as bootstrap
        from linkedin_mcp_server.session_state import rotate_source_profile

        self._write_session(isolate_profile_dir)
        self._quiescent_on(bootstrap.current_login_generation())

        rotate_source_profile(isolate_profile_dir)
        self._write_session(isolate_profile_dir)  # the login succeeds

        assert self._still_latched() is False

        # Then take the session away again. This is what proves the latch was
        # *cleared* rather than merely reporting itself lifted because the
        # predicate happened to be true: an implementation that answers from the
        # predicate every time would arm again here, while a cleared latch stays
        # silent whatever the files say. Verified by mutation, an earlier version
        # of this test passed against exactly that.
        rotate_source_profile(isolate_profile_dir)

        assert self._still_latched() is False

    async def test_the_gate_refuses_before_it_can_reach_a_browser(
        self, isolate_profile_dir, monkeypatch
    ):
        import linkedin_mcp_server.bootstrap as bootstrap
        from linkedin_mcp_server.exceptions import AuthStaleOnOwnerError

        self._write_session(isolate_profile_dir)
        self._quiescent_on(bootstrap.current_login_generation())

        # Through the real pre-tool gate, which is what a forwarded call hits.
        with pytest.raises(AuthStaleOnOwnerError):
            await ensure_tool_ready_or_raise("get_person_profile")

    async def test_every_later_call_names_the_same_broken_session(
        self, isolate_profile_dir, monkeypatch
    ):
        """The latch must put its generation on the failures it raises.

        Only the first call reaches this through `handle_auth_error`, which knows
        what it observed. Every call after that is answered by the latch, and a
        failure raised there without the generation gives the *second* client a
        marker with nothing to compare against. That client then repairs
        unguarded, and rotates away the session the first one is signing in for.
        """
        import linkedin_mcp_server.bootstrap as bootstrap
        from linkedin_mcp_server.exceptions import AuthStaleOnOwnerError

        self._write_session(isolate_profile_dir)
        broken = bootstrap.current_login_generation()
        self._quiescent_on(broken)

        with pytest.raises(AuthStaleOnOwnerError) as caught:
            bootstrap._raise_if_auth_quiescent()

        assert caught.value.generation == broken


class TestTwoClientsMeetingOneDeadSession:
    """Only one of them may retire it, and `_lock` cannot arrange that.

    Two frontends can be told to sign in for the same dead session. The lock
    around this function is process-local, so it serializes each process against
    itself and knows nothing about the other one.
    """

    def _write_session(self, profile_dir):
        from linkedin_mcp_server.session_state import (
            portable_cookie_path,
            write_source_state,
        )

        (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
        (profile_dir / "Default" / "Cookies").write_text("placeholder")
        portable_cookie_path(profile_dir).write_text("[]")
        write_source_state(profile_dir)

    async def test_without_the_generation_the_second_client_destroys_the_first(
        self, isolate_profile_dir, monkeypatch
    ):
        """The damage, reproduced first, so the fix below is not asserting thin air.

        This is the shipped behaviour of the unguarded path: rotation is safe
        against a *concurrent* login, because it takes the profile lease, but
        nothing stops a client that arrives after one has finished.
        """
        import linkedin_mcp_server.bootstrap as bootstrap
        from linkedin_mcp_server.session_state import load_source_state

        self._write_session(isolate_profile_dir)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_login_flow", AsyncMock()
        )

        # A finishes a login: a fresh session, and a generation to go with it.
        self._write_session(isolate_profile_dir)
        fresh = bootstrap.current_login_generation()

        # B arrives holding no generation at all, which is the old call shape.
        with pytest.raises(AuthenticationStartedError):
            await invalidate_auth_and_trigger_relogin()

        # A's session is gone, rotated into quarantine by a client that was
        # complaining about a session already replaced.
        assert load_source_state(isolate_profile_dir) is None
        assert fresh is not None

    async def test_the_generation_stops_the_second_client(
        self, isolate_profile_dir, monkeypatch
    ):
        import linkedin_mcp_server.bootstrap as bootstrap
        from linkedin_mcp_server.session_state import load_source_state

        self._write_session(isolate_profile_dir)
        stale = bootstrap.current_login_generation()

        login = AsyncMock()
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._run_login_flow", login)

        # A finishes its login, writing a different generation.
        self._write_session(isolate_profile_dir)
        fresh = bootstrap.current_login_generation()
        assert fresh != stale

        # B is told what actually happened, and no login is scheduled. An earlier
        # version of this test accepted AuthenticationStartedError here, which
        # blessed a false report: the caller was promised a login window, none
        # opened, because the task stood down the moment it held the profile.
        with pytest.raises(PeerSessionInPlaceError, match="already signed in"):
            await invalidate_auth_and_trigger_relogin(stale_generation=stale)

        assert get_bootstrap_state().login_task is None
        surviving = load_source_state(isolate_profile_dir)
        assert surviving is not None
        assert surviving.login_generation == fresh

    async def test_the_first_client_still_retires_the_session_it_found(
        self, isolate_profile_dir, monkeypatch
    ):
        # The guard must not stop the client that is actually right about the
        # session being dead, which is every ordinary case.
        import linkedin_mcp_server.bootstrap as bootstrap
        from linkedin_mcp_server.session_state import load_source_state

        self._write_session(isolate_profile_dir)
        stale = bootstrap.current_login_generation()
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_login_flow", AsyncMock()
        )

        with pytest.raises(AuthenticationStartedError):
            await invalidate_auth_and_trigger_relogin(stale_generation=stale)

        assert load_source_state(isolate_profile_dir) is None

    async def test_an_abandoned_first_attempt_does_not_block_the_second_client(
        self, isolate_profile_dir, monkeypatch
    ):
        """A different generation is not on its own proof that anyone succeeded.

        An abandoned login leaves the profile rotated and nothing written, so the
        generation reads as None: different from the stale one, and yet no session
        exists. A guard that asked only whether the generation had moved would tell
        the next client "another client already signed in" and never start the login
        it is asking for. Measured, that is exactly what the half-guard does.
        """
        import linkedin_mcp_server.bootstrap as bootstrap
        from linkedin_mcp_server.session_state import rotate_source_profile

        self._write_session(isolate_profile_dir)
        stale = bootstrap.current_login_generation()
        login = AsyncMock()
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._run_login_flow", login)

        # A starts a login and the user closes the window: rotated, nothing new.
        rotate_source_profile(isolate_profile_dir)
        assert bootstrap.current_login_generation() is None

        # B has to be allowed through, and has to open a login.
        with pytest.raises(AuthenticationStartedError):
            await invalidate_auth_and_trigger_relogin(stale_generation=stale)

        login.assert_called_once()


class TestTheAutoImportInheritsTheGuard:
    """`start_login_if_needed` tries an import first, and it rotates too.

    The guard has to reach it, not only the login window behind it. A client whose
    keychain read or profile discovery ran long is the realistic late arrival, and
    the import retires the current session the moment it holds the profile.
    """

    async def test_the_generation_reaches_the_import(
        self, isolate_profile_dir, monkeypatch, _stub_import_env
    ):
        from linkedin_mcp_server.config import set_config
        from linkedin_mcp_server.config.schema import AppConfig

        # Installed rather than loaded: the import path calls get_config(), which
        # parses sys.argv, and under pytest that is pytest's own command line.
        config = AppConfig()
        config.browser.auto_import_from_browser = True
        set_config(config)
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)

        seen: dict[str, object] = {}

        async def fake_import(_browser, *, user_data_dir, **kwargs):
            seen.update(kwargs)
            return False

        monkeypatch.setattr(_IMPORT_TARGET, fake_import)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_login_flow", AsyncMock()
        )

        with pytest.raises(AuthenticationInProgressError):
            await _start_login_if_needed(superseded_by="the-broken-one")

        assert seen.get("superseded_by") == "the-broken-one"


class TestTheAutomaticPathCarriesWhatItObserved:
    """A login a tool call started is not somebody insisting at a terminal.

    The readiness gate has just looked at the profile, so it knows which session
    it found wanting. Passing that on lets the login stand down if a peer signs in
    while this one is still getting there. `--login` deliberately keeps the
    unguarded default, which is what makes it an override rather than a request.
    """

    async def test_the_gate_hands_on_the_generation_it_just_read(
        self, isolate_profile_dir, monkeypatch
    ):
        import linkedin_mcp_server.bootstrap as bootstrap
        from linkedin_mcp_server.session_state import (
            portable_cookie_path,
            write_source_state,
        )

        # A session that exists on disk but is about to be found unusable: the
        # gate reads this generation and must pass exactly it on.
        (isolate_profile_dir / "Default").mkdir(parents=True, exist_ok=True)
        (isolate_profile_dir / "Default" / "Cookies").write_text("placeholder")
        portable_cookie_path(isolate_profile_dir).write_text("[]")
        observed = write_source_state(isolate_profile_dir).login_generation

        from linkedin_mcp_server.config import set_config
        from linkedin_mcp_server.config.schema import AppConfig

        # Installed rather than loaded: the gate calls get_config(), which parses
        # sys.argv, and under pytest that is pytest's own command line.
        config = AppConfig()
        config.browser.user_data_dir = str(isolate_profile_dir)
        set_config(config)
        monkeypatch.setattr(bootstrap, "get_config", lambda: config)

        seen: dict[str, object] = {}

        async def capture(ctx=None, **kwargs):
            seen.update(kwargs)

        monkeypatch.setattr(bootstrap, "_start_login_if_needed", capture)
        monkeypatch.setattr(bootstrap, "_auth_ready", lambda: False)
        monkeypatch.setattr(bootstrap, "_browser_setup_ready", lambda: True)

        # Both branches of the gate, because they are separate call sites: a
        # configured Chrome executable skips the managed-binary path entirely and
        # reaches its own. Mutating one and not the other went unnoticed until
        # this covered both.
        for custom_chrome in (False, True):
            seen.clear()
            monkeypatch.setattr(bootstrap, "_uses_custom_chrome", lambda: custom_chrome)

            await bootstrap.ensure_tool_ready_or_raise("get_person_profile")

            assert seen.get("superseded_by") == observed, custom_chrome

    async def test_an_explicit_login_stays_unguarded(self):
        # `--login` reaches interactive_login directly, with no generation, so it
        # always signs in. Asserted on the signature default rather than by
        # driving a browser: the default is the contract.
        import inspect

        from linkedin_mcp_server.setup import UNGUARDED, interactive_login

        default = inspect.signature(interactive_login).parameters["superseded_by"]

        assert default.default is UNGUARDED
