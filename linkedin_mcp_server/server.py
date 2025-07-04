# src/linkedin_mcp_server/server.py
"""
MCP server setup for LinkedIn integration.

This module creates the MCP server and registers all the LinkedIn tools.
"""

from typing import Any, Dict

from fastmcp import FastMCP

from linkedin_mcp_server.drivers.chrome import active_drivers
from linkedin_mcp_server.tools.company import register_company_tools
from linkedin_mcp_server.tools.job import register_job_tools
from linkedin_mcp_server.tools.person import register_person_tools


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
        session_id = "default"  # Using the same default session

        if session_id in active_drivers:
            try:
                active_drivers[session_id].quit()
                del active_drivers[session_id]
                return {
                    "status": "success",
                    "message": "Successfully closed the browser session",
                }
            except Exception as e:
                return {
                    "status": "error",
                    "message": f"Error closing browser session: {str(e)}",
                }
        else:
            return {
                "status": "warning",
                "message": "No active browser session to close",
            }

    return mcp


def shutdown_handler() -> None:
    """Clean up resources on shutdown."""
    for session_id, driver in list(active_drivers.items()):
        try:
            driver.quit()
            del active_drivers[session_id]
        except Exception as e:
            print(f"❌ Error closing driver during shutdown: {e}")
