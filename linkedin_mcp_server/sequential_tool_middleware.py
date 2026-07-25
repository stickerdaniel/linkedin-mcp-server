"""Middleware that serializes MCP tool execution within one server process."""

from __future__ import annotations

import asyncio
import logging
import time

import mcp.types as mt

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

logger = logging.getLogger(__name__)

# Patchright/Playwright error message emitted when the browser context dies.
# Matched as a substring so it works across Playwright versions and transports.
_BROWSER_CONTEXT_CLOSED = "Target page, context or browser has been closed"


def _is_browser_context_closed(exc: Exception) -> bool:
    """Return True if *exc* indicates the Patchright browser context has died."""
    return _BROWSER_CONTEXT_CLOSED in str(exc)


class SequentialToolExecutionMiddleware(Middleware):
    """Ensure only one MCP tool call executes at a time per server process."""

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
            hold_started = time.perf_counter()
            try:
                return await call_next(context)
            except Exception as exc:
                if _is_browser_context_closed(exc):
                    # The Patchright browser context died mid-operation.
                    # Reset the browser singleton so the next tool call gets a
                    # fresh context (cookies are safe on disk — no re-login needed).
                    logger.warning(
                        "Browser context closed during tool '%s' — resetting for next call",
                        tool_name,
                    )
                    try:
                        # Lazy import avoids a circular dependency at module load time.
                        from linkedin_mcp_server.drivers.browser import close_browser
                        await close_browser()
                    except Exception:
                        logger.debug(
                            "close_browser() failed during crash recovery",
                            exc_info=True,
                        )
                    raise ToolError(
                        "The browser context crashed mid-operation. "
                        "The browser has been reset — please retry this tool. "
                        "Your LinkedIn session is still active."
                    ) from exc
                raise
            finally:
                hold_seconds = time.perf_counter() - hold_started
                logger.debug(
                    "Released scraper lock for tool '%s' after %.3fs",
                    tool_name,
                    hold_seconds,
                )
