import os

import pytest
from linkedin_mcp_server.bootstrap import (
    browsers_path,
    ensure_tool_ready_or_raise,
    initialize_bootstrap,
    reset_bootstrap_for_testing,
    start_background_browser_setup_if_needed,
)
from linkedin_mcp_server.exceptions import AuthenticationError


class TestBootstrap:
    async def test_start_background_is_noop(self):
        initialize_bootstrap()
        await start_background_browser_setup_if_needed()

    async def test_ensure_tool_ready_raises_when_no_browser(self, monkeypatch, tmp_path):
        browser_dir = tmp_path / "browsers"
        browser_dir.mkdir()
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browser_dir))
        initialize_bootstrap()

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
