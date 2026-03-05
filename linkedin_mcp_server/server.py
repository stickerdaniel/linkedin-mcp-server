"""
FastMCP server implementation for LinkedIn integration with tool registration.

Creates and configures the MCP server with comprehensive LinkedIn tool suite including
person profiles, company data, job information, and session management capabilities.
"""

import logging
from typing import Any, AsyncIterator, Dict

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from linkedin_mcp_server.authentication import get_authentication_source
from linkedin_mcp_server.drivers.browser import close_browser
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.tools.company import register_company_tools
from linkedin_mcp_server.tools.job import register_job_tools
from linkedin_mcp_server.tools.person import register_person_tools

logger = logging.getLogger(__name__)


@lifespan
async def browser_lifespan(app: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Manage browser lifecycle — cleanup on shutdown."""
    logger.info("LinkedIn MCP Server starting...")
    yield {}
    logger.info("LinkedIn MCP Server shutting down...")
    await close_browser()


@lifespan
async def auth_lifespan(app: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Validate authentication profile exists at startup."""
    get_authentication_source()
    yield {}


def create_mcp_server() -> FastMCP:
    """Create and configure the MCP server with all LinkedIn tools."""
    mcp = FastMCP(
        "linkedin_scraper",
        lifespan=auth_lifespan | browser_lifespan,
        mask_error_details=True,
    )

    # Register all tools
    register_person_tools(mcp)
    register_company_tools(mcp)
    register_job_tools(mcp)

    # Register session management tool
    @mcp.tool(
        title="Close Session",
        annotations={"destructiveHint": True},
        tags={"session"},
    )
    async def close_session() -> Dict[str, Any]:
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
