"""Tests for dependencies.py — bootstrap gating and auto-relogin."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.core.exceptions import AuthenticationError, RateLimitError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
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

    async def test_mid_scrape_auth_error_triggers_relogin(self):
        """AuthenticationError caught in tool wrapper invokes handle_auth_error."""
        from linkedin_mcp_server.tools.person import register_person_tools

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
            with pytest.raises(AuthenticationStartedError):
                await tools["get_person_profile"](
                    linkedin_username="testuser",
                    ctx=mock_ctx,
                    extractor=mock_extractor,
                )

            mock_handle.assert_awaited_once()
            # First arg should be the AuthenticationError
            assert isinstance(mock_handle.call_args[0][0], AuthenticationError)
