"""
FastMCP server implementation for LinkedIn integration with tool registration.

Creates and configures the MCP server with comprehensive LinkedIn tool suite including
person profiles, company data, job information, outreach automation, and session management.
"""

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict

from fastmcp import FastMCP

from linkedin_mcp_server.drivers.browser import close_browser
from linkedin_mcp_server.storage import close_db
from linkedin_mcp_server.tools.company import register_company_tools
from linkedin_mcp_server.tools.job import register_job_tools
from linkedin_mcp_server.tools.outreach import register_outreach_tools
from linkedin_mcp_server.tools.person import register_person_tools
from linkedin_mcp_server.tools.reports import register_report_tools
from linkedin_mcp_server.tools.search import register_search_tools

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastMCP) -> AsyncIterator[None]:
    """Manage server lifecycle - cleanup browser and database on shutdown."""
    logger.info("LinkedIn MCP Server starting...")
    yield
    logger.info("LinkedIn MCP Server shutting down...")
    await close_browser()
    await close_db()


def create_mcp_server() -> FastMCP:
    """Create and configure the MCP server with all LinkedIn tools."""
    mcp = FastMCP("linkedin_scraper", lifespan=lifespan)

    # Register all tools
    register_person_tools(mcp)
    register_company_tools(mcp)
    register_job_tools(mcp)

    # Register new audience building tools
    register_search_tools(mcp)
    register_outreach_tools(mcp)
    register_report_tools(mcp)

    # Register session management tool
    @mcp.tool()
    async def close_session() -> Dict[str, Any]:
        """Close the current browser session and clean up resources."""
        try:
            await close_browser()
            return {
                "status": "success",
                "message": "Successfully closed the browser session and cleaned up resources",
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Error closing browser session: {str(e)}",
            }

    return mcp
