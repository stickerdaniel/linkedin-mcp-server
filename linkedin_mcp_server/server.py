"""
FastMCP server implementation for LinkedIn integration with tool registration.

Creates and configures the MCP server with comprehensive LinkedIn tool suite including
person profiles, company data, job information, and session management capabilities.
"""

import logging
from typing import Any, AsyncIterator

from fastmcp import FastMCP
from fastmcp.server.auth import MultiAuth
from fastmcp.server.lifespan import lifespan

from linkedin_mcp_server.bootstrap import (
    get_runtime_policy,
    initialize_bootstrap,
    start_background_browser_setup_if_needed,
)
from linkedin_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.drivers.browser import close_browser
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.health import register_health_route
from linkedin_mcp_server.http_auth import BearerTokenVerifier
from linkedin_mcp_server.oauth_auth import MinimalOAuthProvider
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


def create_mcp_server() -> FastMCP:
    """Create and configure the MCP server with all LinkedIn tools."""
    config = get_config()
    auth = None
    if config.server.transport == "streamable-http":
        mode = config.server.mcp_auth_mode
        if mode == "bearer" and config.server.mcp_bearer_token:
            auth = BearerTokenVerifier(expected_token=config.server.mcp_bearer_token)
            logger.info("MCP HTTP bearer auth is enabled")
        elif (
            mode == "oauth"
            and config.server.mcp_oauth_base_url
            and config.server.mcp_oauth_client_id
            and config.server.mcp_oauth_client_secret
        ):
            auth = MinimalOAuthProvider(
                base_url=config.server.mcp_oauth_base_url,
                client_id=config.server.mcp_oauth_client_id,
                client_secret=config.server.mcp_oauth_client_secret,
                allowed_redirect_uris=config.server.mcp_oauth_allowed_redirect_uris,
                token_ttl_seconds=config.server.mcp_oauth_token_ttl_seconds,
            )
            logger.info("MCP HTTP OAuth auth is enabled")
        elif (
            mode == "multi"
            and config.server.mcp_bearer_token
            and config.server.mcp_oauth_base_url
            and config.server.mcp_oauth_client_id
            and config.server.mcp_oauth_client_secret
        ):
            oauth = MinimalOAuthProvider(
                base_url=config.server.mcp_oauth_base_url,
                client_id=config.server.mcp_oauth_client_id,
                client_secret=config.server.mcp_oauth_client_secret,
                allowed_redirect_uris=config.server.mcp_oauth_allowed_redirect_uris,
                token_ttl_seconds=config.server.mcp_oauth_token_ttl_seconds,
            )
            bearer = BearerTokenVerifier(expected_token=config.server.mcp_bearer_token)
            auth = MultiAuth(server=oauth, verifiers=[bearer])
            logger.info("MCP HTTP multi auth is enabled (oauth + bearer)")

    mcp = FastMCP(
        "linkedin_scraper",
        lifespan=browser_lifespan,
        mask_error_details=True,
        auth=auth,
    )
    mcp.add_middleware(SequentialToolExecutionMiddleware())

    if config.server.transport == "streamable-http":
        register_health_route(mcp)

    # Register all tools
    register_person_tools(mcp)
    register_company_tools(mcp)
    register_job_tools(mcp)
    register_messaging_tools(mcp)

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
                "message": (
                    "Successfully closed the browser session and cleaned up resources"
                ),
            }
        except Exception as e:
            raise_tool_error(e, "close_session")  # NoReturn

    return mcp
