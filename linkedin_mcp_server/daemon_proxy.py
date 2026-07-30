"""Talking to the owner, from the frontend that forwards to it.

The other half of :mod:`linkedin_mcp_server.daemon_election`: that module gets an
owner running and hands back an attachment, and this one turns the attachment into
something a FastMCP server can serve tools from.

Kept out of ``server.py`` deliberately. That module answers which parts of a
server a role gets; the loopback address, the bearer token, the proxy-environment
refusal and the forwarding deadline are all daemon knowledge, and every one of
them is a way to leak a credential or to hang a call.

No client *session* is cached: one is built per upstream operation, because that
is how ``ProxyProvider`` uses its factory and because a single shared session
would outlive the owner it was opened against. The owner's *address* is another
matter, and it is pinned for this process's life — see below.

**A proxy is pinned to the owner it started with, and an upgrade is enough to
strand it.** The address and token are resolved once, at startup, so a proxy
whose owner goes away fails every operation afterwards rather than finding the
replacement. Reproduced end to end: a proxy serving 19 tools was asked nothing,
its owner was told to stand down the way a newer build does
(``daemon_election._ask_to_stand_down``), and the next listing failed with
``McpError: Client failed to connect``.

That path is not hypothetical. ``@latest`` is the documented install, so the
first client to launch after an upgrade stands the old owner down and every proxy
already attached to it is dead until its own process restarts. Idle exit and a
crashed owner end the same way.

Resolving the backend per operation and re-electing on a pre-dispatch connection
failure is what fixes this, and it belongs with the liveness work rather than
here: a retry needs to know whether the call it is replacing may already have run
against the browser. Until then the flag stays off by default and this is a
documented limitation, not a solved problem.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from linkedin_mcp_server import daemon_owner

if TYPE_CHECKING:
    from fastmcp.server.providers.proxy import ProxyClient, ProxyProvider

    from linkedin_mcp_server.daemon import Attachment

logger = logging.getLogger(__name__)

#: Added to the owner's tool timeout to get the frontend's deadline. The owner is
#: the one that should report a timed-out tool, so the frontend has to outlast it;
#: an equal value races the owner's own error response and turns a diagnosable
#: "tool timed out" into a transport failure.
#:
#: It does **not** bound a queued call, and no value derived from the tool timeout
#: could. The owner serializes in middleware, and a tool's timeout only starts
#: once the middleware lets it through — measured: a tool declared with a one
#: second timeout, queued two seconds behind another call, succeeded after 2.02s.
#: So under real concurrency this deadline can expire while the call is still
#: waiting its turn, and because cancellation is not forwarded, the owner may go
#: on scraping afterwards. That is the orphaned call #606 names, and the heartbeat
#: is what will bound it.
_TIMEOUT_MARGIN_SECONDS = 30.0

#: How long a fetched tool list stays usable for single lookups. FastMCP's own
#: default, set explicitly because the reasoning is not obvious: an owner's tool
#: set never changes over its lifetime, so there is no dynamic list to miss.
#:
#: Not because versions always agree — they need not. An older frontend attaches
#: to a newer owner on purpose (``daemon_version``), so the list served here can
#: be a newer one than this build would have registered locally. That is the
#: intended trade, and it still does not make the list change underneath us.
#:
#: Measured against ``cache_ttl=0`` over a real loopback owner on this machine:
#: about 23ms versus 42ms per forwarded call, for a list that cannot go stale.
#: Both are far below the seconds a browser reopen costs, so the number to take
#: from this is the ordering rather than the absolute figures. The TTL only
#: affects single lookups either way; an explicit ``tools/list`` always
#: re-fetches.
_COMPONENT_CACHE_SECONDS = 300.0


def _client_factory(
    attachment: Attachment, *, timeout: float
) -> Callable[[], ProxyClient]:
    """Build the callable ``ProxyProvider`` uses to reach the owner.

    A fresh client per call rather than one shared session, because that is what
    the provider expects: it opens and closes a client around every upstream
    operation.
    """
    from fastmcp.client.transports import StreamableHttpTransport
    from fastmcp.server.providers.proxy import ProxyClient

    # Verbatim, never rebuilt from host and port: the descriptor's own URL
    # already carries the MCP path and brackets an IPv6 literal, and FastMCP
    # deliberately does not rewrite the path it is given.
    url = attachment.descriptor.url
    token = attachment.token

    def open_client() -> ProxyClient:
        return ProxyClient(
            StreamableHttpTransport(
                url,
                # A plain string becomes `Authorization: Bearer <token>`, which
                # is the form the owner's verifier compares against.
                auth=token,
                # The owner's own factory, reused rather than reimplemented. It
                # forces `trust_env=False`, which is not tidiness: httpx honours
                # HTTP_PROXY even for 127.0.0.1 unless NO_PROXY happens to say
                # otherwise, and the owner reproduced a loopback request arriving
                # at a capture proxy complete with this bearer token. The user's
                # configured proxy is for LinkedIn's traffic, not for this hop.
                httpx_client_factory=daemon_owner.direct_async_http_client,
            ),
            # Load-bearing rather than tuning. Measured twice against a real
            # owner: with no timeout here, a call that outlives the underlying
            # HTTP read timeout never returns at all — still hanging when an
            # outer bound cut it off at 30s. With one, the same call either
            # succeeds or fails cleanly at the deadline. Setting it here also
            # raises the HTTP read timeout, so one value covers both layers.
            timeout=timeout,
        )

    # ProxyClient rather than a plain Client, and that is not a preference:
    # a plain client installs no progress handler, so the progress every
    # browser-backed tool reports is silently dropped. Measured both ways.
    return open_client


def create_proxy_provider(
    attachment: Attachment, *, tool_timeout: float
) -> ProxyProvider:
    """Serve the owner's tools as if they were this server's own."""
    from fastmcp.server.providers.proxy import ProxyProvider

    timeout = tool_timeout + _TIMEOUT_MARGIN_SECONDS
    logger.debug(
        "Forwarding tool calls to the shared browser owner at %s (deadline %.0fs)",
        attachment.descriptor.url,
        timeout,
    )
    return ProxyProvider(
        _client_factory(attachment, timeout=timeout),
        cache_ttl=_COMPONENT_CACHE_SECONDS,
    )
