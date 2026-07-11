import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import mcp.types as mt
import pytest
from fastmcp import FastMCP
from fastmcp.server.middleware import MiddlewareContext

from linkedin_mcp_server import __version__
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.server import create_mcp_server
from linkedin_mcp_server.tools.meta import (
    LEGACY_TOOL_NAMES,
    META_HEALTH_TIMEOUT_SECONDS,
    META_PING_TIMEOUT_SECONDS,
    META_TOOL_NAMES,
    build_health_payload,
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

    async def test_create_mcp_server_masks_error_details(self):
        mcp = create_mcp_server()

        assert mcp._mask_error_details is True

    async def test_linkedin_health_returns_session_paths(self):
        payload = build_health_payload()

        assert payload["server"] == "linkedin-mcp"
        assert payload["mask_error_details"] is True
        assert payload["transport"] in {"stdio", "streamable-http"}
        assert "profile_dir" in payload["storage"]
        assert "patchright_browsers_dir" in payload["storage"]
        assert "lifecycle" in payload["session"]

    async def test_legacy_tool_names_match_registered_scraper_tools(self):
        mcp = create_mcp_server()
        tools = await mcp.list_tools(run_middleware=False)
        legacy_names = {
            tool.name
            for tool in tools
            if not tool.name.startswith("linkedin_")
            and tool.name not in META_TOOL_NAMES
        }

        assert set(LEGACY_TOOL_NAMES) == legacy_names

    async def test_linkedin_ping_lists_tools_and_aliases(self):
        mcp = create_mcp_server()

        health_tool = await mcp.get_tool("linkedin_health")
        ping_tool = await mcp.get_tool("linkedin_ping")
        assert health_tool is not None
        assert ping_tool is not None
        assert health_tool.timeout == META_HEALTH_TIMEOUT_SECONDS
        assert ping_tool.timeout == META_PING_TIMEOUT_SECONDS

        result = await mcp.call_tool("linkedin_ping", {})
        payload = result.structured_content
        assert payload is not None
        assert payload["ok"] is True
        assert payload["pong"] is True
        assert payload["tool_count"] >= len(LEGACY_TOOL_NAMES) + 2
        tool_names = {tool["name"] for tool in payload["tools"]}
        assert "linkedin_health" in tool_names
        assert "linkedin_ping" in tool_names
        assert "linkedin_get_person_profile" in tool_names
        assert "get_person_profile" in tool_names

    async def test_manifest_lists_all_registered_tools(self):
        manifest_path = Path(__file__).resolve().parents[1] / "manifest.json"
        manifest_names = {
            tool["name"] for tool in json.loads(manifest_path.read_text())["tools"]
        }

        mcp = create_mcp_server()
        tools = await mcp.list_tools(run_middleware=False)
        registered = {tool.name for tool in tools}

        missing = registered - manifest_names
        assert not missing, f"manifest.json missing tools: {sorted(missing)}"

    async def test_meta_tools_bypass_scraper_lock(self):
        mcp = FastMCP("test")
        mcp.add_middleware(SequentialToolExecutionMiddleware())

        lock_held = asyncio.Event()
        release = asyncio.Event()

        @mcp.tool
        async def slow_tool() -> dict[str, bool]:
            lock_held.set()
            await release.wait()
            return {"done": True}

        @mcp.tool(name="linkedin_health")
        async def linkedin_health() -> dict[str, bool]:
            return {"ok": True}

        slow_task = asyncio.create_task(mcp.call_tool("slow_tool", {}))
        await lock_held.wait()

        result = await asyncio.wait_for(
            mcp.call_tool("linkedin_health", {}),
            timeout=0.5,
        )
        assert result.structured_content == {"ok": True}

        release.set()
        await slow_task

    async def test_meta_tools_skip_queue_progress(self):
        middleware = SequentialToolExecutionMiddleware()
        fastmcp_context = MagicMock()
        fastmcp_context.request_context = object()
        fastmcp_context.report_progress = AsyncMock()
        call_next = AsyncMock(return_value=MagicMock())
        context = MiddlewareContext(
            message=mt.CallToolRequestParams(name="linkedin_health", arguments={}),
            method="tools/call",
            fastmcp_context=fastmcp_context,
        )

        await middleware.on_call_tool(context, call_next)

        fastmcp_context.report_progress.assert_not_awaited()
        call_next.assert_awaited_once()

    async def test_linkedin_tool_alias_matches_legacy_tool(self):
        mcp = create_mcp_server()

        legacy = await mcp.get_tool("get_person_profile")
        aliased = await mcp.get_tool("linkedin_get_person_profile")

        assert legacy is not None
        assert aliased is not None
        assert legacy.description == aliased.description

    async def test_linkedin_close_session_alias_registered_after_close_session(self):
        mcp = create_mcp_server()

        legacy = await mcp.get_tool("close_session")
        aliased = await mcp.get_tool("linkedin_close_session")

        assert legacy is not None
        assert aliased is not None
        assert legacy.description == aliased.description

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


class TestServerVersion:
    def test_create_mcp_server_advertises_package_version(self):
        # Without an explicit version=, FastMCP advertises its own library
        # version in serverInfo instead of ours.
        mcp = create_mcp_server()

        assert mcp.version == __version__


class TestBrowserLifespan:
    async def test_browser_lifespan_runs_bootstrap_and_closes_browser(self, monkeypatch):
        from linkedin_mcp_server.server import browser_lifespan

        init = MagicMock()
        start_setup = AsyncMock()
        close = AsyncMock()
        monkeypatch.setattr(
            "linkedin_mcp_server.server.initialize_bootstrap", init
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.server.start_background_browser_setup_if_needed",
            start_setup,
        )
        monkeypatch.setattr("linkedin_mcp_server.server.close_browser", close)

        mcp = create_mcp_server()
        async with browser_lifespan(mcp):
            init.assert_called_once()
            start_setup.assert_awaited_once()

        close.assert_awaited_once()


class TestCloseSession:
    async def test_close_session_success(self, monkeypatch):
        close = AsyncMock()
        monkeypatch.setattr("linkedin_mcp_server.server.close_browser", close)

        mcp = create_mcp_server()
        result = await mcp.call_tool("close_session", {})

        close.assert_awaited_once()
        assert result.structured_content == {
            "status": "success",
            "message": "Successfully closed the browser session and cleaned up resources",
        }

    async def test_close_session_raises_tool_error_on_failure(self, monkeypatch):
        from fastmcp.exceptions import ToolError

        monkeypatch.setattr(
            "linkedin_mcp_server.server.close_browser",
            AsyncMock(side_effect=RuntimeError("browser stuck")),
        )

        mcp = create_mcp_server()
        with pytest.raises(
            ToolError, match="Failed to close the browser session"
        ):
            await mcp.call_tool("close_session", {})
