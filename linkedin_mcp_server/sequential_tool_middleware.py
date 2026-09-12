"""Middleware that serializes MCP tool execution across server processes."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime

import mcp.types as mt

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.exceptions import BrowserBusyError
from linkedin_mcp_server.pacing import JobStore, load_account_budget, tool_call_gap
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

    Serial is not the same as paced, though, and this is also where the pacing
    lives: a jittered gap between consecutive calls, and one unit of the shared
    account budget spent per call, so that every LinkedIn-touching tool is
    counted -- not only the bulk-enrichment ones that ask ``pacing`` themselves.
    """

    # Tools that answer from local disk and never reach LinkedIn. Pacing them
    # would charge the account budget for activity that never happened and make
    # a status poll wait out a gap meant for page loads. Named rather than
    # derived: there is no flag on a tool saying whether it navigates, and a
    # wrong guess here is silent -- an unlisted local tool merely pays a gap it
    # did not owe, while a listed navigating one would escape pacing entirely.
    _LOCAL_ONLY_TOOLS = frozenset(
        {
            "get_enrichment_status",
            "get_company_cache",
            "query_company_cache",
        }
    )

    # Tools that count their own LinkedIn activity against the same ledger,
    # once per profile or company rather than once per call. They are paced
    # like anything else -- they do reach LinkedIn -- but recording here as
    # well would add one unit per call on top of the ones they already wrote,
    # and the ledger drives a daily cap, so an overstated count stops the next
    # bunch early. Skipping the record is not the same as skipping the gap,
    # which is why this is a separate set rather than another entry above.
    _SELF_RECORDING_TOOLS = frozenset(
        {
            "run_enrichment_bunch",
            "enrich_companies",
            "enrich_company_deep",
        }
    )

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._store = JobStore()
        # Monotonic instant the next call may start at. 0 lets the first call
        # of the process run immediately -- the gap is between calls, and there
        # is nothing yet to be spaced from.
        self._next_call_at = 0.0

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
            # Slept holding the lock on purpose: the gap is between calls to
            # LinkedIn, so letting a queued call run through it would defeat
            # it. The cross-process lease is not held here -- that one is taken
            # inside, after the wait, so no other process is blocked by ours.
            if tool_name not in self._LOCAL_ONLY_TOOLS:
                await self._space_out_the_call(context, tool_name)
            return await self._run_owning_the_profile(context, call_next, tool_name)

    async def _space_out_the_call(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        tool_name: str,
    ) -> None:
        """Wait out whatever is left of the gap since the previous call."""
        wait_seconds = self._next_call_at - time.monotonic()
        if wait_seconds <= 0:
            return

        logger.debug(
            "Pacing tool '%s': %.2fs left of the gap since the previous call",
            tool_name,
            wait_seconds,
        )
        await self._report_progress(
            context,
            message=f"Pacing LinkedIn activity, starting in {wait_seconds:.1f}s",
        )
        await asyncio.sleep(wait_seconds)

    def _record_one_account_action(self) -> None:
        """Spend one unit of the shared account budget for this call.

        LinkedIn counts per account, so a direct tool call has to draw on the
        same ledger the bulk jobs do -- otherwise a day of interactive scraping
        leaves the budget reading as untouched and the next job spends it all
        again. Recorded even when the call failed: a 429 is activity too.

        Best-effort. The ledger lives on disk, and a read-only or full home
        directory must cost a count, never the tool call itself.
        """
        try:
            now = datetime.now()
            budget = load_account_budget(self._store, now)
            budget.ledger.record(now)
            self._store.save(budget)
        except Exception:
            logger.debug(
                "Could not record the action against the account budget", exc_info=True
            )

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
            # Both inside this finally rather than around the whole call: a
            # call that gave up waiting for the lease never reached this point
            # and never touched LinkedIn, so it owes neither a budget unit nor
            # a gap. Still inside the asyncio lock, so the next call in this
            # process sees the deadline before it tests it.
            if tool_name not in self._LOCAL_ONLY_TOOLS:
                if tool_name not in self._SELF_RECORDING_TOOLS:
                    self._record_one_account_action()
                self._next_call_at = time.monotonic() + tool_call_gap(
                    os.environ.get(EnvironmentKeys.TOOL_CALL_GAP_SECONDS)
                )
            lease.release()
            # Hand the browser over now if someone is waiting, rather than
            # holding it for the rest of this process's lifetime.
            try:
                await release_profile_if_idle_or_requested()
            except Exception:
                logger.debug("Profile handoff check failed", exc_info=True)
