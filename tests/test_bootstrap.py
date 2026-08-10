import os
import subprocess

import pytest
from linkedin_mcp_server.bootstrap import (
    browsers_path,
    ensure_browser_installed,
    ensure_tool_ready_or_raise,
    initialize_bootstrap,
    reset_bootstrap_for_testing,
    start_background_browser_setup_if_needed,
)
from linkedin_mcp_server.exceptions import AuthenticationError


class TestEnsureBrowserInstalled:
    def test_returns_true_when_already_present(self, tmp_path):
        browser_dir = tmp_path / "browsers"
        browser_dir.mkdir()
        (browser_dir / "chromium-1234").mkdir()

        assert ensure_browser_installed(browser_dir) is True

    def test_installs_when_missing(self, tmp_path, monkeypatch):
        browser_dir = tmp_path / "browsers"
        calls = []

        def fake_run(cmd, env=None, check=None, timeout=None):
            calls.append((cmd, env, check, timeout))
            browser_dir.mkdir(parents=True)
            (browser_dir / "chromium-1234").mkdir()
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert ensure_browser_installed(browser_dir) is True
        assert len(calls) == 1
        cmd, env, check, _timeout = calls[0]
        assert cmd[-2:] == ["install", "chromium"]
        assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(browser_dir)
        assert check is True

    def test_returns_false_on_install_failure(self, tmp_path, monkeypatch):
        browser_dir = tmp_path / "browsers"

        def fake_run(cmd, env=None, check=None, timeout=None):  # noqa: ARG001
            raise subprocess.CalledProcessError(1, cmd)

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert ensure_browser_installed(browser_dir) is False

    def test_returns_false_when_install_reports_success_but_dir_still_empty(
        self, tmp_path, monkeypatch
    ):
        browser_dir = tmp_path / "browsers"

        def fake_run(cmd, env=None, check=None, timeout=None):  # noqa: ARG001
            return subprocess.CompletedProcess(cmd, 0)  # no-op, dir stays empty

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert ensure_browser_installed(browser_dir) is False


class TestBootstrap:
    async def test_start_background_self_heals_missing_browser(self, monkeypatch, tmp_path):
        browser_dir = tmp_path / "browsers"
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browser_dir))
        initialize_bootstrap()

        calls = []
        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.ensure_browser_installed",
            lambda *_a, **_k: calls.append(True) or True,
        )

        await start_background_browser_setup_if_needed()
        assert calls == [True]

    async def test_ensure_tool_ready_self_heals_then_raises_on_missing_session(
        self, monkeypatch, tmp_path
    ):
        """Browser missing but auto-install succeeds -- should fall through to the
        session check, not the browser-missing error."""
        browser_dir = tmp_path / "browsers"
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browser_dir))
        initialize_bootstrap()

        def fake_ensure_browser_installed(path):
            path.mkdir(parents=True, exist_ok=True)
            (path / "chromium-1234").mkdir(exist_ok=True)
            return True

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.ensure_browser_installed",
            fake_ensure_browser_installed,
        )

        with pytest.raises(AuthenticationError, match="No valid LinkedIn session"):
            await ensure_tool_ready_or_raise("search_jobs")

    async def test_ensure_tool_ready_raises_when_no_browser_and_autoinstall_fails(
        self, monkeypatch, tmp_path
    ):
        browser_dir = tmp_path / "browsers"
        browser_dir.mkdir()
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browser_dir))
        initialize_bootstrap()

        monkeypatch.setattr(
            "linkedin_mcp_server.bootstrap.ensure_browser_installed",
            lambda *_a, **_k: False,
        )

        with pytest.raises(AuthenticationError, match="not installed"):
            await ensure_tool_ready_or_raise("search_jobs")

    async def test_ensure_tool_ready_raises_when_no_session(self, monkeypatch, tmp_path):
        browser_dir = tmp_path / "browsers"
        browser_dir.mkdir()
        (browser_dir / "chromium-1234").mkdir()
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browser_dir))
        initialize_bootstrap()

        with pytest.raises(AuthenticationError, match="No valid LinkedIn session"):
            await ensure_tool_ready_or_raise("get_person_profile")

    def test_reset_bootstrap_clears_state(self):
        initialize_bootstrap()
        reset_bootstrap_for_testing()
        assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ

    def test_reset_bootstrap_clears_browser_env_var(self):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/tmp/stale-browser-cache"
        reset_bootstrap_for_testing()
        assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ

    def test_managed_browser_path_defaults_under_auth_root(self, isolate_profile_dir):
        path = browsers_path()
        assert path == isolate_profile_dir.parent / "patchright-browsers"
