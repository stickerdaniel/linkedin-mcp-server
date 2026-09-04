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
from linkedin_mcp_server.tool_interval import try_claim_start

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

    An optional minimum interval between tool-call starts (see
    ``min_tool_interval_seconds``) is enforced after the in-process lock and
    before the profile lease, so waiting never holds the browser.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_start_mono: float | None = None

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

    async def _sleep_reporting(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        seconds: float,
        *,
        message: str,
    ) -> None:
        """Sleep *seconds*, honouring cancellation and reporting progress."""
        if seconds <= 0:
            return
        await self._report_progress(context, message=message)
        # Chunk so a cancel reaches the waiter without waiting out the full
        # interval, and so progress can be re-announced on long waits.
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 0.5))

    async def _await_min_interval(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        tool_name: str,
    ) -> None:
        """Wait until this tool call may start under the configured interval."""
        interval = get_config().server.min_tool_interval_seconds
        if interval <= 0:
            return

        if self._last_start_mono is not None:
            mono_wait = interval - (time.monotonic() - self._last_start_mono)
            if mono_wait > 0:
                logger.debug(
                    "Tool '%s' waiting %.3fs for in-process min interval",
                    tool_name,
                    mono_wait,
                )
                await self._sleep_reporting(
                    context,
                    mono_wait,
                    message=(
                        f"Waiting {mono_wait:.1f}s for minimum tool-call interval"
                    ),
                )

        lease = get_profile_lease()
        auth_root = lease.auth_root
        while True:
            wait = try_claim_start(auth_root, interval)
            if wait <= 0:
                self._last_start_mono = time.monotonic()
                return
            logger.debug(
                "Tool '%s' waiting %.3fs for cross-process min interval",
                tool_name,
                wait,
            )
            await self._sleep_reporting(
                context,
                wait,
                message=(
                    f"Waiting {wait:.1f}s for minimum tool-call interval "
                    "(shared profile)"
                ),
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
            # Interval before the lease: waiting must not pin the browser.
            await self._await_min_interval(context, tool_name)
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
