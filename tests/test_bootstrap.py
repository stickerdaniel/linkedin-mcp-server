import asyncio
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.bootstrap import (
    AuthState,
    _auto_import_allowed,
    _force_move_auth_state_aside,
    _has_install_for,
    _patchright_install_targets,
    _start_login_if_needed,
    browser_setup_ready,
    browsers_path,
    configure_browser_environment,
    ensure_browser_installed,
    ensure_tool_ready_or_raise,
    full_chromium_ready,
    get_bootstrap_state,
    get_runtime_policy,
    initialize_bootstrap,
    install_metadata_path,
    invalidate_auth_and_trigger_relogin,
    invalidate_browser_setup,
    reset_bootstrap_for_testing,
    RuntimePolicy,
    SetupState,
    shell_ready,
    start_background_browser_setup_if_needed,
)
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.core.exceptions import NetworkError
from linkedin_mcp_server.exceptions import (
    AuthenticationInProgressError,
    AuthenticationStartedError,
    BrowserSetupInProgressError,
    CookieDecryptionError,
    DockerHostLoginRequiredError,
    LinkedInMCPError,
    NoLinkedInSessionFoundError,
)
from linkedin_mcp_server.session_state import (
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
        async def fake_start_login(ctx=None) -> None:
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
        login_task = MagicMock()
        login_task.done.return_value = False

        initialize_bootstrap("managed")
        state = get_bootstrap_state()
        state.setup_task = setup_task
        state.login_task = login_task

        reset_bootstrap_for_testing()

        setup_task.cancel.assert_called_once_with()
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


class TestShellAndFullReady:
    """The split predicates: shell-only vs full-chromium readiness."""

    @pytest.fixture(autouse=True)
    def _headless_config(self, monkeypatch):
        _set_headless(monkeypatch, True)

    def test_shell_ready_true_full_false_with_only_shell(
        self, isolate_profile_dir, monkeypatch
    ):
        """Only the headless shell present: shell_ready True, full_chromium_ready False."""
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium_headless_shell-1217"])
        _write_metadata(install_metadata_path(), bdir)
        assert shell_ready() is True
        assert full_chromium_ready() is False

    def test_both_ready_with_complete_install(self, isolate_profile_dir, monkeypatch):
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium_headless_shell-1217"])
        _write_metadata(install_metadata_path(), bdir)
        assert shell_ready() is True
        assert full_chromium_ready() is True

    def test_shell_false_when_only_full_present(self, isolate_profile_dir, monkeypatch):
        """Full chromium without the shell: shell_ready False (the shell is the gate)."""
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217"])
        _write_metadata(install_metadata_path(), bdir)
        assert shell_ready() is False
        assert full_chromium_ready() is False

    def test_both_false_when_metadata_shape_bad(self, isolate_profile_dir, monkeypatch):
        _patch_targets_and_version(monkeypatch)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium_headless_shell-1217"])
        _write_metadata(install_metadata_path(), bdir, version=2)
        assert shell_ready() is False
        assert full_chromium_ready() is False


class TestModeAwareGate:
    """ensure_tool_ready_or_raise gates on the binary the configured mode uses."""

    async def test_headless_mode_releases_on_shell_only(
        self, isolate_profile_dir, monkeypatch
    ):
        """Headless server: only the shell present -> gate releases to the auth path."""
        _patch_targets_and_version(monkeypatch)
        _set_headless(monkeypatch, True)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium_headless_shell-1217"])
        _write_metadata(install_metadata_path(), bdir)
        configure_browser_environment()

        # Past the setup gate, auth gating decides; force auth ready so the call
        # returns normally (no setup-in-progress raise).
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: True)

        initialize_bootstrap("managed")

        result = await ensure_tool_ready_or_raise("get_person_profile")
        assert result is None

    async def test_headed_mode_blocks_until_full_chromium(
        self, isolate_profile_dir, monkeypatch
    ):
        """--no-headless server: shell-only is not enough -> setup-in-progress raise."""
        _patch_targets_and_version(monkeypatch)
        _set_headless(monkeypatch, False)
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

    async def test_headed_mode_releases_on_full_chromium(
        self, isolate_profile_dir, monkeypatch
    ):
        """--no-headless server with full chromium present: gate releases."""
        _patch_targets_and_version(monkeypatch)
        _set_headless(monkeypatch, False)
        bdir = browsers_path()
        _materialize_install(bdir, ["chromium-1217", "chromium_headless_shell-1217"])
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

        async def fake_install(extra_arg: str) -> None:
            calls.append(extra_arg)

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_patchright_install", fake_install
        )
        return calls

    async def test_headless_lazy_stops_after_shell(
        self, isolate_profile_dir, monkeypatch
    ):
        """Plain headless mode installs only the shell; metadata records shell-only."""
        _patch_targets_and_version(monkeypatch)
        config = SimpleNamespace(
            browser=SimpleNamespace(headless=True, eager_full_chromium=False)
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        calls = self._stub_install(monkeypatch)

        from linkedin_mcp_server.bootstrap import _run_browser_setup

        await _run_browser_setup()

        assert calls == ["--only-shell"]
        payload = json.loads(install_metadata_path().read_text())
        assert payload["version"] == 3
        assert payload["installed_targets"] == {
            "chromium-": False,
            "chromium_headless_shell-": True,
        }

    async def test_ensure_full_records_shell_before_full_stage_fails(
        self, isolate_profile_dir, monkeypatch
    ):
        """A --no-shell failure still leaves the shell recorded as installed."""
        from linkedin_mcp_server.exceptions import BrowserSetupFailedError

        _patch_targets_and_version(monkeypatch)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.full_chromium_ready", lambda: False
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.shell_ready", lambda: False)

        calls: list[str] = []

        async def fake_install(extra_arg: str) -> None:
            calls.append(extra_arg)
            if extra_arg == "--no-shell":
                raise BrowserSetupFailedError("network down")

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_patchright_install", fake_install
        )

        from linkedin_mcp_server.bootstrap import _ensure_full_chromium_installed

        with pytest.raises(BrowserSetupFailedError):
            await _ensure_full_chromium_installed()

        assert calls == ["--only-shell", "--no-shell"]
        payload = json.loads(install_metadata_path().read_text())
        assert payload["installed_targets"] == {
            "chromium-": False,
            "chromium_headless_shell-": True,
        }

    async def test_headed_installs_both_in_order(
        self, isolate_profile_dir, monkeypatch
    ):
        """Headed mode installs shell then full chromium; metadata records both."""
        _patch_targets_and_version(monkeypatch)
        config = SimpleNamespace(
            browser=SimpleNamespace(headless=False, eager_full_chromium=False)
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        calls = self._stub_install(monkeypatch)

        from linkedin_mcp_server.bootstrap import _run_browser_setup

        await _run_browser_setup()

        assert calls == ["--only-shell", "--no-shell"]
        payload = json.loads(install_metadata_path().read_text())
        assert payload["installed_targets"] == {
            "chromium-": True,
            "chromium_headless_shell-": True,
        }

    async def test_eager_knob_installs_both_in_headless(
        self, isolate_profile_dir, monkeypatch
    ):
        """eager_full_chromium runs the --no-shell stage even in headless mode."""
        _patch_targets_and_version(monkeypatch)
        config = SimpleNamespace(
            browser=SimpleNamespace(headless=True, eager_full_chromium=True)
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.get_config", lambda: config)
        calls = self._stub_install(monkeypatch)

        from linkedin_mcp_server.bootstrap import _run_browser_setup

        await _run_browser_setup()

        assert calls == ["--only-shell", "--no-shell"]

    async def test_single_setup_task_runs_both_stages(
        self, isolate_profile_dir, monkeypatch
    ):
        """Both install stages run inside the one background setup task."""
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
        assert calls == ["--only-shell", "--no-shell"]


class TestEnsureBrowserInstalledTarget:
    """ensure_browser_installed requests the shell or full chromium per mode."""

    def _stub(self, monkeypatch):
        shell_calls = {"value": 0}
        full_calls = {"value": 0}

        async def fake_shell() -> None:
            shell_calls["value"] += 1

        async def fake_full() -> None:
            full_calls["value"] += 1

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_install_shell_only", fake_shell
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._ensure_full_chromium_installed", fake_full
        )
        return shell_calls, full_calls

    def test_shell_target_installs_shell_only(self, isolate_profile_dir, monkeypatch):
        _patch_targets_and_version(monkeypatch)
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.shell_ready", lambda: False)
        shell_calls, full_calls = self._stub(monkeypatch)

        ensure_browser_installed(full=False)

        assert shell_calls["value"] == 1
        assert full_calls["value"] == 0

    def test_full_target_installs_full(self, isolate_profile_dir, monkeypatch):
        _patch_targets_and_version(monkeypatch)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.full_chromium_ready", lambda: False
        )
        shell_calls, full_calls = self._stub(monkeypatch)

        ensure_browser_installed(full=True)

        assert shell_calls["value"] == 0
        assert full_calls["value"] == 1

    def test_shell_target_noop_when_shell_present(
        self, isolate_profile_dir, monkeypatch
    ):
        monkeypatch.setattr("linkedin_mcp_server.bootstrap.shell_ready", lambda: True)
        shell_calls, full_calls = self._stub(monkeypatch)

        ensure_browser_installed(full=False)

        assert shell_calls["value"] == 0
        assert full_calls["value"] == 0

    def test_full_target_noop_when_full_present(self, isolate_profile_dir, monkeypatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.full_chromium_ready", lambda: True
        )
        shell_calls, full_calls = self._stub(monkeypatch)

        ensure_browser_installed(full=True)

        assert shell_calls["value"] == 0
        assert full_calls["value"] == 0


class TestLazyFullChromiumTrigger:
    """The headed manual-login fallback installs full chromium before launching."""

    def _stub(self, monkeypatch, *, custom_chrome: bool):
        order: list[str] = []

        async def fake_full() -> None:
            order.append("full")

        async def fake_login(_profile_dir) -> bool:
            order.append("login")
            return True

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._uses_custom_chrome", lambda: custom_chrome
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._ensure_full_chromium_installed", fake_full
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.interactive_login", fake_login
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.get_profile_dir",
            lambda: Path("/tmp/profile"),
        )
        return order

    async def test_installs_full_chromium_before_headed_launch(self, monkeypatch):
        order = self._stub(monkeypatch, custom_chrome=False)

        from linkedin_mcp_server.bootstrap import _run_login_flow

        await _run_login_flow()

        assert order == ["full", "login"]

    async def test_skips_full_chromium_for_custom_chrome(self, monkeypatch):
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

        async def fake_import(_browser, *, user_data_dir):
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

        async def fake_import(_browser, *, user_data_dir):
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

        async def fake_import(_browser, *, user_data_dir):
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

        async def fake_import(_browser, *, user_data_dir):
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
