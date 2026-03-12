"""Middleware that serializes MCP tool execution within one server process."""

from __future__ import annotations

import asyncio
import logging
import time

import mcp.types as mt

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult

logger = logging.getLogger(__name__)


class SequentialToolExecutionMiddleware(Middleware):
    """Ensure only one MCP tool call executes at a time per server process."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        tool_name = context.message.name
        wait_started = time.perf_counter()
        logger.debug("Waiting for scraper lock for tool '%s'", tool_name)

        async with self._lock:
            wait_seconds = time.perf_counter() - wait_started
            logger.debug(
                "Acquired scraper lock for tool '%s' after %.3fs",
                tool_name,
                wait_seconds,
            )
            hold_started = time.perf_counter()
            try:
                return await call_next(context)
            finally:
                hold_seconds = time.perf_counter() - hold_started
                logger.debug(
                    "Released scraper lock for tool '%s' after %.3fs",
                    tool_name,
                    hold_seconds,
                )
