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

        async def fake_install(extra_arg: str) -> None:
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

        async def fake_install(extra_arg: str) -> None:
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


class TestEnsureBrowserInstalledSkipsCustomChrome:
    """A custom executable must not trigger a managed download.

    The gap this closes was survivable while two of the three CLI modes needed
    only the 92 MiB shell. Now they all want the full browser, so it is 170 MiB
    fetched for something that is never launched -- and for an operator whose
    network cannot reach the CDN, it is the difference between signing in and
    not. `_uses_custom_chrome()` already existed and said so in its docstring;
    only this caller never asked.
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

        async def fake_install() -> None:
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

        async def fake_install() -> None:
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

        async def fake_install() -> None:
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


class TestLoginInstallBackstop:
    """The headed manual-login fallback installs full chromium before launching."""

    def _stub(self, monkeypatch, *, custom_chrome: bool):
        order: list[str] = []

        async def fake_full() -> None:
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
