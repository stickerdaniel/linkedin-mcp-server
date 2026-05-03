"""
FastMCP server implementation for LinkedIn integration with tool registration.

Creates and configures the MCP server with comprehensive LinkedIn tool suite including
person profiles, company data, job information, and session management capabilities.
"""

import logging
from typing import Any, AsyncIterator

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from linkedin_mcp_server.bootstrap import (
    get_runtime_policy,
    initialize_bootstrap,
    start_background_browser_setup_if_needed,
)
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.drivers.browser import close_browser
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.tools.company import register_company_tools
from linkedin_mcp_server.tools.job import register_job_tools
from linkedin_mcp_server.tools.messaging import register_messaging_tools
from linkedin_mcp_server.tools.person import register_person_tools

logger = logging.getLogger(__name__)


@lifespan
async def browser_lifespan(app: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Manage browser lifecycle — cleanup on shutdown.

    Derived runtime durability must not depend on this hook. Docker runtime
    sessions are checkpoint-committed when they are created.
    """
    del app
    logger.info("LinkedIn MCP Server starting...")
    initialize_bootstrap(get_runtime_policy())
    await start_background_browser_setup_if_needed()
    yield {}
    logger.info("LinkedIn MCP Server shutting down...")
    await close_browser()


def create_mcp_server(*, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS) -> FastMCP:
    """Create and configure the MCP server with all LinkedIn tools."""
    mcp = FastMCP(
        "linkedin_scraper",
        lifespan=browser_lifespan,
        mask_error_details=True,
    )
    mcp.add_middleware(SequentialToolExecutionMiddleware())

    # Register all tools
    register_person_tools(mcp, tool_timeout=tool_timeout)
    register_company_tools(mcp, tool_timeout=tool_timeout)
    register_job_tools(mcp, tool_timeout=tool_timeout)
    register_messaging_tools(mcp, tool_timeout=tool_timeout)

    # Register session management tool
    @mcp.tool(
        timeout=tool_timeout,
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
