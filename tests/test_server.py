import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.server.middleware import MiddlewareContext
from starlette.testclient import TestClient

from linkedin_mcp_server.mcp_auth import StaticBearerAuthProvider
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

    async def test_create_mcp_server_configures_static_bearer_auth(self):
        mcp = create_mcp_server(mcp_auth_token="secret")

        assert isinstance(mcp.auth, StaticBearerAuthProvider)
        assert await mcp.auth.verify_token("wrong") is None
        access_token = await mcp.auth.verify_token("secret")
        assert access_token is not None
        assert access_token.client_id == "static-bearer-client"

    def test_streamable_http_rejects_missing_and_bad_bearer_token(self):
        mcp = create_mcp_server(mcp_auth_token="secret")
        app = mcp.http_app(path="/mcp", transport="streamable-http")
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            },
        }
        base_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        client = TestClient(app)
        missing = client.post("/mcp", headers=base_headers, json=payload)
        malformed = client.post(
            "/mcp",
            headers={**base_headers, "Authorization": "Basic secret"},
            json=payload,
        )
        wrong = client.post(
            "/mcp",
            headers={**base_headers, "Authorization": "Bearer wrong"},
            json=payload,
        )

        assert missing.status_code == 401
        assert malformed.status_code == 401
        assert wrong.status_code == 401

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
