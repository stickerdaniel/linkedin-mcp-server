import asyncio
import json
import logging
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.bootstrap import (
    AuthState,
    _auto_import_allowed,
    _camoufox_assets_ready,
    _camoufox_binary_path,
    _camoufox_install_lock,
    _force_move_auth_state_aside,
    _has_install_for,
    _patchright_install_targets,
    _refresh_background_task_state,
    _remove_incomplete_camoufox_assets,
    _run_camoufox_fetch,
    _write_camoufox_ready_marker,
    _start_login_if_needed,
    _try_auto_import_session,
    browser_setup_ready,
    browsers_path,
    camoufox_ready,
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
    BrowserSetupFailedError,
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


def _isolate_fetch_assets(
    monkeypatch,
    tmp_path: Path,
    *,
    install_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Keep failed-fetch cleanup away from the developer's real Camoufox cache."""
    import camoufox.pkgman as pkgman

    monkeypatch.setattr(
        pkgman, "INSTALL_DIR", install_dir or tmp_path / "camoufox-cache"
    )
    mmdb_path = tmp_path / "GeoLite2-City.mmdb"
    mmdb_path.write_bytes(b"valid-looking preexisting database")
    addon_dir = tmp_path / "fetch-assets" / "addons" / "UBO"
    addon_dir.mkdir(parents=True)
    (addon_dir / "manifest.json").write_text(
        json.dumps({"manifest_version": 2, "name": "uBlock Origin", "version": "1"})
    )
    (addon_dir / "possibly-truncated.js").write_text("partial")
    monkeypatch.setattr(
        "linkedin_mcp_server.bootstrap._camoufox_browser_dir", lambda: None
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.bootstrap._camoufox_mmdb_path", lambda: mmdb_path
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.bootstrap._camoufox_mmdb_ready", lambda _path: True
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.bootstrap._camoufox_addon_dirs",
        lambda _browser_dir: [addon_dir],
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.bootstrap._camoufox_components_ready", lambda: True
    )
    return mmdb_path, addon_dir


def _camoufox_install_lock_probe(lock_path: Path, connection, release_event) -> None:
    async def hold_lock() -> None:
        async with _camoufox_install_lock(lock_path):
            connection.send("acquired")
            release_event.wait(timeout=10)

    try:
        asyncio.run(hold_lock())
    finally:
        connection.close()


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
        _set_headless(monkeypatch, True)
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
    cookie_path.write_text(
        json.dumps(
            [
                {
                    "name": "li_at",
                    "domain": ".linkedin.com",
                    "value": "session",
                }
            ]
        )
    )
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
        """Force-move happens inside the cross-process-locked login task."""
        _make_auth_ready(isolate_profile_dir)

        async def fake_login(_profile_dir):
            assert not isolate_profile_dir.exists()
            assert not portable_cookie_path(isolate_profile_dir).exists()
            assert not source_state_path(isolate_profile_dir).exists()
            return True

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.interactive_login", fake_login
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._skips_managed_binary", lambda: True
        )
        initialize_bootstrap("managed")

        with pytest.raises(AuthenticationStartedError, match="Session expired"):
            await invalidate_auth_and_trigger_relogin()

        state = get_bootstrap_state()
        assert state.auth_state is AuthState.STARTING
        assert state.login_task is not None
        await state.login_task

        # Files were moved only after the task acquired the source lock.
        assert not isolate_profile_dir.exists()
        assert not portable_cookie_path(isolate_profile_dir).exists()
        assert not source_state_path(isolate_profile_dir).exists()

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


def _set_camoufox(monkeypatch, *, chrome_path: str | None = None) -> None:
    """Point bootstrap at a complete Camoufox config test double."""
    config = SimpleNamespace(
        browser=SimpleNamespace(
            browser_engine="camoufox",
            chrome_path=chrome_path,
            headless=True,
            eager_full_chromium=False,
            login_inline_wait_seconds=0,
            auto_import_from_browser=False,
        ),
        server=SimpleNamespace(transport="stdio", host="127.0.0.1"),
        is_interactive=False,
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


class TestCamoufoxProvisioning:
    """Camoufox has its own readiness gate and fetch path, never Patchright's."""

    def test_binary_path_uses_read_only_version_probe(self, monkeypatch, tmp_path):
        import camoufox.pkgman as pkgman

        calls: list[Path] = []

        class SupportedVersion:
            @staticmethod
            def from_path(path):
                calls.append(path)
                return SimpleNamespace(is_supported=lambda: True)

        monkeypatch.setattr(pkgman, "INSTALL_DIR", tmp_path)
        monkeypatch.setattr(pkgman, "Version", SupportedVersion)
        monkeypatch.setattr(pkgman, "OS_NAME", "lin")
        monkeypatch.setattr(pkgman, "LAUNCH_FILE", {"lin": "camoufox-bin"})

        assert _camoufox_binary_path() == tmp_path / "camoufox-bin"
        assert calls == [tmp_path]

    def test_ready_requires_executable_file(self, monkeypatch, tmp_path):
        executable = tmp_path / "camoufox-bin"
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_binary_path",
            lambda: executable,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_assets_ready", lambda: True
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_ready_marker_valid", lambda: True
        )

        assert camoufox_ready() is False
        executable.write_text("browser")
        if os.name != "nt":
            executable.chmod(0o600)
            assert camoufox_ready() is False
            executable.chmod(0o700)
        assert camoufox_ready() is True

        executable.write_bytes(b"")
        assert camoufox_ready() is False

    def test_ready_requires_atomic_completion_marker(self, monkeypatch, tmp_path):
        executable = tmp_path / "camoufox-bin"
        executable.write_text("browser")
        if os.name != "nt":
            executable.chmod(0o700)
        marker_valid = False
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_binary_path",
            lambda: executable,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_assets_ready", lambda: True
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_ready_marker_valid",
            lambda: marker_valid,
        )

        assert camoufox_ready() is False
        marker_valid = True
        assert camoufox_ready() is True

    def test_assets_require_valid_mmdb_and_complete_default_addon(
        self, monkeypatch, tmp_path
    ):
        browser_dir = tmp_path / "browser"
        addon_dir = browser_dir / "addons" / "UBO"
        addon_dir.mkdir(parents=True)
        mmdb_path = tmp_path / "GeoLite2-City.mmdb"
        mmdb_path.write_bytes(b"validated by stub")
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_browser_dir",
            lambda: browser_dir,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_mmdb_path", lambda: mmdb_path
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_mmdb_ready", lambda path: True
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_addon_dirs",
            lambda _browser_dir: [addon_dir],
        )

        assert _camoufox_assets_ready() is False
        (addon_dir / "manifest.json").write_text(
            json.dumps({"manifest_version": 2, "name": "uBlock Origin", "version": "1"})
        )
        assert _camoufox_assets_ready() is True

    def test_incomplete_assets_are_removed_before_fetch(self, monkeypatch, tmp_path):
        browser_dir = tmp_path / "browser"
        addon_dir = browser_dir / "addons" / "UBO"
        addon_dir.mkdir(parents=True)
        (addon_dir / "partial.xpi").write_bytes(b"partial")
        mmdb_path = tmp_path / "GeoLite2-City.mmdb"
        mmdb_path.write_bytes(b"partial")
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_browser_dir",
            lambda: browser_dir,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_mmdb_path", lambda: mmdb_path
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_mmdb_ready", lambda path: False
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_addon_dirs",
            lambda _browser_dir: [addon_dir],
        )

        _remove_incomplete_camoufox_assets()

        assert not mmdb_path.exists()
        assert not addon_dir.exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX execute bits only")
    async def test_fetch_repairs_supported_non_executable_cache(
        self, monkeypatch, tmp_path
    ):
        import camoufox.pkgman as pkgman

        executable = tmp_path / "camoufox-bin"
        executable.write_text("browser")
        executable.chmod(0o600)
        metadata = tmp_path / "version.json"
        metadata.write_text("supported")
        metadata.chmod(0o600)
        monkeypatch.setattr(pkgman, "INSTALL_DIR", tmp_path)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_browser_dir", lambda: tmp_path
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_binary_path",
            lambda: executable,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_assets_ready", lambda: True
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_ready_marker_valid", lambda: True
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._remove_incomplete_camoufox_assets",
            lambda: None,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_install_lock_path",
            lambda: tmp_path.parent / "camoufox-install.lock",
        )

        async def unexpected_subprocess(*_args, **_kwargs):
            raise AssertionError("permission repair must avoid a no-op fetch")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_subprocess)

        await _run_camoufox_fetch()

        assert os.access(executable, os.X_OK)
        assert os.access(metadata, os.X_OK)

    async def test_fetch_invalidates_supported_cache_with_missing_binary(
        self, monkeypatch, tmp_path
    ):
        import camoufox.pkgman as pkgman

        browser_dir = tmp_path / "browser"
        browser_dir.mkdir()
        _isolate_fetch_assets(monkeypatch, tmp_path, install_dir=browser_dir)
        (browser_dir / "version.json").write_text("claims-supported")
        missing_executable = browser_dir / "camoufox-bin"
        ready = False

        class FakeProcess:
            returncode = 0

            async def communicate(self):
                nonlocal ready
                assert not browser_dir.exists()
                ready = True
                return b"installed", b""

        async def fake_subprocess(*_args, **_kwargs):
            return FakeProcess()

        monkeypatch.setattr(pkgman, "INSTALL_DIR", browser_dir)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_browser_dir",
            lambda: browser_dir,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_binary_path",
            lambda: missing_executable,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._remove_incomplete_camoufox_assets",
            lambda: None,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: ready
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_components_ready",
            lambda: ready,
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_install_lock_path",
            lambda: tmp_path / "camoufox-install.lock",
        )

        await _run_camoufox_fetch()

        assert ready is True

    async def test_fetch_invalidates_supported_cache_with_empty_binary(
        self, monkeypatch, tmp_path
    ):
        import camoufox.pkgman as pkgman

        browser_dir = tmp_path / "browser"
        browser_dir.mkdir()
        _isolate_fetch_assets(monkeypatch, tmp_path, install_dir=browser_dir)
        (browser_dir / "version.json").write_text("claims-supported")
        executable = browser_dir / "camoufox-bin"
        executable.write_bytes(b"")
        if os.name != "nt":
            executable.chmod(0o700)
        ready = False

        class FakeProcess:
            returncode = 0

            async def communicate(self):
                nonlocal ready
                assert not browser_dir.exists()
                ready = True
                return b"installed", b""

        async def fake_subprocess(*_args, **_kwargs):
            return FakeProcess()

        monkeypatch.setattr(pkgman, "INSTALL_DIR", browser_dir)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_browser_dir",
            lambda: browser_dir,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_binary_path",
            lambda: executable,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._remove_incomplete_camoufox_assets",
            lambda: None,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: ready
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_components_ready",
            lambda: ready,
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_install_lock_path",
            lambda: tmp_path / "camoufox-install.lock",
        )

        await _run_camoufox_fetch()

        assert ready is True

    def test_marker_is_safe_when_directory_fsync_is_unsupported(
        self, monkeypatch, tmp_path
    ):
        import camoufox.pkgman as pkgman

        monkeypatch.setattr(pkgman, "INSTALL_DIR", tmp_path)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._fsync_directory",
            MagicMock(side_effect=OSError("directory fsync unsupported")),
        )

        _write_camoufox_ready_marker()

        marker = tmp_path / ".linkedin-mcp-ready.json"
        assert marker.is_file()
        assert json.loads(marker.read_text())["schema"] == 1

    async def test_fetch_invalidates_cache_with_corrupt_version_metadata(
        self, monkeypatch, tmp_path
    ):
        import camoufox.pkgman as pkgman

        browser_dir = tmp_path / "browser"
        browser_dir.mkdir()
        _isolate_fetch_assets(monkeypatch, tmp_path, install_dir=browser_dir)
        (browser_dir / "version.json").write_text("{truncated")
        ready = False

        class FakeProcess:
            returncode = 0

            async def communicate(self):
                nonlocal ready
                assert not browser_dir.exists()
                ready = True
                return b"installed", b""

        async def fake_subprocess(*_args, **_kwargs):
            return FakeProcess()

        monkeypatch.setattr(pkgman, "INSTALL_DIR", browser_dir)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._remove_incomplete_camoufox_assets",
            lambda: None,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: ready
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_components_ready",
            lambda: ready,
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_install_lock_path",
            lambda: tmp_path / "camoufox-install.lock",
        )

        await _run_camoufox_fetch()

        assert ready is True

    @pytest.mark.parametrize("full", [False, True])
    def test_cli_install_fetches_camoufox_only(
        self, isolate_profile_dir, monkeypatch, full
    ):
        _set_camoufox(monkeypatch, chrome_path="/usr/bin/chromium")
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: False
        )
        calls: list[str] = []

        async def fake_fetch() -> None:
            calls.append("camoufox")

        async def fail_patchright() -> None:
            raise AssertionError("Patchright installer must not run for Camoufox")

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_camoufox_fetch", fake_fetch
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_install_shell_only", fail_patchright
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._ensure_full_chromium_installed",
            fail_patchright,
        )

        ensure_browser_installed(full=full)

        assert calls == ["camoufox"]

    def test_cli_install_noops_when_camoufox_is_ready(
        self, isolate_profile_dir, monkeypatch
    ):
        _set_camoufox(monkeypatch)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: True
        )
        fetch = MagicMock()
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._run_camoufox_fetch", fetch)

        ensure_browser_installed(full=True)

        fetch.assert_not_called()

    async def test_fetch_exit_zero_without_binary_fails(self, monkeypatch, tmp_path):
        mmdb_path, addon_dir = _isolate_fetch_assets(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_components_ready", lambda: False
        )

        class FakeProcess:
            returncode = 0

            async def communicate(self):
                return b"download failed", b""

        async def fake_subprocess(*_args, **_kwargs):
            return FakeProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_install_lock_path",
            lambda: tmp_path / "camoufox-install.lock",
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: False
        )

        with pytest.raises(BrowserSetupFailedError, match="without installing"):
            await _run_camoufox_fetch()

        assert not mmdb_path.exists()
        assert not addon_dir.exists()

    async def test_fetch_timeout_terminates_child_and_clears_assets(
        self, monkeypatch, tmp_path
    ):
        mmdb_path, addon_dir = _isolate_fetch_assets(monkeypatch, tmp_path)
        release_child = asyncio.Event()

        class FakeProcess:
            returncode = None
            terminate_calls = 0

            async def communicate(self):
                await release_child.wait()
                return b"", b""

            async def wait(self):
                await release_child.wait()
                return self.returncode

            def terminate(self):
                self.terminate_calls += 1
                self.returncode = -15
                release_child.set()

            def kill(self):
                raise AssertionError("terminated process should already be reaped")

        process = FakeProcess()

        async def fake_subprocess(*_args, **_kwargs):
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_install_lock_path",
            lambda: tmp_path / "camoufox-install.lock",
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: False
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._CAMOUFOX_FETCH_TIMEOUT_SECONDS", 0.01
        )

        with pytest.raises(BrowserSetupFailedError, match="timed out after 0.01"):
            await _run_camoufox_fetch()

        assert process.terminate_calls == 1
        assert not mmdb_path.exists()
        assert not addon_dir.exists()

    async def test_fetch_rejects_upstream_soft_addon_failure_and_keeps_dirty(
        self, monkeypatch, tmp_path
    ):
        mmdb_path, addon_dir = _isolate_fetch_assets(monkeypatch, tmp_path)

        class FakeProcess:
            returncode = 0

            async def communicate(self):
                return b"Failed to download and extract UBO: truncated xpi", b""

        async def fake_subprocess(*_args, **_kwargs):
            return FakeProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_install_lock_path",
            lambda: tmp_path / "camoufox-install.lock",
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: False
        )

        with pytest.raises(BrowserSetupFailedError, match="truncated xpi"):
            await _run_camoufox_fetch()

        assert not mmdb_path.exists()
        assert not addon_dir.exists()

    async def test_fetch_invalidates_completion_marker_before_subprocess(
        self, monkeypatch, tmp_path
    ):
        import camoufox.pkgman as pkgman

        _isolate_fetch_assets(monkeypatch, tmp_path)
        install_dir = Path(pkgman.INSTALL_DIR)
        install_dir.mkdir(parents=True)
        executable = install_dir / "camoufox-bin"
        executable.write_text("browser")
        if os.name != "nt":
            executable.chmod(0o700)
        marker_path = install_dir / ".linkedin-mcp-ready.json"
        marker_path.write_text("old completion proof")

        class FakeProcess:
            returncode = 1

            async def communicate(self):
                assert not marker_path.exists()
                return b"", b"expected stop"

        async def fake_subprocess(*_args, **_kwargs):
            return FakeProcess()

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_browser_dir",
            lambda: install_dir,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_binary_path",
            lambda: executable,
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_install_lock_path",
            lambda: tmp_path / "camoufox-install.lock",
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: False
        )

        with pytest.raises(BrowserSetupFailedError, match="expected stop"):
            await _run_camoufox_fetch()

        assert not marker_path.exists()

    async def test_fetch_fails_closed_when_dirty_assets_cannot_be_removed(
        self, monkeypatch, tmp_path
    ):
        import camoufox.pkgman as pkgman

        _isolate_fetch_assets(monkeypatch, tmp_path)
        install_dir = Path(pkgman.INSTALL_DIR)
        install_dir.mkdir(parents=True, exist_ok=True)
        marker_path = install_dir / ".linkedin-mcp-ready.json"
        marker_path.write_text("old completion proof")
        subprocess = AsyncMock()

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_install_lock_path",
            lambda: tmp_path / "camoufox-install.lock",
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: False
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._remove_camoufox_fetch_assets",
            lambda: False,
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", subprocess)

        with pytest.raises(BrowserSetupFailedError, match="Could not clear"):
            await _run_camoufox_fetch()

        assert not marker_path.exists()
        subprocess.assert_not_awaited()

    async def test_failed_fetch_discards_even_valid_looking_partial_assets(
        self, monkeypatch, tmp_path
    ):
        mmdb_path, addon_dir = _isolate_fetch_assets(monkeypatch, tmp_path)

        class FakeProcess:
            returncode = 1

            async def communicate(self):
                return b"", b"addon download failed"

        async def fake_subprocess(*_args, **_kwargs):
            return FakeProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_install_lock_path",
            lambda: tmp_path / "camoufox-install.lock",
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: False
        )

        with pytest.raises(BrowserSetupFailedError, match="addon download failed"):
            await _run_camoufox_fetch()

        assert not mmdb_path.exists()
        assert not addon_dir.exists()

    async def test_communicate_error_reaps_child_and_discards_partial_assets(
        self, monkeypatch, tmp_path
    ):
        mmdb_path, addon_dir = _isolate_fetch_assets(monkeypatch, tmp_path)

        class FakeProcess:
            returncode = None
            terminate_calls = 0

            async def communicate(self):
                raise OSError("pipe read failed")

            def terminate(self):
                self.terminate_calls += 1
                self.returncode = -15

            def kill(self):
                raise AssertionError("terminate already reaped the child")

        process = FakeProcess()

        async def fake_subprocess(*_args, **_kwargs):
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_install_lock_path",
            lambda: tmp_path / "camoufox-install.lock",
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: False
        )

        with pytest.raises(OSError, match="pipe read failed"):
            await _run_camoufox_fetch()

        assert process.terminate_calls == 1
        assert not mmdb_path.exists()
        assert not addon_dir.exists()

    async def test_communicate_error_retains_lock_on_separate_process_wait(
        self, monkeypatch, tmp_path
    ):
        mmdb_path, addon_dir = _isolate_fetch_assets(monkeypatch, tmp_path)
        wait_started = asyncio.Event()
        release_wait = asyncio.Event()
        lock_path = tmp_path / "camoufox-install.lock"

        class FakeProcess:
            returncode = None

            async def communicate(self):
                mmdb_path.write_bytes(b"partial download")
                addon_dir.mkdir(parents=True)
                (addon_dir / "partial").write_bytes(b"incomplete")
                raise OSError("pipe failed before child exit")

            async def wait(self):
                wait_started.set()
                await release_wait.wait()
                self.returncode = -9
                return self.returncode

            def terminate(self):
                pass

            def kill(self):
                pass

        process = FakeProcess()

        async def fake_subprocess(*_args, **_kwargs):
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_install_lock_path",
            lambda: lock_path,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: False
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._SUBPROCESS_TERMINATE_TIMEOUT_SECONDS",
            0.01,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._CAMOUFOX_INSTALL_LOCK_POLL_SECONDS",
            0.001,
        )

        try:
            with pytest.raises(OSError, match="pipe failed"):
                await _run_camoufox_fetch()
            assert wait_started.is_set()
            assert mmdb_path.exists()
            assert addon_dir.exists()

            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.02):
                    async with _camoufox_install_lock(lock_path):
                        pass
        finally:
            release_wait.set()

        async with asyncio.timeout(1):
            while mmdb_path.exists() or addon_dir.exists():
                await asyncio.sleep(0)

        async with asyncio.timeout(1):
            async with _camoufox_install_lock(lock_path):
                pass

    def test_install_lock_serializes_independent_processes(self, tmp_path):
        lock_path = tmp_path / "camoufox-install.lock"
        context = multiprocessing.get_context("spawn")
        receive_connection, send_connection = context.Pipe(duplex=False)
        release_event = context.Event()
        child = context.Process(
            target=_camoufox_install_lock_probe,
            args=(lock_path, send_connection, release_event),
        )
        child.start()
        send_connection.close()

        async def attempt_while_owned() -> None:
            async with asyncio.timeout(0.05):
                async with _camoufox_install_lock(lock_path):
                    raise AssertionError("second process bypassed install lock")

        try:
            assert receive_connection.poll(5), "child did not acquire install lock"
            assert receive_connection.recv() == "acquired"
            with pytest.raises(TimeoutError):
                asyncio.run(attempt_while_owned())
        finally:
            release_event.set()
            receive_connection.close()
            child.join(timeout=10)
            if child.is_alive():
                child.terminate()
                child.join(timeout=10)

        assert child.exitcode == 0

        async def acquire_after_release() -> None:
            async with _camoufox_install_lock(lock_path):
                pass

        asyncio.run(acquire_after_release())

    async def test_concurrent_fetch_rechecks_ready_inside_install_lock(
        self, monkeypatch, tmp_path
    ):
        _isolate_fetch_assets(monkeypatch, tmp_path)
        ready = False
        release_fetch = asyncio.Event()
        fetch_started = asyncio.Event()
        subprocess_calls = 0

        class FakeProcess:
            returncode = 0

            async def communicate(self):
                nonlocal ready
                fetch_started.set()
                await release_fetch.wait()
                ready = True
                return b"installed", b""

        async def fake_subprocess(*_args, **_kwargs):
            nonlocal subprocess_calls
            subprocess_calls += 1
            return FakeProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_install_lock_path",
            lambda: tmp_path / "camoufox-install.lock",
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: ready
        )

        first = asyncio.create_task(_run_camoufox_fetch())
        await fetch_started.wait()
        second = asyncio.create_task(_run_camoufox_fetch())
        await asyncio.sleep(0)
        release_fetch.set()
        await asyncio.gather(first, second)

        assert subprocess_calls == 1

    async def test_cancelled_fetch_terminates_and_reaps_child(
        self, monkeypatch, tmp_path
    ):
        mmdb_path, addon_dir = _isolate_fetch_assets(monkeypatch, tmp_path)
        communicate_started = asyncio.Event()
        terminated = asyncio.Event()

        class FakeProcess:
            returncode = None
            terminate_calls = 0
            kill_calls = 0
            reaped = False

            async def communicate(self):
                communicate_started.set()
                await terminated.wait()
                self.returncode = -15
                self.reaped = True
                return b"", b"terminated"

            def terminate(self):
                self.terminate_calls += 1
                terminated.set()

            def kill(self):
                self.kill_calls += 1
                terminated.set()

        process = FakeProcess()

        async def fake_subprocess(*_args, **_kwargs):
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_install_lock_path",
            lambda: tmp_path / "camoufox-install.lock",
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: False
        )

        task = asyncio.create_task(_run_camoufox_fetch())
        await communicate_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert process.terminate_calls == 1
        assert process.kill_calls == 0
        assert process.reaped is True
        assert not mmdb_path.exists()
        assert not addon_dir.exists()

    async def test_unreaped_fetch_retains_lock_until_child_exits(
        self, monkeypatch, tmp_path
    ):
        mmdb_path, addon_dir = _isolate_fetch_assets(monkeypatch, tmp_path)
        communicate_started = asyncio.Event()
        release_child = asyncio.Event()
        lock_path = tmp_path / "camoufox-install.lock"

        class FakeProcess:
            returncode = None
            terminate_calls = 0
            kill_calls = 0

            async def communicate(self):
                communicate_started.set()
                mmdb_path.write_bytes(b"partial download")
                addon_dir.mkdir(parents=True)
                (addon_dir / "partial").write_bytes(b"incomplete")
                await release_child.wait()
                self.returncode = -9
                return b"", b"killed"

            def terminate(self):
                self.terminate_calls += 1

            def kill(self):
                self.kill_calls += 1

        process = FakeProcess()

        async def fake_subprocess(*_args, **_kwargs):
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_install_lock_path",
            lambda: lock_path,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: False
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._SUBPROCESS_TERMINATE_TIMEOUT_SECONDS",
            0.01,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._CAMOUFOX_INSTALL_LOCK_POLL_SECONDS",
            0.001,
        )

        try:
            task = asyncio.create_task(_run_camoufox_fetch())
            await communicate_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert process.terminate_calls == 1
            assert process.kill_calls == 1
            assert mmdb_path.exists()
            assert addon_dir.exists()
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.02):
                    async with _camoufox_install_lock(lock_path):
                        pass
        finally:
            release_child.set()

        async with asyncio.timeout(1):
            while mmdb_path.exists() or addon_dir.exists():
                await asyncio.sleep(0)

        async with asyncio.timeout(1):
            async with _camoufox_install_lock(lock_path):
                pass

    async def test_signal_errors_also_retain_fetch_lock(self, monkeypatch, tmp_path):
        _isolate_fetch_assets(monkeypatch, tmp_path)
        communicate_started = asyncio.Event()
        release_child = asyncio.Event()
        lock_path = tmp_path / "camoufox-install.lock"

        class FakeProcess:
            returncode = None

            async def communicate(self):
                communicate_started.set()
                await release_child.wait()
                self.returncode = 1
                return b"", b"permission denied"

            def terminate(self):
                raise PermissionError("terminate denied")

            def kill(self):
                raise PermissionError("kill denied")

        async def fake_subprocess(*_args, **_kwargs):
            return FakeProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._camoufox_install_lock_path",
            lambda: lock_path,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: False
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._SUBPROCESS_TERMINATE_TIMEOUT_SECONDS",
            0.01,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._CAMOUFOX_INSTALL_LOCK_POLL_SECONDS",
            0.001,
        )

        task = asyncio.create_task(_run_camoufox_fetch())
        await communicate_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.02):
                async with _camoufox_install_lock(lock_path):
                    pass

        release_child.set()
        await asyncio.sleep(0)
        async with asyncio.timeout(1):
            async with _camoufox_install_lock(lock_path):
                pass

    async def test_background_completion_stays_failed_when_binary_is_missing(
        self, isolate_profile_dir, monkeypatch
    ):
        _set_camoufox(monkeypatch)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: False
        )

        async def fake_fetch() -> None:
            return None

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_camoufox_fetch", fake_fetch
        )
        initialize_bootstrap("managed")

        await start_background_browser_setup_if_needed()
        state = get_bootstrap_state()
        assert state.setup_state is SetupState.RUNNING
        assert state.setup_task is not None
        await state.setup_task
        await _refresh_background_task_state()

        assert state.setup_state is SetupState.FAILED
        assert state.setup_task is None
        assert "binary is unavailable" in (state.last_error or "")

    async def test_background_fetch_marks_ready_only_after_binary_exists(
        self, isolate_profile_dir, monkeypatch
    ):
        _set_camoufox(monkeypatch)
        ready = False
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: ready
        )

        async def fake_fetch() -> None:
            nonlocal ready
            ready = True

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap._run_camoufox_fetch", fake_fetch
        )
        initialize_bootstrap("managed")

        await start_background_browser_setup_if_needed()
        state = get_bootstrap_state()
        assert state.setup_task is not None
        await state.setup_task
        await _refresh_background_task_state()

        assert state.setup_state is SetupState.READY

    async def test_docker_gate_fails_when_camoufox_binary_is_missing(
        self, isolate_profile_dir, monkeypatch
    ):
        _set_camoufox(monkeypatch)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.camoufox_ready", lambda: False
        )
        monkeypatch.setattr("linkedin_mcp_server.bootstrap._auth_ready", lambda: True)
        initialize_bootstrap("docker")

        with pytest.raises(BrowserSetupFailedError, match="missing from the Docker"):
            await ensure_tool_ready_or_raise("get_person_profile")

        assert get_bootstrap_state().setup_state is SetupState.FAILED


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

    @pytest.fixture(autouse=True)
    def _patchright_config(self, monkeypatch):
        _set_headless(monkeypatch, True)

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
            "linkedin_mcp_server.bootstrap._engine_self_manages_binary", lambda: False
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
        "linkedin_mcp_server.bootstrap.close_browser", AsyncMock(return_value=True)
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

        async def spy_close_browser() -> bool:
            order.append("close")
            return True

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

    async def test_uncertain_teardown_aborts_before_import(
        self, isolate_profile_dir, monkeypatch, _stub_import_env
    ):
        import_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(_IMPORT_TARGET, import_mock)
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.close_browser",
            AsyncMock(return_value=False),
        )

        with pytest.raises(NetworkError, match="teardown before session import"):
            await _try_auto_import_session()

        import_mock.assert_not_awaited()

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
