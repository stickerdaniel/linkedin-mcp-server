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
would outlive the owner it was opened against. The address and token are read
per operation too, from :class:`DaemonProxyBackend`, so following a replacement
takes no further machinery — see below for what it still takes.

**A proxy is still pinned to the owner it started with, and an upgrade is enough
to strand it.** Not because the address is captured any more, but because
nothing replaces the attachment the backend holds. A proxy whose owner goes away
therefore fails every operation afterwards rather than finding the replacement.
Reproduced end to end: a proxy serving 19 tools was asked nothing, its owner was
told to stand down the way a newer build does
(``daemon_election._ask_to_stand_down``), and the next listing failed with
``McpError: Client failed to connect``.

That path is not hypothetical. ``@latest`` is the documented install, so the
first client to launch after an upgrade stands the old owner down and every proxy
already attached to it is dead until its own process restarts. Idle exit and a
crashed owner end the same way.

What is left is noticing that the owner is gone and electing another. That
belongs with the liveness work rather than here, because a retry needs to know
whether the call it is replacing may already have run against the browser. Until
then the flag stays off by default and this is a documented limitation, not a
solved problem.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING

from linkedin_mcp_server import daemon_owner

if TYPE_CHECKING:
    from pathlib import Path

    from fastmcp.server.providers.proxy import ProxyClient, ProxyProvider

    from linkedin_mcp_server.config.schema import AppConfig
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


class DaemonProxyBackend:
    """The owner this proxy forwards to, and what it would take to find another.

    One object rather than two loose values, and the reason is the second half:
    an owner is replaced by every upgrade, so the address a proxy uses has to be
    a thing that can change rather than a value captured at startup. Nothing
    changes it yet — that is the next change — but the shape has to exist before
    anything can.

    It carries the election's inputs alongside the answer because nothing
    downstream has them otherwise. ``create_proxy_provider`` used to receive an
    ``Attachment`` and a timeout, and ``create_mcp_server`` has no configuration
    parameter at all, so the proxy layer could not have elected a replacement
    even if it had wanted to. Reaching for ``get_config()`` there instead would
    walk into the argv sensitivity ``daemon_auth`` already documents: an unloaded
    singleton parses whatever ``sys.argv`` happens to hold, which for a directly
    constructed server is pytest's command line.

    Not frozen, unlike the ``Attachment`` it holds. The attachment is a proved
    fact about one owner and must not be edited; which attachment is current is
    exactly the thing that moves.
    """

    def __init__(
        self,
        *,
        attachment: Attachment,
        auth_root: Path,
        profile: Path,
        config: AppConfig,
    ) -> None:
        self._attachment = attachment
        #: What an election needs, kept rather than looked up again.
        self.auth_root = auth_root
        self.profile = profile
        self.config = config

    @property
    def attachment(self) -> Attachment:
        """The owner to talk to right now."""
        return self._attachment

    def open_client(self, *, timeout: float) -> ProxyClient:
        """Build a client for whoever the owner is at this moment.

        Read here rather than closed over, and that is the whole point of this
        object. ``ProxyProvider`` calls its factory for every upstream operation
        and never caches the client, so a factory that reads current state
        follows a replacement without any further machinery. A factory that
        captured the address instead keeps using it for the process's life.

        A fresh client per call rather than one shared session, because that is
        what the provider expects: it opens and closes a client around every
        upstream operation.
        """
        from fastmcp.client.transports import StreamableHttpTransport
        from fastmcp.server.providers.proxy import ProxyClient

        attachment = self._attachment
        # Verbatim, never rebuilt from host and port: the descriptor's own URL
        # already carries the MCP path and brackets an IPv6 literal, and FastMCP
        # deliberately does not rewrite the path it is given.
        url = attachment.descriptor.url
        # Read together with the URL, never separately. They are one credential
        # pair for one owner, and a token kept across an address change would
        # authenticate against a process it was never issued for.
        token = attachment.token

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


def create_proxy_provider(
    backend: DaemonProxyBackend, *, tool_timeout: float
) -> ProxyProvider:
    """Serve the owner's tools as if they were this server's own."""
    from fastmcp.server.providers.proxy import ProxyProvider

    timeout = tool_timeout + _TIMEOUT_MARGIN_SECONDS
    logger.debug(
        "Forwarding tool calls to the shared browser owner at %s (deadline %.0fs)",
        backend.attachment.descriptor.url,
        timeout,
    )
    # One callable, built once and never replaced. That is a requirement rather
    # than a style: `ProxyProvider` hands this object to every `ProxyTool` it
    # builds while listing, and keeps those tools for `_COMPONENT_CACHE_SECONDS`.
    # Reassigning the provider's factory later would not reach them, so the
    # object has to stay the same one and resolve inside.
    #
    # ProxyClient rather than a plain Client, and that is not a preference
    # either: a plain client installs no progress handler, so the progress every
    # browser-backed tool reports is silently dropped. Measured both ways.
    return ProxyProvider(
        partial(backend.open_client, timeout=timeout),
        cache_ttl=_COMPONENT_CACHE_SECONDS,
    )
