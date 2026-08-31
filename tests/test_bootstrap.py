import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import errno
import io
import json
import logging
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import IO, Any, Callable, cast
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
    OwnerStandingDownError,
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
        async def fake_setup(**_kwargs: object) -> None:
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

    async def test_owner_first_use_starts_deferred_setup(self, monkeypatch):
        from linkedin_mcp_server.server_role import ServerRole, set_process_role

        release = asyncio.Event()

        async def fake_setup(**_kwargs: object) -> None:
            await release.wait()

        _patch_inline_wait(monkeypatch, 0)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.browser_setup_ready", lambda: False
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_browser_setup", fake_setup
        )
        set_process_role(ServerRole.OWNER)
        initialize_bootstrap("managed")

        with pytest.raises(BrowserSetupInProgressError):
            await ensure_tool_ready_or_raise("search_jobs")

        state = get_bootstrap_state()
        assert state.setup_state is SetupState.RUNNING
        assert state.setup_task is not None
        release.set()
        await state.setup_task

    async def test_unclaimed_managed_root_is_refused_before_readiness(
        self, tmp_path, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import ProfileRootRefusedError

        profile = tmp_path / "unclaimed-start" / "profile"
        metadata = profile.parent / "browser-install.json"
        metadata.parent.mkdir(parents=True)
        metadata.write_text("unrelated")
        monkeypatch.setattr(bootstrap, "get_profile_dir", lambda: profile)
        monkeypatch.setattr(
            bootstrap,
            "get_config",
            lambda: SimpleNamespace(browser=SimpleNamespace(chrome_path=None)),
        )
        monkeypatch.setattr(
            bootstrap,
            "_browser_setup_ready",
            lambda: pytest.fail("readiness ran before profile ownership was proved"),
        )

        initialize_bootstrap("managed")
        with pytest.raises(ProfileRootRefusedError):
            await start_background_browser_setup_if_needed()

        assert metadata.read_text() == "unrelated"
        assert get_bootstrap_state().setup_task is None

    async def test_setup_in_progress_raises(self, monkeypatch):
        _patch_inline_wait(monkeypatch, 0)
        initialize_bootstrap("managed")
        state = get_bootstrap_state()
        state.setup_state = SetupState.RUNNING
        state.setup_task = MagicMock(done=lambda: False)
        state.setup_check_complete = asyncio.Event()
        state.setup_check_complete.set()

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
        await start_background_browser_setup_if_needed()
        assert calls == [1]

    async def test_ready_owner_reports_retained_revisions(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.server_role import ServerRole, set_process_role

        _patch_targets_and_version(monkeypatch)
        _set_headless(monkeypatch, True)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217"])
        _write_metadata(install_metadata_path(), bdir)
        calls = self._record(monkeypatch)
        set_process_role(ServerRole.OWNER)

        initialize_bootstrap("managed")
        bootstrap.report_retained_browser_revisions_if_ready()
        report_task = get_bootstrap_state().cache_report_task
        assert report_task is not None
        await report_task

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

    async def test_a_failing_post_setup_report_stays_on_its_own_task(
        self, isolate_profile_dir, monkeypatch, caplog
    ):
        """Nobody awaits this task, so an escape is only reported on drop."""
        from linkedin_mcp_server import bootstrap

        def explode() -> None:
            raise json.JSONDecodeError("inventory", "", 0)

        monkeypatch.setattr(bootstrap, "_report_retained_browser_revisions", explode)

        with caplog.at_level(logging.WARNING, logger="linkedin_mcp_server.bootstrap"):
            bootstrap._schedule_retained_browser_revision_report()
            task = get_bootstrap_state().cache_report_task
            assert task is not None
            await task

        assert task.exception() is None, "the escape was left for the loop to report"
        assert "Could not inspect the retained browser cache" in caplog.text


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

        release = asyncio.Event()

        async def fake_setup(**_kwargs: object) -> None:
            await release.wait()

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_browser_setup", fake_setup
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: True)

        initialize_bootstrap("managed")

        with pytest.raises(BrowserSetupInProgressError):
            await ensure_tool_ready_or_raise("get_person_profile")

        task = get_bootstrap_state().setup_task
        assert task is not None
        release.set()
        await task

    async def test_immediate_setup_failure_uses_the_setup_error_contract(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        async def setup(**_kwargs: object) -> None:
            raise NotADirectoryError("browser cache is a file")

        config = SimpleNamespace(browser=SimpleNamespace(chrome_path=None))
        monkeypatch.setattr(bootstrap, "get_config", lambda: config)
        monkeypatch.setattr(bootstrap, "_browser_setup_ready", lambda: False)
        monkeypatch.setattr(bootstrap, "_run_browser_setup", setup)
        initialize_bootstrap("managed")

        with pytest.raises(BrowserSetupFailedError, match="browser cache is a file"):
            await bootstrap.start_background_browser_setup_if_needed()

        assert get_bootstrap_state().setup_task is None
        assert get_bootstrap_state().setup_state is SetupState.IDLE

    async def test_completed_setup_failure_is_reported_before_retry(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        fail = asyncio.Event()
        retry = asyncio.Event()
        attempts = 0

        async def setup(**_kwargs: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                await fail.wait()
                raise BrowserSetupFailedError("the mirror refused")
            await retry.wait()

        config = SimpleNamespace(browser=SimpleNamespace(chrome_path=None))
        monkeypatch.setattr(bootstrap, "get_config", lambda: config)
        monkeypatch.setattr(bootstrap, "_browser_setup_ready", lambda: False)
        monkeypatch.setattr(bootstrap, "_run_browser_setup", setup)
        initialize_bootstrap("managed")

        with pytest.raises(BrowserSetupInProgressError):
            await ensure_tool_ready_or_raise("get_person_profile")

        task = get_bootstrap_state().setup_task
        assert task is not None
        fail.set()
        with pytest.raises(BrowserSetupFailedError, match="the mirror refused"):
            await task

        with pytest.raises(BrowserSetupFailedError, match="the mirror refused"):
            await ensure_tool_ready_or_raise("get_person_profile")

        assert attempts == 1
        assert get_bootstrap_state().setup_task is None

        with pytest.raises(BrowserSetupInProgressError):
            await ensure_tool_ready_or_raise("get_person_profile")
        assert attempts == 2
        retry.set()
        retry_task = get_bootstrap_state().setup_task
        assert retry_task is not None
        await retry_task

    async def test_a_retry_within_the_owner_grace_gets_the_first_failure(
        self, isolate_profile_dir, monkeypatch
    ):
        """The whole chain the grace exists for, on the production path.

        The test above proves the retry consumes the failure; this one proves
        the owner is still there to be retried. An owner configured with an idle
        timeout shorter than the "minute or two" the failure message asks for
        exits between the two calls, and the retry then reaches a fresh owner
        that starts setup over instead of reporting what went wrong.
        """
        from linkedin_mcp_server import bootstrap, daemon_liveness, daemon_owner
        from linkedin_mcp_server.daemon_owner import _serve_until_stopped
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError
        from linkedin_mcp_server.server_role import ServerRole

        fail = asyncio.Event()
        attempts = 0

        async def setup(**_kwargs: object) -> None:
            nonlocal attempts
            attempts += 1
            await fail.wait()
            raise BrowserSetupFailedError("the mirror refused")

        config = SimpleNamespace(browser=SimpleNamespace(chrome_path=None))
        monkeypatch.setattr(bootstrap, "get_config", lambda: config)
        monkeypatch.setattr(bootstrap, "_browser_setup_ready", lambda: False)
        monkeypatch.setattr(bootstrap, "_run_browser_setup", setup)
        monkeypatch.setattr(bootstrap, "process_role", lambda: ServerRole.OWNER)
        monkeypatch.setattr(daemon_owner, "_SETUP_FAILURE_RETRY_GRACE_SECONDS", 5.0)
        initialize_bootstrap("managed")

        liveness = daemon_liveness.get_liveness()
        liveness.the_endpoint_is_live()

        with pytest.raises(BrowserSetupInProgressError):
            await ensure_tool_ready_or_raise("get_person_profile")

        task = get_bootstrap_state().setup_task
        assert task is not None
        fail.set()
        with pytest.raises(BrowserSetupFailedError, match="the mirror refused"):
            await task
        assert bootstrap.browser_setup_failure_pending()

        server = MagicMock()
        server.should_exit = False

        async def serves() -> None:
            await asyncio.sleep(0.2)

        serving = asyncio.create_task(serves())
        await _serve_until_stopped(server, serving, [], 0.05, lock=None)
        assert server.should_exit is False, "the owner exited before the retry"

        with pytest.raises(BrowserSetupFailedError, match="the mirror refused"):
            await ensure_tool_ready_or_raise("get_person_profile")
        assert attempts == 1, "the retry started a new setup instead of reporting"

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

        async def fail_setup(**_kwargs: object) -> None:
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

    async def test_readiness_io_is_inside_the_setup_deadline(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError
        from linkedin_mcp_server.server_role import ServerRole

        blocked = threading.Event()
        release = threading.Event()
        stood_down: list[str] = []

        def readiness() -> bool:
            blocked.set()
            release.wait()
            return False

        config = SimpleNamespace(browser=SimpleNamespace(chrome_path=None))
        monkeypatch.setattr(bootstrap, "get_config", lambda: config)
        monkeypatch.setattr(bootstrap, "_browser_setup_ready", readiness)
        monkeypatch.setattr(bootstrap, "_BACKGROUND_BROWSER_SETUP_SECONDS", 0.01)
        monkeypatch.setattr(bootstrap, "process_role", lambda: ServerRole.OWNER)
        monkeypatch.setattr(
            bootstrap,
            "ask_this_process_to_stand_down",
            lambda reason: stood_down.append(reason),
        )

        initialize_bootstrap("managed")
        started = asyncio.get_running_loop().time()
        try:
            with pytest.raises(BrowserSetupFailedError, match="background deadline"):
                await start_background_browser_setup_if_needed()
        finally:
            release.set()

        assert blocked.is_set()
        assert asyncio.get_running_loop().time() - started < 0.1
        assert stood_down == ["managed browser setup exceeded its background deadline"]
        assert get_bootstrap_state().setup_state is SetupState.IDLE

    async def test_cancelled_first_caller_does_not_abandon_readiness(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        blocked = threading.Event()
        release = threading.Event()
        installed = asyncio.Event()

        def readiness() -> bool:
            blocked.set()
            release.wait()
            return False

        async def setup(**_kwargs: object) -> None:
            installed.set()

        config = SimpleNamespace(browser=SimpleNamespace(chrome_path=None))
        monkeypatch.setattr(bootstrap, "get_config", lambda: config)
        monkeypatch.setattr(bootstrap, "_browser_setup_ready", readiness)
        monkeypatch.setattr(bootstrap, "_run_browser_setup", setup)

        initialize_bootstrap("managed")
        caller = asyncio.create_task(start_background_browser_setup_if_needed())
        while not blocked.is_set():
            await asyncio.sleep(0)
        shared = get_bootstrap_state().setup_task
        assert shared is not None
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        assert get_bootstrap_state().setup_task is shared
        release.set()
        await shared
        assert installed.is_set()

    async def test_a_cancelled_caller_leaves_no_waiter_behind(
        self, isolate_profile_dir, monkeypatch
    ):
        """Waiting for readiness must not outlive the caller that asked for it.

        ``asyncio.shield`` wraps its argument in a task of its own, and that
        task keeps running after the shield has raised into the caller: it stays
        registered on the Event until the Event is set, once per tool call that
        timed out. Awaiting the Event directly lets ``Event.wait`` drop its own
        waiter in a ``finally``, while the shared setup task, which is what the
        shield was there to protect, is not something this await can cancel.
        """
        from linkedin_mcp_server import bootstrap

        blocked = threading.Event()
        release = threading.Event()
        installed = asyncio.Event()

        def readiness() -> bool:
            blocked.set()
            release.wait()
            return False

        async def setup(**_kwargs: object) -> None:
            installed.set()

        config = SimpleNamespace(browser=SimpleNamespace(chrome_path=None))
        monkeypatch.setattr(bootstrap, "get_config", lambda: config)
        monkeypatch.setattr(bootstrap, "_browser_setup_ready", readiness)
        monkeypatch.setattr(bootstrap, "_run_browser_setup", setup)

        initialize_bootstrap("managed")
        before = asyncio.all_tasks()
        caller = asyncio.create_task(start_background_browser_setup_if_needed())
        while not blocked.is_set():
            await asyncio.sleep(0)
        shared = get_bootstrap_state().setup_task
        checked = get_bootstrap_state().setup_check_complete
        assert shared is not None and checked is not None
        # The caller is parked on the Event by now, which is what makes the
        # absence of a waiter after the cancellation mean something.
        assert len(checked._waiters) == 1
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        assert list(checked._waiters) == [], "the Event still holds a waiter"
        assert asyncio.all_tasks() - before - {shared, caller} == set()

        # And the shared setup the shield was protecting still runs to the end.
        assert get_bootstrap_state().setup_task is shared
        release.set()
        await shared
        assert installed.is_set()

    async def test_readiness_miss_keeps_existing_metadata_until_install_succeeds(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        release = asyncio.Event()
        bdir = browsers_path()
        bdir.mkdir(parents=True)
        _write_metadata(install_metadata_path(), bdir)

        async def setup(**_kwargs: object) -> None:
            await release.wait()

        config = SimpleNamespace(browser=SimpleNamespace(chrome_path=None))
        monkeypatch.setattr(bootstrap, "get_config", lambda: config)
        monkeypatch.setattr(bootstrap, "_browser_setup_ready", lambda: False)
        monkeypatch.setattr(bootstrap, "_run_browser_setup", setup)
        monkeypatch.setattr(
            bootstrap,
            "_discard_browser_install_metadata",
            lambda: pytest.fail("readiness must not delete existing metadata"),
        )

        initialize_bootstrap("managed")
        await start_background_browser_setup_if_needed()

        task = get_bootstrap_state().setup_task
        assert task is not None
        assert install_metadata_path().exists()
        release.set()
        await task

    async def test_installer_activity_extends_the_inactivity_deadline(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        async def progressing_setup(
            *, activity_callback: Callable[[], None], **_kwargs: object
        ) -> None:
            for _ in range(3):
                await asyncio.sleep(0.04)
                activity_callback()

        monkeypatch.setattr(bootstrap, "_browser_setup_ready", lambda: False)
        monkeypatch.setattr(bootstrap, "_run_browser_setup", progressing_setup)
        monkeypatch.setattr(bootstrap, "_BACKGROUND_BROWSER_SETUP_SECONDS", 0.06)

        await bootstrap._run_background_browser_setup()

    async def test_activity_still_extends_inactivity_under_the_ceiling(
        self, isolate_profile_dir, monkeypatch
    ):
        """The absolute ceiling must not cost the inactivity extension."""
        from linkedin_mcp_server import bootstrap

        rounds = 0

        async def progressing_setup(
            *, activity_callback: Callable[[], None], **_kwargs: object
        ) -> None:
            nonlocal rounds
            for _ in range(6):
                await asyncio.sleep(0.04)
                activity_callback()
                rounds += 1

        monkeypatch.setattr(bootstrap, "_browser_setup_ready", lambda: False)
        monkeypatch.setattr(bootstrap, "_run_browser_setup", progressing_setup)
        monkeypatch.setattr(bootstrap, "_BACKGROUND_BROWSER_SETUP_SECONDS", 0.06)
        monkeypatch.setattr(bootstrap, "_BROWSER_SETUP_LIFETIME_SECONDS", 30.0)

        await bootstrap._run_background_browser_setup()

        # Six rounds of 40ms outlive a 60ms inactivity window only because each
        # one rescheduled it. The margin is a third of the window rather than a
        # quarter of it: at 15ms against 20ms an ordinary scheduling delay was
        # enough to expire the deadline this test says cannot expire.
        assert rounds == 6

    async def test_continuous_activity_cannot_extend_the_absolute_lifetime(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        rounds = 0

        async def never_stops_moving(
            *, activity_callback: Callable[[], None], **_kwargs: object
        ) -> None:
            nonlocal rounds
            while True:
                await asyncio.sleep(0.001)
                activity_callback()
                rounds += 1

        monkeypatch.setattr(bootstrap, "_browser_setup_ready", lambda: False)
        monkeypatch.setattr(bootstrap, "_run_browser_setup", never_stops_moving)
        monkeypatch.setattr(bootstrap, "_BACKGROUND_BROWSER_SETUP_SECONDS", 30.0)
        monkeypatch.setattr(bootstrap, "_BROWSER_SETUP_LIFETIME_SECONDS", 0.05)

        with pytest.raises(BrowserSetupFailedError, match="absolute lifetime"):
            await asyncio.wait_for(bootstrap._run_background_browser_setup(), 5)

        # The inactivity window was 30s and never expired: the ceiling did.
        assert rounds > 1

    async def test_owner_idle_hold_ends_after_the_lifetime_failure(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap, daemon_liveness
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError
        from linkedin_mcp_server.server_role import ServerRole

        async def never_stops_moving(
            *, activity_callback: Callable[[], None], **_kwargs: object
        ) -> None:
            while True:
                await asyncio.sleep(0.001)
                activity_callback()

        liveness = MagicMock()
        stood_down: list[str] = []
        config = SimpleNamespace(browser=SimpleNamespace(chrome_path=None))
        monkeypatch.setattr(bootstrap, "get_config", lambda: config)
        monkeypatch.setattr(bootstrap, "_browser_setup_ready", lambda: False)
        monkeypatch.setattr(bootstrap, "browser_setup_ready", lambda: False)
        monkeypatch.setattr(bootstrap, "_run_browser_setup", never_stops_moving)
        monkeypatch.setattr(bootstrap, "process_role", lambda: ServerRole.OWNER)
        monkeypatch.setattr(daemon_liveness, "get_liveness", lambda: liveness)
        monkeypatch.setattr(
            bootstrap,
            "ask_this_process_to_stand_down",
            lambda reason: stood_down.append(reason),
        )
        monkeypatch.setattr(bootstrap, "_BACKGROUND_BROWSER_SETUP_SECONDS", 30.0)
        monkeypatch.setattr(bootstrap, "_BROWSER_SETUP_LIFETIME_SECONDS", 0.05)

        initialize_bootstrap("managed")
        await start_background_browser_setup_if_needed()
        task = get_bootstrap_state().setup_task
        assert task is not None
        with pytest.raises(BrowserSetupFailedError, match="absolute lifetime"):
            await asyncio.wait_for(task, 5)

        assert not bootstrap.browser_setup_in_progress()
        assert get_bootstrap_state().setup_requires_stand_down, (
            "the owner holds itself back rather than retrying into the same wall"
        )
        liveness.background_activity_finished.assert_called_once()

        await bootstrap._refresh_background_task_state()
        detail = bootstrap._consume_background_setup_failure()

        assert detail is not None and "absolute lifetime" in detail
        assert stood_down == ["managed browser setup exceeded its absolute lifetime"], (
            "and the owner log names the bound that ran out, not the other one"
        )

    async def test_owner_setup_completion_resets_the_idle_clock(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap, daemon_liveness
        from linkedin_mcp_server.server_role import ServerRole

        async def setup(**_kwargs: object) -> None:
            return None

        liveness = MagicMock()
        monkeypatch.setattr(bootstrap, "_browser_setup_ready", lambda: False)
        monkeypatch.setattr(bootstrap, "_run_browser_setup", setup)
        monkeypatch.setattr(bootstrap, "process_role", lambda: ServerRole.OWNER)
        monkeypatch.setattr(daemon_liveness, "get_liveness", lambda: liveness)

        await bootstrap._run_background_browser_setup()

        liveness.background_activity_finished.assert_called_once()

    async def test_ready_owner_setup_resets_the_idle_clock(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap, daemon_liveness
        from linkedin_mcp_server.server_role import ServerRole

        liveness = MagicMock()
        monkeypatch.setattr(bootstrap, "_browser_setup_ready", lambda: True)
        monkeypatch.setattr(bootstrap, "process_role", lambda: ServerRole.OWNER)
        monkeypatch.setattr(daemon_liveness, "get_liveness", lambda: liveness)

        await bootstrap._run_background_browser_setup()

        liveness.background_activity_finished.assert_called_once()

    async def test_failed_owner_setup_resets_the_idle_clock(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap, daemon_liveness
        from linkedin_mcp_server.server_role import ServerRole

        async def setup(**_kwargs: object) -> None:
            raise RuntimeError("install failed")

        liveness = MagicMock()
        monkeypatch.setattr(bootstrap, "_browser_setup_ready", lambda: False)
        monkeypatch.setattr(bootstrap, "_run_browser_setup", setup)
        monkeypatch.setattr(bootstrap, "process_role", lambda: ServerRole.OWNER)
        monkeypatch.setattr(daemon_liveness, "get_liveness", lambda: liveness)

        with pytest.raises(RuntimeError, match="install failed"):
            await bootstrap._run_background_browser_setup()

        liveness.background_activity_finished.assert_called_once()

    async def test_background_setup_has_a_whole_operation_deadline(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError
        from linkedin_mcp_server.server_role import ServerRole

        stood_down: list[str] = []

        async def never_finishes(**_kwargs: object) -> None:
            await asyncio.Event().wait()

        config = SimpleNamespace(browser=SimpleNamespace(chrome_path=None))
        monkeypatch.setattr(bootstrap, "get_config", lambda: config)
        monkeypatch.setattr(bootstrap, "_run_browser_setup", never_finishes)
        monkeypatch.setattr(bootstrap, "_BACKGROUND_BROWSER_SETUP_SECONDS", 0.01)
        monkeypatch.setattr(bootstrap, "browser_setup_ready", lambda: False)
        monkeypatch.setattr(bootstrap, "process_role", lambda: ServerRole.OWNER)
        monkeypatch.setattr(
            bootstrap,
            "ask_this_process_to_stand_down",
            lambda reason: stood_down.append(reason),
        )
        monkeypatch.setattr(
            bootstrap,
            "stand_down_reason",
            lambda: stood_down[-1] if stood_down else None,
        )

        initialize_bootstrap("managed")
        await start_background_browser_setup_if_needed()

        task = get_bootstrap_state().setup_task
        assert task is not None
        with pytest.raises(BrowserSetupFailedError, match="background deadline"):
            await task
        assert not bootstrap.browser_setup_in_progress()
        assert stood_down == []
        with pytest.raises(BrowserSetupFailedError, match="background deadline"):
            await ensure_tool_ready_or_raise("get_person_profile")
        assert stood_down == ["managed browser setup exceeded its background deadline"]

        with pytest.raises(OwnerStandingDownError, match="owner is restarting"):
            await ensure_tool_ready_or_raise("get_person_profile")
        assert get_bootstrap_state().setup_task is None

    async def test_inner_timeout_keeps_its_specific_failure(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        async def startup_timeout(**_kwargs: object) -> None:
            raise TimeoutError("supervisor did not arm")

        monkeypatch.setattr(bootstrap, "_run_browser_setup", startup_timeout)
        monkeypatch.setattr(bootstrap, "_BACKGROUND_BROWSER_SETUP_SECONDS", 60.0)

        with pytest.raises(TimeoutError, match="supervisor did not arm"):
            await bootstrap._run_background_browser_setup()

    async def test_slow_profile_ownership_cannot_block_the_deadline(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        blocked = threading.Event()
        release = threading.Event()

        def slow_owned(profile: Path) -> Path:
            blocked.set()
            release.wait()
            return profile

        async def setup_must_not_start(**_kwargs: object) -> None:
            pytest.fail("ownership must finish before setup starts")

        config = SimpleNamespace(browser=SimpleNamespace(chrome_path=None))
        monkeypatch.setattr(bootstrap, "get_config", lambda: config)
        monkeypatch.setattr(bootstrap, "_owned", slow_owned)
        monkeypatch.setattr(bootstrap, "_browser_setup_ready", lambda: False)
        monkeypatch.setattr(bootstrap, "_run_browser_setup", setup_must_not_start)
        monkeypatch.setattr(bootstrap, "_BACKGROUND_BROWSER_SETUP_SECONDS", 0.01)
        fallback = threading.Timer(0.2, release.set)
        fallback.start()
        started = asyncio.get_running_loop().time()
        try:
            with pytest.raises(BrowserSetupFailedError, match="background deadline"):
                await bootstrap.start_background_browser_setup_if_needed()
        finally:
            release.set()
            fallback.cancel()

        assert blocked.is_set()
        assert asyncio.get_running_loop().time() - started < 0.1

    async def test_slow_cache_filesystem_cannot_block_the_deadline(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        blocked = threading.Event()
        release = threading.Event()

        def slow_mkdir(path: Path) -> None:
            blocked.set()
            release.wait()

        async def install_must_not_start(*args: object, **kwargs: object) -> None:
            pytest.fail("the deadline should expire during cache preparation")

        monkeypatch.setattr(bootstrap, "secure_mkdir", slow_mkdir)
        monkeypatch.setattr(
            bootstrap, "_run_patchright_install", install_must_not_start
        )
        monkeypatch.setattr(bootstrap, "_BACKGROUND_BROWSER_SETUP_SECONDS", 0.01)

        started = asyncio.get_running_loop().time()
        try:
            with pytest.raises(BrowserSetupFailedError, match="background deadline"):
                await bootstrap._run_background_browser_setup()
        finally:
            release.set()

        assert blocked.is_set()
        assert asyncio.get_running_loop().time() - started < 0.1

    async def test_setup_filesystem_work_uses_a_daemon_thread(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        daemon_flags: list[bool] = []
        real_thread = threading.Thread

        def thread(*args: Any, **kwargs: Any) -> threading.Thread:
            daemon_flags.append(bool(kwargs.get("daemon")))
            return real_thread(*args, **kwargs)

        monkeypatch.setattr(bootstrap.threading, "Thread", thread)

        await bootstrap._run_in_daemon_thread(lambda: None)

        assert daemon_flags == [True]

    async def test_shutdown_awaits_setup_cancellation_cleanup(
        self, isolate_profile_dir, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        cleaned = asyncio.Event()

        async def setup(**_kwargs: object) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                cleaned.set()
                raise

        task = asyncio.create_task(setup())
        bootstrap.get_bootstrap_state().setup_task = task
        await asyncio.sleep(0)

        await bootstrap.stop_background_browser_setup()

        assert cleaned.is_set()
        assert bootstrap.get_bootstrap_state().setup_task is None

    async def test_shutdown_preserves_simultaneous_caller_cancellation(
        self, isolate_profile_dir
    ):
        from linkedin_mcp_server import bootstrap

        stopping: list[asyncio.Task[None]] = []

        async def setup(**_kwargs: object) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                stopping[0].cancel()
                raise

        task = asyncio.create_task(setup())
        bootstrap.get_bootstrap_state().setup_task = task
        await asyncio.sleep(0)
        stop = asyncio.create_task(bootstrap.stop_background_browser_setup())
        stopping.append(stop)

        with pytest.raises(asyncio.CancelledError):
            await stop

        assert bootstrap.get_bootstrap_state().setup_task is None


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


class _FakeStdin:
    def __init__(self) -> None:
        self.closed = False
        self.written = bytearray()

    def is_closing(self) -> bool:
        return self.closed

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _FakeProtocol:
    def __init__(self, *lines: bytes) -> None:
        self.lines = list(lines)

    async def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""

    async def read(self, n: int = -1) -> bytes:
        if not self.lines:
            return b""
        head = self.lines[0]
        if 0 <= n < len(head):
            self.lines[0] = head[n:]
            return head[:n]
        self.lines.pop(0)
        return head


_SUPERVISOR_NONCE = "0123456789abcdef" * 4


def _completed_windows_wait() -> SimpleNamespace:
    return SimpleNamespace(
        done=lambda: True,
        _registered=False,
        _wait_handle=None,
        _event=None,
        _event_fut=None,
        _proactor=None,
        _ov=None,
    )


class _FakeProc:
    """A running process: ``returncode`` stays None until ``wait()`` reaps it."""

    def __init__(self, chunks: list[bytes], returncode: int) -> None:
        self.pid = 424242  # a real Process carries one
        self.stdout = _FakeStdout(chunks)
        self.stderr = _FakeProtocol(
            f"armed {_SUPERVISOR_NONCE}\n".encode(),
            f"started {_SUPERVISOR_NONCE} {self.pid}\n".encode(),
        )
        self.stdin = _FakeStdin()
        self.returncode: int | None = None
        self._final = returncode
        self.killed = False
        self.waited = False

    async def wait(self) -> int:
        result = await self._wait()
        self.waited = True
        return result

    async def _wait(self) -> int:
        # On cleanup, closing the lease makes the supervisor drain and exit. On
        # normal completion the caller must consume stdout first or a real pipe
        # can fill and hold process collection open.
        if not self.stdin.closed:
            assert self.stdout.exhausted, "wait() awaited before stdout was drained"
        self.returncode = self._final
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _NeverExitingProc(_FakeProc):
    """Only the containment path reaps this one; it never exits itself."""

    async def _wait(self) -> int:
        while not self.stdin.closed:
            await asyncio.sleep(0.001)
        self.returncode = self._final
        return self.returncode


class _Spawned:
    """What the patched ``create_subprocess_exec`` was given, and handed back."""

    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}
        self.proc: _FakeProc | None = None


class TestDaemonThreadHandover:
    """Who owns the answer when the awaiting task is cancelled around it."""

    class _InlineThreads:
        """The bootstrap module's threading, with Thread running its target."""

        class Thread:
            def __init__(self, *, target, name=None, daemon=None, args=()):
                self._target = target
                self._args = args

            def start(self) -> None:
                self._target(*self._args)

        def __getattr__(self, name: str):
            return getattr(threading, name)

    async def test_a_cancel_after_the_answer_lands_still_discards_it(self, monkeypatch):
        """The value was set on the future, and the task never accepted it.

        A task cancelled while the future it awaits is already done resumes
        with the cancellation rather than the value, so without a handover
        here the installer root would keep its pin until the process exits.
        """
        from linkedin_mcp_server import bootstrap

        monkeypatch.setattr(bootstrap, "threading", self._InlineThreads())
        discarded: list[str] = []

        task = asyncio.create_task(
            bootstrap._run_in_daemon_thread(
                lambda: "the installer root", discard=discarded.append
            )
        )
        # One turn to reach the await and queue the completion, a second to let
        # that completion set the result. Cancelling now finds the future done.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert discarded == ["the installer root"]

    async def test_a_delivered_answer_is_not_discarded(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        monkeypatch.setattr(bootstrap, "threading", self._InlineThreads())
        discarded: list[str] = []

        answer = await bootstrap._run_in_daemon_thread(
            lambda: "the installer root", discard=discarded.append
        )

        assert answer == "the installer root"
        assert discarded == []


class TestInstallerTemporaryRootRemoval:
    """What the cleanup says when it cannot take the download away."""

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
    @pytest.mark.skipif(
        getattr(os, "geteuid", lambda: 1)() == 0,
        reason="root ignores directory modes",
    )
    def test_a_refused_entry_names_the_root_it_stays_in(self, tmp_path, caplog):
        from linkedin_mcp_server import bootstrap

        root = tmp_path / "linkedin-mcp-installer-x"
        locked = root / "locked"
        locked.mkdir(parents=True)
        (locked / "chromium.zip").write_bytes(b"archive")
        pin = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        details = os.fstat(pin)
        temporary = bootstrap._InstallerTemporaryRoot(
            root, details.st_dev, details.st_ino, pin
        )
        locked.chmod(0o500)
        try:
            with caplog.at_level(
                logging.WARNING, logger="linkedin_mcp_server.bootstrap"
            ):
                bootstrap._remove_installer_temporary_root(temporary)
        finally:
            locked.chmod(0o700)

        assert (locked / "chromium.zip").exists(), "the archive is still there"
        assert str(root) in caplog.text
        assert "chromium.zip" in caplog.text

    def test_an_unidentifiable_windows_root_is_named(
        self, tmp_path, monkeypatch, caplog
    ):
        """The Windows branch leaves before rmtree, so it reports for itself."""
        from linkedin_mcp_server import bootstrap

        root = tmp_path / "linkedin-mcp-installer-z"
        root.mkdir()
        (root / "chromium.zip").write_bytes(b"archive")
        temporary = bootstrap._InstallerTemporaryRoot(root, 1, 2, object())
        real_stat = bootstrap.os.stat

        def refuse(target, *args, **kwargs):
            # By string: with os.name forced to "nt", Path() builds a
            # WindowsPath, which never compares equal to this PosixPath.
            if str(target) == str(root):
                raise PermissionError(errno.EACCES, "sharing violation")
            return real_stat(target, *args, **kwargs)

        monkeypatch.setattr(bootstrap.os, "name", "nt")
        monkeypatch.setattr(bootstrap.os, "stat", refuse)
        monkeypatch.setattr(
            bootstrap, "_close_installer_temporary_root_pin", lambda _root: None
        )

        with caplog.at_level(logging.WARNING, logger="linkedin_mcp_server.bootstrap"):
            bootstrap._remove_installer_temporary_root(temporary)

        assert (root / "chromium.zip").exists()
        assert str(root) in caplog.text
        assert "sharing violation" in caplog.text

    def test_a_windows_root_already_gone_says_nothing(
        self, tmp_path, monkeypatch, caplog
    ):
        from linkedin_mcp_server import bootstrap

        root = tmp_path / "linkedin-mcp-installer-w"
        temporary = bootstrap._InstallerTemporaryRoot(root, 1, 2, object())
        monkeypatch.setattr(bootstrap.os, "name", "nt")
        monkeypatch.setattr(
            bootstrap, "_close_installer_temporary_root_pin", lambda _root: None
        )

        with caplog.at_level(logging.WARNING, logger="linkedin_mcp_server.bootstrap"):
            bootstrap._remove_installer_temporary_root(temporary)

        assert caplog.text == ""

    def test_a_cleared_root_says_nothing(self, tmp_path, caplog):
        from linkedin_mcp_server import bootstrap

        root = tmp_path / "linkedin-mcp-installer-y"
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "chromium.zip").write_bytes(b"archive")
        pin = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        details = os.fstat(pin)
        temporary = bootstrap._InstallerTemporaryRoot(
            root, details.st_dev, details.st_ino, pin
        )

        with caplog.at_level(logging.WARNING, logger="linkedin_mcp_server.bootstrap"):
            bootstrap._remove_installer_temporary_root(temporary)

        assert not root.exists()
        assert caplog.text == ""


class TestInstallerSupervisorLaunch:
    @pytest.fixture(autouse=True)
    def _stable_supervisor_nonce(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        monkeypatch.setattr(bootstrap, "new_nonce", lambda: _SUPERVISOR_NONCE)

    def test_posix_supervisor_runs_the_file_without_startup_hooks(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        monkeypatch.setattr(
            bootstrap, "os", SimpleNamespace(name="posix", environ=os.environ)
        )

        command = bootstrap._installer_supervisor_command(["target", "arg"])

        # ``-S`` is the flag that stops a system or virtual-environment
        # ``sitecustomize``; ``-I`` alone leaves it running.
        assert command[1:4] == ["-I", "-S", "-u"]
        supervisor = Path(command[4])
        assert supervisor.is_absolute()
        assert supervisor.name == "installer_supervisor.py"
        assert supervisor.is_file()
        assert command[-3:] == ["--", "target", "arg"]

    def test_windows_supervisor_keeps_the_module_form(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        monkeypatch.setattr(
            bootstrap, "os", SimpleNamespace(name="nt", environ=os.environ)
        )

        command = bootstrap._installer_supervisor_command(["target", "arg"])

        assert command == [
            sys.executable,
            "-P",
            "-m",
            "linkedin_mcp_server.installer_supervisor",
            "--",
            "target",
            "arg",
        ]

    async def test_silent_download_file_growth_reports_activity(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        snapshots = iter([(("archive.zip", 1024, 1),)])
        active = asyncio.Event()
        monkeypatch.setattr(
            bootstrap,
            "_installer_download_snapshot",
            lambda temporary_root, extraction_paths: next(snapshots),
        )
        monkeypatch.setattr(bootstrap, "_INSTALLER_ACTIVITY_POLL_SECONDS", 0.001)

        watcher = asyncio.create_task(
            bootstrap._watch_installer_activity(active.set, Path("private"), (), ())
        )
        try:
            await asyncio.wait_for(active.wait(), timeout=1)
        finally:
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher

    async def test_a_footprint_under_the_ceiling_is_activity_and_not_a_failure(
        self, monkeypatch
    ):
        """Growth is reported; only the total decides whether it is refused."""
        from linkedin_mcp_server import bootstrap

        opening = (("archive.zip", 256, 1),)
        scripted = [
            (("archive.zip", 512, 2),),
            (("archive.zip", 768, 3),),
        ]
        snapshots = iter(scripted)
        active = asyncio.Event()

        monkeypatch.setattr(
            bootstrap,
            "_installer_download_snapshot",
            lambda temporary_root, extraction_paths: next(snapshots, scripted[-1]),
        )
        monkeypatch.setattr(bootstrap, "_INSTALLER_ACTIVITY_POLL_SECONDS", 0.001)
        monkeypatch.setattr(bootstrap, "_INSTALLER_MAX_WRITTEN_BYTES", 1024)

        watcher = asyncio.create_task(
            bootstrap._watch_installer_activity(
                active.set, Path("private"), (), opening
            )
        )
        try:
            await asyncio.wait_for(active.wait(), timeout=2)
            await asyncio.sleep(0.02)
            assert not watcher.done(), "a footprint under the ceiling is not a failure"
        finally:
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher

    async def test_a_tree_a_previous_attempt_left_still_counts(self, monkeypatch):
        """Bytes this install did not write are still bytes it is answerable for.

        The opposite rule, charging only growth, is what an attempt refused for
        its size escapes on: patchright skips a cache directory it has already
        marked complete, so the retry writes nothing and passes.
        """
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        inherited = (("chromium-1217/chrome", 4096, 1),)
        monkeypatch.setattr(
            bootstrap,
            "_installer_download_snapshot",
            lambda temporary_root, extraction_paths: inherited,
        )
        monkeypatch.setattr(bootstrap, "_INSTALLER_ACTIVITY_POLL_SECONDS", 0.001)
        monkeypatch.setattr(bootstrap, "_INSTALLER_MAX_WRITTEN_BYTES", 1024)

        with pytest.raises(BrowserSetupFailedError, match="past its 1.0 KiB limit"):
            await asyncio.wait_for(
                bootstrap._watch_installer_activity(
                    lambda: None, Path("private"), (), inherited
                ),
                timeout=2,
            )

    async def test_a_runaway_download_stops_and_reaps_the_installer(self, monkeypatch):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        proc = _NeverExitingProc([], 0)
        overflowing = (("archive.zip", 5 * 1024**3, 1),)
        snapshots = iter([(), overflowing])

        async def hanging_lines(stream: object):
            await asyncio.Event().wait()
            if False:  # pragma: no cover - makes this an async generator
                yield ""

        monkeypatch.setattr(bootstrap, "_installer_lines", hanging_lines)
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )
        monkeypatch.setattr(
            bootstrap,
            "_installer_download_snapshot",
            lambda temporary_root, extraction_paths: next(snapshots, overflowing),
        )
        monkeypatch.setattr(bootstrap, "_INSTALLER_ACTIVITY_POLL_SECONDS", 0.001)

        with pytest.raises(BrowserSetupFailedError) as failure:
            await asyncio.wait_for(
                bootstrap._run_patchright_install(
                    "--no-shell", activity_callback=lambda: None
                ),
                5,
            )

        assert "past its 4.0 GiB limit" in str(failure.value)
        assert len(str(failure.value)) < 200, "the diagnostic is bounded"
        assert proc.waited, "the installer was reaped rather than left running"

    async def test_a_late_bound_the_final_accounting_denies_is_only_a_warning(
        self, monkeypatch, caplog
    ):
        """A watcher bound the tree no longer shows does not fail the install.

        The final snapshot is the one that decides, and here it finds an empty
        tree: whatever the watcher saw is gone from disk, so the damage the
        ceiling exists to bound is gone with it. The install keeps its own
        result and the breach stays a log line.
        """
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        proc = _FakeProc([], 0)

        async def watcher(
            _callback: Callable[[], None],
            _temporary_root: Path,
            _extraction_paths: tuple[Path, ...],
            _opening: tuple[tuple[str, int, int], ...],
        ) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise BrowserSetupFailedError(
                    "holds 5.0 GiB, past its 4.0 GiB limit"
                ) from None

        async def delayed_lines(stream: object):
            await asyncio.sleep(0)
            setattr(stream, "exhausted", True)
            if False:  # pragma: no cover - makes this an async generator
                yield ""

        monkeypatch.setattr(bootstrap, "_installer_lines", delayed_lines)
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )
        monkeypatch.setattr(bootstrap, "_watch_installer_activity", watcher)

        with caplog.at_level(logging.WARNING, logger="linkedin_mcp_server.bootstrap"):
            await bootstrap._run_patchright_install(
                "--no-shell", activity_callback=lambda: None
            )

        assert "exceeded a setup bound" in caplog.text

    async def test_bytes_written_before_the_first_poll_are_charged(self, monkeypatch):
        """Everything lands inside one poll interval and is still refused.

        Nothing here ever grows between two polls, so a ceiling reading the
        difference between them sees zero. It is the footprint the first poll
        finds that decides.
        """
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        proc = _NeverExitingProc([], 0)
        overflowing = (("archive.zip", 5 * 1024**3, 1),)
        disk: list[tuple[tuple[str, int, int], ...]] = [()]

        async def spawn(*_args: object, **_kwargs: object) -> _FakeProc:
            # Everything lands at once, inside the first poll interval.
            disk[0] = overflowing
            return proc

        async def hanging_lines(stream: object):
            await asyncio.Event().wait()
            if False:  # pragma: no cover - makes this an async generator
                yield ""

        monkeypatch.setattr(bootstrap, "_installer_lines", hanging_lines)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(
            bootstrap,
            "_installer_download_snapshot",
            lambda temporary_root, extraction_paths: disk[0],
        )
        monkeypatch.setattr(bootstrap, "_INSTALLER_ACTIVITY_POLL_SECONDS", 0.001)

        with pytest.raises(BrowserSetupFailedError) as failure:
            await asyncio.wait_for(
                bootstrap._run_patchright_install(
                    "--no-shell", activity_callback=lambda: None
                ),
                5,
            )

        assert "holds 5.0 GiB" in str(failure.value)
        assert proc.waited, "the installer was reaped rather than left running"

    @staticmethod
    def _exits_before_pipe_eof(
        monkeypatch,
        disk: list[tuple[tuple[str, int, int], ...]],
        grown: tuple[tuple[str, int, int], ...],
        returncode: int = 0,
    ) -> _FakeProc:
        """An installer reaped while its own pipes are still open.

        The ordering CPython 3.14 can produce: ``Process.returncode`` is
        published from the child watcher, and pipe EOF arrives afterwards. The
        supervision loop leaves as soon as the process task resolves, so the
        last of the install lands with the activity watcher parked between two
        five-second polls and nothing but the final snapshot left to see it.
        """
        from linkedin_mcp_server import bootstrap

        exited = asyncio.Event()

        class _ExitsBeforePipeEOF(_FakeProc):
            async def _wait(self) -> int:
                self.returncode = self._final
                exited.set()
                return self.returncode

        proc = _ExitsBeforePipeEOF([], returncode)

        async def trailing_lines(stream: object):
            await exited.wait()
            disk[0] = grown
            setattr(stream, "exhausted", True)
            yield "done"

        monkeypatch.setattr(bootstrap, "_installer_lines", trailing_lines)
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )
        monkeypatch.setattr(
            bootstrap,
            "_installer_download_snapshot",
            lambda temporary_root, extraction_paths: disk[0],
        )
        return proc

    async def test_growth_after_process_exit_beats_a_successful_install(
        self, isolate_profile_dir, tmp_path, monkeypatch
    ):
        """The final snapshot refuses a tree the installer exited 0 on."""
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        _patch_targets_and_version(monkeypatch)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
        disk: list[tuple[tuple[str, int, int], ...]] = [()]
        proc = self._exits_before_pipe_eof(
            monkeypatch, disk, (("chrome", 5 * 1024**3, 1),)
        )

        with pytest.raises(BrowserSetupFailedError) as failure:
            await asyncio.wait_for(
                bootstrap._run_browser_setup(activity_callback=lambda: None), 5
            )

        assert proc.returncode == 0, "the installer itself succeeded"
        assert "past its 4.0 GiB limit" in str(failure.value)
        assert not install_metadata_path().exists(), "no browser was recorded ready"

    async def test_the_installers_own_failure_outranks_the_final_bound(
        self, isolate_profile_dir, tmp_path, monkeypatch
    ):
        """A nonzero exit keeps its own message, which says what went wrong.

        The ceiling adds nothing to an install that is refused already, and the
        output it collected is the only account of the failure there is.
        """
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        _patch_targets_and_version(monkeypatch)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
        disk: list[tuple[tuple[str, int, int], ...]] = [()]
        self._exits_before_pipe_eof(
            monkeypatch, disk, (("chrome", 5 * 1024**3, 1),), returncode=1
        )

        with pytest.raises(BrowserSetupFailedError) as failure:
            await asyncio.wait_for(
                bootstrap._run_browser_setup(activity_callback=lambda: None), 5
            )

        assert str(failure.value) == "done"
        assert not install_metadata_path().exists()

    async def test_a_normal_install_passes_the_final_accounting(
        self, isolate_profile_dir, tmp_path, monkeypatch
    ):
        """A tree that stays under the ceiling is recorded as installed."""
        from linkedin_mcp_server import bootstrap

        _patch_targets_and_version(monkeypatch)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
        monkeypatch.setattr(bootstrap, "_INSTALLER_MAX_WRITTEN_BYTES", 1024)
        disk: list[tuple[tuple[str, int, int], ...]] = [()]
        self._exits_before_pipe_eof(monkeypatch, disk, (("chrome", 512, 2),))

        await asyncio.wait_for(
            bootstrap._run_browser_setup(activity_callback=lambda: None), 5
        )

        payload = json.loads(install_metadata_path().read_text())
        assert payload["installed_targets"]["chromium-"] is True

    async def test_an_oversized_completed_target_cannot_pass_on_retry(
        self, isolate_profile_dir, tmp_path, monkeypatch
    ):
        """A tree refused for its size stays refused when patchright skips it.

        The first attempt leaves the extraction target complete, marker and
        all, so the second one downloads nothing. Charging growth would then
        find none and record the very tree the ceiling had just refused.
        """
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        _patch_targets_and_version(monkeypatch)
        browsers = tmp_path / "browsers"
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
        monkeypatch.setattr(bootstrap, "_INSTALLER_MAX_WRITTEN_BYTES", 4096)
        target = browsers / "chromium-1217"
        spawned: list[_FakeProc] = []

        async def spawn(*_args: object, **_kwargs: object) -> _FakeProc:
            if not target.exists():
                target.mkdir(parents=True)
                (target / "chrome").write_bytes(b"a" * 16384)
                (target / "INSTALLATION_COMPLETE").write_text("")
            proc = _FakeProc([], 0)
            spawned.append(proc)
            return proc

        async def delayed_lines(stream: object):
            await asyncio.sleep(0)
            setattr(stream, "exhausted", True)
            if False:  # pragma: no cover - makes this an async generator
                yield ""

        monkeypatch.setattr(bootstrap, "_installer_lines", delayed_lines)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

        for attempt in range(2):
            with pytest.raises(BrowserSetupFailedError, match="past its 4.0 KiB limit"):
                await asyncio.wait_for(bootstrap._run_browser_setup(), 5)
            assert not install_metadata_path().exists(), (
                f"attempt {attempt} recorded a browser the ceiling refused"
            )

        assert (target / "INSTALLATION_COMPLETE").is_file(), (
            "the refused tree is the completed one patchright would skip"
        )
        assert len(spawned) == 1, "and the retry was refused before it could run"

    async def test_a_direct_install_watches_bytes_without_a_progress_callback(
        self, monkeypatch
    ):
        """No progress callback is not a reason to install without a ceiling.

        ``--login``, ``--status`` and ``--import-from-browser`` hold no
        inactivity deadline and pass none, which is not an opinion about how
        many bytes they may write.
        """
        from linkedin_mcp_server import bootstrap

        proc = _FakeProc([], 0)
        watched: list[Callable[[], None]] = []

        async def watcher(
            callback: Callable[[], None],
            _temporary_root: Path,
            _extraction_paths: tuple[Path, ...],
            _opening: tuple[tuple[str, int, int], ...],
        ) -> None:
            watched.append(callback)
            await asyncio.Event().wait()

        async def delayed_lines(stream: object):
            await asyncio.sleep(0)
            setattr(stream, "exhausted", True)
            if False:  # pragma: no cover - makes this an async generator
                yield ""

        monkeypatch.setattr(bootstrap, "_installer_lines", delayed_lines)
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )
        monkeypatch.setattr(bootstrap, "_watch_installer_activity", watcher)

        await asyncio.wait_for(bootstrap._run_patchright_install("--no-shell"), 5)

        assert len(watched) == 1, "the watcher ran for a caller that passed none"
        assert watched[0]() is None, "and had somewhere to report progress to"

    async def test_endless_growth_contains_a_direct_install(self, monkeypatch):
        """The ceiling reaps the installer and its private root either way."""
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        proc = _NeverExitingProc([], 0)
        overflowing = (("archive.zip", 5 * 1024**3, 1),)
        snapshots = iter([(), overflowing])
        removed: list[Path] = []

        def cleanup(temporary_root: bootstrap._InstallerTemporaryRoot) -> None:
            removed.append(temporary_root.path)
            bootstrap._remove_installer_temporary_root(temporary_root)

        async def hanging_lines(stream: object):
            await asyncio.Event().wait()
            if False:  # pragma: no cover - makes this an async generator
                yield ""

        monkeypatch.setattr(bootstrap, "_installer_lines", hanging_lines)
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )
        monkeypatch.setattr(
            bootstrap,
            "_installer_download_snapshot",
            lambda temporary_root, extraction_paths: next(snapshots, overflowing),
        )
        monkeypatch.setattr(bootstrap, "_INSTALLER_ACTIVITY_POLL_SECONDS", 0.001)
        monkeypatch.setattr(
            bootstrap, "_start_installer_temporary_root_cleanup", cleanup
        )

        with pytest.raises(BrowserSetupFailedError, match="past its 4.0 GiB limit"):
            await asyncio.wait_for(bootstrap._run_patchright_install("--no-shell"), 5)

        assert proc.waited, "the installer was reaped rather than left running"
        assert removed and not removed[0].exists(), "and its private root is gone"

    async def test_the_absolute_lifetime_stops_a_direct_install(self, monkeypatch):
        """An install nothing else bounds still expires.

        Nothing grows here, so the byte ceiling has nothing to refuse and no
        caller is holding an inactivity deadline: the lifetime is the only
        bound left, and without it this install runs forever.
        """
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        proc = _NeverExitingProc([], 0)

        async def hanging_lines(stream: object):
            await asyncio.Event().wait()
            if False:  # pragma: no cover - makes this an async generator
                yield ""

        monkeypatch.setattr(bootstrap, "_installer_lines", hanging_lines)
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )
        monkeypatch.setattr(bootstrap, "_BROWSER_SETUP_LIFETIME_SECONDS", 0.05)

        with pytest.raises(BrowserSetupFailedError, match="absolute lifetime"):
            await asyncio.wait_for(bootstrap._run_patchright_install("--no-shell"), 5)

        assert proc.waited, "the installer was reaped rather than left running"

    async def test_the_background_path_arms_one_lifetime_and_not_two(
        self, isolate_profile_dir, monkeypatch
    ):
        """The installer must not re-arm a bound its caller already holds.

        A second scope would restart the six hours from wherever the installer
        happened to begin, so an attempt could outlive the ceiling by however
        long the readiness check and the setup around it took.
        """
        from linkedin_mcp_server import bootstrap

        armed = 0
        real_timeout_at = asyncio.timeout_at

        def counting_timeout_at(when: float) -> object:
            nonlocal armed
            armed += 1
            return real_timeout_at(when)

        async def setup(**_kwargs: object) -> None:
            async with bootstrap._setup_lifetime():
                await asyncio.sleep(0)

        monkeypatch.setattr(bootstrap, "_browser_setup_ready", lambda: False)
        monkeypatch.setattr(bootstrap, "_run_browser_setup", setup)
        monkeypatch.setattr(asyncio, "timeout_at", counting_timeout_at)

        await bootstrap._run_background_browser_setup()

        # One lifetime and one inactivity deadline, and nothing from the layer
        # underneath them.
        assert armed == 2

    def test_real_activity_scanner_observes_private_archive_growth(self, tmp_path):
        from linkedin_mcp_server import bootstrap

        temporary_root = tmp_path / "private"
        archive = temporary_root / "playwright-download-current" / "archive.zip"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"a")

        before = bootstrap._installer_download_snapshot(temporary_root, ())
        archive.write_bytes(b"a" * 1001)
        after = bootstrap._installer_download_snapshot(temporary_root, ())

        assert after != before

    def test_unrelated_global_download_does_not_report_activity(self, tmp_path):
        from linkedin_mcp_server import bootstrap

        temporary_root = tmp_path / "private"
        unrelated = tmp_path / "global" / "playwright-download-other" / "archive.zip"
        temporary_root.mkdir()
        unrelated.parent.mkdir(parents=True)
        unrelated.write_bytes(b"a")

        before = bootstrap._installer_download_snapshot(temporary_root, ())
        unrelated.write_bytes(b"a" * 1001)
        after = bootstrap._installer_download_snapshot(temporary_root, ())

        assert after == before

    def test_only_the_locked_cache_revision_reports_activity(
        self, tmp_path, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        temporary_root = tmp_path / "private"
        cache = tmp_path / "cache"
        temporary_root.mkdir()
        cache.mkdir()
        environment = {"PLAYWRIGHT_BROWSERS_PATH": str(cache)}
        monkeypatch.setattr(
            bootstrap,
            "_patchright_install_targets",
            lambda: {
                bootstrap._FULL_DIR_PREFIX: "123",
                bootstrap._SHELL_DIR_PREFIX: "123",
            },
        )
        # The auxiliary targets have their own tests below; this one is about
        # which browser revision the poll is bound to.
        monkeypatch.setattr(
            bootstrap, "_installer_auxiliary_names", lambda browser_selected: ()
        )
        extraction_paths = bootstrap._installer_extraction_paths(
            "--no-shell", environment
        )

        before = bootstrap._installer_download_snapshot(
            temporary_root, extraction_paths
        )
        unrelated = cache / "chromium-999" / "chrome-linux" / "chrome"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_bytes(b"a")
        after_unrelated = bootstrap._installer_download_snapshot(
            temporary_root, extraction_paths
        )
        expected = cache / "chromium-123" / "chrome-linux" / "chrome"
        expected.parent.mkdir(parents=True)
        expected.write_bytes(b"a")
        after_expected = bootstrap._installer_download_snapshot(
            temporary_root, extraction_paths
        )

        assert extraction_paths == (cache / "chromium-123",)
        assert after_unrelated == before
        assert after_expected != after_unrelated

    def test_a_direct_download_archive_reports_activity(self, tmp_path):
        from linkedin_mcp_server import bootstrap

        temporary_root = tmp_path / "private"
        temporary_root.mkdir()
        archive = temporary_root / "playwright-download-current.zip"
        archive.write_bytes(b"a")
        before = bootstrap._installer_download_snapshot(temporary_root, ())

        archive.write_bytes(b"ab")
        after = bootstrap._installer_download_snapshot(temporary_root, ())

        assert before != after
        assert after == ((str(archive), 2, archive.stat().st_mtime_ns),)

    def test_a_symlinked_file_is_measured_as_the_link_it_is(self, tmp_path):
        """The deadline is held open by this install's writes and no others.

        ``os.walk`` refuses to descend through a directory symlink, and a file
        symlink reaches as far by another name.
        """
        from linkedin_mcp_server import bootstrap

        temporary_root = tmp_path / "private"
        download = temporary_root / "playwright-download-current"
        download.mkdir(parents=True)
        foreign = tmp_path / "foreign.log"
        foreign.write_bytes(b"a")
        link = download / "archive.zip"
        try:
            link.symlink_to(foreign)
        except (OSError, NotImplementedError):
            pytest.skip("this platform does not allow creating a symlink here")

        before = bootstrap._installer_download_snapshot(temporary_root, ())
        foreign.write_bytes(b"a" * 4096)
        after = bootstrap._installer_download_snapshot(temporary_root, ())

        assert any(name == str(link) for name, _size, _mtime in after), (
            "the link is still in the snapshot"
        )
        assert after == before, "and a foreign target's growth is not activity"

    def test_an_unreadable_entry_refuses_the_measurement(
        self, tmp_path: Path, monkeypatch
    ):
        """An entry that cannot be read is not an entry that is not there.

        A suppressed read error subtracts those bytes from the ceiling, so the
        install it was meant to bound passes as a smaller one.
        """
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        target = tmp_path / "chromium-1234"
        target.mkdir()
        (target / "archive.zip").write_bytes(b"x" * 16)
        # pathlib reaches the file through os.stat, with follow_symlinks off.
        real_stat = os.stat

        def failing(code):
            def answer(path, **rest):
                if str(path).endswith("archive.zip"):
                    raise OSError(code, os.strerror(code))
                return real_stat(path, **rest)

            return answer

        monkeypatch.setattr(bootstrap.os, "stat", failing(errno.EIO))
        with pytest.raises(BrowserSetupFailedError, match="could not be measured"):
            bootstrap._installer_download_snapshot(tmp_path, (target,))

        monkeypatch.setattr(bootstrap.os, "stat", failing(errno.ENOENT))
        measured = bootstrap._installer_download_snapshot(tmp_path, (target,))

        assert not [entry for entry in measured if entry[0].endswith("archive.zip")], (
            "and an entry the installer removed mid-walk is still ordinary"
        )

        monkeypatch.setattr(bootstrap.os, "stat", failing(errno.EACCES))
        if os.name == "nt":
            # Windows says this instead of reporting a delete-pending file gone.
            bootstrap._installer_download_snapshot(tmp_path, (target,))
        else:
            with pytest.raises(BrowserSetupFailedError, match="could not be measured"):
                bootstrap._installer_download_snapshot(tmp_path, (target,))

    async def test_a_vanished_peak_leaves_the_install_its_result(
        self, monkeypatch, caplog
    ):
        """The ceiling bounds the footprint that stays, not the peak (#815).

        A breach the watcher reports in the same turn the installer exits is
        never consumed by the supervision loop, so what decides is the final
        accounting. It runs on every success, which is why an install whose
        archive is gone and whose tree fits is kept, and said out loud rather
        than dropped.
        """
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        proc = _FakeProc([b"done\n"], 0)

        async def breach(*_args: object) -> None:
            raise BrowserSetupFailedError("Browser setup exceeded its size limit")

        monkeypatch.setattr(bootstrap, "_watch_installer_activity", breach)
        monkeypatch.setattr(
            bootstrap,
            "_installer_download_snapshot",
            lambda *_args: (("kept", 1024, 1),),
        )
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )

        with caplog.at_level(logging.WARNING, logger="linkedin_mcp_server.bootstrap"):
            await asyncio.wait_for(bootstrap._run_patchright_install("--no-shell"), 5)

        assert proc.returncode == 0, "the install kept its own result"
        assert "exceeded a setup bound" in caplog.text

    async def test_a_failed_install_keeps_its_message_over_the_scan(self, monkeypatch):
        """The installer named the cause, so the accounting may not answer for it.

        Everything the scan can say here is that it could not measure a tree
        the install already abandoned.
        """
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        proc = _FakeProc([b"ERROR: the mirror refused the archive\n"], 1)
        scans = iter([()])

        def snapshot(
            _temporary_root: Path, _extraction_paths: tuple[Path, ...]
        ) -> tuple[tuple[str, int, int], ...]:
            for opening in scans:
                return opening
            raise BrowserSetupFailedError(
                "Patchright Chromium browser setup could not be measured "
                "against its size limit"
            )

        async def watcher(
            _callback: Callable[[], None],
            _temporary_root: Path,
            _extraction_paths: tuple[Path, ...],
            _opening: tuple[tuple[str, int, int], ...],
        ) -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(bootstrap, "_watch_installer_activity", watcher)
        monkeypatch.setattr(bootstrap, "_installer_download_snapshot", snapshot)
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )

        with pytest.raises(BrowserSetupFailedError) as failure:
            await bootstrap._run_patchright_install("--no-shell")

        assert "the mirror refused the archive" in str(failure.value)

    def test_a_pending_temp_cleanup_is_waited_for_at_exit(self, monkeypatch):
        """The removal runs on a daemon thread, which nothing else waits for.

        A one-shot CLI mode reaches interpreter exit within milliseconds of a
        refused install, and the root left behind holds its download.
        """
        from linkedin_mcp_server import bootstrap

        registered: list[Callable[[], None]] = []
        started = threading.Event()
        removed = threading.Event()

        def remove(_root: object) -> None:
            started.set()
            time.sleep(0.3)
            removed.set()

        monkeypatch.setattr(bootstrap, "_installer_cleanups_awaited", False)
        monkeypatch.setattr(bootstrap.atexit, "register", registered.append)
        monkeypatch.setattr(bootstrap, "_remove_installer_temporary_root", remove)

        bootstrap._start_installer_temporary_root_cleanup(
            cast(Any, SimpleNamespace(path=Path("unused"), device=0, inode=0, pin=None))
        )

        assert started.wait(5), "the cleanup thread ran"
        assert bootstrap._await_installer_temporary_root_cleanups in registered
        for hook in registered:
            hook()

        assert removed.is_set(), "and exit waited for it to finish"

    def test_an_unreadable_temporary_root_refuses_the_measurement(
        self, tmp_path: Path, monkeypatch
    ):
        """The archive lives in there, so a directory that will not open hides it.

        A pattern match over the root answers an unreadable directory with no
        matches, which reads exactly like an install that has written nothing.
        """
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        real_scandir = os.scandir

        def unreadable(path, *rest):
            if Path(path) == tmp_path:
                raise OSError(errno.EIO, "I/O error")
            return real_scandir(path, *rest)

        monkeypatch.setattr(bootstrap.os, "scandir", unreadable)

        with pytest.raises(BrowserSetupFailedError, match="could not be measured"):
            bootstrap._installer_download_snapshot(tmp_path, ())

    async def test_a_standing_oversized_cache_says_what_to_remove(self, monkeypatch):
        """Every retry refuses the same tree, so the message has to name it.

        Nothing the installer does can shrink a cache it is never started for,
        and the guidance the client receives is to try again.
        """
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        spawned: list[object] = []
        monkeypatch.setattr(
            bootstrap,
            "_installer_download_snapshot",
            lambda *_args: (("chromium-1234", 5 * 1024**3, 1),),
        )
        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            AsyncMock(side_effect=lambda *args, **rest: spawned.append(args)),
        )

        with pytest.raises(BrowserSetupFailedError) as failure:
            await bootstrap._run_patchright_install("--no-shell")

        assert str(bootstrap.browsers_path()) in str(failure.value)
        assert not spawned, "and the installer was never started for it"

    async def test_a_scan_failure_mid_install_stops_the_installer(self, monkeypatch):
        """The real watcher, whose poll fails once the installer is running.

        Everything it cannot measure becomes a bound, so the install it can no
        longer watch is stopped rather than left to finish unobserved.
        """
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        proc = _NeverExitingProc([], 0)
        scans = iter([()])

        def snapshot(
            _temporary_root: Path, _extraction_paths: tuple[Path, ...]
        ) -> tuple[tuple[str, int, int], ...]:
            for opening in scans:
                return opening
            raise RuntimeError("the tree cannot be read")

        async def hanging_lines(stream: object):
            await asyncio.Event().wait()
            if False:  # pragma: no cover - makes this an async generator
                yield ""

        monkeypatch.setattr(bootstrap, "_installer_lines", hanging_lines)
        monkeypatch.setattr(bootstrap, "_installer_download_snapshot", snapshot)
        monkeypatch.setattr(bootstrap, "_INSTALLER_ACTIVITY_POLL_SECONDS", 0.001)
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )

        with pytest.raises(BrowserSetupFailedError, match="can no longer be measured"):
            await asyncio.wait_for(bootstrap._run_patchright_install("--no-shell"), 5)

        assert proc.waited, "the installer it could not watch was reaped"

    async def test_an_oversized_custom_cache_names_itself(self, monkeypatch, tmp_path):
        """The operator moved the cache, so the default is not what to remove."""
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        elsewhere = tmp_path / "browser-cache"
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(elsewhere))
        monkeypatch.setattr(
            bootstrap,
            "_installer_download_snapshot",
            lambda *_args: (("chromium-1234", 5 * 1024**3, 1),),
        )
        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            AsyncMock(side_effect=lambda *args, **rest: pytest.fail("it started")),
        )

        with pytest.raises(BrowserSetupFailedError) as failure:
            await bootstrap._run_patchright_install("--no-shell")

        assert str(elsewhere) in str(failure.value)
        assert str(bootstrap.browsers_path()) not in str(failure.value)

    def test_activity_scanner_matches_patchright_temp_layout(self):
        from importlib.metadata import distribution

        source = Path(
            str(
                distribution("patchright").locate_file(
                    "patchright/driver/package/lib/coreBundle.js"
                )
            )
        ).read_text()
        marker = source.index('"playwright-download-"')
        contract = source[marker - 500 : marker + 500]

        assert "mkdtemp" in contract
        assert "tmpdir()" in contract
        assert "zipPath" in contract

    def test_patchright_cli_preserves_the_private_temp_environment(self):
        from importlib.metadata import distribution

        installed = distribution("patchright")
        entrypoint = Path(
            str(installed.locate_file("patchright/__main__.py"))
        ).read_text()
        driver = Path(
            str(installed.locate_file("patchright/_impl/_driver.py"))
        ).read_text()

        assert "env=get_driver_env()" in entrypoint
        assert "env = os.environ.copy()" in driver

    async def test_a_substituted_watchers_failure_does_not_replace_success(
        self, monkeypatch, caplog
    ):
        """The supervisor's backstop, which the real watcher never reaches.

        It converts everything it can catch into a bound, so this drives a
        replacement that raises something else. The production path is
        `test_a_scan_failure_mid_install_stops_the_installer` below.
        """
        from linkedin_mcp_server import bootstrap

        proc = _FakeProc([], 0)

        async def watcher(
            _callback: Callable[[], None],
            _temporary_root: Path,
            _extraction_paths: tuple[Path, ...],
            _opening: tuple[tuple[str, int, int], ...],
        ) -> None:
            raise RuntimeError("watcher unavailable")

        async def delayed_lines(stream: object):
            await asyncio.sleep(0)
            setattr(stream, "exhausted", True)
            if False:  # pragma: no cover - makes this an async generator
                yield ""

        monkeypatch.setattr(bootstrap, "_installer_lines", delayed_lines)
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )
        monkeypatch.setattr(bootstrap, "_watch_installer_activity", watcher)

        with caplog.at_level(logging.WARNING, logger="linkedin_mcp_server.bootstrap"):
            await bootstrap._run_patchright_install(
                "--no-shell", activity_callback=lambda: None
            )

        assert "activity watcher failed" in caplog.text

    async def test_a_watcher_that_cannot_poll_refuses_the_install(
        self, tmp_path, monkeypatch
    ):
        """Losing the poll is losing the ceiling, so the install goes with it.

        A snapshot that cannot run leaves nothing able to stop a download
        already in flight, and the final accounting only ever judges what a
        finished installer left behind.
        """
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        async def unavailable(function, *args, **kwargs):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(bootstrap, "_run_in_daemon_thread", unavailable)
        monkeypatch.setattr(bootstrap, "_INSTALLER_ACTIVITY_POLL_SECONDS", 0.001)

        watching = asyncio.create_task(
            bootstrap._watch_installer_activity(lambda: None, tmp_path, (), ())
        )
        with pytest.raises(BrowserSetupFailedError) as failure:
            await asyncio.wait_for(watching, 5)

        assert isinstance(failure.value.__cause__, RuntimeError)
        assert bootstrap._installer_bound_breached(watching), (
            "and the supervision loop stops the installer over it"
        )

    async def test_parent_cancellation_during_watcher_cleanup_is_preserved(
        self, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        proc = _FakeProc([], 0)
        cleanup_started = asyncio.Event()

        async def watcher(
            _callback: Callable[[], None],
            _temporary_root: Path,
            _extraction_paths: tuple[Path, ...],
            _opening: tuple[tuple[str, int, int], ...],
        ) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await asyncio.Event().wait()

        async def delayed_lines(stream: object):
            await asyncio.sleep(0)
            setattr(stream, "exhausted", True)
            if False:  # pragma: no cover - makes this an async generator
                yield ""

        monkeypatch.setattr(bootstrap, "_installer_lines", delayed_lines)
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )
        monkeypatch.setattr(bootstrap, "_watch_installer_activity", watcher)

        install = asyncio.create_task(
            bootstrap._run_patchright_install(
                "--no-shell", activity_callback=lambda: None
            )
        )
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        install.cancel()

        with pytest.raises(asyncio.CancelledError):
            await install

    async def test_launches_the_internal_supervisor_and_reads_its_target(
        self, tmp_path, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        proc = _FakeProc([], 0)
        captured: dict[str, object] = {}

        async def fake_exec(*args: object, **kwargs: object) -> _FakeProc:
            captured["args"] = args
            captured["kwargs"] = kwargs
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setenv("PYTHONPATH", ".")
        environment = bootstrap._installer_environment(tmp_path / "private")

        started = await bootstrap._start_installer_supervisor("--no-shell", environment)

        assert started.process is proc
        assert proc.stdin.written == (
            f"{_SUPERVISOR_NONCE}\nstart {_SUPERVISOR_NONCE}\n".encode()
        )
        args = captured["args"]
        assert isinstance(args, tuple)
        assert args[1:4] == ("-I", "-S", "-u")
        assert args[4] == str(
            Path(bootstrap.__file__).with_name("installer_supervisor.py").resolve()
        )
        assert args[5] == "--"
        assert args[6:10] == (sys.executable, "-P", "-m", "patchright")
        assert args[-2:] == ("chromium", "--no-shell")
        kwargs = cast(dict[str, object], captured["kwargs"])
        assert kwargs["stdin"] is asyncio.subprocess.PIPE
        assert kwargs["stdout"] is asyncio.subprocess.PIPE
        assert kwargs["stderr"] is asyncio.subprocess.PIPE
        assert kwargs["env"] is environment
        assert "PYTHONPATH" not in environment
        assert {environment[name] for name in ("TMPDIR", "TMP", "TEMP")} == {
            str(tmp_path / "private")
        }

    async def test_windows_assigns_the_gate_before_releasing_it(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        events: list[str] = []
        proc = _FakeProc([], 0)
        managed_proc = cast(Any, proc)
        popen = SimpleNamespace(_handle=1234)
        overlapped = SimpleNamespace(address=5678)
        proactor = SimpleNamespace(_cache={})
        process_wait = SimpleNamespace(
            _handle=1234,
            _proactor=proactor,
            _ov=overlapped,
            _event_fut=None,
            _registered=True,
            done=lambda: False,
        )
        proactor._cache[overlapped.address] = (
            process_wait,
            overlapped,
            0,
            object(),
        )
        managed_proc._transport = SimpleNamespace(
            _loop=SimpleNamespace(_proactor=proactor),
            get_extra_info=lambda name: popen if name == "subprocess" else None,
        )

        class _Job:
            name = None
            closed = False

            @classmethod
            def anonymous(cls):
                events.append("create-job")
                return cls()

            def assign_asyncio_process(self, child: object) -> object:
                assert child is proc
                assert managed_proc._transport.get_extra_info("subprocess") is popen
                events.append("assign-gate")
                return popen

            def close(self) -> None:
                self.closed = True
                events.append("close-job")

        async def fake_exec(*args: object, **kwargs: object) -> _FakeProc:
            events.append("start-gate")
            assert args[:5] == ("gate", "nonce", "--", sys.executable, "-P")
            return proc

        def gate(target: list[str], nonce: str) -> list[str]:
            events.append("build-gate")
            assert nonce == "nonce"
            return ["gate", nonce, "--", *target]

        def release(stream: object, nonce: str) -> None:
            assert stream is proc.stdin
            assert nonce == "nonce"
            events.append("release-gate")

        monkeypatch.setattr(
            bootstrap, "os", SimpleNamespace(name="nt", environ=os.environ)
        )
        monkeypatch.setattr(bootstrap, "WindowsJob", _Job)
        monkeypatch.setattr(bootstrap, "release_nonce", lambda: "nonce")
        monkeypatch.setattr(bootstrap, "windows_gate_command", gate)
        monkeypatch.setattr(bootstrap, "release_windows_gate", release)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

        started = await bootstrap._start_installer_supervisor("--no-shell", {})

        assert started.process is proc
        assert started.windows_popen is popen
        assert started.windows_wait is process_wait
        assert started.assigned
        assert events == [
            "create-job",
            "build-gate",
            "start-gate",
            "assign-gate",
            "release-gate",
        ]

    async def test_startup_diagnostics_do_not_hide_control_frames(
        self, tmp_path, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        proc = _FakeProc([], 0)
        proc.stderr = _FakeProtocol(
            b"armed\nstarted 1\nsitecustomize warning without newline"
            + f"armed {_SUPERVISOR_NONCE}\n".encode(),
            b"started 2\ninstrumentation warning" * 10_000
            + f"started {_SUPERVISOR_NONCE} {proc.pid}\n".encode(),
        )
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )
        environment = bootstrap._installer_environment(tmp_path / "private")

        started = await bootstrap._start_installer_supervisor("--no-shell", environment)

        assert started.process is proc
        assert proc.stdin.written == (
            f"{_SUPERVISOR_NONCE}\nstart {_SUPERVISOR_NONCE}\n".encode()
        )

    async def test_cleanup_drains_output_before_process_collection(self):
        from linkedin_mcp_server import bootstrap

        proc = _FakeProc([b"buffered output\n"], 1)

        await bootstrap._stop_installer_once(cast(Any, proc))

        assert proc.stdout.exhausted
        assert proc.waited

    async def test_start_failure_reports_the_tracebacks_final_cause(self, monkeypatch):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        proc = _FakeProc([], 1)
        stderr = asyncio.StreamReader()
        stderr.feed_data(b"Traceback (most recent call last):\n")
        asyncio.get_running_loop().call_soon(
            stderr.feed_data, b"ProcessTreeError: nested job refused\n"
        )
        asyncio.get_running_loop().call_soon(stderr.feed_eof)
        proc.stderr = cast(Any, stderr)
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )

        with pytest.raises(BrowserSetupFailedError, match="nested job refused"):
            await bootstrap._start_installer_supervisor(
                "--no-shell", bootstrap._installer_environment(Path("private"))
            )

    async def test_start_failure_sanitizes_the_final_diagnostic(self, monkeypatch):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        proc = _FakeProc([], 1)
        stderr = asyncio.StreamReader()
        stderr.feed_data(
            b"sitecustomize: https://user:secret@example.test/x?token=secret\x1b[2J\n"
        )
        stderr.feed_eof()
        proc.stderr = cast(Any, stderr)
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )

        with pytest.raises(BrowserSetupFailedError) as excinfo:
            await bootstrap._start_installer_supervisor("--no-shell", {})

        reported = str(excinfo.value)
        assert "secret" not in reported
        assert "\x1b" not in reported
        assert "https://***@example.test/***" in reported

    async def _start_error(self, monkeypatch, *writes: bytes) -> str:
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        proc = _FakeProc([], 1)
        proc.stderr = _FakeProtocol(*writes)
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )

        with pytest.raises(BrowserSetupFailedError) as excinfo:
            await bootstrap._start_installer_supervisor("--no-shell", {})
        return str(excinfo.value)

    async def test_a_url_longer_than_the_retained_tail_keeps_its_secret(
        self, monkeypatch
    ):
        """The bound used to be applied to raw bytes, which is a cut like any
        other: it took the scheme away and left the query behind, where nothing
        recognises it. Redaction now happens before the trim, so the URL is an
        origin long before the tail is short enough to need one.
        """
        from linkedin_mcp_server import bootstrap

        long_url = "https://mirror.example/" + "a" * 9000 + "?key=s3cr3t"
        reported = await self._start_error(
            monkeypatch, f"sitecustomize: {long_url}\n".encode()
        )

        assert "s3cr3t" not in reported
        assert len(reported) < bootstrap._MAX_START_ERROR_CHARS
        assert "https://mirror.example/***" in reported

    async def test_an_authority_longer_than_the_tail_is_dropped_whole(
        self, monkeypatch
    ):
        """An authority that never ends cannot be reduced to an origin either.

        It stays whole in the buffer while it is read, so the trim is the first
        thing to reach it, and the head it would take is the part that names
        it. The credential arrives after that head is gone and has to go with
        it, scheme and all.
        """
        from linkedin_mcp_server import bootstrap

        reported = await self._start_error(
            monkeypatch,
            b"https://" + b"u" * (bootstrap._MAX_START_ERROR_CHARS + 100),
            b":s3cr3t@mirror.example/x\n",
        )

        assert "s3cr3t" not in reported
        assert "uuuu" not in reported
        assert bootstrap._OMITTED_RUN_HEAD in reported

    async def test_no_read_boundary_leaks_into_the_start_error(self, monkeypatch):
        """Every point a read can split the diagnostic at.

        The tail is trimmed between reads, so a URL open at a read boundary has
        to survive both the append that completes it and the trim that may run
        in between.
        """
        line = (
            "sitecustomize: https://ci-bot:s3cr3t@mirror.example/p?key=s3cr3t working\n"
        )
        filler = "warming up\n" * 800
        leaking = []
        for split in range(len(line) + 1):
            reported = await self._start_error(
                monkeypatch,
                (filler + line[:split]).encode(),
                line[split:].encode(),
            )
            if "s3cr3t" in reported:
                leaking.append(split)

        assert leaking == []

    @pytest.mark.parametrize(
        ("lines", "message", "written"),
        [
            (
                [],
                "did not become ready before its startup deadline",
                f"{_SUPERVISOR_NONCE}\n".encode(),
            ),
            (
                [f"armed {_SUPERVISOR_NONCE}\n".encode()],
                "did not start its worker before its startup deadline",
                f"{_SUPERVISOR_NONCE}\nstart {_SUPERVISOR_NONCE}\n".encode(),
            ),
            (
                [b"armed\n"],
                "did not become ready before its startup deadline",
                f"{_SUPERVISOR_NONCE}\n".encode(),
            ),
            (
                [
                    f"armed {_SUPERVISOR_NONCE}\n".encode(),
                    b"started 424242\n",
                ],
                "did not start its worker before its startup deadline",
                f"{_SUPERVISOR_NONCE}\nstart {_SUPERVISOR_NONCE}\n".encode(),
            ),
        ],
    )
    async def test_handshake_timeout_reports_the_missing_stage(
        self, monkeypatch, lines, message, written
    ):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        class _HangingProtocol(_FakeProtocol):
            async def read(self, n: int = -1) -> bytes:
                if self.lines:
                    return await super().read(n)
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        proc = _FakeProc([], 1)
        proc.stderr = _HangingProtocol(*lines)
        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )
        monkeypatch.setattr(bootstrap, "_INSTALLER_START_SECONDS", 0.01)

        with pytest.raises(BrowserSetupFailedError, match=message):
            await bootstrap._start_installer_supervisor(
                "--no-shell", bootstrap._installer_environment(Path("private"))
            )

        assert proc.stdin.written == written
        assert proc.stdin.closed
        assert proc.waited

    async def test_cancellation_waits_for_tree_cleanup(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        proc = _FakeProc([], 0)
        started = asyncio.Event()
        release = asyncio.Event()

        async def delayed_cleanup(*args: object, **kwargs: object) -> None:
            started.set()
            await release.wait()

        monkeypatch.setattr(bootstrap, "_stop_installer_once", delayed_cleanup)
        stopping = asyncio.create_task(bootstrap._stop_installer(cast(Any, proc)))
        await started.wait()
        stopping.cancel()
        await asyncio.sleep(0)

        assert not stopping.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stopping

    async def test_hard_fallback_stops_the_unresponsive_supervisor(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        proc = _FakeProc([], 0)
        exits = iter([False, True])

        async def fake_exit(*args: object, **kwargs: object) -> bool:
            return next(exits)

        monkeypatch.setattr(bootstrap, "_wait_for_installer_exit", fake_exit)

        await bootstrap._stop_installer(cast(Any, proc))

        assert proc.stdin.closed
        assert proc.killed

    async def test_cleanup_stages_share_one_stop_deadline(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        proc = _FakeProc([], 0)
        remaining_values = iter([10.0, 6.0, 4.0, 0.0, 0.0])
        observed_remaining: list[float] = []
        exit_waits: list[float] = []

        def remaining(_deadline: float) -> float:
            value = next(remaining_values)
            observed_remaining.append(value)
            return value

        async def never_exits(_proc: object, seconds: float) -> bool:
            exit_waits.append(seconds)
            return False

        async def blocked_drain(_stream: object) -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(bootstrap, "_installer_stop_remaining", remaining)
        monkeypatch.setattr(bootstrap, "_wait_for_installer_exit", never_exits)
        monkeypatch.setattr(bootstrap, "_drain_installer_stream", blocked_drain)

        await bootstrap._stop_installer_once(cast(Any, proc))

        assert proc.killed
        assert exit_waits == [6.0, 4.0]
        assert observed_remaining == [10.0, 6.0, 4.0, 0.0, 0.0]

    async def test_assigned_windows_cleanup_releases_handle_before_drain_off_loop(
        self,
    ):
        from linkedin_mcp_server import bootstrap

        main_thread = threading.get_ident()
        worker_threads: list[int] = []
        events: list[str] = []
        proc = _FakeProc([], 0)

        class _Handle:
            closed = False

            def Close(self) -> None:
                self.closed = True
                events.append("release-handle")

        popen = SimpleNamespace(returncode=None, _handle=_Handle())

        class _Job:
            closed = False

            def terminate(self) -> None:
                worker_threads.append(threading.get_ident())
                events.append("terminate-job")
                proc.returncode = 1
                popen.returncode = 1
                proc.stdout.exhausted = True

            def release_popen_handle(self, process: object) -> None:
                assert process is popen
                assert popen.returncode == 1
                popen._handle.Close()

            def wait_until_empty(self, *, timeout: float) -> None:
                worker_threads.append(threading.get_ident())
                assert timeout <= bootstrap._INSTALLER_STOP_SECONDS
                assert popen._handle.closed
                events.append("drain-job")
                self.closed = True

        process_wait = SimpleNamespace(
            done=lambda: True,
            _registered=False,
            _wait_handle=None,
            _event=None,
            _event_fut=None,
            _proactor=None,
            _ov=None,
        )
        job = _Job()
        managed = bootstrap._InstallerProcess(
            cast(Any, proc),
            windows_job=cast(Any, job),
            windows_popen=cast(Any, popen),
            windows_wait=cast(Any, process_wait),
            assigned=True,
        )

        await bootstrap._stop_installer_once(managed)

        assert worker_threads and all(
            thread != main_thread for thread in worker_threads
        )
        assert events == ["terminate-job", "release-handle", "drain-job"]
        assert not managed.assigned
        assert managed.windows_popen is None

    async def test_waits_for_the_unregister_completion_callback(self):
        from linkedin_mcp_server import bootstrap

        event_fut = asyncio.get_running_loop().create_future()

        class _ProcessWait:
            _registered = False
            _wait_handle = None
            _event = object()
            _proactor = object()
            _ov = object()

            def __init__(self) -> None:
                self._event_fut = event_fut

            def done(self) -> bool:
                return True

            def _unregister_wait_cb(self, _future: object) -> None:
                self._event = None
                self._event_fut = None
                self._proactor = None
                self._ov = None

        process_wait = _ProcessWait()
        cast(Any, event_fut)._done_callback = process_wait._unregister_wait_cb
        event_fut.set_result(True)
        asyncio.get_running_loop().call_soon(
            process_wait._unregister_wait_cb, event_fut
        )
        managed = bootstrap._InstallerProcess(
            cast(Any, _FakeProc([], 0)), windows_wait=cast(Any, process_wait)
        )

        deadline = asyncio.get_running_loop().time() + 1.0
        await bootstrap._prove_windows_wait_unregistered(managed, deadline)

        assert process_wait._event_fut is None
        assert process_wait._proactor is None
        assert process_wait._ov is None

    async def test_missing_unregister_proof_keeps_the_popen_handle(self):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.process_tree import ProcessTreeError

        proc = _FakeProc([], 0)
        popen = SimpleNamespace(returncode=None, _handle=object())
        released = False

        class _Job:
            def terminate(self) -> None:
                proc.returncode = 1
                popen.returncode = 1

            def release_popen_handle(self, _process: object) -> None:
                nonlocal released
                released = True

        managed = bootstrap._InstallerProcess(
            cast(Any, proc),
            windows_job=cast(Any, _Job()),
            windows_popen=cast(Any, popen),
            assigned=True,
        )
        deadline = asyncio.get_running_loop().time() + 1.0

        with pytest.raises(ProcessTreeError, match="was not retained"):
            await bootstrap._cleanup_assigned_windows_job_once(managed, deadline)

        assert not released
        assert managed.assigned
        assert managed.windows_popen is popen

    async def test_termination_failure_closes_the_kill_on_close_job(self):
        from linkedin_mcp_server import bootstrap

        proc = _FakeProc([], 0)

        class _Job:
            closed = False

            def terminate(self) -> None:
                raise RuntimeError("termination failed")

            def close(self) -> None:
                self.closed = True

        job = _Job()
        managed = bootstrap._InstallerProcess(
            cast(Any, proc),
            windows_job=cast(Any, job),
            windows_popen=cast(Any, object()),
            assigned=True,
        )

        with pytest.raises(RuntimeError, match="termination failed"):
            await bootstrap._cleanup_assigned_windows_job_once(
                managed, asyncio.get_running_loop().time() + 1.0
            )

        assert job.closed
        assert not managed.assigned

    @pytest.mark.skipif(os.name != "nt", reason="requires the CPython proactor")
    async def test_real_cpython_wait_unregister_contract(self):
        from linkedin_mcp_server import bootstrap

        raw_proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys; sys.stdin.buffer.read(1)",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        transport = cast(Any, raw_proc)._transport
        popen = transport.get_extra_info("subprocess")
        process_wait = bootstrap._capture_windows_process_wait(raw_proc, popen)
        wait_contract = cast(Any, process_wait)
        managed = bootstrap._InstallerProcess(
            raw_proc,
            windows_popen=popen,
            windows_wait=process_wait,
        )
        assert type(process_wait).__name__ == "_WaitHandleFuture"
        assert wait_contract._event_fut is None
        assert wait_contract._proactor is transport._loop._proactor
        assert wait_contract._ov is not None

        await bootstrap._close_installer_lease(managed)
        await bootstrap._wait_for_direct_process_exit(managed)
        deadline = asyncio.get_running_loop().time() + 5.0
        await bootstrap._prove_windows_wait_unregistered(managed, deadline)
        await raw_proc.wait()

        assert wait_contract._event_fut is None
        assert wait_contract._proactor is None
        assert wait_contract._ov is None

    async def test_unassigned_windows_cleanup_closes_the_gate_and_job(self):
        from linkedin_mcp_server import bootstrap

        proc = _FakeProc([], 0)

        class _Job:
            closed = False

            def close(self) -> None:
                self.closed = True

        job = _Job()
        managed = bootstrap._InstallerProcess(
            cast(Any, proc), windows_job=cast(Any, job), assigned=False
        )

        await bootstrap._stop_installer_once(managed)

        assert proc.stdin.closed
        assert proc.waited
        assert job.closed


class TestPatchrightInstallStreaming:
    """The install streams its output as it arrives rather than after it ends."""

    def _patch_proc(
        self, monkeypatch, chunks: list[bytes], returncode: int
    ) -> "_Spawned":
        """Patch the subprocess and record the exec kwargs and the fake process."""
        spawned = _Spawned()

        async def fake_start(extra_arg: str, environment: dict[str, str]) -> _FakeProc:
            spawned.kwargs["extra_arg"] = extra_arg
            spawned.kwargs["environment"] = environment
            spawned.proc = _FakeProc(chunks, returncode)
            return spawned.proc

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._start_installer_supervisor", fake_start
        )
        return spawned

    async def test_second_cancel_before_task_settlement_cannot_skip_cleanup(
        self, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        installer_started = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_completed = asyncio.Event()
        output_cancelled = asyncio.Event()
        release = asyncio.Event()

        class _BlockedOutput:
            async def read(self, _n: int = -1) -> bytes:
                try:
                    await asyncio.Event().wait()
                    raise AssertionError("unreachable")
                except asyncio.CancelledError:
                    output_cancelled.set()
                    await release.wait()
                    raise

        class _BlockedProc(_FakeProc):
            def __init__(self) -> None:
                super().__init__([], 0)
                self.stdout = cast(Any, _BlockedOutput())

            async def wait(self) -> int:
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        proc = _BlockedProc()

        async def fake_start(
            _extra_arg: str, _environment: dict[str, str]
        ) -> _BlockedProc:
            installer_started.set()
            return proc

        async def delayed_cleanup(_proc: object) -> None:
            cleanup_started.set()
            await release.wait()
            cleanup_completed.set()

        monkeypatch.setattr(bootstrap, "_start_installer_supervisor", fake_start)
        monkeypatch.setattr(bootstrap, "_stop_installer_once", delayed_cleanup)

        installing = asyncio.create_task(
            bootstrap._run_patchright_install("--no-shell")
        )
        await asyncio.wait_for(installer_started.wait(), timeout=1.0)
        installing.cancel()

        cleanup_seen = asyncio.create_task(cleanup_started.wait())
        output_seen = asyncio.create_task(output_cancelled.wait())
        done, pending = await asyncio.wait(
            {cleanup_seen, output_seen},
            timeout=1.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert done, "the cancellation path did not reach its first pre-cleanup await"
        installing.cancel()
        release.set()
        for waiter in pending:
            waiter.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        with pytest.raises(asyncio.CancelledError):
            await installing
        assert cleanup_completed.is_set()

    async def test_slow_temporary_root_creation_cannot_block_timeout(
        self, tmp_path, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        blocked = threading.Event()
        release = threading.Event()
        created = threading.Event()

        private = tmp_path / "private"

        def slow_temporary_root(*, prefix: str, dir: Path) -> str:
            assert prefix == "linkedin-mcp-installer-"
            assert dir == Path(tempfile.gettempdir()).resolve()
            blocked.set()
            release.wait()
            private.mkdir()
            created.set()
            return str(private)

        monkeypatch.setattr(bootstrap.tempfile, "mkdtemp", slow_temporary_root)
        self._patch_proc(monkeypatch, [], 0)
        fallback = threading.Timer(0.2, release.set)
        fallback.start()
        started = asyncio.get_running_loop().time()
        try:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.01):
                    await bootstrap._run_patchright_install("--no-shell")
        finally:
            release.set()
            fallback.cancel()

        assert blocked.is_set()
        assert asyncio.get_running_loop().time() - started < 0.1
        assert await asyncio.to_thread(created.wait, 1.0)
        for _ in range(100):
            if not private.exists():
                break
            await asyncio.sleep(0.01)
        assert not private.exists(), "the canceled thread discarded its pinned root"

    async def test_cold_registry_read_cannot_block_timeout(self, tmp_path, monkeypatch):
        from linkedin_mcp_server import bootstrap

        blocked = threading.Event()
        release = threading.Event()

        def slow_targets() -> dict[str, str]:
            blocked.set()
            release.wait()
            return {"chromium-": "1217"}

        monkeypatch.setattr(bootstrap, "_patchright_install_targets", slow_targets)
        monkeypatch.setattr(
            bootstrap,
            "_create_installer_temporary_root",
            lambda: bootstrap._InstallerTemporaryRoot(tmp_path / "private", 0, 0, None),
        )
        self._patch_proc(monkeypatch, [], 0)
        fallback = threading.Timer(0.2, release.set)
        fallback.start()
        started = asyncio.get_running_loop().time()
        try:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.01):
                    await bootstrap._run_patchright_install("--no-shell")
        finally:
            release.set()
            fallback.cancel()

        assert blocked.is_set()
        assert asyncio.get_running_loop().time() - started < 0.1

    @pytest.mark.parametrize("release", [(3, 12, 0), (3, 12, 3), (3, 13, 15)])
    def test_windows_creates_its_own_acl_on_every_supported_python(
        self, tmp_path, monkeypatch, release
    ):
        """No Python version floor applies to the installer's temporary root.

        ``tempfile.mkdtemp`` gained its owner-only Windows behaviour in 3.12.4,
        and this path does not call it: ``create_owner_only_directory`` names
        the owner itself and hands ``CreateDirectoryW`` a protected DACL, so
        3.12.0 gets exactly the same directory 3.13 does. The releases below the
        old floor are the point of the sweep.
        """
        from linkedin_mcp_server import bootstrap, windows_acl

        temporary_root = tmp_path / "private"
        temporary_root.mkdir()
        entry = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700, st_dev=1, st_ino=2, st_file_attributes=0
        )
        created: list[Path] = []
        monkeypatch.setattr(bootstrap.os, "name", "nt")
        monkeypatch.setattr(bootstrap.sys, "version_info", release)
        monkeypatch.setattr(bootstrap, "_installer_temporary_parent", lambda: tmp_path)
        monkeypatch.setattr(
            bootstrap.tempfile,
            "mkdtemp",
            lambda **_kwargs: pytest.fail("the Windows path must not use mkdtemp"),
        )

        def create(parent: Path, *, prefix: str) -> tuple[Path, object]:
            created.append(parent)
            return temporary_root, object()

        monkeypatch.setattr(windows_acl, "create_owner_only_directory", create)
        monkeypatch.setattr(bootstrap.Path, "lstat", lambda _path: entry)

        root = bootstrap._create_installer_temporary_root()

        assert root.path == temporary_root
        assert created == [tmp_path]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes are required")
    def test_replaceable_temporary_parent_is_refused_before_creation(
        self, tmp_path, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.private_state import PrivateStateError

        shared = tmp_path / "shared"
        shared.mkdir(mode=0o777)
        shared.chmod(0o777)
        monkeypatch.setattr(bootstrap.tempfile, "gettempdir", lambda: str(shared))
        monkeypatch.setattr(
            bootstrap.tempfile,
            "mkdtemp",
            lambda **_kwargs: pytest.fail("a replaceable root was created"),
        )

        with pytest.raises(PrivateStateError, match="can be replaced"):
            bootstrap._create_installer_temporary_root()

    def test_windows_reparse_temporary_root_is_refused_after_pinning(
        self, tmp_path, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap, windows_acl
        from linkedin_mcp_server.private_state import PrivateStateError

        temporary_root = tmp_path / "private"
        temporary_root.mkdir()
        entry = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700,
            st_dev=1,
            st_ino=2,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
        )
        closed: list[object] = []
        pin = object()
        monkeypatch.setattr(bootstrap.os, "name", "nt")
        monkeypatch.setattr(bootstrap, "_installer_temporary_parent", lambda: tmp_path)
        monkeypatch.setattr(
            windows_acl,
            "create_owner_only_directory",
            lambda *_args, **_kwargs: (temporary_root, pin),
        )
        monkeypatch.setattr(bootstrap.Path, "lstat", lambda _path: entry)
        monkeypatch.setattr(
            windows_acl, "close_directory_pin", lambda handle: closed.append(handle)
        )

        with pytest.raises(PrivateStateError, match="reparse point"):
            bootstrap._create_installer_temporary_root()

        assert closed == [pin]

    def test_mac_extended_acl_on_temp_parent_is_refused(self, tmp_path, monkeypatch):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.private_state import PrivateStateError

        parent = tmp_path / "temporary"
        parent.mkdir()
        checked: list[Path] = []
        monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
        monkeypatch.setattr(bootstrap.tempfile, "gettempdir", lambda: str(parent))

        def refuse(path: Path) -> None:
            checked.append(path)
            if path == parent:
                raise PrivateStateError("access control list")

        monkeypatch.setattr(bootstrap, "verify_no_extended_acl", refuse)
        monkeypatch.setattr(
            bootstrap.tempfile,
            "mkdtemp",
            lambda **_kwargs: pytest.fail("an ACL-replaceable root was created"),
        )

        with pytest.raises(PrivateStateError, match="access control list"):
            bootstrap._create_installer_temporary_root()

        assert checked == [parent]

    def test_installer_temporary_root_is_hardened_before_use(
        self, tmp_path, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        temporary_root = tmp_path / "private"
        events: list[tuple[str, Path]] = []

        def make_temporary_root(*, prefix: str, dir: Path) -> str:
            assert prefix == "linkedin-mcp-installer-"
            temporary_root.mkdir()
            events.append(("create", temporary_root))
            return str(temporary_root)

        def harden(path: Path) -> None:
            assert path == temporary_root
            events.append(("harden", path))

        monkeypatch.setattr(bootstrap.tempfile, "mkdtemp", make_temporary_root)
        monkeypatch.setattr(bootstrap, "harden_created_directory", harden)

        created = bootstrap._create_installer_temporary_root()

        assert created.path == temporary_root
        assert (created.device, created.inode) == (
            temporary_root.stat().st_dev,
            temporary_root.stat().st_ino,
        )
        assert events == [("create", temporary_root), ("harden", temporary_root)]

    def test_installer_temporary_root_is_removed_when_hardening_fails(
        self, tmp_path, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        temporary_root = tmp_path / "private"

        def make_temporary_root(*, prefix: str, dir: Path) -> str:
            assert prefix == "linkedin-mcp-installer-"
            temporary_root.mkdir()
            return str(temporary_root)

        def refuse(_path: Path) -> None:
            raise RuntimeError("ACLs unavailable")

        monkeypatch.setattr(bootstrap.tempfile, "mkdtemp", make_temporary_root)
        monkeypatch.setattr(bootstrap, "harden_created_directory", refuse)

        with pytest.raises(RuntimeError, match="ACLs unavailable"):
            bootstrap._create_installer_temporary_root()
        assert not temporary_root.exists()

    @pytest.mark.skipif(os.name == "nt", reason="the Windows pin prevents replacement")
    def test_cleanup_refuses_a_replaced_temporary_root(self, tmp_path, monkeypatch):
        from linkedin_mcp_server import bootstrap

        temporary_root = tmp_path / "private"

        def make_temporary_root(*, prefix: str, dir: Path) -> str:
            assert prefix == "linkedin-mcp-installer-"
            temporary_root.mkdir()
            return str(temporary_root)

        monkeypatch.setattr(bootstrap.tempfile, "mkdtemp", make_temporary_root)
        created = bootstrap._create_installer_temporary_root()
        moved = tmp_path / "original"
        temporary_root.rename(moved)
        temporary_root.mkdir()
        victim = temporary_root / "keep"
        victim.write_text("unrelated")

        bootstrap._remove_installer_temporary_root(created)

        assert victim.read_text() == "unrelated"
        assert moved.is_dir()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX descriptors are required")
    def test_cleanup_thread_start_failure_closes_the_root_pin(
        self, tmp_path, monkeypatch, caplog
    ):
        from linkedin_mcp_server import bootstrap

        temporary_root = tmp_path / "private"
        temporary_root.mkdir()
        details = temporary_root.stat()
        pin = os.open(temporary_root, os.O_RDONLY | os.O_DIRECTORY)
        created = bootstrap._InstallerTemporaryRoot(
            temporary_root, details.st_dev, details.st_ino, pin
        )

        class RefusingThread:
            def __init__(self, **_kwargs):
                pass

            def start(self) -> None:
                raise RuntimeError("thread limit")

        monkeypatch.setattr(bootstrap.threading, "Thread", RefusingThread)
        with caplog.at_level(logging.WARNING, logger="linkedin_mcp_server.bootstrap"):
            bootstrap._start_installer_temporary_root_cleanup(created)

        with pytest.raises(OSError):
            os.fstat(pin)
        assert temporary_root.is_dir()
        assert "Could not start browser installer temp cleanup" in caplog.text

    @pytest.mark.skipif(os.name == "nt", reason="dir_fd anchors POSIX cleanup")
    def test_cleanup_stays_on_the_open_root_during_path_replacement(
        self, tmp_path, monkeypatch
    ):
        import shutil

        from linkedin_mcp_server import bootstrap

        temporary_root = tmp_path / "private"
        temporary_root.mkdir()
        (temporary_root / "download").write_text("partial")
        details = temporary_root.stat()
        pin = os.open(temporary_root, os.O_RDONLY | os.O_DIRECTORY)
        created = bootstrap._InstallerTemporaryRoot(
            temporary_root, details.st_dev, details.st_ino, pin
        )
        moved = tmp_path / "original"
        victim = temporary_root / "keep"
        real_rmtree = shutil.rmtree

        def swap_then_remove(
            path: str,
            *,
            dir_fd: int | None = None,
            onexc: Any = None,
        ) -> None:
            assert path == "."
            assert dir_fd is not None
            temporary_root.rename(moved)
            temporary_root.mkdir()
            victim.write_text("unrelated")
            real_rmtree(path, dir_fd=dir_fd, onexc=onexc)

        monkeypatch.setattr(bootstrap.shutil, "rmtree", swap_then_remove)

        bootstrap._remove_installer_temporary_root(created)

        assert victim.read_text() == "unrelated"
        assert list(moved.iterdir()) == []

    async def test_private_temp_environment_is_removed_after_tree_exit(
        self, tmp_path, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        temporary_root = tmp_path / "private"

        def make_temporary_root(*, prefix: str, dir: Path) -> str:
            assert prefix == "linkedin-mcp-installer-"
            temporary_root.mkdir()
            return str(temporary_root)

        monkeypatch.setattr(bootstrap.tempfile, "mkdtemp", make_temporary_root)
        spawned = self._patch_proc(monkeypatch, [], 0)
        remove = bootstrap._remove_installer_temporary_root
        removed_after_exit: list[bool] = []

        def checked_remove(root: bootstrap._InstallerTemporaryRoot) -> None:
            removed_after_exit.append(spawned.proc is not None and spawned.proc.waited)
            remove(root)

        monkeypatch.setattr(
            bootstrap, "_remove_installer_temporary_root", checked_remove
        )

        await bootstrap._run_patchright_install("--no-shell")

        async with asyncio.timeout(1):
            while temporary_root.exists():
                await asyncio.sleep(0.001)
        environment = cast(dict[str, str], spawned.kwargs["environment"])
        assert {environment[name] for name in ("TMPDIR", "TMP", "TEMP")} == {
            str(temporary_root)
        }
        assert removed_after_exit == [True]

    async def test_the_callback_gets_every_line_in_order(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        spawned = self._patch_proc(
            monkeypatch,
            [b"Downloading 10%\n", b"\n", b"Downloading 100%\n"],
            0,
        )
        seen: list[str] = []
        await bootstrap._run_patchright_install("--no-shell", line_callback=seen.append)

        # The supervisor exposes the target's already-combined stdout. The fake
        # wait also asserts the pipe was drained before process collection.
        assert spawned.kwargs["extra_arg"] == "--no-shell"
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

        async def fake_start(extra_arg: str, _environment: dict[str, str]):
            proc = _FakeProc([payload.encode() + b"\n"], 0)
            return proc

        monkeypatch.setattr(bootstrap, "_start_installer_supervisor", fake_start)
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
        """Userinfo in a download URL reaches neither the terminal nor the log."""
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
        assert "https://***@mirror.example/***" in everything

    @pytest.mark.parametrize(
        "variable",
        [
            "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST",
            "npm_config_playwright_chromium_download_host",
            "npm_package_config_playwright_chromium_download_host",
            "PLAYWRIGHT_DOWNLOAD_HOST",
            "npm_config_playwright_download_host",
            "npm_package_config_playwright_download_host",
        ],
    )
    async def test_mirror_path_credentials_are_not_reported(
        self, monkeypatch, caplog, variable: str
    ):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        token = "private-access-token"
        host = f"https://mirror.example/{token}"
        url = f"{host}/builds/chromium.zip"
        monkeypatch.setenv(variable, host)
        self._patch_proc(monkeypatch, [f"Downloading from {url}\n".encode()], 1)

        with caplog.at_level(logging.DEBUG, logger="linkedin_mcp_server.bootstrap"):
            with pytest.raises(BrowserSetupFailedError) as excinfo:
                await bootstrap._run_patchright_install("--no-shell")

        everything = str(excinfo.value) + " ".join(r.message for r in caplog.records)
        assert token not in everything
        assert "https://mirror.example/***" in everything

    @pytest.mark.parametrize("configured", ["", "https://mirror.example"])
    async def test_a_redirect_target_cannot_carry_a_path_credential_out(
        self, monkeypatch, caplog, configured: str
    ):
        """The URL patchright reports is the one it ended on, not the one set.

        Its download client follows redirects and interpolates the final
        location into the timeout it raises, so a mirror can answer 302 and put
        a capability nobody configured into the debug log and into the retained
        failure that becomes ``BrowserSetupFailedError``. Redacting the
        configured prefix never reaches that URL: it runs with the variable both
        unset and set to an origin the redirect leaves.
        """
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        if configured:
            monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST", configured)
        else:
            monkeypatch.delenv("PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST", raising=False)
        redirected = "https://cdn.example/another-bearer-token/chromium.zip"
        self._patch_proc(
            monkeypatch,
            [
                f"Downloading Chromium from {redirected}\n".encode(),
                f"Timeout 30000ms exceeded while downloading {redirected}\n".encode(),
            ],
            1,
        )
        seen: list[str] = []

        with caplog.at_level(logging.DEBUG, logger="linkedin_mcp_server.bootstrap"):
            with pytest.raises(BrowserSetupFailedError) as excinfo:
                await bootstrap._run_patchright_install("--no-shell")
            with pytest.raises(BrowserSetupFailedError):
                await bootstrap._run_patchright_install(
                    "--no-shell", line_callback=seen.append
                )

        everything = (
            str(excinfo.value)
            + " ".join(r.message for r in caplog.records)
            + " ".join(seen)
        )
        assert "another-bearer-token" not in everything
        assert "https://cdn.example/***" in everything

    async def test_a_redirect_target_split_across_reads_is_still_redacted(
        self, monkeypatch
    ):
        """Sanitising a growing buffer must not print the far half of a URL.

        Each read redacts the whole buffer again, so the redaction has to be
        stable under append: a version that drops the delimiter along with the
        path lets the next read's token land against the hostname.
        """
        from linkedin_mcp_server import bootstrap

        url = "https://cdn.example/another-bearer-token/chromium.zip"
        line = f"Downloading Chromium from {url}\n".encode()
        leaked: list[int] = []

        for cut in range(len(line) + 1):
            self._patch_proc(monkeypatch, [line[:cut], line[cut:]], 0)
            seen: list[str] = []
            await bootstrap._run_patchright_install(
                "--no-shell", line_callback=seen.append
            )
            if "another-bearer-token" in "".join(seen):
                leaked.append(cut)

        assert leaked == []

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
        """Any escape from the read loop must close and reap the supervisor.

        The supervisor owns the Python wrapper, Node CLI and downloader, so the
        lease cannot finish while only the direct child has stopped.
        """
        from linkedin_mcp_server import bootstrap

        spawned = self._patch_proc(monkeypatch, [b"downloading\n"], 0)

        def explode(_line: str) -> None:
            raise RuntimeError("consumer died")

        with pytest.raises(RuntimeError, match="consumer died"):
            await bootstrap._run_patchright_install("--no-shell", line_callback=explode)

        assert spawned.proc is not None
        assert spawned.proc.stdin.closed
        assert spawned.proc.waited

    async def test_cancellation_stops_the_installer(self, tmp_path, monkeypatch):
        from linkedin_mcp_server import bootstrap

        temporary_root = tmp_path / "private"

        def make_temporary_root(*, prefix: str, dir: Path) -> str:
            assert prefix == "linkedin-mcp-installer-"
            temporary_root.mkdir()
            return str(temporary_root)

        monkeypatch.setattr(bootstrap.tempfile, "mkdtemp", make_temporary_root)
        spawned = self._patch_proc(monkeypatch, [b"downloading\n"], 0)

        def cancel(_line: str) -> None:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await bootstrap._run_patchright_install("--no-shell", line_callback=cancel)

        assert spawned.proc is not None
        assert spawned.proc.stdin.closed
        assert spawned.proc.waited
        async with asyncio.timeout(1):
            while temporary_root.exists():
                await asyncio.sleep(0.001)

    async def test_blocked_temp_cleanup_does_not_hold_cancellation(
        self, tmp_path, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        temporary_root = tmp_path / "private"

        def make_temporary_root(*, prefix: str, dir: Path) -> str:
            assert prefix == "linkedin-mcp-installer-"
            temporary_root.mkdir()
            return str(temporary_root)

        lines_started = asyncio.Event()

        async def hanging_lines(_stream: object):
            lines_started.set()
            await asyncio.Event().wait()
            if False:  # pragma: no cover - makes this an async generator
                yield ""

        cleanup_started = threading.Event()
        cleanup_release = threading.Event()
        cleanup_daemon: list[bool] = []
        remove = bootstrap._remove_installer_temporary_root

        def blocked_remove(root: bootstrap._InstallerTemporaryRoot) -> None:
            cleanup_daemon.append(threading.current_thread().daemon)
            cleanup_started.set()
            cleanup_release.wait(timeout=5)
            remove(root)

        monkeypatch.setattr(bootstrap.tempfile, "mkdtemp", make_temporary_root)
        monkeypatch.setattr(bootstrap, "_installer_lines", hanging_lines)
        monkeypatch.setattr(
            bootstrap, "_remove_installer_temporary_root", blocked_remove
        )
        self._patch_proc(monkeypatch, [], 0)

        install = asyncio.create_task(bootstrap._run_patchright_install("--no-shell"))
        await asyncio.wait_for(lines_started.wait(), timeout=1)
        install.cancel()
        done, _pending = await asyncio.wait({install}, timeout=1)
        completed_promptly = install in done
        try:
            async with asyncio.timeout(1):
                while not cleanup_started.is_set():
                    await asyncio.sleep(0.001)
            assert cleanup_daemon == [True]
            assert temporary_root.exists()
        finally:
            cleanup_release.set()
            if not install.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await install

        assert completed_promptly
        with pytest.raises(asyncio.CancelledError):
            install.result()
        async with asyncio.timeout(1):
            while temporary_root.exists():
                await asyncio.sleep(0.001)

    async def test_windows_normal_completion_uses_exit_callback_before_pipe_eof(
        self, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        events: list[str] = []
        pipe_eof = asyncio.Event()

        class _Handle:
            closed = False

            def Close(self) -> None:
                self.closed = True
                events.append("release-handle")

        popen = SimpleNamespace(returncode=None, _handle=_Handle())

        class _Job:
            closed = False
            terminated = False

            def terminate(self) -> None:
                events.append("terminate-job")
                self.terminated = True

            def release_popen_handle(self, process: object) -> None:
                assert process is popen
                assert popen.returncode == 0
                popen._handle.Close()

            def wait_until_empty(self, *, timeout: float) -> None:
                assert timeout <= bootstrap._INSTALLER_STOP_SECONDS
                assert popen._handle.closed
                events.append("drain-job")
                self.closed = True

        job = _Job()

        class _Process:
            pid = 424242
            stdin = _FakeStdin()
            stderr = _FakeProtocol()
            returncode: int | None = None

            def __init__(self) -> None:
                self.stdout = _HeldStream(self)
                self.wait_calls = 0

            async def wait(self) -> int:
                self.wait_calls += 1
                await pipe_eof.wait()
                return 0

            def kill(self) -> None:  # pragma: no cover - success path
                raise AssertionError("normal completion killed the gate")

        class _HeldStream:
            def __init__(self, process: _Process) -> None:
                self.process = process
                self.sent = False

            async def read(self, _n: int = -1) -> bytes:
                if not self.sent:
                    self.sent = True
                    self.process.returncode = 0
                    popen.returncode = 0
                    events.append("proactor-exit")
                    return b"complete\n"
                while not job.terminated:
                    await asyncio.sleep(0)
                events.append("stdout-eof")
                pipe_eof.set()
                return b""

        process = _Process()
        managed = bootstrap._InstallerProcess(
            cast(Any, process),
            windows_job=cast(Any, job),
            windows_popen=cast(Any, popen),
            windows_wait=cast(Any, _completed_windows_wait()),
            assigned=True,
        )
        monkeypatch.setattr(
            bootstrap, "_start_installer_supervisor", AsyncMock(return_value=managed)
        )
        process_wait = asyncio.create_task(process.wait())
        await asyncio.sleep(0)
        assert not process_wait.done()

        await asyncio.wait_for(
            bootstrap._run_patchright_install("--no-shell"), timeout=1
        )

        assert await process_wait == 0
        assert process.wait_calls == 1
        assert events.index("proactor-exit") < events.index("terminate-job")
        assert events.index("terminate-job") < events.index("stdout-eof")
        assert events.index("release-handle") < events.index("drain-job")
        assert not managed.assigned

    async def test_windows_callback_failure_drains_the_job(self, monkeypatch):
        from linkedin_mcp_server import bootstrap

        events: list[str] = []

        class _Handle:
            closed = False

            def Close(self) -> None:
                self.closed = True
                events.append("release-handle")

        popen = SimpleNamespace(returncode=None, _handle=_Handle())

        class _Process:
            pid = 424242
            stdin = _FakeStdin()
            stdout = _FakeStdout([b"downloading\n"])
            stderr = _FakeProtocol()
            returncode: int | None = None

            async def wait(self) -> int:  # pragma: no cover - Windows uses callback
                raise AssertionError("cleanup waited for pipe EOF before termination")

            def kill(self) -> None:  # pragma: no cover - assigned Job owns cleanup
                raise AssertionError("callback cleanup killed only the gate")

        process = _Process()

        class _Job:
            closed = False

            def terminate(self) -> None:
                events.append("terminate-job")
                process.returncode = 1
                popen.returncode = 1
                process.stdout.exhausted = True

            def release_popen_handle(self, child: object) -> None:
                assert child is popen
                popen._handle.Close()

            def wait_until_empty(self, *, timeout: float) -> None:
                assert timeout <= bootstrap._INSTALLER_STOP_SECONDS
                assert popen._handle.closed
                events.append("drain-job")
                self.closed = True

        job = _Job()
        managed = bootstrap._InstallerProcess(
            cast(Any, process),
            windows_job=cast(Any, job),
            windows_popen=cast(Any, popen),
            windows_wait=cast(Any, _completed_windows_wait()),
            assigned=True,
        )
        monkeypatch.setattr(
            bootstrap, "_start_installer_supervisor", AsyncMock(return_value=managed)
        )

        def explode(_line: str) -> None:
            raise RuntimeError("consumer died")

        with pytest.raises(RuntimeError, match="consumer died"):
            await bootstrap._run_patchright_install("--no-shell", line_callback=explode)

        assert events == ["terminate-job", "release-handle", "drain-job"]
        assert not managed.assigned

    async def test_windows_cancellation_during_normal_drain_reuses_one_cleanup(
        self, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap

        events: list[str] = []
        drain_started = threading.Event()
        release_drain = threading.Event()

        class _Handle:
            closed = False

            def Close(self) -> None:
                self.closed = True
                events.append("release-handle")

        popen = SimpleNamespace(returncode=0, _handle=_Handle())

        class _Job:
            closed = False

            def terminate(self) -> None:
                events.append("terminate-job")

            def release_popen_handle(self, child: object) -> None:
                assert child is popen
                popen._handle.Close()

            def wait_until_empty(self, *, timeout: float) -> None:
                assert timeout <= bootstrap._INSTALLER_STOP_SECONDS
                events.append("drain-start")
                drain_started.set()
                assert release_drain.wait(timeout)
                events.append("drain-finish")
                self.closed = True

        proc = _FakeProc([], 0)
        proc.returncode = 0
        managed = bootstrap._InstallerProcess(
            cast(Any, proc),
            windows_job=cast(Any, _Job()),
            windows_popen=cast(Any, popen),
            windows_wait=cast(Any, _completed_windows_wait()),
            assigned=True,
        )
        monkeypatch.setattr(
            bootstrap, "_start_installer_supervisor", AsyncMock(return_value=managed)
        )

        install = asyncio.create_task(bootstrap._run_patchright_install("--no-shell"))
        assert await asyncio.to_thread(drain_started.wait, 1)
        cleanup = managed.windows_cleanup
        assert cleanup is not None
        install.cancel()
        await asyncio.sleep(0)

        assert not install.done()
        assert managed.windows_cleanup is cleanup
        assert events == ["terminate-job", "release-handle", "drain-start"]

        release_drain.set()
        with pytest.raises(asyncio.CancelledError):
            await install

        assert managed.windows_cleanup is cleanup
        assert events == [
            "terminate-job",
            "release-handle",
            "drain-start",
            "drain-finish",
        ]
        assert not managed.assigned

    async def test_windows_drain_failure_is_not_swallowed_or_retried(self, monkeypatch):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.process_tree import ProcessTreeError

        events: list[str] = []

        class _Handle:
            closed = False

            def Close(self) -> None:
                self.closed = True
                events.append("release-handle")

        popen = SimpleNamespace(returncode=0, _handle=_Handle())

        class _Job:
            closed = False

            def terminate(self) -> None:
                events.append("terminate-job")

            def release_popen_handle(self, child: object) -> None:
                assert child is popen
                popen._handle.Close()

            def wait_until_empty(self, *, timeout: float) -> None:
                assert timeout <= bootstrap._INSTALLER_STOP_SECONDS
                events.append("drain-job")
                raise ProcessTreeError("drain failed")

        proc = _FakeProc([], 0)
        proc.returncode = 0
        managed = bootstrap._InstallerProcess(
            cast(Any, proc),
            windows_job=cast(Any, _Job()),
            windows_popen=cast(Any, popen),
            windows_wait=cast(Any, _completed_windows_wait()),
            assigned=True,
        )
        monkeypatch.setattr(
            bootstrap, "_start_installer_supervisor", AsyncMock(return_value=managed)
        )

        with pytest.raises(ProcessTreeError, match="drain failed"):
            await bootstrap._run_patchright_install("--no-shell")

        assert events == ["terminate-job", "release-handle", "drain-job"]
        assert managed.assigned


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

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            # A path segment is a capability as often as a query parameter is,
            # and a redirect can introduce one from a host nothing configured.
            (
                "https://mirror.example/s3cr3t/chromium.zip",
                "https://mirror.example/***",
            ),
            ("https://mirror.example/s3cr3t", "https://mirror.example/***"),
            # A bare trailing slash still keeps its delimiter, because this runs
            # on a buffer that is still growing.
            ("https://mirror.example/", "https://mirror.example/***"),
            # An origin alone says which mirror answered and is kept whole.
            ("https://mirror.example", "https://mirror.example"),
            ("https://mirror.example:8443", "https://mirror.example:8443"),
            # Node normalises backslashes before resolving, so this is the same
            # URL and loses the same path.
            ("https:\\\\mirror.example\\s3cr3t\\x.zip", "https://mirror.example/***"),
            # IPv6 keeps its brackets and its port; a literal is an origin too.
            ("https://[2001:db8::1]:8443/s3cr3t/x", "https://[2001:db8::1]:8443/***"),
            ("https://[::1]/s3cr3t", "https://[::1]/***"),
            # A fragment and a query keep their own delimiter for the same
            # append-stability reason the path does.
            ("https://mirror.example#s3cr3t", "https://mirror.example#***"),
            ("https://mirror.example?t=s3cr3t", "https://mirror.example?***"),
            # Malformed authorities are vouched for by nothing, so nothing of
            # them is kept.
            ("https://[unclosed/s3cr3t", "https://***/***"),
            ("https://mirror.example:notaport/s3cr3t", "https://***/***"),
            ("https://]/s3cr3t", "https://***/***"),
        ],
    )
    def test_a_url_keeps_its_origin_and_nothing_else(self, url, expected):
        from linkedin_mcp_server.bootstrap import _safe_to_print

        assert _safe_to_print(f"from {url} now") == f"from {expected} now"

    @pytest.mark.parametrize(
        "url",
        [
            "https://mirror.example/s3cr3t/x.zip",
            "https://ci-bot:s3cr3t@mirror.example/a/b",
            "https://mirror.example/dl?token=s3cr3t",
            "https://mirror.example#s3cr3t",
            "https://[2001:db8::1]:8443/s3cr3t/x",
            "https:\\\\mirror.example\\s3cr3t\\x.zip",
            # The malformed shapes matter most here: a marker that a later read
            # can complete into a plausible hostname prints the rest beside it.
            "https://[unclosed/s3cr3t",
            "https://mirror.example:notaport/s3cr3t",
        ],
    )
    def test_redaction_is_stable_however_the_text_arrives(self, url):
        """``_safe_to_print`` re-runs on a buffer that keeps growing.

        Its output is therefore its own input on the next read, and a secret
        that arrives in two halves must not survive the join. Every offset is
        swept because only some of them straddle a delimiter.
        """
        from linkedin_mcp_server.bootstrap import _safe_to_print

        text = f"Downloading Chromium from {url} now"
        surviving = [
            cut
            for cut in range(len(text) + 1)
            if "s3cr3t" in _safe_to_print(_safe_to_print(text[:cut]) + text[cut:])
        ]

        assert surviving == []

    def test_a_download_host_without_a_scheme_is_still_redacted(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The one case the per-URL rule cannot reach, which is why both run.

        A configured host with no scheme is not a URL to any pattern here, so
        the path patchright pastes onto it would print as ordinary text.
        """
        from linkedin_mcp_server.bootstrap import _safe_to_print

        monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST", "mirror.example/s3cr3t")

        assert _safe_to_print("from mirror.example/s3cr3t/builds/x.zip") == (
            "from ***/builds/x.zip"
        )

    def test_an_invalid_unused_fallback_does_not_break_output(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.bootstrap import _safe_to_print

        monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST", "https://valid.example")
        monkeypatch.setenv("PLAYWRIGHT_DOWNLOAD_HOST", "http://[")

        assert _safe_to_print("Downloading from https://valid.example/x.zip") == (
            "Downloading from https://valid.example/***"
        )

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

        async def fake_start(extra_arg: str, _environment: dict[str, str]):
            proc = _FakeProc([payload.encode() + b"\n"], 0)
            return proc

        monkeypatch.setattr(bootstrap, "_start_installer_supervisor", fake_start)
        await bootstrap._run_patchright_install("--no-shell", line_callback=seen.append)

        assert "s3cr3t" not in "".join(seen)


class TestAnApostropheDoesNotEndAUrl:
    """A URL run used to stop at ``'``, and everything after it printed.

    Node keeps a literal apostrophe in a path, a query and userinfo alike, and
    patchright interpolates the location a redirect *ended* on into its timeout
    and its error. ``https://cdn.example/browser's.zip?X-Amz-Signature=…`` was
    therefore redacted as far as the quote and printed from there on, into the
    debug log, into the line callback and into the retained failure that becomes
    ``BrowserSetupFailedError``.

    The apostrophe was there for one reason, which is that ``'. URL: `` closes a
    quoted response body and sanitation runs before that marker is read. Only
    those two characters are held back now.
    """

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            # The finding's own URL: the signature sits behind the apostrophe.
            (
                "https://cdn.example/browser's.zip?X-Amz-Signature=s3cr3t",
                "https://cdn.example/***",
            ),
            # An apostrophe inside userinfo, which is a credential by itself.
            (
                "https://ci'bot:s3cr3t@mirror.example/x",
                "https://***@mirror.example/***",
            ),
            ("https://mirror.example/a'b/c?t=s3cr3t", "https://mirror.example/***"),
            ("https://mirror.example?t=s3cr3t'&u=2", "https://mirror.example?***"),
            ("https://mirror.example#f'g=s3cr3t", "https://mirror.example#***"),
            # An apostrophe immediately after the delimiter, so the first thing
            # the old pattern refused was also the first thing it kept.
            ("https://mirror.example/'s3cr3t", "https://mirror.example/***"),
            # Backslashes are the same URL to Node, and hold the same secret.
            ("https:\\\\mirror.example\\a'b\\s3cr3t", "https://mirror.example/***"),
        ],
    )
    def test_the_whole_url_goes_including_its_apostrophes(self, url, expected):
        from linkedin_mcp_server.bootstrap import _safe_to_print

        cleaned = _safe_to_print(f"from {url} now")

        assert cleaned == f"from {expected} now"
        assert "s3cr3t" not in cleaned

    @pytest.mark.parametrize(
        "url",
        [
            "https://cdn.example/browser's.zip?X-Amz-Signature=s3cr3t",
            "https://ci'bot:s3cr3t@mirror.example/x",
            "https://mirror.example/a'b/c?t=s3cr3t",
            "https://mirror.example/'s3cr3t",
            "https://[2001:db8::1]:8443/a'b/s3cr3t",
            "https://[unclosed/a'b/s3cr3t",
        ],
    )
    def test_an_apostrophe_url_survives_no_split(self, url):
        """The buffer grows between passes, so the output is the next input.

        Every offset is swept because only some of them leave an apostrophe at
        the end of the text, which is where the closing marker is held back.
        """
        from linkedin_mcp_server.bootstrap import _safe_to_print

        text = f"Downloading Chromium from {url} now"
        surviving = [
            cut
            for cut in range(len(text) + 1)
            if "s3cr3t" in _safe_to_print(_safe_to_print(text[:cut]) + text[cut:])
        ]

        assert surviving == []

    @pytest.mark.parametrize(
        "text",
        [
            # A body ending on a URL: the run reaches the marker's apostrophe.
            "Download failed: server returned code 403 body 'go to "
            "https://evil.example/tok'. URL: https://cdn.example/x.zip",
            # And the same with the marker's full stop inside the run.
            "server returned code 404 body 'https://evil.example/a'. URL: http://h/z",
        ],
    )
    def test_the_closing_marker_survives_redaction(self, text):
        """What the apostrophe boundary was protecting, protected precisely.

        Losing the marker is not a cosmetic error: ``_installer_lines`` reads it
        out of sanitised text, and without it the body never closes and every
        later line of the install is dropped as body.
        """
        from linkedin_mcp_server import bootstrap

        cleaned = bootstrap._safe_to_print(text)

        assert bootstrap._RESPONSE_BODY_CLOSES in cleaned
        assert bootstrap._RESPONSE_BODY_CLOSED.search(cleaned) is not None
        assert "evil.example/***" in cleaned

    @pytest.mark.parametrize("split", [1, 2])
    def test_a_marker_split_by_a_read_is_still_held_back(self, split):
        """Half a marker at the end of a buffer is still a marker.

        The rest of it arrives on the next read, and sanitation runs again on
        the joined text. Swallowing the apostrophe now would leave nothing for
        that pass to find. Both offsets a URL run can reach are swept: the
        marker's apostrophe alone, and its full stop with it.
        """
        from linkedin_mcp_server import bootstrap

        marker = bootstrap._RESPONSE_BODY_CLOSES
        head = (
            "Download failed: server returned code 403 body 'go to "
            f"https://evil.example/s3cr3t{marker[:split]}"
        )
        tail = f"{marker[split:]}https://cdn.example/x.zip"

        joined = bootstrap._safe_to_print(bootstrap._safe_to_print(head) + tail)

        assert "s3cr3t" not in joined
        assert bootstrap._RESPONSE_BODY_CLOSED.search(joined) is not None

    def test_an_apostrophe_outside_a_marker_is_still_redacted(self):
        """Only a real marker earns the hold-back, not any trailing quote."""
        from linkedin_mcp_server.bootstrap import _safe_to_print

        assert _safe_to_print("from https://mirror.example/s3cr3t' now") == (
            "from https://mirror.example/*** now"
        )

    async def test_sanitisation_runs_before_the_markers_are_read(self, monkeypatch):
        """Measured, because the whole hold-back depends on the order.

        ``_split_installer_output`` sanitises the buffer and ``_installer_lines``
        then looks for the body markers in what it returns. So the sanitiser is
        the only thing that ever sees the raw bytes, and the marker detection
        never does.
        """
        from linkedin_mcp_server import bootstrap

        original = bootstrap._safe_to_print
        offered: list[str] = []

        def spy(text: str) -> str:
            offered.append(text)
            return original(text)

        monkeypatch.setattr(bootstrap, "_safe_to_print", spy)
        lines: list[str] = []

        async def fake_start(extra_arg: str, _environment: dict[str, str]):
            return _FakeProc(
                [
                    b"Download failed: server returned code 403 body 'page'. URL: "
                    b"https://cdn.example/browser's.zip?sig=s3cr3t\n"
                    b"back to normal\n"
                ],
                0,
            )

        monkeypatch.setattr(bootstrap, "_start_installer_supervisor", fake_start)
        await bootstrap._run_patchright_install(
            "--no-shell", line_callback=lines.append
        )

        assert any("s3cr3t" in text for text in offered), "the raw URL reaches it"
        assert not any("s3cr3t" in line for line in lines)
        assert lines[-1] == "back to normal", "and the body still closed"

    async def test_no_output_path_keeps_the_apostrophe_suffix(
        self, monkeypatch, caplog
    ):
        """Every consumer of a line at once: log, callback and retained failure.

        They share one producer, so a leak reaches all three, and a fix has to
        be checked against all three rather than against the pattern alone.
        """
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        seen: list[str] = []

        async def fake_start(extra_arg: str, _environment: dict[str, str]):
            return _FakeProc(
                [
                    b"Downloading Chrome for Testing from "
                    b"https://cdn.example/browser's.zip?X-Amz-Signature=s3cr3t\n"
                ],
                1,
            )

        monkeypatch.setattr(bootstrap, "_start_installer_supervisor", fake_start)
        caplog.set_level(logging.DEBUG, logger="linkedin_mcp_server.bootstrap")

        with pytest.raises(BrowserSetupFailedError) as raised:
            await bootstrap._run_patchright_install("--no-shell")

        assert "s3cr3t" not in str(raised.value), "the retained failure"
        assert "cdn.example" in str(raised.value), "and the mirror is still named"
        assert "s3cr3t" not in caplog.text, "the debug log"

        seen.clear()
        caplog.clear()
        with pytest.raises(BrowserSetupFailedError):
            await bootstrap._run_patchright_install(
                "--no-shell", line_callback=seen.append
            )

        assert "s3cr3t" not in "".join(seen), "the line callback"

    @pytest.mark.parametrize("straddle", [0, 1])
    async def test_an_apostrophe_url_is_redacted_wherever_the_cut_falls(
        self, monkeypatch, straddle
    ):
        """The fragment boundaries, for the shape that used to escape them.

        The URL is preceded by the space patchright writes in front of one.
        Gluing it to the filler instead would measure the ``\\b`` the pattern
        opens on rather than the apostrophe this is about.
        """
        from linkedin_mcp_server import bootstrap

        secret = " https://cdn.example/browser's.zip?X-Amz-Signature=s3cr3t"
        cap = bootstrap._MAX_LINE_CHARS
        offset = [cap, bootstrap._READ_CHUNK][straddle] - len(secret) // 2
        payload = "F" * offset + secret + "F" * cap
        seen: list[str] = []

        async def fake_start(extra_arg: str, _environment: dict[str, str]):
            return _FakeProc([payload.encode() + b"\n"], 0)

        monkeypatch.setattr(bootstrap, "_start_installer_supervisor", fake_start)
        await bootstrap._run_patchright_install("--no-shell", line_callback=seen.append)

        assert "s3cr3t" not in "".join(seen)


class TestAForcedCutDoesNotSplitAUrl:
    """A cut through a URL is the one thing sanitation cannot repair after it.

    ``_safe_to_print`` runs on the buffer, so every URL that has arrived whole
    is an origin before anything is emitted. A URL still arriving is not, and
    the cut that bounds a line used to fall wherever the cap did: the near
    fragment then carried a piece of the authority and the far one carried the
    rest of it, scheme-less, past every pattern in this file and into the debug
    log, the line callback and ``BrowserSetupFailedError``.

    Two writes, because that is what makes the case: a pipe hands over what has
    been written, and patchright's downloader writes its progress and its errors
    separately. One write that completes the authority is redacted before any
    cut and never reaches this.
    """

    #: Userinfo and a query parameter, so both sides of a cut carry something.
    URL = "https://ci-bot:s3cr3t@mirror.example/p?key=s3cr3t"

    async def _lines(self, *writes: bytes) -> list[str]:
        from linkedin_mcp_server.bootstrap import _installer_lines

        stream = cast(Any, _FakeStdout(list(writes)))
        return [line async for line in _installer_lines(stream)]

    def _writes(self, cap: int, offset: int, head: int) -> tuple[bytes, bytes]:
        """The URL *offset* characters in front of the cut, split after *head*.

        The space is the one patchright writes in front of a URL, and it is not
        decoration: ``_URL_IN_TEXT`` opens on ``\\b``, so filler glued straight
        onto the scheme measures that boundary rather than the cut.
        """
        first = "F" * (cap - offset - 1) + " " + self.URL[:head]
        return first.encode(), self.URL[head:].encode() + b" done\n"

    async def test_an_authority_still_arriving_at_the_cut_is_not_split(self):
        """The reported shape, at the offset that produced it.

        Ten characters in, so the cut falls inside the authority rather than in
        front of it, and only the first half of the userinfo has been read when
        the fragment is forced out.
        """
        from linkedin_mcp_server import bootstrap

        cap = bootstrap._MAX_LINE_CHARS
        printed = "".join(await self._lines(*self._writes(cap, offset=10, head=14)))

        assert "s3cr3t" not in printed
        assert "https://***@mirror.example/***" in printed
        assert printed.count("F") == cap - 11, "the ordinary output still arrives"

    async def test_no_offset_into_the_url_leaks_it(self, monkeypatch):
        """Every cut position crossed with every read boundary, exhaustively.

        Run against a small cap. Nothing in the splitter reads the cap's value
        for anything but the comparison, and a 64 KiB one turns this into two
        thousand copies of a 128 KiB buffer: the sweep took minutes and proved
        the same thing. Small also puts the URL over the cap, which is the case
        that has no cut point at all.
        """
        from linkedin_mcp_server import bootstrap

        cap = 40
        monkeypatch.setattr(bootstrap, "_MAX_LINE_CHARS", cap)
        monkeypatch.setattr(bootstrap, "_READ_CHUNK", cap)
        leaking = []
        for offset in range(1, cap):
            for head in range(offset + 1, len(self.URL) + 1):
                printed = "".join(await self._lines(*self._writes(cap, offset, head)))
                if "s3cr3t" in printed:
                    leaking.append((offset, head))

        assert leaking == []

    async def test_a_url_longer_than_the_cap_is_dropped_rather_than_cut(self):
        """No point in front of a run that is already a cap long.

        Holding it back instead would grow the buffer without limit, which is
        what the cap exists to prevent, so the marker goes out and the run does
        not. The credential arrives after the drop has begun and has to be
        dropped with it: it carries no scheme of its own by then.
        """
        from linkedin_mcp_server import bootstrap

        cap = bootstrap._MAX_LINE_CHARS
        printed = "".join(
            await self._lines(
                b"https://" + b"T" * cap,
                b"more-s3cr3t@mirror.example/p?key=s3cr3t\n",
                b"installing\n",
            )
        )

        assert "s3cr3t" not in printed
        assert "T" * 8 not in printed, "the unread authority is not printed either"
        assert printed.startswith(bootstrap._OMITTED_RUN_HEAD)
        assert "installing" in printed, "output after the run still arrives"

    async def test_a_long_ordinary_token_with_slashes_still_arrives(self):
        """The drop needs a scheme, and base64 is not one.

        ``//`` occurs by coincidence in a base64 blob, in a path list and in a
        Windows share, and a run over the cap is far more often one of those
        than a scheme-less credential. Dropping on that evidence would delete
        ordinary output, so the cut stands there.
        """
        from linkedin_mcp_server import bootstrap

        cap = bootstrap._MAX_LINE_CHARS
        blob = "//" + "N" * (2 * cap)
        printed = "".join(await self._lines(blob.encode(), b"\ninstalling\n"))

        assert printed.count("N") == 2 * cap, "nothing of it was dropped"
        assert "installing" in printed

    async def test_no_read_boundary_finishes_a_url_early(self):
        """Every point a read can split this URL at, through the reader.

        Sanitation runs per read, on a buffer that is still growing, so an open
        run is redacted once per read and has to stay collapsible when the rest
        of it lands. ``_redacted_origin`` keeps the delimiter behind the
        authority for exactly that, and this measures the property through the
        stream rather than through the function.
        """
        payload = f"Downloading Chromium from {self.URL} now\n"
        leaking = []
        for split in range(len(payload) + 1):
            printed = "".join(
                await self._lines(payload[:split].encode(), payload[split:].encode())
            )
            if "s3cr3t" in printed:
                leaking.append(split)

        assert leaking == []

    async def test_a_dropped_run_keeps_the_markers_around_it(self):
        """The drop is bounded by the body's markers on both sides.

        The opening marker survives because the cut moves to where the run
        opens, which is behind the quote that opens the body. The closing one
        survives because sanitation holds its first characters back and the drop
        leaves those where they are. Losing either would elide the rest of the
        install.
        """
        from linkedin_mcp_server import bootstrap

        cap = bootstrap._MAX_LINE_CHARS
        opener = "Download failed: server returned code 403 body '"
        lines = await self._lines(
            (opener + "https://" + "T" * (2 * cap)).encode(),
            b"'. URL: http://mirror.example/z.zip\ninstalling\n",
        )
        printed = "\n".join(lines)

        assert "TTTT" not in printed, "the body is still dropped"
        assert bootstrap._OMITTED_BODY in printed
        assert "'. URL: http://mirror.example/***" in printed
        assert "installing" in lines


class TestEofFragmentsLikeEveryOtherBoundary:
    """Sanitation lengthens what it cannot parse, and EOF used to emit it whole.

    An authority ``urlsplit`` refuses becomes a marker longer than the text it
    stood for, so a buffer under the cap on the way in can be well over it on
    the way out. Every other boundary fragments what comes back; the last one
    yielded an unterminated ``http://[`` line as one 109 000-character piece.
    """

    async def _pieces(self, *writes: bytes) -> list[tuple[str, bool]]:
        from linkedin_mcp_server.bootstrap import _split_installer_output

        stream = cast(Any, _FakeStdout(list(writes)))
        return [piece async for piece in _split_installer_output(stream)]

    async def test_what_sanitation_grows_at_eof_is_still_bounded(self):
        """Exactly the cap, so the fold and the growth both land on EOF."""
        from linkedin_mcp_server import bootstrap

        cap = bootstrap._MAX_LINE_CHARS
        payload = ("http://[ " * (cap // 9 + 1))[:cap]
        pieces = await self._pieces(payload.encode())
        printed = "".join(text for text, _ended in pieces)

        assert len(printed) > cap, "sanitation grew this past the cap"
        assert max(len(text) for text, _ended in pieces) <= cap
        assert [ended for _text, ended in pieces] == [False] * (len(pieces) - 1) + [
            True
        ], "and only the last piece ends the physical line"
        assert printed.count("http://***/***") == payload.count("http://"), (
            "no fragment boundary fell inside a redaction"
        )


class TestAConfiguredMirrorSurvivesEveryBoundary:
    """A download host without an HTTP(S) scheme is still a secret.

    ``_safe_to_print`` removes a configured value by exact replacement, so it
    can only act with the whole value in the buffer. The machinery that carries
    a URL across a boundary keys on the scheme instead, and the loader accepts
    a mirror that has none: scheme-less, scheme-relative and under another
    scheme are all configurations this server starts on. A boundary through one
    of those emitted its head and kept its tail, and the private path left with
    the tail through the debug log, the line callback, the retained failure and
    the MCP error that failure becomes.

    Both boundaries are swept, because they cut in opposite directions: the
    forced fragment emits the head and holds the tail, and the retained tail
    discards the head and keeps what a client is shown.
    """

    #: Long enough to cross either boundary from both sides, and shaped like a
    #: mirror that authenticates by path rather than by userinfo, which is the
    #: shape no pattern in that file recognises on its own.
    FORMS = {
        "scheme-less": "mirror.example/" + "P" * 30 + "/s3cr3t",
        "scheme-relative": "//mirror.example/" + "P" * 30 + "/s3cr3t",
        "non-http": "ftp://mirror.example/" + "P" * 30 + "/s3cr3t",
    }

    async def _lines(self, *writes: bytes) -> list[str]:
        from linkedin_mcp_server.bootstrap import _installer_lines

        stream = cast(Any, _FakeStdout(list(writes)))
        return [line async for line in _installer_lines(stream)]

    @pytest.mark.parametrize("form", sorted(FORMS))
    async def test_no_read_boundary_splits_a_configured_mirror(self, monkeypatch, form):
        """Every point a read can split the value at, through the reader.

        Run against a small cap for the same reason the URL sweep is: nothing
        in the splitter reads the cap for anything but a comparison, and a
        64 KiB one turns this into hundreds of copies of a 128 KiB buffer. The
        space in front of the value is the one patchright writes there.
        """
        from linkedin_mcp_server import bootstrap

        configured = self.FORMS[form]
        monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST", configured)
        cap = 40
        monkeypatch.setattr(bootstrap, "_MAX_LINE_CHARS", cap)
        monkeypatch.setattr(bootstrap, "_READ_CHUNK", cap)
        payload = "F" * 10 + " " + configured + " done\n"
        leaking = []
        for split in range(1, len(payload)):
            printed = "".join(
                await self._lines(payload[:split].encode(), payload[split:].encode())
            )
            if "s3cr3t" in printed:
                leaking.append(split)
            assert "done" in printed, "and the stream is not elided to reach that"

        assert leaking == []

    @pytest.mark.parametrize("form", sorted(FORMS))
    async def test_no_feed_boundary_beheads_a_configured_mirror(
        self, monkeypatch, form
    ):
        """The retained tail trims from the front, which is where the name is.

        Losing the head of a value ``_safe_to_print`` matches whole is what
        makes the rest of it unrecognisable, so the trim has to move off it the
        way it already moves off a URL.
        """
        from linkedin_mcp_server.bootstrap import _RedactedTail

        configured = self.FORMS[form]
        monkeypatch.setenv("PLAYWRIGHT_DOWNLOAD_HOST", configured)
        payload = ("F" * 5 + " " + configured + " done").encode()
        leaking = []
        for split in range(1, len(payload)):
            tail = _RedactedTail(20)
            tail.feed(payload[:split])
            tail.feed(payload[split:])
            kept = tail.finish()
            if "s3cr3t" in kept:
                leaking.append(split)
            assert kept.endswith("done"), "and later output still reaches the message"

        assert leaking == []

    async def test_the_captured_startup_tail_holds_no_configured_path(
        self, monkeypatch
    ):
        """Through the consumer, not through the class on its own.

        ``_read_supervisor_frame`` is what feeds every startup byte into the
        tail, and ``_supervisor_start_error`` turns its last line into the
        ``BrowserSetupFailedError`` an MCP client is shown.
        """
        from linkedin_mcp_server import bootstrap

        configured = self.FORMS["scheme-less"]
        monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST", configured)
        monkeypatch.setattr(bootstrap, "_MAX_START_ERROR_CHARS", 20)
        noise = "F" * 5 + " " + configured + " refused to arm\n"
        leaking = []
        for split in range(1, len(noise)):
            stream = cast(
                Any, _FakeStdout([noise[:split].encode(), noise[split:].encode()])
            )
            frame, captured = await bootstrap._read_supervisor_frame(
                stream, marker=b"armed ", accept=lambda _frame: False, timeout=5.0
            )
            assert frame is None, "the supervisor never armed"
            detail = captured.finish().splitlines()[-1]
            if "s3cr3t" in detail:
                leaking.append(split)
            assert "refused to arm" in detail, "and the reason still reaches the error"

        assert leaking == []

    async def test_a_long_ordinary_slash_token_is_still_printed_whole(
        self, monkeypatch
    ):
        """The detection is by value, never by shape.

        Dropping every over-long ``//`` run would close the same hole and take
        the base64 blobs, the path lists and the Windows shares with it. A
        mirror is configured here, so the check is live while the token that is
        not one goes through untouched.
        """
        from linkedin_mcp_server import bootstrap

        monkeypatch.setenv(
            "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST", self.FORMS["scheme-relative"]
        )
        cap = bootstrap._MAX_LINE_CHARS
        blob = "//" + "N" * (2 * cap)
        printed = "".join(await self._lines(blob.encode(), b"\ninstalling\n"))

        assert printed.count("N") == 2 * cap, "nothing of it was dropped"
        assert "installing" in printed

    async def test_a_mirror_of_nothing_but_slashes_is_not_spliced_everywhere(
        self, monkeypatch
    ):
        """``rstrip("/")`` can empty a value, and an empty needle matches between
        every pair of characters. Left unguarded, the marker would be spliced
        through the whole buffer and every position would read as the start of
        a secret."""
        from linkedin_mcp_server.bootstrap import _safe_to_print

        monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST", "///")

        assert _safe_to_print("Downloading Chromium") == "Downloading Chromium"


class TestSanitationIsLinearInWhatArrives:
    """A pipe hands over what is available, which is often a single byte.

    Folding every read into one string and re-sanitising the result made the
    work quadratic in the length of an unterminated line: the copies and the
    rescans both grew with the buffer while the reads stayed tiny. That delays
    the fragment which proves the installer is alive, and it can spend the
    inactivity deadline while the child is writing continuously.

    Counted rather than timed. A duration measures the machine, and this has to
    fail on the algorithm.
    """

    class _OneByteStdout:
        """A pipe at its worst: one byte per read, like a slow writer."""

        def __init__(self, payload: bytes) -> None:
            self._payload = payload
            self._at = 0

        async def read(self, n: int = -1) -> bytes:
            if self._at >= len(self._payload):
                return b""
            self._at += 1
            return self._payload[self._at - 1 : self._at]

    async def _scanned(self, payload: bytes) -> int:
        """Characters handed to ``_safe_to_print`` for the whole stream."""
        from linkedin_mcp_server import bootstrap

        original = bootstrap._safe_to_print
        scanned = 0

        def spy(text: str) -> str:
            nonlocal scanned
            scanned += len(text)
            return original(text)

        with pytest.MonkeyPatch.context() as patching:
            patching.setattr(bootstrap, "_safe_to_print", spy)
            stream = cast(Any, self._OneByteStdout(payload))
            async for _ in bootstrap._installer_lines(stream):
                pass
        return scanned

    async def test_an_unterminated_line_is_not_rescanned_per_read(self):
        """Under the cap, one fold at the newline is all this needs.

        Measured before the staging area: 4 000 bytes delivered one at a time
        scanned 8.0 million characters, and 8 000 scanned 32.0 million.
        """
        size = 4000
        scanned = await self._scanned(b"F" * size + b"\n")

        assert scanned <= 2 * size

    async def test_the_work_tracks_the_bytes_and_not_their_square(self):
        """Doubling the line doubled the work; it used to quadruple it."""
        small = await self._scanned(b"F" * 2000 + b"\n")
        large = await self._scanned(b"F" * 4000 + b"\n")

        assert large <= 3 * small

    async def test_a_line_over_the_cap_stays_bounded_under_tiny_reads(
        self, monkeypatch
    ):
        """The forced-cut path, which folds on size rather than on a newline.

        Each fold has to be charged to the window it emits, so the total stays
        proportional to the bytes that arrived rather than to their square.
        """
        from linkedin_mcp_server import bootstrap

        cap = 64
        monkeypatch.setattr(bootstrap, "_MAX_LINE_CHARS", cap)
        size = 4000
        scanned = await self._scanned(b"F" * size + b"\n")

        assert scanned <= 4 * size


class TestQuotedResponseBodiesAreDropped:
    """The one part of this output a stranger writes is not printed at all.

    Patchright's ``Download failed: server returned code N body '…'. URL: …``
    quotes whatever a refusing mirror sent, verbatim and once per retry. That
    body is where the escape sequences, the reflected credentials, the rich
    markup and the 400-digit sizes all came from. A single-line body is dropped
    between its markers. A multiline body takes the remaining stream because its
    own lines can forge the closing shape.
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

        async def fake_start(extra_arg: str, _environment: dict[str, str]):
            proc = _FakeProc(list(chunks), 0)
            return proc

        monkeypatch.setattr(bootstrap, "_start_installer_supervisor", fake_start)
        await bootstrap._run_patchright_install("--no-shell", line_callback=seen.append)
        return seen

    async def test_nothing_between_the_markers_is_printed(self, monkeypatch):
        seen = await self._lines(monkeypatch, self.SAMPLE.encode())
        printed = "\n".join(seen)

        assert "<response body omitted>" in printed
        assert "denied" not in printed
        assert "999%" not in printed
        assert "sup3rs3cr3tvalue" not in printed

    async def test_the_authenticated_prefix_survives(self, monkeypatch):
        """The status before the remote body remains useful and trustworthy."""
        seen = await self._lines(monkeypatch, self.SAMPLE.encode())
        printed = "\n".join(seen)

        assert "server returned code 403" in printed
        assert "URL:" not in printed
        assert "coreBundle.js" not in printed

    async def test_a_multiline_body_takes_later_attempt_output_with_it(
        self, monkeypatch
    ):
        """No later line has an authenticated boundary from the remote body."""
        seen = await self._lines(
            monkeypatch,
            self.SAMPLE.encode(),
            b"Downloading Chrome for Testing 149.0.7827.55 from https://cdn/x.zip\n",
        )

        assert not any("Downloading Chrome for Testing" in line for line in seen)

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
        assert seen[0].endswith("'. URL: http://b/***")

    async def test_a_body_cannot_close_itself_with_prose_behind_it(self, monkeypatch):
        """On one physical line only its final closer can end the body."""
        stream = (
            "Download failed: server returned code 403 body '<html> "
            "'. URL: http://forged/x and then sup3rs3cr3tvalue "
            "still inside the page '. URL: http://real/x.zip\n"
            "    at IncomingMessage.handleError (/x/coreBundle.js:1:1)\n"
        )
        seen = await self._lines(monkeypatch, stream.encode())
        printed = "\n".join(seen)

        assert "sup3rs3cr3tvalue" not in printed
        assert "still inside the page" not in printed
        assert "'. URL: http://real/***" in printed
        assert "coreBundle.js" in printed, "and patchright's own trace comes back"

    async def test_a_multiline_body_cannot_forge_its_closing_marker(self, monkeypatch):
        """After one body newline the protocol has no authentic closing shape."""
        stream = (
            "Download failed: server returned code 403 body '<html>\n"
            "'. URL: http://forged/x\n"
            "reflected sup3rs3cr3tvalue\n"
            "'. URL: http://real/x.zip\n"
            "later installer output\n"
        )
        seen = await self._lines(monkeypatch, stream.encode())
        printed = "\n".join(seen)

        assert "sup3rs3cr3tvalue" not in printed
        assert "later installer output" not in printed
        assert printed.endswith("<response body omitted>")

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


class TestAConfiguredMirrorCannotDeleteTheMarkers:
    """A value an operator names must not disarm the drop a stranger triggers.

    ``_safe_to_print`` replaces a configured mirror and ``_installer_lines``
    reads patchright's markers out of what comes back, in that order. With
    ``PLAYWRIGHT_DOWNLOAD_HOST=body`` the opener stopped matching and the quoted
    response body printed in full.
    """

    async def _lines(self, *writes: bytes) -> list[str]:
        from linkedin_mcp_server.bootstrap import _installer_lines

        stream = cast(Any, _FakeStdout(list(writes)))
        return [line async for line in _installer_lines(stream)]

    async def test_a_mirror_named_like_the_opener_still_drops_the_body(
        self, monkeypatch
    ):
        """The reported configuration, against the message patchright writes."""
        monkeypatch.setenv(
            "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST", "https://mirror.example"
        )
        monkeypatch.setenv("PLAYWRIGHT_DOWNLOAD_HOST", "body")
        line = (
            "Error: Download failed: server returned code 403 body "
            "'SIGNED_SECRET'. URL: https://mirror.example/p?token=SIGNED_SECRET\n"
        )
        printed = "\n".join(await self._lines(line.encode()))

        assert "SIGNED_SECRET" not in printed
        assert "<response body omitted>" in printed
        assert printed.endswith("'. URL: https://mirror.example/***"), (
            "and the URL below the origin is still redacted"
        )

    async def test_a_mirror_named_like_the_closer_does_not_elide_the_install(
        self, monkeypatch
    ):
        """A deleted closer leaves the body open, and open takes the rest."""
        monkeypatch.setenv("PLAYWRIGHT_DOWNLOAD_HOST", "URL:")
        stream = (
            "Error: Download failed: server returned code 403 body "
            "'<html>denied</html>'. URL: http://h/z.zip\n"
            "    at IncomingMessage.handleError (/x/coreBundle.js:1:1)\n"
        )
        printed = "\n".join(await self._lines(stream.encode()))

        assert "denied" not in printed
        assert "<response body omitted>" in printed
        assert "coreBundle.js" in printed, "and the installer's own trace comes back"

    def test_a_marker_split_by_a_read_survives_the_replacement(self, monkeypatch):
        """Sanitation runs per fold, so an edit to the folded half sticks."""
        from linkedin_mcp_server.bootstrap import _RESPONSE_BODY_OPENS, _safe_to_print

        monkeypatch.setenv("PLAYWRIGHT_DOWNLOAD_HOST", "bo")
        head = _safe_to_print("Error: Download failed: server returned code 403 bo")
        joined = _safe_to_print(head + "dy 'SIGNED_SECRET'. URL: http://h/z.zip")

        assert _RESPONSE_BODY_OPENS.search(joined) is not None

    def test_a_value_straddling_a_marker_leaves_the_marker_whole(self, monkeypatch):
        """The overlap is kept, which is the documented edge of this guard.

        A sentinel stands where the marker was and no value matches across it,
        so an occurrence reaching over that edge is replaced nowhere. This
        records the residue rather than claiming it away.
        """
        from linkedin_mcp_server.bootstrap import _RESPONSE_BODY_OPENS, _safe_to_print

        monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST", "403 body 'PRIVATE")
        printed = _safe_to_print("server returned code 403 body 'PRIVATE/x.zip")

        assert _RESPONSE_BODY_OPENS.search(printed) is not None
        assert "PRIVATE" in printed, "the residue this design accepts"

    def test_an_ordinary_mirror_is_still_replaced_whole(self, monkeypatch):
        """Nothing about a value clear of the markers changed."""
        from linkedin_mcp_server.bootstrap import _safe_to_print

        monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST", "mirror.example/s3cr3t")
        printed = _safe_to_print(
            "server returned code 403 body 'x'. URL: mirror.example/s3cr3t/x.zip"
        )

        assert printed == "server returned code 403 body 'x'. URL: ***/x.zip"


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

        async def fake_install(
            extra_arg: str, *, line_callback=None, activity_callback=None
        ) -> None:
            seen["extra_arg"] = extra_arg
            seen["line_callback"] = line_callback
            seen["activity_callback"] = activity_callback

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

    def test_refuses_unclaimed_root_before_readiness_or_install(
        self, tmp_path, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import ProfileRootRefusedError

        profile = tmp_path / "unclaimed-cli" / "profile"
        unrelated = profile.parent / "patchright-browsers" / "keep.txt"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("keep")
        monkeypatch.setattr(bootstrap, "get_profile_dir", lambda: profile)
        monkeypatch.setattr(
            bootstrap,
            "browser_ready",
            lambda: pytest.fail("readiness ran before ownership was proved"),
        )

        with pytest.raises(ProfileRootRefusedError):
            ensure_browser_installed()

        assert unrelated.read_text() == "keep"

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


def _installed_patchright_registry() -> dict[str, Any]:
    """Read the browsers.json of the patchright actually installed here."""
    import patchright

    registry = Path(patchright.__file__).parent / "driver" / "package" / "browsers.json"
    return cast(dict[str, Any], json.loads(registry.read_text()))


def _registry_entry(name: str) -> dict[str, Any]:
    for entry in _installed_patchright_registry()["browsers"]:
        if entry.get("name") == name:
            return cast(dict[str, Any], entry)
    raise AssertionError(f"{name} is missing from the bundled browsers.json")


class TestPatchrightCommandTargetContract:
    """What ``patchright install chromium <flag>`` really extracts.

    Every claim here is measured against the installed patchright rather than
    modelled: the target list comes from its own ``--dry-run``, which resolves
    the command exactly as an install would and prints each install location
    without opening a socket, and the Windows-only condition comes from the
    resolver in the shipped driver bundle, because it cannot be observed from
    a POSIX host.
    """

    def _dry_run_locations(self, tmp_path, *flags: str) -> list[str]:
        import subprocess

        cache = tmp_path / "browsers"
        environment = dict(os.environ)
        environment["PLAYWRIGHT_BROWSERS_PATH"] = str(cache)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "patchright",
                "install",
                "chromium",
                *flags,
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            env=environment,
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr
        prefix = "Install location:"
        locations = [
            line.split(prefix, 1)[1].strip()
            for line in completed.stdout.splitlines()
            if prefix in line
        ]
        assert locations, completed.stdout
        return [Path(location).name for location in locations]

    def test_the_locked_release_is_the_one_this_contract_describes(self):
        import importlib.metadata

        assert importlib.metadata.version("patchright") == "1.61.2"

    def _assert_is_an_ffmpeg_directory(self, name: str) -> None:
        """Pin the kind, and the revision to one this browsers.json names.

        Never the literal ``ffmpeg-<base>``: a host that ``revisionOverrides``
        covers gets ``ffmpeg_<hostPlatform>_special-<override>`` instead, and
        both are the current target where they occur. What no host may produce
        is a revision the registry does not name at all.
        """
        entry = _registry_entry("ffmpeg")
        assert name.startswith("ffmpeg")
        assert name.rsplit("-", 1)[1] in {str(entry["revision"])} | {
            str(revision) for revision in entry.get("revisionOverrides", {}).values()
        }

    def test_no_shell_installs_chromium_and_ffmpeg(self, tmp_path):
        chromium = _registry_entry("chromium")

        measured = self._dry_run_locations(tmp_path, "--no-shell")

        # Two directories, in this order: the browser the flag names, at the
        # revision browsers.json gives it, and then ffmpeg, which no flag names
        # and which every browser argument pulls in regardless.
        assert len(measured) == 2, measured
        assert measured[0] == f"chromium-{chromium['revision']}"
        self._assert_is_an_ffmpeg_directory(measured[1])
        assert not any(
            name.startswith(("firefox", "webkit", "winldd")) for name in measured
        )

    def test_only_shell_installs_the_shell_and_ffmpeg(self, tmp_path):
        shell = _registry_entry("chromium-headless-shell")

        measured = self._dry_run_locations(tmp_path, "--only-shell")

        assert len(measured) == 2, measured
        assert measured[0] == f"chromium_headless_shell-{shell['revision']}"
        self._assert_is_an_ffmpeg_directory(measured[1])

    def test_the_measured_command_targets_are_all_accounted_for(self, tmp_path):
        from linkedin_mcp_server import bootstrap

        cache = tmp_path / "browsers"
        environment = {"PLAYWRIGHT_BROWSERS_PATH": str(cache)}

        accounted = {
            path.name
            for path in bootstrap._installer_extraction_paths("--no-shell", environment)
        }

        measured = set(self._dry_run_locations(tmp_path, "--no-shell"))
        assert measured <= accounted
        # Over-approximation stays inside the registry: only ffmpeg's own
        # revisionOverrides may appear beyond what this host resolves.
        overrides = _registry_entry("ffmpeg").get("revisionOverrides", {})
        assert accounted - measured <= {
            f"ffmpeg_{host.replace('-', '_')}_special-{revision}"
            for host, revision in overrides.items()
        }

    def test_winldd_is_pushed_only_on_windows(self):
        """The condition a POSIX dry-run cannot show, read from the resolver."""
        import patchright

        bundle = (
            Path(patchright.__file__).parent
            / "driver"
            / "package"
            / "lib"
            / "coreBundle.js"
        ).read_text()

        assert (
            'if (process.platform === "win32")\n'
            '          executables.push(this.findExecutable("winldd"));' in bundle
        )
        # And ffmpeg's condition beside it: any argument resolving to a browser.
        assert (
            "if (executable?.browserName)\n"
            '            executables.push(this.findExecutable("ffmpeg"));' in bundle
        )
        # winldd carries no revisionOverrides, so its directory is the plain one.
        assert "revisionOverrides" not in _registry_entry("winldd")


class TestInstallerAuxiliaryAccounting:
    def _stub_registry(self, monkeypatch, tmp_path, browsers):
        package = tmp_path / "patchright_pkg"
        (package / "driver" / "package").mkdir(parents=True)
        (package / "driver" / "package" / "browsers.json").write_text(
            json.dumps({"browsers": browsers})
        )
        module = MagicMock()
        module.__file__ = str(package / "__init__.py")
        monkeypatch.setitem(sys.modules, "patchright", module)

    #: The shape of the locked registry, trimmed to what the accounting reads.
    _BROWSERS = [
        {"name": "chromium", "revision": "1228", "installByDefault": True},
        {
            "name": "chromium-headless-shell",
            "revision": "1228",
            "installByDefault": True,
        },
        {"name": "firefox", "revision": "1532", "installByDefault": True},
        {
            "name": "webkit",
            "revision": "2311",
            "installByDefault": True,
            "revisionOverrides": {"mac14-arm64": "2251"},
        },
        {
            "name": "ffmpeg",
            "revision": "1011",
            "installByDefault": True,
            "revisionOverrides": {"mac12-arm64": "1010"},
        },
        {"name": "winldd", "revision": "1007", "installByDefault": False},
    ]

    def _paths(self, cache, extra_arg="--no-shell"):
        from linkedin_mcp_server import bootstrap

        return {
            path.name
            for path in bootstrap._installer_extraction_paths(
                extra_arg, {"PLAYWRIGHT_BROWSERS_PATH": str(cache)}
            )
        }

    def test_ffmpeg_is_accounted_for_alongside_chromium(self, monkeypatch, tmp_path):
        self._stub_registry(monkeypatch, tmp_path, self._BROWSERS)

        names = self._paths(tmp_path / "cache")

        assert "chromium-1228" in names
        assert "ffmpeg-1011" in names
        # The override candidate too: only one of the two exists on any host and
        # this side cannot tell which, so both are named.
        assert "ffmpeg_mac12_arm64_special-1010" in names

    def test_unrelated_browsers_and_stale_revisions_stay_out(
        self, monkeypatch, tmp_path
    ):
        self._stub_registry(monkeypatch, tmp_path, self._BROWSERS)

        names = self._paths(tmp_path / "cache")

        assert not any(name.startswith(("firefox", "webkit")) for name in names)
        assert "chromium_headless_shell-1228" not in names
        assert all(name.endswith(("-1228", "-1011", "-1010")) for name in names), names

    def test_winldd_is_accounted_for_on_windows_only(self, monkeypatch, tmp_path):
        self._stub_registry(monkeypatch, tmp_path, self._BROWSERS)

        assert "winldd-1007" not in self._paths(tmp_path / "posix")

        monkeypatch.setattr(sys, "platform", "win32")

        assert "winldd-1007" in self._paths(tmp_path / "windows")

    def test_an_oversized_ffmpeg_is_refused(self, monkeypatch, tmp_path):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        self._stub_registry(monkeypatch, tmp_path, self._BROWSERS)
        monkeypatch.setattr(bootstrap, "_INSTALLER_MAX_WRITTEN_BYTES", 1024)
        cache = tmp_path / "cache"
        temporary_root = tmp_path / "private"
        temporary_root.mkdir()
        oversized = cache / "ffmpeg-1011" / "ffmpeg"
        oversized.parent.mkdir(parents=True)
        oversized.write_bytes(b"a" * 4096)

        paths = bootstrap._installer_extraction_paths(
            "--no-shell", {"PLAYWRIGHT_BROWSERS_PATH": str(cache)}
        )
        snapshot = bootstrap._installer_download_snapshot(temporary_root, paths)

        with pytest.raises(BrowserSetupFailedError, match="past its"):
            bootstrap._refuse_oversized_install(snapshot)

    def test_an_oversized_winldd_is_refused_on_windows(self, monkeypatch, tmp_path):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        self._stub_registry(monkeypatch, tmp_path, self._BROWSERS)
        monkeypatch.setattr(bootstrap, "_INSTALLER_MAX_WRITTEN_BYTES", 1024)
        cache = tmp_path / "cache"
        temporary_root = tmp_path / "private"
        temporary_root.mkdir()
        oversized = cache / "winldd-1007" / "PrintDeps.exe"
        oversized.parent.mkdir(parents=True)
        oversized.write_bytes(b"a" * 4096)
        environment = {"PLAYWRIGHT_BROWSERS_PATH": str(cache)}

        on_posix = bootstrap._installer_download_snapshot(
            temporary_root,
            bootstrap._installer_extraction_paths("--no-shell", environment),
        )
        monkeypatch.setattr(sys, "platform", "win32")
        on_windows = bootstrap._installer_download_snapshot(
            temporary_root,
            bootstrap._installer_extraction_paths("--no-shell", environment),
        )

        # Same tree, and only the Windows accounting can see the target in it.
        bootstrap._refuse_oversized_install(on_posix)
        with pytest.raises(BrowserSetupFailedError, match="past its"):
            bootstrap._refuse_oversized_install(on_windows)

    def test_an_oversized_unrelated_browser_is_not_this_installs_to_refuse(
        self, monkeypatch, tmp_path
    ):
        from linkedin_mcp_server import bootstrap

        self._stub_registry(monkeypatch, tmp_path, self._BROWSERS)
        monkeypatch.setattr(bootstrap, "_INSTALLER_MAX_WRITTEN_BYTES", 1024)
        cache = tmp_path / "cache"
        temporary_root = tmp_path / "private"
        temporary_root.mkdir()
        for name in ("firefox-1532", "webkit-2311", "ffmpeg-999"):
            stray = cache / name / "payload"
            stray.parent.mkdir(parents=True)
            stray.write_bytes(b"a" * 4096)

        snapshot = bootstrap._installer_download_snapshot(
            temporary_root,
            bootstrap._installer_extraction_paths(
                "--no-shell", {"PLAYWRIGHT_BROWSERS_PATH": str(cache)}
            ),
        )

        bootstrap._refuse_oversized_install(snapshot)

    def test_readiness_metadata_ignores_the_auxiliary_targets(
        self, monkeypatch, tmp_path
    ):
        from linkedin_mcp_server import bootstrap

        self._stub_registry(monkeypatch, tmp_path, self._BROWSERS)
        monkeypatch.setattr(sys, "platform", "win32")

        assert bootstrap._patchright_install_targets() == {
            "chromium-": "1228",
            "chromium_headless_shell-": "1228",
        }
        assert bootstrap._revision_dir_prefix("ffmpeg-1011") is None
        assert bootstrap._revision_dir_prefix("winldd-1007") is None


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

    def test_refuses_to_delete_metadata_under_an_unclaimed_root(
        self, tmp_path, monkeypatch, caplog
    ):
        from linkedin_mcp_server import bootstrap

        profile = tmp_path / "unclaimed" / "profile"
        metadata = profile.parent / "browser-install.json"
        metadata.parent.mkdir(parents=True)
        metadata.write_text("unrelated")
        monkeypatch.setattr(bootstrap, "get_profile_dir", lambda: profile)

        initialize_bootstrap("managed")
        state = get_bootstrap_state()
        state.setup_state = SetupState.READY
        with caplog.at_level(logging.WARNING, logger="linkedin_mcp_server.bootstrap"):
            invalidate_browser_setup()

        assert metadata.read_text() == "unrelated"
        assert state.setup_state is SetupState.IDLE
        assert "unclaimed profile root" in caplog.text

    def test_refuses_to_overwrite_metadata_under_an_unclaimed_root(
        self, tmp_path, monkeypatch
    ):
        from linkedin_mcp_server import bootstrap
        from linkedin_mcp_server.exceptions import ProfileRootRefusedError

        profile = tmp_path / "unclaimed-write" / "profile"
        metadata = profile.parent / "browser-install.json"
        metadata.parent.mkdir(parents=True)
        metadata.write_text("unrelated")
        monkeypatch.setattr(bootstrap, "get_profile_dir", lambda: profile)

        with pytest.raises(ProfileRootRefusedError):
            bootstrap._write_install_metadata(
                tmp_path / "browsers", {"chromium-": True}
            )

        assert metadata.read_text() == "unrelated"

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
        release = asyncio.Event()

        async def fake_setup(**_kwargs: object) -> None:
            await release.wait()

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

        # State is reset and setup starts, while stale metadata remains until a
        # successful install replaces it.
        assert install_metadata_path().exists()
        assert state.setup_state is SetupState.RUNNING
        assert state.setup_task is not None
        release.set()
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
