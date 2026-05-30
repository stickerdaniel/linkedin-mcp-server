import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.server.middleware import MiddlewareContext
from starlette.routing import Route
from starlette.testclient import TestClient

from linkedin_mcp_server.health import HEALTH_PATH
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.server import create_mcp_server


class TestHealthEndpoint:
    def test_health_route_not_registered_for_stdio(self, monkeypatch):
        from linkedin_mcp_server.config import reset_config

        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
        monkeypatch.delenv("TRANSPORT", raising=False)
        reset_config()
        mcp = create_mcp_server()
        paths = [
            route.path
            for route in mcp._get_additional_http_routes()
            if isinstance(route, Route)
        ]
        assert HEALTH_PATH not in paths

    def test_health_returns_ok_without_auth(self, monkeypatch):
        from linkedin_mcp_server.config import reset_config

        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
        monkeypatch.setenv("TRANSPORT", "streamable-http")
        reset_config()
        monkeypatch.setenv("MCP_AUTH_MODE", "bearer")
        monkeypatch.setenv("MCP_BEARER_TOKEN", "test-token")
        mcp = create_mcp_server()
        app = mcp.http_app(transport="streamable-http")
        client = TestClient(app)

        response = client.get(HEALTH_PATH)

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_health_head_returns_ok(self, monkeypatch):
        from linkedin_mcp_server.config import reset_config

        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
        monkeypatch.setenv("TRANSPORT", "streamable-http")
        reset_config()
        mcp = create_mcp_server()
        app = mcp.http_app(transport="streamable-http")
        client = TestClient(app)

        response = client.head(HEALTH_PATH)

        assert response.status_code == 200


class TestSequentialToolExecutionMiddleware:
    async def test_create_mcp_server_registers_sequential_tool_middleware(
        self, monkeypatch
    ):
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
        mcp = create_mcp_server()

        assert any(
            isinstance(middleware, SequentialToolExecutionMiddleware)
            for middleware in mcp.middleware
        )

    async def test_create_mcp_server_enables_http_auth_when_configured(
        self, monkeypatch
    ):
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
        monkeypatch.setenv("TRANSPORT", "streamable-http")
        monkeypatch.setenv("MCP_AUTH_MODE", "bearer")
        monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
        monkeypatch.setenv("MCP_BEARER_TOKEN", "test-token")
        mcp = create_mcp_server()

        assert mcp.auth is not None

    async def test_create_mcp_server_enables_oauth_auth_when_configured(
        self, monkeypatch
    ):
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
        monkeypatch.setenv("TRANSPORT", "streamable-http")
        monkeypatch.setenv("MCP_AUTH_MODE", "oauth")
        monkeypatch.setenv("MCP_OAUTH_BASE_URL", "https://example.com")
        monkeypatch.setenv("MCP_OAUTH_CLIENT_ID", "cid")
        monkeypatch.setenv("MCP_OAUTH_CLIENT_SECRET", "secret")
        monkeypatch.setenv(
            "MCP_OAUTH_ALLOWED_REDIRECT_URIS",
            "https://claude.ai/api/mcp/auth_callback",
        )
        mcp = create_mcp_server()

        assert mcp.auth is not None

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
