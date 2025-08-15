# src/linkedin_mcp_server/server.py
"""
FastMCP server implementation for LinkedIn integration with tool registration.

Creates and configures the MCP server with comprehensive LinkedIn tool suite including
person profiles, company data, job information, and session management capabilities.
Provides clean shutdown handling and resource cleanup.
"""

import logging
from typing import Any, Dict

from fastmcp import FastMCP

from linkedin_mcp_server.tools.company import register_company_tools
from linkedin_mcp_server.tools.job import register_job_tools
from linkedin_mcp_server.tools.person import register_person_tools

logger = logging.getLogger(__name__)


def create_mcp_server() -> FastMCP:
    """Create and configure the MCP server with all LinkedIn tools."""
    mcp = FastMCP("linkedin_scraper")

    # Register all tools
    register_person_tools(mcp)
    register_company_tools(mcp)
    register_job_tools(mcp)

    # Register session management tool
    @mcp.tool()
    async def close_session() -> Dict[str, Any]:
        """Close the current browser session and clean up resources."""
        from linkedin_mcp_server.drivers import close_all_sessions, is_driver_needed

        try:
            close_all_sessions()

            if is_driver_needed():
                message = "Successfully closed Chrome WebDriver session and cleaned up resources"
            else:
                message = "No persistent browser session to close (using fast-linkedin-scraper)"

            return {
                "status": "success",
                "message": message,
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Error closing browser session: {str(e)}",
            }

    return mcp


def shutdown_handler() -> None:
    """Clean up resources on shutdown."""
    from linkedin_mcp_server.drivers import close_all_sessions

    close_all_sessions()
