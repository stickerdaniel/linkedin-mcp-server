"""Middleware that serializes MCP tool execution across server processes."""

from __future__ import annotations

import asyncio
import logging
import time

import mcp.types as mt

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.exceptions import BrowserBusyError
from linkedin_mcp_server.profile_lease import get_profile_lease

logger = logging.getLogger(__name__)


class SequentialToolExecutionMiddleware(Middleware):
    """Ensure only one tool call at a time drives the shared LinkedIn browser.

    Two layers, because one is not enough:

    * an ``asyncio.Lock`` serializes calls inside this process, where several MCP
      sessions can share one server;
    * the profile lease serializes calls across processes, where each MCP client
      instance spawns its own server against the same Chromium profile.

    Without the second layer two processes open that profile simultaneously and
    the last one to close silently overwrites the other's cookies.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def _report_progress(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        *,
        message: str,
    ) -> None:
        fastmcp_context = context.fastmcp_context
        if fastmcp_context is None or fastmcp_context.request_context is None:
            return

        await fastmcp_context.report_progress(
            progress=0,
            total=100,
            message=message,
        )

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        tool_name = context.message.name
        wait_started = time.perf_counter()
        logger.debug("Waiting for scraper lock for tool '%s'", tool_name)
        await self._report_progress(
            context,
            message="Queued waiting for scraper lock",
        )

        async with self._lock:
            wait_seconds = time.perf_counter() - wait_started
            logger.debug(
                "Acquired scraper lock for tool '%s' after %.3fs",
                tool_name,
                wait_seconds,
            )
            await self._report_progress(
                context,
                message="Scraper lock acquired, starting tool",
            )
            return await self._run_owning_the_profile(context, call_next, tool_name)

    async def _run_owning_the_profile(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
        tool_name: str,
    ) -> ToolResult:
        """Run the tool while this process owns the browser profile."""
        # Imported here so the module stays importable without the driver.
        from linkedin_mcp_server.drivers.browser import (
            note_activity,
            note_call_started,
            release_profile_if_idle_or_requested,
        )

        lease = get_profile_lease()
        acquired = lease.try_acquire()
        if not acquired:
            await self._report_progress(
                context,
                message=(
                    "Another LinkedIn MCP client is using the browser; "
                    "waiting for it to hand over"
                ),
            )
            budget = get_config().browser.browser_wait_seconds
            acquired = await lease.acquire(timeout=budget)

        if not acquired:
            # Raised as a ToolError here, not via error_handler: an exception
            # thrown in middleware does not pass through raise_tool_error, and
            # mask_error_details would otherwise hide the explanation.
            logger.info("Tool '%s' gave up waiting for the shared browser", tool_name)
            raise ToolError(str(BrowserBusyError()))

        hold_started = time.perf_counter()
        try:
            # Marks the browser as in use so the background handoff poll cannot
            # close it out from under this call. Inside the try so the finally
            # always balances it, including if the call is cancelled.
            note_call_started()
            return await call_next(context)
        finally:
            hold_seconds = time.perf_counter() - hold_started
            logger.debug(
                "Released scraper lock for tool '%s' after %.3fs",
                tool_name,
                hold_seconds,
            )
            note_activity()
            lease.release()
            # Hand the browser over now if someone is waiting, rather than
            # holding it for the rest of this process's lifetime.
            try:
                await release_profile_if_idle_or_requested()
            except Exception:
                logger.debug("Profile handoff check failed", exc_info=True)
