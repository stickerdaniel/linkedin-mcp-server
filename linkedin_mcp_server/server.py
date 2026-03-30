"""FastMCP server with LinkedIn tool registration and sequential execution."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult

from linkedin_mcp_server.bootstrap import (
    initialize_bootstrap,
    start_background_browser_setup_if_needed,
)
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.drivers.browser import close_browser
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.tools.company import register_company_tools
from linkedin_mcp_server.tools.job import register_job_tools
from linkedin_mcp_server.tools.person import register_person_tools

logger = logging.getLogger(__name__)


class SequentialToolExecutionMiddleware(Middleware):
    """Ensure only one MCP tool call executes at a time."""

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


@lifespan
async def browser_lifespan(app: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Manage browser lifecycle -- cleanup on shutdown."""
    del app
    logger.info("LinkedIn MCP Server starting...")
    initialize_bootstrap()

    config = get_config()
    if config.server.login_serve:
        from linkedin_mcp_server.drivers.browser import adopt_browser
        from linkedin_mcp_server.setup import interactive_login_keep_alive

        browser = await interactive_login_keep_alive()
        await adopt_browser(browser)
        logger.info("login-serve: browser adopted from interactive login")
    else:
        await start_background_browser_setup_if_needed()

    yield {}
    logger.info("LinkedIn MCP Server shutting down...")
    await close_browser()


def create_mcp_server() -> FastMCP:
    """Create and configure the MCP server with all LinkedIn tools."""
    mcp = FastMCP(
        "linkedin_scraper",
        lifespan=browser_lifespan,
        mask_error_details=True,
    )
    mcp.add_middleware(SequentialToolExecutionMiddleware())

    register_person_tools(mcp)
    register_company_tools(mcp)
    register_job_tools(mcp)

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Close Session",
        annotations={"destructiveHint": True},
        tags={"session"},
    )
    async def close_session() -> dict[str, Any]:
        """Close the current browser session and clean up resources."""
        try:
            await close_browser()
            return {
                "status": "success",
                "message": "Successfully closed the browser session and cleaned up resources",
            }
        except Exception as e:
            raise_tool_error(e, "close_session")  # NoReturn

    return mcp
