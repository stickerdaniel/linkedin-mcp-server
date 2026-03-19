import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.server.middleware import MiddlewareContext

from linkedin_mcp_server.config.schema import OAuthConfig
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.server import create_mcp_server


class TestSequentialToolExecutionMiddleware:
    async def test_create_mcp_server_registers_sequential_tool_middleware(self):
        mcp = create_mcp_server()

        assert any(
            isinstance(middleware, SequentialToolExecutionMiddleware)
            for middleware in mcp.middleware
        )

    async def test_sequential_tool_middleware_serializes_parallel_tool_calls(self):
        mcp = FastMCP("test")
        mcp.add_middleware(SequentialToolExecutionMiddleware())

        active_calls = 0
        max_active_calls = 0

        @mcp.tool
        async def slow_tool(delay: float = 0.05) -> dict[str, float]:
            nonlocal active_calls, max_active_calls
            active_calls += 1
            max_active_calls = max(max_active_calls, active_calls)
            try:
                await asyncio.sleep(delay)
                return {"delay": delay}
            finally:
                active_calls -= 1

        result_one, result_two = await asyncio.gather(
            mcp.call_tool("slow_tool", {"delay": 0.05}),
            mcp.call_tool("slow_tool", {"delay": 0.05}),
        )

        assert max_active_calls == 1
        assert result_one.structured_content == {"delay": 0.05}
        assert result_two.structured_content == {"delay": 0.05}

    async def test_sequential_tool_middleware_preserves_tool_results(self):
        mcp = FastMCP("test")
        mcp.add_middleware(SequentialToolExecutionMiddleware())

        @mcp.tool
        async def simple_tool(value: int) -> dict[str, int]:
            return {"value": value}

        result = await mcp.call_tool("simple_tool", {"value": 7})

        assert result.structured_content == {"value": 7}

    async def test_sequential_tool_middleware_reports_queue_progress(self):
        middleware = SequentialToolExecutionMiddleware()
        fastmcp_context = MagicMock()
        fastmcp_context.request_context = object()
        fastmcp_context.report_progress = AsyncMock()
        call_next = AsyncMock(return_value=MagicMock())
        context = MiddlewareContext(
            message=mt.CallToolRequestParams(name="slow_tool", arguments={}),
            method="tools/call",
            fastmcp_context=fastmcp_context,
        )

        await middleware.on_call_tool(context, call_next)

        fastmcp_context.report_progress.assert_has_awaits(
            [
                call(
                    progress=0,
                    total=100,
                    message="Queued waiting for scraper lock",
                ),
                call(
                    progress=0,
                    total=100,
                    message="Scraper lock acquired, starting tool",
                ),
            ]
        )


class TestServerAuth:
    async def test_create_mcp_server_no_auth_by_default(self):
        mcp = create_mcp_server()
        assert mcp.auth is None

    async def test_create_mcp_server_with_oauth(self):
        from linkedin_mcp_server.auth import PasswordOAuthProvider

        oauth_config = OAuthConfig(
            enabled=True,
            base_url="https://example.com",
            password="secret",
        )
        mcp = create_mcp_server(oauth_config=oauth_config)
        assert isinstance(mcp.auth, PasswordOAuthProvider)
