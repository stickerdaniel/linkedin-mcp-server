"""
FastMCP server implementation for LinkedIn integration with tool registration.

Creates and configures the MCP server with comprehensive LinkedIn tool suite including
person profiles, company data, job information, and session management capabilities.
"""

import asyncio
import hashlib
import hmac
import logging
from typing import TYPE_CHECKING, Any, AsyncIterator

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
from linkedin_mcp_server.daemon_auth import (
    FrontendAuthRepairMiddleware,
    OwnerAuthSignalMiddleware,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.server_role import ServerRole, set_process_role
from linkedin_mcp_server.update_check import UpdateNoticeMiddleware
from linkedin_mcp_server.tools.company import register_company_tools
from linkedin_mcp_server.tools.enrichment import register_enrichment_tools
from linkedin_mcp_server.tools.feed import register_feed_tools
from linkedin_mcp_server.tools.job import register_job_tools
from linkedin_mcp_server.tools.messaging import register_messaging_tools
from linkedin_mcp_server.tools.person import register_person_tools
from linkedin_mcp_server.tools.post import register_post_tools

if TYPE_CHECKING:
    from linkedin_mcp_server.daemon_proxy import DaemonProxyBackend

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
    proxy_backend: "DaemonProxyBackend | None" = None,
) -> FastMCP:
    """Create and configure the MCP server with all LinkedIn tools.

    *role* selects which parts belong to this process. It defaults to
    :attr:`ServerRole.DIRECT`, which is exactly the historical server, so every
    existing caller keeps what it had.

    *auth_token* is the bearer token a daemon owner requires. Only an owner has
    one: it listens on a loopback port that every process on the machine can
    reach, and a browser the user merely points at a page can reach it too.

    *proxy_backend* is the owner a proxy forwards to, held as an object rather
    than as a bare attachment because which owner that is can change: an upgrade
    replaces one, and a proxy that captured the address at startup would keep
    using it. The two credentials are kept apart on purpose and must not be
    conflated: *auth_token* is what an owner *demands* of callers, while the
    backend's token is what a proxy *presents* to one.
    """
    if auth_token is not None and role is not ServerRole.OWNER:
        # A token on a stdio server protects nothing — there is no port to
        # protect — and passing one is a caller that believes it built the
        # owner. Refused rather than ignored, because the mistake it describes
        # is an endpoint that was meant to be authenticated.
        raise ValueError(
            f"Only a daemon owner authenticates requests, not {role.value}"
        )
    if role is ServerRole.PROXY and proxy_backend is None:
        # A proxy registers no tools of its own, so without an owner it would
        # serve an empty tool list and look like a server whose tools vanished.
        raise ValueError(
            "A proxy has no tools of its own and needs an owner to forward to"
        )
    if proxy_backend is not None and role is not ServerRole.PROXY:
        # Only a proxy forwards. Accepting an owner here would mean a server that
        # was handed a bearer token for a shared browser and quietly ignored it.
        raise ValueError(f"Only a proxy forwards to an owner, not {role.value}")

    # The role is also process state, because the auth gates live in `bootstrap`
    # and are reached from a tool body with no server to ask (`server_role`).
    # Claimed here rather than left to the entry points: anything may call this
    # function directly, and a caller that built an OWNER while the process was
    # still unclaimed would get an owner with every auth gate switched off, which
    # is a detached process opening a login window nobody can see.
    #
    # The claim refuses a second, different role rather than applying it. That
    # check lives in `set_process_role` so every entry point is covered by one
    # rule, including the owner's, which claims OWNER before it ever gets here.
    set_process_role(role)

    mcp = FastMCP(
        "mcp-server-linkedin",
        version=__version__,
        # Selected here rather than gated inside, because for a proxy every step
        # of it is wrong: it would install a Chromium this process never opens,
        # and then claim to be closing a browser it never had.
        lifespan=browser_lifespan if role.drives_browser else None,
        mask_error_details=True,
        auth=_StaticTokenAuth(auth_token) if auth_token is not None else None,
    )
    # Added before the serializing middleware below, which makes it the outer one.
    # An inner position would work: `close_browser` does not consult the in-flight
    # count, so quiescence succeeds from there, and the lease reference the inner
    # layer holds is released in its own `finally`. Outside is preferred because
    # the marker reports whether the profile is still held, and from inside that
    # answer would describe the layer below rather than the process.
    if role is ServerRole.OWNER:
        mcp.add_middleware(OwnerAuthSignalMiddleware())
        # Before the serializing middleware below, which puts it outside. That
        # is the whole point rather than a detail: a call spends most of its
        # life queued, and a frontend that gives up while its call is still
        # waiting its turn is the commonest way to end up with work nobody
        # wants. Inside, a call would only become cancellable once it already
        # had the browser.
        from linkedin_mcp_server.daemon_liveness import OwnerCallLivenessMiddleware

        mcp.add_middleware(OwnerCallLivenessMiddleware())
    # The other half, and only ever on the process that has a user in front of it.
    if role is ServerRole.PROXY:
        # Handed the same budget every tool here is given, rather than reading it
        # from the configuration singleton. The two agree for a server `cli_main`
        # built, which loaded that configuration first, and need not for one
        # constructed directly: measured at `tool_timeout=1.2` with no
        # configuration loaded, the repair budgeted itself 149.8 seconds of a
        # 1.2-second call, and the first read of an unloaded singleton parses
        # whatever `sys.argv` happens to hold.
        mcp.add_middleware(FrontendAuthRepairMiddleware(tool_timeout=tool_timeout))
        # After the repair, which keeps that one outermost. The order decides how
        # the two compose: a call and the replay a sign-in triggers each get
        # their own liveness wrapper, rather than one wrapper around the pair.
        # The other way round, an owner that departed between the two would leave
        # the replay unrecoverable.
        from linkedin_mcp_server.daemon_proxy import (
            FrontendCallHeartbeatMiddleware,
            FrontendOwnerRecoveryMiddleware,
        )

        assert proxy_backend is not None  # the role check above established it
        mcp.add_middleware(FrontendOwnerRecoveryMiddleware(proxy_backend))
        # Innermost of the three, which is what makes a replay behave: a call the
        # recovery middleware runs again enters here a second time and gets its
        # own identifier and its own heartbeats, against whichever owner it is
        # talking to now.
        mcp.add_middleware(FrontendCallHeartbeatMiddleware(proxy_backend))
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

    if proxy_backend is not None:
        from linkedin_mcp_server.daemon_proxy import create_proxy_provider

        mcp.add_provider(
            create_proxy_provider(proxy_backend, tool_timeout=tool_timeout)
        )
        # Not the default, which is "warn": a failing provider is logged and
        # skipped, and for a server whose *only* provider this is, that turns a
        # dead owner into an empty tool list with no explanation. Measured on
        # 3.4.4 — `tools/list` returned `[]` and the call that followed failed as
        # an unknown tool. A transport failure has to look like one.
        mcp.provider_error_strategy = "raise"

    # Every local registration below drives Chromium, so a proxy takes none of
    # them and serves the owner's through the provider above instead.
    if role.drives_browser:
        register_person_tools(mcp, tool_timeout=tool_timeout)
        register_company_tools(mcp, tool_timeout=tool_timeout)
        register_job_tools(mcp, tool_timeout=tool_timeout)
        register_messaging_tools(mcp, tool_timeout=tool_timeout)
        register_feed_tools(mcp, tool_timeout=tool_timeout)
        register_post_tools(mcp, tool_timeout=tool_timeout)
        register_enrichment_tools(mcp, tool_timeout=tool_timeout)

        # Inside the gate with the rest, and easy to miss because it is the one
        # tool defined here rather than in a `register_*` call. Left out of the
        # gate it would collide with the forwarded tool of the same name and
        # close a browser this process does not have.
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
