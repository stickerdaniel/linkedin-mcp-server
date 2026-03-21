"""
FastMCP server implementation for LinkedIn integration with tool registration.

Creates and configures the MCP server with comprehensive LinkedIn tool suite including
person profiles, company data, job information, and session management capabilities.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, AsyncIterator

from fastmcp import FastMCP

if TYPE_CHECKING:
    from linkedin_mcp_server.config.schema import OAuthConfig
from fastmcp.server.lifespan import lifespan

from linkedin_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.authentication import get_authentication_source
from linkedin_mcp_server.drivers.browser import close_browser
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.tools.company import register_company_tools
from linkedin_mcp_server.tools.job import register_job_tools
from linkedin_mcp_server.tools.person import register_person_tools

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


def create_mcp_server(oauth_config: "OAuthConfig | None" = None) -> FastMCP:
    """Create and configure the MCP server with all LinkedIn tools."""
    auth = None
    if oauth_config and oauth_config.enabled:
        from linkedin_mcp_server.auth import PasswordOAuthProvider

        if oauth_config.base_url is None:
            raise ValueError("oauth_config.base_url must be set when OAuth is enabled")
        if oauth_config.password is None:
            raise ValueError("oauth_config.password must be set when OAuth is enabled")
        auth = PasswordOAuthProvider(
            base_url=oauth_config.base_url,
            password=oauth_config.password,
        )

    mcp = FastMCP(
        "linkedin_scraper",
        lifespan=auth_lifespan | browser_lifespan,
        mask_error_details=True,
        auth=auth,
    )
    mcp.add_middleware(SequentialToolExecutionMiddleware())

    # Register all tools
    register_person_tools(mcp)
    register_company_tools(mcp)
    register_job_tools(mcp)

    # Register session management tool
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
