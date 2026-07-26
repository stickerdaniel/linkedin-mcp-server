import asyncio
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


class TestBrowserLifespan:
    """The lifespan owns the background handoff poller.

    Nothing else starts it, and nothing else stops it. Without the poller an
    owner that goes idle never notices a waiting process; without the cancel a
    stdio server would not exit.
    """

    @staticmethod
    def _patched(monkeypatch, watcher):
        """Replace the lifespan's collaborators, returning the close spy."""
        import linkedin_mcp_server.server as server_module

        monkeypatch.setattr(server_module, "initialize_bootstrap", MagicMock())
        monkeypatch.setattr(server_module, "get_runtime_policy", MagicMock())
        monkeypatch.setattr(
            server_module, "start_background_browser_setup_if_needed", AsyncMock()
        )
        monkeypatch.setattr(server_module, "watch_for_handoff_requests", watcher)
        close_browser = AsyncMock()
        monkeypatch.setattr(server_module, "close_browser", close_browser)
        return close_browser

    async def test_poller_runs_for_the_life_of_the_server(self, monkeypatch):
        from linkedin_mcp_server.server import browser_lifespan

        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def watcher():
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        close_browser = self._patched(monkeypatch, watcher)

        async with browser_lifespan(MagicMock()):
            await asyncio.wait_for(started.wait(), timeout=1)
            assert not cancelled.is_set()
            close_browser.assert_not_awaited()

        assert cancelled.is_set()
        close_browser.assert_awaited_once()

    async def test_a_dead_poller_still_lets_the_browser_close(self, monkeypatch):
        """A poller that raised must not take the browser down with it.

        Awaiting a task that already failed re-raises its exception. If that
        propagated out of the teardown, ``close_browser`` would be skipped and
        Chromium would stay on the shared profile past shutdown — exactly the
        corruption the lease exists to prevent.
        """
        from linkedin_mcp_server.server import browser_lifespan

        async def watcher():
            raise RuntimeError("poller crashed")

        close_browser = self._patched(monkeypatch, watcher)

        async with browser_lifespan(MagicMock()):
            # Let the doomed task reach its exception before teardown.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        close_browser.assert_awaited_once()

    async def test_a_fatal_poller_error_still_closes_but_propagates(self, monkeypatch):
        """A BaseException is not ours to swallow, but the browser still closes.

        ``Exception`` is caught deliberately; ``KeyboardInterrupt`` and friends
        must keep travelling. What must not happen is that they take the profile
        with them by skipping the close.
        """
        from linkedin_mcp_server.server import browser_lifespan

        class Fatal(BaseException):
            """Stands in for KeyboardInterrupt, which would abort the run."""

        async def watcher():
            raise Fatal("fatal")

        close_browser = self._patched(monkeypatch, watcher)

        with pytest.raises(Fatal):
            async with browser_lifespan(MagicMock()):
                await asyncio.sleep(0)
                await asyncio.sleep(0)

        close_browser.assert_awaited_once()

    async def test_teardown_closes_the_browser_when_the_body_raises(self, monkeypatch):
        from linkedin_mcp_server.server import browser_lifespan

        async def watcher():
            await asyncio.sleep(3600)

        close_browser = self._patched(monkeypatch, watcher)

        with pytest.raises(RuntimeError, match="boom"):
            async with browser_lifespan(MagicMock()):
                raise RuntimeError("boom")

        close_browser.assert_awaited_once()


class TestServerVersion:
    def test_create_mcp_server_advertises_package_version(self):
        # Without an explicit version=, FastMCP advertises its own library
        # version in serverInfo instead of ours.
        mcp = create_mcp_server()

        assert mcp.version == __version__
