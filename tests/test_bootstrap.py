import asyncio
import os
from unittest.mock import MagicMock

import pytest

from linkedin_mcp_server.bootstrap import (
    AuthState,
    ensure_tool_ready_or_raise,
    get_bootstrap_state,
    get_runtime_policy,
    initialize_bootstrap,
    install_metadata_path,
    browsers_path,
    reset_bootstrap_for_testing,
    SetupState,
    start_background_browser_setup_if_needed,
)
from linkedin_mcp_server.exceptions import (
    AuthenticationInProgressError,
    AuthenticationStartedError,
    BrowserSetupInProgressError,
    DockerHostLoginRequiredError,
)


class TestBootstrap:
    async def test_managed_startup_starts_background_setup(self, monkeypatch):
        async def fake_setup() -> None:
            return None

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

    async def test_setup_in_progress_raises(self):
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

        initialize_bootstrap("managed")
        state = get_bootstrap_state()
        state.auth_state = AuthState.IN_PROGRESS
        state.login_task = MagicMock(done=lambda: False)

        with pytest.raises(AuthenticationInProgressError):
            await ensure_tool_ready_or_raise("get_person_profile")

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

    async def test_cancelled_setup_task_retries_cleanly(self):
        initialize_bootstrap("managed")
        state = get_bootstrap_state()
        task = asyncio.create_task(asyncio.sleep(10), name="browser-setup")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        state.setup_task = task

        with pytest.raises(BrowserSetupInProgressError):
            await ensure_tool_ready_or_raise("search_jobs")

        assert state.setup_state is SetupState.RUNNING
        assert state.setup_task is not None

    def test_managed_browser_path_defaults_under_auth_root(self, isolate_profile_dir):
        path = browsers_path()
        assert path == isolate_profile_dir.parent / "patchright-browsers"

    def test_install_metadata_path_defaults_under_auth_root(self, isolate_profile_dir):
        path = install_metadata_path()
        assert path == isolate_profile_dir.parent / "browser-install.json"

    def test_runtime_policy_uses_initialized_value(self):
        initialize_bootstrap("managed")
        assert get_runtime_policy() == "managed"
