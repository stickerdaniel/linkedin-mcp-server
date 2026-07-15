"""Tests for dependencies.py — bootstrap gating and auto-relogin."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    NetworkError,
    RateLimitError,
)
from linkedin_mcp_server.dependencies import (
    _ensure_browser_alive,
    _maybe_check_ip_drift,
    _reset_ip_drift_call_counter_for_testing,
    get_ready_extractor,
    handle_auth_error,
)
from linkedin_mcp_server.exceptions import (
    AuthenticationStartedError,
    DockerHostLoginRequiredError,
)


class TestHandleAuthError:
    async def test_managed_triggers_relogin(self):
        """On managed runtime, close browser + trigger relogin."""
        with (
            patch(
                "linkedin_mcp_server.dependencies.get_runtime_policy",
                return_value="managed",
            ),
            patch(
                "linkedin_mcp_server.dependencies.close_browser",
                new_callable=AsyncMock,
            ) as mock_close,
            patch(
                "linkedin_mcp_server.dependencies.invalidate_auth_and_trigger_relogin",
                new_callable=AsyncMock,
                side_effect=AuthenticationStartedError("login opened"),
            ) as mock_relogin,
        ):
            with pytest.raises(AuthenticationStartedError):
                await handle_auth_error(
                    AuthenticationError("Session expired"), ctx=None
                )

            mock_close.assert_awaited_once()
            mock_relogin.assert_awaited_once_with(None)

    async def test_docker_raises_host_error(self):
        """On Docker runtime, raise DockerHostLoginRequiredError."""
        with patch(
            "linkedin_mcp_server.dependencies.get_runtime_policy",
            return_value="docker",
        ):
            with pytest.raises(DockerHostLoginRequiredError, match="host machine"):
                await handle_auth_error(
                    AuthenticationError("Session expired"), ctx=None
                )


class TestGetReadyExtractor:
    async def test_ready_resumes_to_scrape_path(self):
        """When gating returns (login resolved in-budget), control falls through
        to get_or_create_browser + ensure_authenticated and returns an extractor.
        """
        browser = MagicMock()
        browser.page = MagicMock()
        browser.page.evaluate = AsyncMock(return_value="1")
        with (
            patch(
                "linkedin_mcp_server.dependencies.ensure_tool_ready_or_raise",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
                return_value=browser,
            ) as mock_get_browser,
            patch(
                "linkedin_mcp_server.dependencies.ensure_authenticated",
                new_callable=AsyncMock,
            ) as mock_ensure_auth,
            patch(
                "linkedin_mcp_server.dependencies.get_config",
                return_value=AppConfig(),
            ),
        ):
            from linkedin_mcp_server.scraping import LinkedInExtractor

            extractor = await get_ready_extractor(ctx=None, tool_name="test_tool")

            assert isinstance(extractor, LinkedInExtractor)
            assert extractor._engine == "patchright"
            mock_get_browser.assert_awaited_once()
            mock_ensure_auth.assert_awaited_once()

    async def test_auth_error_triggers_relogin(self):
        """AuthenticationError from ensure_authenticated triggers relogin."""
        with (
            patch(
                "linkedin_mcp_server.dependencies.ensure_tool_ready_or_raise",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.ensure_authenticated",
                new_callable=AsyncMock,
                side_effect=AuthenticationError("Session expired or invalid."),
            ),
            patch(
                "linkedin_mcp_server.dependencies.handle_auth_error",
                new_callable=AsyncMock,
                side_effect=AuthenticationStartedError("login opened"),
            ) as mock_handle,
        ):
            with pytest.raises(AuthenticationStartedError):
                await get_ready_extractor(ctx=None, tool_name="test_tool")

            mock_handle.assert_awaited_once()

    async def test_non_auth_error_uses_standard_handler(self):
        """RateLimitError goes through raise_tool_error, not relogin."""
        with (
            patch(
                "linkedin_mcp_server.dependencies.ensure_tool_ready_or_raise",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
                side_effect=RateLimitError("Too many requests"),
            ),
            patch(
                "linkedin_mcp_server.dependencies.handle_auth_error",
                new_callable=AsyncMock,
            ) as mock_handle,
        ):
            with pytest.raises(ToolError, match="Rate limit"):
                await get_ready_extractor(ctx=None, tool_name="test_tool")

            mock_handle.assert_not_awaited()

    async def test_browser_binary_missing_invalidates_and_raises_actionable(self):
        """Patchright "Executable doesn't exist" surfaces as actionable BrowserBinaryMissingError, and metadata is dropped."""
        err = NetworkError(
            "Failed to start browser: BrowserType.launch_persistent_context: "
            "Executable doesn't exist at /tmp/foo/chrome-headless-shell. "
            "Looks like Playwright was just installed or updated. "
            "Please run the following command to download new browsers: patchright install"
        )
        with (
            patch(
                "linkedin_mcp_server.dependencies.ensure_tool_ready_or_raise",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
                side_effect=err,
            ),
            patch(
                "linkedin_mcp_server.dependencies.invalidate_browser_setup"
            ) as mock_invalidate,
        ):
            with pytest.raises(
                ToolError, match="Patchright Chromium browser is missing"
            ):
                await get_ready_extractor(ctx=None, tool_name="test_tool")

            mock_invalidate.assert_called_once()

    async def test_unrelated_network_error_is_not_treated_as_binary_missing(self):
        """A generic connection error must not call invalidate_browser_setup or surface the binary-missing copy."""
        err = NetworkError("Failed to start browser: connection reset by peer")
        with (
            patch(
                "linkedin_mcp_server.dependencies.ensure_tool_ready_or_raise",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
                side_effect=err,
            ),
            patch(
                "linkedin_mcp_server.dependencies.invalidate_browser_setup"
            ) as mock_invalidate,
        ):
            with pytest.raises(ToolError, match="Network error"):
                await get_ready_extractor(ctx=None, tool_name="test_tool")

            mock_invalidate.assert_not_called()

    async def test_mid_scrape_auth_error_triggers_relogin(self, monkeypatch):
        """AuthenticationError caught in tool wrapper invokes handle_auth_error."""
        from linkedin_mcp_server.config.schema import AppConfig
        from linkedin_mcp_server.tools.person import register_person_tools

        # get_person_profile resolves a StealthProfile via get_config() for
        # rate-limit pacing before it ever reaches scrape_person() -- without
        # this, the REAL get_config() parses pytest's own sys.argv and
        # SystemExits. Same pattern as test_browser_driver.py's _mock_config.
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.person.get_config", lambda: AppConfig()
        )

        mock_mcp = MagicMock()
        tools = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

        mock_mcp.tool = capture_tool
        register_person_tools(mock_mcp)

        mock_extractor = AsyncMock()
        mock_extractor.scrape_person = AsyncMock(
            side_effect=AuthenticationError("Auth barrier detected")
        )

        mock_ctx = MagicMock()
        mock_ctx.report_progress = AsyncMock()

        with patch(
            "linkedin_mcp_server.tools.person.handle_auth_error",
            new_callable=AsyncMock,
            side_effect=AuthenticationStartedError("login opened"),
        ) as mock_handle:
            with pytest.raises(ToolError, match="login opened"):
                await tools["get_person_profile"](
                    linkedin_username="testuser",
                    ctx=mock_ctx,
                    extractor=mock_extractor,
                )

            mock_handle.assert_awaited_once()
            # First arg should be the AuthenticationError
            assert isinstance(mock_handle.call_args[0][0], AuthenticationError)


class TestMaybeCheckIpDrift:
    @pytest.fixture(autouse=True)
    def _reset_counter(self):
        _reset_ip_drift_call_counter_for_testing()
        yield
        _reset_ip_drift_call_counter_for_testing()

    async def test_skips_entirely_without_a_proxy_configured(self):
        with (
            patch(
                "linkedin_mcp_server.dependencies.get_config",
                return_value=AppConfig(),
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_ip_drift_monitor"
            ) as mock_get_monitor,
        ):
            for _ in range(50):
                await _maybe_check_ip_drift(MagicMock())
            mock_get_monitor.assert_not_called()

    async def test_checks_only_every_nth_call_when_proxy_configured(self):
        config = AppConfig()
        config.browser.proxy_server = "http://proxy.example.com:8080"
        mock_monitor = MagicMock()
        mock_monitor.check = AsyncMock()

        with (
            patch("linkedin_mcp_server.dependencies.get_config", return_value=config),
            patch(
                "linkedin_mcp_server.dependencies.get_ip_drift_monitor",
                return_value=mock_monitor,
            ),
        ):
            page = MagicMock()
            for _ in range(19):
                await _maybe_check_ip_drift(page)
            mock_monitor.check.assert_not_called()

            await _maybe_check_ip_drift(page)  # 20th call
            mock_monitor.check.assert_awaited_once_with(page)

    async def test_a_failed_check_does_not_raise(self):
        config = AppConfig()
        config.browser.proxy_server = "http://proxy.example.com:8080"
        mock_monitor = MagicMock()
        mock_monitor.check = AsyncMock(side_effect=RuntimeError("boom"))

        with (
            patch("linkedin_mcp_server.dependencies.get_config", return_value=config),
            patch(
                "linkedin_mcp_server.dependencies.get_ip_drift_monitor",
                return_value=mock_monitor,
            ),
        ):
            for _ in range(20):
                await _maybe_check_ip_drift(MagicMock())  # does not raise


class TestEnsureBrowserAlive:
    async def test_alive_browser_is_returned_unchanged(self):
        browser = MagicMock()
        browser.page.evaluate = AsyncMock(return_value="1")

        with patch(
            "linkedin_mcp_server.dependencies.get_or_create_browser",
            new_callable=AsyncMock,
        ) as mock_get_browser:
            result = await _ensure_browser_alive(browser)

        assert result is browser
        mock_get_browser.assert_not_called()

    async def test_transport_failure_triggers_one_relaunch(self):
        dead_browser = MagicMock()
        dead_browser.page.evaluate = AsyncMock(
            side_effect=RuntimeError("Target page, context or browser has been closed")
        )
        fresh_browser = MagicMock()

        with (
            patch(
                "linkedin_mcp_server.dependencies.close_browser",
                new_callable=AsyncMock,
            ) as mock_close,
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
                return_value=fresh_browser,
            ) as mock_get_browser,
        ):
            result = await _ensure_browser_alive(dead_browser)

        assert result is fresh_browser
        mock_close.assert_awaited_once()
        mock_get_browser.assert_awaited_once()

    async def test_semantic_failure_propagates_without_relaunch(self):
        browser = MagicMock()
        browser.page.evaluate = AsyncMock(side_effect=RuntimeError("some other error"))

        with (
            patch(
                "linkedin_mcp_server.dependencies.close_browser",
                new_callable=AsyncMock,
            ) as mock_close,
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
            ) as mock_get_browser,
        ):
            with pytest.raises(RuntimeError, match="some other error"):
                await _ensure_browser_alive(browser)

        mock_close.assert_not_called()
        mock_get_browser.assert_not_called()
