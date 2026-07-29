"""
FastMCP server implementation for LinkedIn integration with tool registration.

Creates and configures the MCP server with comprehensive LinkedIn tool suite including
person profiles, company data, job information, and session management capabilities.
"""

import asyncio
import hashlib
import hmac
import logging
from typing import Any, AsyncIterator

from fastmcp import FastMCP

# fastmcp's own AccessToken, not the SDK's: it subclasses it to carry the JWT
# claims (`fastmcp/server/auth/auth.py:54`), and returning the base class from
# an override narrows what every caller downstream was promised.
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from fastmcp.server.lifespan import lifespan

from linkedin_mcp_server import __version__
from linkedin_mcp_server.bootstrap import (
    get_runtime_policy,
    initialize_bootstrap,
    start_background_browser_setup_if_needed,
)
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.drivers.browser import (
    close_browser,
    watch_for_handoff_requests,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.server_role import ServerRole
from linkedin_mcp_server.update_check import UpdateNoticeMiddleware
from linkedin_mcp_server.tools.company import register_company_tools
from linkedin_mcp_server.tools.feed import register_feed_tools
from linkedin_mcp_server.tools.job import register_job_tools
from linkedin_mcp_server.tools.messaging import register_messaging_tools
from linkedin_mcp_server.tools.person import register_person_tools
from linkedin_mcp_server.tools.post import register_post_tools

logger = logging.getLogger(__name__)


class _StaticTokenAuth(TokenVerifier):
    """Accepts exactly the one token the owner published, and nothing else.

    A whole OAuth flow would be ceremony here: both ends are this installation,
    the token is minted at startup, never reused, and written to a file only
    this account can read. What it has to be is unguessable and compared without
    leaking where two tokens diverge, which is what the digest comparison below
    is for.
    """

    def __init__(self, token: str) -> None:
        super().__init__()
        # A digest rather than the token, so the verifier's own repr does not
        # carry the credential — the surrounding code logs whole objects at
        # DEBUG and users paste those logs into issue reports.
        #
        # Deliberately not claimed: that this keeps the token out of the
        # process. It does not. The owner holds it to serve the stand-down
        # route, and every accepted request builds an AccessToken around it.
        # Anything able to read this process's memory has already won, and the
        # token is the least of what it finds there.
        self._expected = hashlib.sha256(token.encode()).digest()

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(
            self._expected, hashlib.sha256(token.encode()).digest()
        ):
            return None
        return AccessToken(token=token, client_id="linkedin-mcp-frontend", scopes=[])


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
    # Hands the browser to another process that asks for it, and closes it when
    # idle. Both need a timer: once tool calls stop arriving, nothing else would
    # ever notice a waiter.
    handoff_watch = asyncio.create_task(
        watch_for_handoff_requests(), name="linkedin-profile-handoff"
    )
    try:
        yield {}
    finally:
        logger.info("LinkedIn MCP Server shutting down...")
        handoff_watch.cancel()
        try:
            try:
                await handoff_watch
            except asyncio.CancelledError:
                # Only the cancellation we just asked for is ours to absorb.
                # While the watcher winds down, one aimed at this task arrives
                # as the same exception, and a bare pass would swallow it,
                # leaving whoever asked us to stop with a teardown that
                # reported success. cancelling() counts the requests made
                # against *this* task, which is what tells the two apart.
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
            except Exception:
                # A poller that already died re-raises here. Swallowing it keeps
                # shutdown on course; leaving Chromium running on the shared
                # profile is the corruption this whole mechanism exists to
                # prevent, and it matters more than the reason the poll failed.
                logger.warning("Profile handoff watcher failed", exc_info=True)
        finally:
            # In a finally, not after the except, so a BaseException the poller
            # raised still closes the browser on its way out. Those are not ours
            # to swallow, but they are also no reason to abandon the profile.
            await close_browser()


def create_mcp_server(
    *,
    tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    role: ServerRole = ServerRole.DIRECT,
    auth_token: str | None = None,
) -> FastMCP:
    """Create and configure the MCP server with all LinkedIn tools.

    *role* selects which parts belong to this process. It defaults to
    :attr:`ServerRole.DIRECT`, which is exactly the historical server, so every
    existing caller keeps what it had.

    *auth_token* is the bearer token a daemon owner requires. Only an owner has
    one: it listens on a loopback port that every process on the machine can
    reach, and a browser the user merely points at a page can reach it too.
    """
    if auth_token is not None and role is not ServerRole.OWNER:
        # A token on a stdio server protects nothing — there is no port to
        # protect — and passing one is a caller that believes it built the
        # owner. Refused rather than ignored, because the mistake it describes
        # is an endpoint that was meant to be authenticated.
        raise ValueError(
            f"Only a daemon owner authenticates requests, not {role.value}"
        )

    mcp = FastMCP(
        "mcp-server-linkedin",
        version=__version__,
        lifespan=browser_lifespan,
        mask_error_details=True,
        auth=_StaticTokenAuth(auth_token) if auth_token is not None else None,
    )
    # Profile ownership belongs to whoever launches Chromium. A process that
    # only forwards calls must not take the lease: it would either block itself
    # until its own timeout, or take the lease and leave the process that
    # actually needs it waiting for one held by a caller that never uses it.
    if role.drives_browser:
        mcp.add_middleware(SequentialToolExecutionMiddleware())
    # The notice is appended to one tool result per process, so it belongs
    # wherever a user reads results. On a shared owner it would reach whichever
    # client happened to call first and nobody after that, however many clients
    # attach over the owner's life.
    if role.faces_a_client:
        mcp.add_middleware(UpdateNoticeMiddleware())

    # Register all tools
    register_person_tools(mcp, tool_timeout=tool_timeout)
    register_company_tools(mcp, tool_timeout=tool_timeout)
    register_job_tools(mcp, tool_timeout=tool_timeout)
    register_messaging_tools(mcp, tool_timeout=tool_timeout)
    register_feed_tools(mcp, tool_timeout=tool_timeout)
    register_post_tools(mcp, tool_timeout=tool_timeout)

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
