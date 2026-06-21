import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import mcp.types as mt
import pytest
from fastmcp import FastMCP
from fastmcp.server.middleware import MiddlewareContext

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.server import create_mcp_server


@pytest.fixture(autouse=True)
def _stub_browser_config(monkeypatch):
    """Give the middleware teardown a clean local config.

    The middleware closes the browser after each tool call in ephemeral CDP
    mode by reading ``get_config()``. Under pytest the config singleton is reset
    between tests, so without this stub it would re-parse pytest's argv. A plain
    AppConfig (no CDP endpoint) makes the teardown a safe no-op.
    """
    monkeypatch.setattr(
        "linkedin_mcp_server.drivers.browser.get_config", lambda: AppConfig()
    )


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

    async def test_on_call_tool_closes_ephemeral_cdp_browser(self):
        middleware = SequentialToolExecutionMiddleware()
        call_next = AsyncMock(return_value=MagicMock())
        context = MiddlewareContext(
            message=mt.CallToolRequestParams(name="get_person_profile", arguments={}),
            method="tools/call",
            fastmcp_context=None,
        )

        with patch(
            "linkedin_mcp_server.drivers.browser.close_browser_after_tool_if_ephemeral",
            new_callable=AsyncMock,
        ) as mock_teardown:
            await middleware.on_call_tool(context, call_next)

        mock_teardown.assert_awaited_once()

    async def test_on_call_tool_closes_ephemeral_cdp_browser_even_on_error(self):
        middleware = SequentialToolExecutionMiddleware()
        call_next = AsyncMock(side_effect=RuntimeError("boom"))
        context = MiddlewareContext(
            message=mt.CallToolRequestParams(name="get_person_profile", arguments={}),
            method="tools/call",
            fastmcp_context=None,
        )

        with patch(
            "linkedin_mcp_server.drivers.browser.close_browser_after_tool_if_ephemeral",
            new_callable=AsyncMock,
        ) as mock_teardown:
            with pytest.raises(RuntimeError, match="boom"):
                await middleware.on_call_tool(context, call_next)

        mock_teardown.assert_awaited_once()
