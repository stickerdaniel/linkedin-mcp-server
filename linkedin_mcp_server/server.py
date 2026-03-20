"""
FastMCP server implementation for LinkedIn integration with tool registration.

Creates and configures the MCP server with comprehensive LinkedIn tool suite including
person profiles, company data, job information, and session management capabilities.
"""

import logging
from typing import Any, AsyncIterator

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from linkedin_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.authentication import get_authentication_source
from linkedin_mcp_server.drivers.browser import close_browser, hard_reset_browser
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.tools.company import register_company_tools
from linkedin_mcp_server.tools.job import register_job_tools
from linkedin_mcp_server.tools.person import register_person_tools
from linkedin_mcp_server.tools.posts import register_posts_tools

logger = logging.getLogger(__name__)


@lifespan
async def browser_lifespan(app: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Manage browser lifecycle — cleanup on shutdown.

    Derived runtime durability must not depend on this hook. Docker runtime
    sessions are checkpoint-committed when they are created.
    """
    logger.info("LinkedIn MCP Server starting...")
    yield {}
    logger.info("LinkedIn MCP Server shutting down...")
    await close_browser()


@lifespan
async def auth_lifespan(app: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Validate authentication profile exists at startup."""
    logger.info("Validating LinkedIn authentication...")
    get_authentication_source()
    yield {}


def create_mcp_server() -> FastMCP:
    """Create and configure the MCP server with all LinkedIn tools."""
    mcp = FastMCP(
        "linkedin_scraper",
        lifespan=auth_lifespan | browser_lifespan,
        mask_error_details=True,
    )
    mcp.add_middleware(SequentialToolExecutionMiddleware())

    # Register all tools
    register_person_tools(mcp)
    register_company_tools(mcp)
    register_job_tools(mcp)
    register_posts_tools(mcp)

    # Register session management tools
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

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Reset Session",
        annotations={"destructiveHint": True},
        tags={"session"},
    )
    async def reset_session() -> dict[str, Any]:
        """Hard-reset the browser session: close and wipe the derived runtime profile.

        Use this when get_person_profile or get_company_profile returns
        session_status='session_blocked' and subsequent calls also return empty sections.
        The next scrape will re-bridge from source cookies with a completely fresh context.

        Note: most effective in containerized (Docker) environments. On local host runs
        where the server uses the source profile directly, this closes the browser but
        cannot wipe the source profile — re-run with --login to create a fresh session.
        """
        try:
            await hard_reset_browser()
            return {
                "status": "success",
                "message": (
                    "Browser closed and runtime profile wiped. "
                    "Next scrape will re-bridge from source cookies."
                ),
            }
        except Exception as e:
            raise_tool_error(e, "reset_session")  # NoReturn

    return mcp
