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
from linkedin_mcp_server.daemon_proxy import TIMEOUT_MARGIN_SECONDS
from linkedin_mcp_server.exceptions import BrowserBusyError
from linkedin_mcp_server.profile_lease import get_profile_lease
from linkedin_mcp_server.server_role import ServerRole, process_role
from linkedin_mcp_server.tool_interval import (
    force_claim_start,
    future_stamp_skew_seconds,
    try_claim_start,
)

logger = logging.getLogger(__name__)

# Leave this much of the owner's pre-tool margin for raising and proxying a
# ToolError after a contended lease wait. Spending the entire remainder on
# ``lease.acquire`` lets the frontend deadline expire first and surfaces a
# transport timeout instead of the structured error (#877).
_PRE_TOOL_RESPONSE_RESERVE_SECONDS = 2.0


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
        *,
        deadline: float | None = None,
    ) -> None:
        """Wait until this tool call may start under the configured interval.

        ``deadline`` is an absolute monotonic end for pre-tool waiting. Daemon
        owners pass the frontend's proxy margin; a standalone DIRECT server
        leaves it ``None`` and uses the tool timeout instead, because there is
        no proxy deadline to protect.
        """
        config = get_config().server
        interval = config.min_tool_interval_seconds
        if interval <= 0:
            return

        if deadline is None:
            # DIRECT: middleware runs before FastMCP's fail_after, and there is
            # no daemon frontend. Waiting the full configured interval is the
            # documented "wait rather than fail" behaviour; the tool timeout is
            # the interval's ceiling (see ServerConfig.validate).
            deadline = time.monotonic() + config.tool_timeout_seconds
            budget_label = f"{config.tool_timeout_seconds:g}s tool timeout"
        else:
            budget_label = (
                f"{TIMEOUT_MARGIN_SECONDS:g}s daemon proxy margin "
                "reserved beyond the tool timeout"
            )

        async def sleep_within_budget(seconds: float, *, message: str) -> None:
            if seconds <= 0:
                return
            remaining = deadline - time.monotonic()
            if seconds > remaining:
                raise ToolError(
                    f"Minimum tool-call interval ({interval:g}s) has not "
                    f"elapsed, and waiting would exceed the {budget_label}. "
                    f"Retry shortly."
                )
            await self._sleep_reporting(context, seconds, message=message)

        if self._last_start_mono is not None:
            mono_wait = interval - (time.monotonic() - self._last_start_mono)
            if mono_wait > 0:
                logger.debug(
                    "Tool '%s' waiting %.3fs for in-process min interval",
                    tool_name,
                    mono_wait,
                )
                await sleep_within_budget(
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
            remaining = deadline - time.monotonic()
            skew = future_stamp_skew_seconds(auth_root)
            # A stamp farther ahead than this call can wait will never
            # converge inside the budget: each retry only chips interval-
            # sized pieces off the skew and then raises. Pace once, claim
            # at local time, and proceed.
            if skew > remaining:
                pace = min(interval, max(0.0, remaining))
                logger.debug(
                    "Tool '%s' absorbing %.1fs future stamp skew "
                    "(%.1fs budget left) with a %.1fs pace then claiming "
                    "locally",
                    tool_name,
                    skew,
                    remaining,
                    pace,
                )
                if pace > 0:
                    await self._sleep_reporting(
                        context,
                        pace,
                        message=(
                            f"Waiting {pace:.1f}s for minimum tool-call "
                            "interval (shared profile clock skew)"
                        ),
                    )
                force_claim_start(auth_root)
                self._last_start_mono = time.monotonic()
                return
            logger.debug(
                "Tool '%s' waiting %.3fs for cross-process min interval",
                tool_name,
                wait,
            )
            await sleep_within_budget(
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
            # On the daemon owner the frontend's deadline is
            # tool_timeout + TIMEOUT_MARGIN_SECONDS; everything before the
            # tool body — interval wait *and* contended lease acquisition —
            # shares that margin, or the client sees a transport timeout.
            pre_tool_deadline: float | None = None
            if process_role() is ServerRole.OWNER:
                pre_tool_deadline = time.monotonic() + TIMEOUT_MARGIN_SECONDS
            await self._await_min_interval(
                context, tool_name, deadline=pre_tool_deadline
            )
            return await self._run_owning_the_profile(
                context,
                call_next,
                tool_name,
                pre_tool_deadline=pre_tool_deadline,
            )

    async def _run_owning_the_profile(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
        tool_name: str,
        *,
        pre_tool_deadline: float | None = None,
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
            if pre_tool_deadline is not None:
                remaining = pre_tool_deadline - time.monotonic()
                usable = remaining - _PRE_TOOL_RESPONSE_RESERVE_SECONDS
                if usable <= 0:
                    raise ToolError(
                        "The daemon proxy margin was spent waiting for the "
                        "minimum tool-call interval, with no time left to "
                        "wait for the shared browser. Retry shortly."
                    )
                # Keep a slice of the margin for constructing and delivering
                # the ToolError if acquire times out; otherwise the frontend
                # deadline expires first and the client sees a transport
                # failure instead of BrowserBusyError.
                budget = min(budget, usable)
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
