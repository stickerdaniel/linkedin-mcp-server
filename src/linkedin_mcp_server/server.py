# src/linkedin_mcp_server/server.py
"""
MCP server setup for LinkedIn integration.

This module creates the MCP server and registers all the LinkedIn tools.
"""

from typing import Dict, Any
from mcp.server.fastmcp import FastMCP

from linkedin_mcp_server.client import LinkedInClientManager
from linkedin_mcp_server.tools.person import register_person_tools
from linkedin_mcp_server.tools.company import register_company_tools
from linkedin_mcp_server.tools.job import register_job_tools
from linkedin_mcp_server.tools.messaging import register_messaging_tools
from linkedin_mcp_server.tools.connections import register_connection_tools


def create_mcp_server() -> FastMCP:
    """Create and configure the MCP server with all LinkedIn tools."""
    mcp = FastMCP("linkedin_scraper")

    # Register all tools
    register_person_tools(mcp)
    register_company_tools(mcp)
    register_job_tools(mcp)
    register_messaging_tools(mcp)
    register_connection_tools(mcp)

    # Register session management tool
    @mcp.tool()
    async def close_session() -> Dict[str, Any]:
        """Close the current LinkedIn session and clean up resources."""
        try:
            LinkedInClientManager.reset_client()
            return {
                "status": "success",
                "message": "Successfully closed the LinkedIn session",
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Error closing LinkedIn session: {str(e)}",
            }

    return mcp


def shutdown_handler() -> None:
    """Clean up resources on shutdown."""
    try:
        LinkedInClientManager.reset_client()
    except Exception as e:
        print(f"❌ Error closing LinkedIn client during shutdown: {e}")
