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
per operation too, from :class:`DaemonProxyBackend`, so a proxy follows a
replacement owner instead of keeping an address it captured at startup.

**Which owner that is changes while this process runs**, and the rest of this
module is about surviving it. ``@latest`` is the documented install, so the first
client to launch after an upgrade stands the old owner down, and every proxy
already attached to it would otherwise be dead until its own process restarts.
Reproduced end to end before any of this existed: a proxy serving 19 tools was
asked nothing, its owner was told to stand down the way a newer build does
(``daemon_election._ask_to_stand_down``), and the next listing failed with
``McpError: Client failed to connect``. Idle exit and a crashed owner end the
same way.

Noticing the departure is one half; repeating the call is the other, and it is
the half that can do damage. There is no dispatch acknowledgement anywhere in the
protocol, so a failure is classified where the boundary is known
(:class:`OwnerUnreachableError`) rather than diagnosed from a message afterwards.
Only a request that provably never left this process may be repeated when running
it twice would send a connection request twice.
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime
import enum
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx
import mcp.types as mt
from fastmcp.client.progress import ProgressHandler
from fastmcp.client.telemetry import client_span
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.telemetry import inject_trace_context
from fastmcp.tools import ToolResult
from fastmcp.utilities.timeout import normalize_timeout_to_timedelta
from opentelemetry.trace import Status, StatusCode

from linkedin_mcp_server import daemon_owner
from linkedin_mcp_server.daemon_auth import a_repeat_could_change_something
from linkedin_mcp_server.daemon_descriptor import PROTOCOL_VERSION
from linkedin_mcp_server.daemon_liveness import (
    CALL_HEADER,
    HEARTBEAT_PATH,
    HEARTBEAT_SECONDS,
    REFUSAL_KEY,
    RETIRING,
    UNMARKED_CALL,
    new_call_id,
    unknown_outcome,
)

if TYPE_CHECKING:
    from pathlib import Path

    from fastmcp.server.providers.proxy import ProxyClient, ProxyProvider

    from linkedin_mcp_server.config.schema import AppConfig
    from linkedin_mcp_server.daemon import Attachment

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CallBinding:
    """One call, and the owner it belongs to for as long as it runs.

    The two travel together and must not be read separately. The heartbeats and
    the request itself both name an owner, and if they name *different* ones the
    mechanism inverts: the owner running the call never hears a beat and cancels
    it after the expiry, while a call that is very much alive is reported to the
    user as abandoned. That is reachable, because with no component cache every
    call re-lists first, and a replacement adopted between the two would move
    the backend underneath this call.
    """

    call_id: str
    attachment: Attachment


#: The call the current task is making, for the factory to address and stamp.
#: A context variable rather than an argument because `ProxyProvider` builds the
#: client itself: the middleware that knows the call never sees the code that
#: builds the transport.
_call_being_made: contextvars.ContextVar[_CallBinding | None] = contextvars.ContextVar(
    "linkedin_mcp_call", default=None
)

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

#: The provider's component cache, switched off. FastMCP's default is 300
#: seconds and this module used to take it, on the stated grounds that an owner's
#: tool set never changes over its lifetime. That was true and is no longer the
#: question: a proxy now outlives several owners, and after an upgrade several
#: versions, so what a cache holds is one owner's answer about itself being
#: served for a different owner's calls.
#:
#: Clearing the cache when a replacement is adopted was the first design and it
#: is not sound. ``ProxyProvider`` writes each cache after a listing completes,
#: outside any lock (``proxy.py``, ``_list_tools`` and its three siblings), so a
#: listing already in flight against the departing owner can refill the cache
#: *after* the replacement was adopted. Reproduced against the installed provider
#: by holding a listing open across an adoption: the cleared cache came back
#: holding the old owner's components. No clear-on-adoption design closes that,
#: because the write is in code this module does not own.
#:
#: Freshness is not the sharp end. ``a_repeat_could_change_something`` reads
#: ``readOnlyHint`` off the tool the provider hands back, so a stale annotation
#: is what decides whether a call may be repeated against the new owner.
#:
#: Measured on this machine against a real loopback owner: 27.8ms versus 56.0ms
#: median per forwarded call, so the cache was worth about 28ms. A forwarded call
#: drives a browser through a LinkedIn page and takes seconds. Only single
#: lookups change either way; an explicit ``tools/list`` re-fetched regardless.
_NO_COMPONENT_CACHE = 0.0


class OwnerFailure(enum.Enum):
    """Why an owner could not take a call, in the terms recovery acts on.

    Separate from whether anything was sent, which is its own field on
    :class:`OwnerUnreachableError`: a burying failure is always one where the
    tool request never left, but the two answer different questions.
    """

    #: No connection could be opened. Maybe gone, maybe restarting; the
    #: election's own probe decides.
    UNREACHABLE = "unreachable"
    #: Connected, then no usable answer: a read timeout, a protocol error or a
    #: 5xx. A stalled owner looks like this and may answer a moment later.
    OWNER_ERROR = "owner_error"
    #: 401. The token this frontend holds is not the one the process on that
    #: port accepts, which usually means a different owner holds the port now.
    TOKEN_REJECTED = "token_rejected"
    #: 404 on the heartbeat route. Not an owner of this protocol.
    ROUTE_MISSING = "route_missing"
    #: This owner has closed admission and is on its way out.
    RETIRING = "retiring"
    #: This owner refused a call it could not identify.
    UNMARKED_REFUSED = "unmarked_refused"
    #: Any other answer. A same-protocol owner gives only 200, 401 and 409 on
    #: the heartbeat route, so anything else is not this owner as this
    #: frontend knows it.
    UNEXPECTED_STATUS = "unexpected_status"

    @property
    def buries(self) -> bool:
        """Whether this instance must never be dispatched to again.

        The failures that say something about the owner rather than about one
        moment of it. A retiring owner still answers the ping an election
        probes with, so without this it would be found, attached to, and
        refused again for as long as it took to leave.
        """
        return self in _BURYING


_BURYING = frozenset(
    {
        OwnerFailure.ROUTE_MISSING,
        OwnerFailure.RETIRING,
        OwnerFailure.UNMARKED_REFUSED,
        OwnerFailure.UNEXPECTED_STATUS,
    }
)


class OwnerUnreachableError(Exception):
    """The owner a proxy is attached to could not be reached.

    Raised where the boundary is known rather than diagnosed later, because two
    things only the client can see decide what a caller may do about it: which
    owner failed, and whether the request had left this process.

    *instance_id* is that owner's identity. A failure carries it so a recovery
    that has already happened is not repeated: a call that opened its client
    before a replacement was adopted fails afterwards against the old one, and
    without the identity that late failure would elect again.

    *nothing_was_sent* is the whole of the replay question. There is no dispatch
    acknowledgement anywhere in the protocol, so after an ambiguous transport
    failure this process cannot tell whether the owner was still queued, already
    held the profile lease, or had finished the effect. Only when nothing left
    this process is repeating a mutating call safe. A failed heartbeat
    preflight and an owner's own refusal both answer it for the tool request,
    not for the control exchange that carried the news.

    *classification* is carried to :meth:`DaemonProxyBackend.recover`, which is
    where it decides whether the owner is written off. Derived from the cause
    when not given, because every transport failure raised below already says
    whether a connection was ever opened.
    """

    def __init__(
        self,
        *,
        instance_id: str,
        nothing_was_sent: bool,
        cause: BaseException,
        classification: OwnerFailure | None = None,
    ) -> None:
        if classification is None:
            classification = (
                OwnerFailure.UNREACHABLE
                if _no_connection_was_established(cause)
                else OwnerFailure.OWNER_ERROR
            )
        if classification in (OwnerFailure.UNREACHABLE, OwnerFailure.OWNER_ERROR):
            message = f"The shared browser owner did not answer: {cause}"
        else:
            message = (
                f"The shared browser owner could not take this call "
                f"({classification.value}): {cause}"
            )
        super().__init__(message)
        self.instance_id = instance_id
        self.nothing_was_sent = nothing_was_sent
        self.classification = classification


def unreachable_owner_in(exc: BaseException) -> OwnerUnreachableError | None:
    """Find an unreachable-owner failure in an exception chain, if it is there.

    Walked rather than type-checked directly, and the same reason applies here as
    to ``daemon_auth._auth_failure_in``: what reaches a middleware is a wrapper.
    Measured against a dead port with a warm tool cache, the chain the middleware
    sees is ``ToolError -> RuntimeError('Client failed to connect') ->
    httpx.ConnectError``, because ``FastMCP.call_tool`` masks whatever the tool
    raised. A test that lists first is therefore the only one that exercises the
    real shape; a cold lookup can deliver the failure unwrapped and let a broken
    detector pass.
    """
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, OwnerUnreachableError):
            return current
        current = current.__cause__
    return None


def _no_connection_was_established(exc: BaseException) -> bool:
    """Whether the cause chain proves no connection was opened.

    The narrow, provable class, and it is narrow on purpose. The tempting rule is
    to read the message: FastMCP wraps nearly everything from its connect in
    ``RuntimeError(f"Client failed to connect: {exception}")``, so that prefix
    looks like proof. It is not. Reproduced against a listener that accepted the
    ``initialize`` bytes and then closed: same prefix, ``RemoteProtocolError``
    underneath, and a request that had demonstrably left this process.

    ``ConnectError`` and ``ConnectTimeout`` are the two that mean the connection
    itself was never established, so nothing on it can have been sent.
    """
    import httpx

    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, (httpx.ConnectError, httpx.ConnectTimeout)):
            return True
        current = current.__cause__
    return False


#: The code the MCP client invents when a POST comes back 404 because the
#: session is gone. Not exported by the SDK, so it is spelled out here with its
#: source: ``mcp/client/streamable_http.py``, ``_send_session_terminated_error``.
_SESSION_TERMINATED = 32600


def _the_owner_answered(exc: BaseException) -> bool:
    """Whether *exc* is the owner speaking, rather than a client that gave up.

    An ``McpError`` looks like an answer and usually is one: a JSON-RPC error
    came off the wire, so a process was there to send it. Treating those as a
    departure would elect a replacement for a healthy owner, repeat a read-only
    call for nothing, and swallow ``ProxyProvider``'s own ``METHOD_NOT_FOUND``
    handling, which turns an unsupported listing into an empty list.

    Three codes are not answers, and they matter more than all the real ones put
    together, because they are what a departure *during* a request looks like.
    The client invents each of them itself:

    * ``REQUEST_TIMEOUT`` when nothing came back inside the deadline
      (``mcp/shared/session.py``).
    * ``CONNECTION_CLOSED`` when the read stream closes with requests still
      pending (same file, the receive loop's ``finally``).
    * ``32600``/"Session terminated" when a POST is answered with 404 because
      the session is gone (``mcp/client/streamable_http.py``). Positive rather
      than in the ``-32xxx`` range, which is what makes it unmistakably local.

    Measured against a real loopback owner killed while a call was in flight:
    ``McpError: Timed out while waiting for response to CallToolRequest``. Nobody
    sent that. Passing it through as an answer would leave the frontend attached
    to a process that is gone, which is the failure this module exists to end.

    **Reading the code is a convention here, not a guarantee the protocol
    makes.** Nothing stops a server from sending any of these three integers as a
    real JSON-RPC error, and one that did would be called departed while it was
    answering. Two things make that acceptable. The owner is this same package,
    and it constructs no ``ErrorData`` and raises no ``McpError`` anywhere: a
    failing tool comes back as a result with ``isError`` set, so an owner built
    from this repository cannot send one of these as an answer. And the cost of
    being wrong is bounded, which is the same reason a merely slow owner is fine:
    the election probes, finds the same owner still answering, keeps the
    attachment, and the caller repeats only what was safe to repeat anyway.

    Matching the SDK's message text as well was considered and rejected. It would
    narrow the false positives and widen the dangerous failure: a reworded
    message in a future SDK would silently stop a real departure from being
    recognised, and the frontend would go back to sitting on a dead owner. A
    spurious election is a wasted probe; a missed departure is the bug this
    module exists to fix.

    A departure has three shapes, all measured with the client the provider
    builds, and only the third reaches this question:

    * already gone when the client connects: ``RuntimeError`` over
      ``httpx.ConnectError``, out of ``__aenter__``.
    * gone after the initialize and before the request that follows it, which is
      a real window because one operation opens a client, initializes it and then
      sends its request on that same session: ``anyio.BrokenResourceError`` or
      ``ClosedResourceError``, depending on timing.
    * gone while the request is outstanding: the invented ``McpError`` above.

    The first two are not ``McpError`` and are tagged without consulting any of
    this.
    """
    from mcp.shared.exceptions import McpError

    if not isinstance(exc, McpError):
        return False

    import httpx

    return exc.error.code not in (
        mt.CONNECTION_CLOSED,
        httpx.codes.REQUEST_TIMEOUT,
        _SESSION_TERMINATED,
    )


def _tells_which_owner_failed() -> type:
    """The proxy client that tags a transport failure with its owner.

    Built on demand rather than at import, because ``ProxyClient`` is a lazy
    import everywhere else here: FastMCP's server package is heavy and a direct
    server never needs it.
    """
    from fastmcp.server.providers.proxy import ProxyClient

    class TellsWhichOwnerFailed(ProxyClient):
        """A ``ProxyClient`` that says which owner failed, and whether it heard.

        The connect is one boundary and every discovery listing is another, and
        the listings are the ones that are easy to miss. ``ProxyTool.run`` is
        factory, then ``async with client``, then ``call_tool_mcp`` — but
        ``ProxyProvider._list_tools`` runs ``client.list_tools()`` *after* a
        successful enter, and its three siblings do the same for resources,
        templates and prompts. An owner that dies between the initialize and the
        list request raises out of neither the enter nor the call, so without
        these that failure would carry no owner identity and discovery could
        never recover.

        The dispatch answer differs by boundary, and being precise beats being
        uniformly cautious:

        * ``__aenter__`` — nothing was sent, *whatever* the exception. Not
          because the exception says so but because of where it happened:
          ``call_tool_mcp`` is inside the ``async with`` block and cannot have
          run. An initialize that reached the owner is still not a tool call.
        * the listings — listing changes nothing, so a caller may always repeat
          one and the answer is the same as an enter failure.
        * ``call_tool_mcp`` — only a failure to establish the connection proves
          it. A read timeout or a protocol error may mean the owner is scraping
          right now.

        ``__aexit__`` is a boundary of the opposite kind and tags nothing: it
        runs once the operation has an answer, so the only thing a failure there
        can do is take that answer's place. See its own docstring.
        """

        def __init__(self, *args: Any, instance_id: str, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._instance_id = instance_id

        async def _saying_which_owner(
            self, operation: Awaitable[Any], *, nothing_was_sent: bool | None
        ) -> Any:
            """Await *operation*, tagging a departed owner with its identity.

            *nothing_was_sent* is the boundary's own answer where the boundary
            knows it, and ``None`` where only the cause chain can say.
            """
            try:
                return await operation
            except OwnerUnreachableError:
                # Already ours, from a boundary further in. Tagging twice would
                # bury the answer the inner one recorded.
                raise
            except Exception as exc:
                if _the_owner_answered(exc):
                    raise
                raise OwnerUnreachableError(
                    instance_id=self._instance_id,
                    nothing_was_sent=(
                        _no_connection_was_established(exc)
                        if nothing_was_sent is None
                        else nothing_was_sent
                    ),
                    cause=exc,
                ) from exc

        async def __aenter__(self) -> Any:
            return await self._saying_which_owner(
                super().__aenter__(), nothing_was_sent=True
            )

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: Any,
        ) -> bool | None:
            """Close the session, and let nothing here decide the outcome.

            The one boundary that runs *after* an answer exists, and the reason
            it needs guarding at all is that the answer is already on its way
            out. ``ProxyTool.run`` makes its call inside ``async with client``
            and all four provider listings do the same (``proxy.py``, ``run``
            and ``_list_tools`` with its siblings), while ``Client._disconnect``
            awaits the session task under
            ``suppress(asyncio.CancelledError)`` (``client.py``). Only
            cancellation is suppressed, so an ordinary exception from that task
            leaves this context manager and replaces whatever the operation had
            decided.

            Both directions are damage, measured through the real stack. With a
            tagged ``OwnerUnreachableError`` in flight, the replacement reaches
            the middleware carrying the tag in ``__context__`` only, which
            ``unreachable_owner_in`` does not walk and must not start walking:
            Python sets ``__context__`` on *any* exception raised while another
            is being handled, and this middleware raises its repeat inside the
            ``except`` that holds the first failure, so every unrelated failure
            of a repeat would resolve to the owner loss before it — absorbed by
            today's branches, which read that first failure's own
            ``nothing_was_sent``, and one edit away from not being. The
            middleware therefore re-raises at its ``failure is None`` branch and
            masking flattens that to ``Error calling tool
            'send_connection_request'`` — nothing about a call that may have
            acted. With nothing in flight it is the *result* that is replaced,
            and the owner had already run the tool: the same generic failure for
            a call that did send the connection request, and this half carries no
            tag anywhere, so no detector could have found it.

            Swallowing adds no leak of its own here, which is a narrower claim
            than a promise about every way cleanup can fail. A client here is
            built for one operation and dropped (``open_client``), and
            ``_disconnect`` drops the session task in its own ``finally`` —
            cancelling it first if it is still running — before this is reached,
            so what arrives is the report of a failure and not a resource still
            held; what a future ``_disconnect`` might leave behind when *it*
            fails is its own to answer for. The operation succeeded or it
            failed, and a departure this one misses is found by the next
            operation, which opens a client of its own.
            Cancellation is not caught, because ``CancelledError`` is a
            ``BaseException`` and a caller giving up is not the session's own
            failure.

            One falsy return says both things. Python consults it only while an
            exception is in flight, where falsy means "do not suppress" and the
            tagged failure goes on propagating; with nothing in flight it is
            ignored and the result stands.
            """
            try:
                return await super().__aexit__(exc_type, exc_val, exc_tb)
            except Exception as closing:
                logger.debug(
                    "The shared browser owner's session failed while closing, "
                    "after the operation had already %s",
                    "failed" if exc_val is not None else "answered",
                    exc_info=closing,
                )
                return False

        async def list_tools_mcp(self, *args: Any, **kwargs: Any) -> Any:
            return await self._saying_which_owner(
                super().list_tools_mcp(*args, **kwargs), nothing_was_sent=True
            )

        async def list_resources_mcp(self, *args: Any, **kwargs: Any) -> Any:
            return await self._saying_which_owner(
                super().list_resources_mcp(*args, **kwargs), nothing_was_sent=True
            )

        async def list_resource_templates_mcp(self, *args: Any, **kwargs: Any) -> Any:
            return await self._saying_which_owner(
                super().list_resource_templates_mcp(*args, **kwargs),
                nothing_was_sent=True,
            )

        async def list_prompts_mcp(self, *args: Any, **kwargs: Any) -> Any:
            return await self._saying_which_owner(
                super().list_prompts_mcp(*args, **kwargs), nothing_was_sent=True
            )

        async def call_tool_mcp(
            self,
            name: str,
            arguments: dict[str, Any],
            progress_handler: ProgressHandler | None = None,
            timeout: datetime.timedelta | float | int | None = None,
            meta: dict[str, Any] | None = None,
        ) -> mt.CallToolResult:
            """Call the owner without discovering its schema after it answers.

            ``ClientSession.call_tool`` validates a successful result by listing
            tools when this fresh session has no cached output schema. A failure in
            that second request replaces an answer to a call that may already have
            changed LinkedIn, and its ``ConnectError`` then looks safe to repeat.
            The owner already validates declared output schemas before answering;
            this boundary only needs the protocol result it received.
            """

            async def send_request() -> mt.CallToolResult:
                with client_span(
                    f"tools/call {name}",
                    "tools/call",
                    name,
                    session_id=self.transport.get_session_id(),
                    tool_name=name,
                ) as span:
                    logger.debug("[%s] called call_tool: %s", self.name, name)
                    propagated_meta = inject_trace_context(meta)
                    request_meta = (
                        mt.RequestParams.Meta(**propagated_meta)
                        if propagated_meta
                        else None
                    )
                    result = await self._await_with_session_monitoring(
                        self.session.send_request(
                            mt.ClientRequest(
                                mt.CallToolRequest(
                                    params=mt.CallToolRequestParams(
                                        name=name,
                                        arguments=arguments,
                                        _meta=request_meta,
                                    )
                                )
                            ),
                            mt.CallToolResult,
                            request_read_timeout_seconds=(
                                normalize_timeout_to_timedelta(timeout)
                            ),
                            progress_callback=(
                                progress_handler or self._progress_handler
                            ),
                        )
                    )
                    if result.isError and span.is_recording():
                        span.set_attribute("error.type", "tool_error")
                        description = ""
                        if result.content and isinstance(
                            result.content[0], mt.TextContent
                        ):
                            description = result.content[0].text
                        span.set_status(Status(StatusCode.ERROR, description))
                    return result

            return await self._saying_which_owner(send_request(), nothing_was_sent=None)

    return TellsWhichOwnerFailed


#: How many elections one recovery may join. The second exists only for a
#: caller whose burial the first election did not know about.
_ELECTIONS_PER_RECOVERY = 2


@dataclass(frozen=True)
class _Flight:
    """One election in progress, and which owners it was told to pass over."""

    task: asyncio.Task[Attachment | None]
    buried: frozenset[str]


class _WrittenOff(Exception):
    """The cause on a failure for an owner this process already wrote off."""

    def __init__(self, classification: OwnerFailure) -> None:
        super().__init__(f"the owner was already written off ({classification.value})")


def _refuse_control_only(attachment: Attachment) -> None:
    """Refuse to dispatch anything through a pair proved for control only.

    Checked wherever an attachment becomes somewhere to send a call, not just
    where it is produced. The lookup that makes one never marks it attachable,
    and that is one rule in one place; this makes the prohibition hold even if
    that place changes.
    """
    if attachment.control_only:
        raise ValueError(
            "A shared browser owner proved for control only cannot run tools"
        )


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
    supply defaults for a directly constructed server, not the exact inputs that
    elected the owner being replaced.

    Not frozen, unlike the ``Attachment`` it holds. The attachment is a proved
    fact about one owner and must not be edited; which attachment is current is
    exactly the thing that moves.

    **Some owners are written off for good.** An owner that refused a call as
    retiring, answered the heartbeat route with 404 or anything unexpected, or
    refused an unmarked call is recorded in ``_unusable`` and never dispatched
    to again by this process. Every consumer asks: a new call, a listing, an
    election's result before it is adopted, and a caller that joined an election
    already in progress. A call already running keeps the owner it was bound to,
    because moving its heartbeats would get it cancelled by the owner running it.
    """

    def __init__(
        self,
        *,
        attachment: Attachment,
        auth_root: Path,
        profile: Path,
        config: AppConfig,
    ) -> None:
        _refuse_control_only(attachment)
        self._attachment = attachment
        #: What an election needs, kept rather than looked up again.
        self.auth_root = auth_root
        self.profile = profile
        self.config = config
        #: The one election in progress, or nothing. Only :meth:`recover` writes
        #: it, and only :meth:`_elect` writes ``_attachment``, which is what makes
        #: comparing instance ids a sound test of whether a failure is current.
        self._electing: _Flight | None = None
        #: Owners this process will not dispatch to again, and why. Only grows.
        self._unusable: dict[str, OwnerFailure] = {}

    @property
    def attachment(self) -> Attachment:
        """The owner to talk to right now."""
        return self._attachment

    def note_failure(self, instance_id: str, classification: OwnerFailure) -> None:
        """Write *instance_id* off if *classification* says it is unusable.

        Synchronous and first, before any election is joined or started, so a
        burial this caller learned is part of every decision made after it.
        """
        if classification.buries and instance_id not in self._unusable:
            logger.info(
                "Not using this shared browser owner again (%s)", classification.value
            )
            self._unusable[instance_id] = classification

    def attachment_for_a_call(self) -> Attachment:
        """The owner a new call may be dispatched to, or a failure saying why not.

        The failure says nothing was sent, which is true: nothing was. It names
        the owner that was written off, so the recovery above elects past it.
        """
        attachment = self._attachment
        _refuse_control_only(attachment)
        instance = attachment.descriptor.instance_id
        written_off = self._unusable.get(instance)
        if written_off is not None:
            raise OwnerUnreachableError(
                instance_id=instance,
                nothing_was_sent=True,
                cause=_WrittenOff(written_off),
                classification=written_off,
            )
        return attachment

    async def recover(
        self,
        failed_instance: str,
        *,
        classification: OwnerFailure = OwnerFailure.UNREACHABLE,
    ) -> Attachment | None:
        """Find an owner to replace *failed_instance*, at most once at a time.

        Returns the attachment to use now, or ``None`` when none could be
        established.

        *classification* is why the failed owner failed. One that buries it is
        recorded before anything else happens (:meth:`note_failure`).

        **The identity check comes first, and it is what keeps a late failure
        cheap.** Several calls can be in flight against one owner. The first to
        fail elects a replacement; the others fail afterwards still carrying the
        old identity, and without this they would each elect again, against an
        owner that is already answering.

        **The latest burial wins over an election already running.** A caller
        that joins a flight started before it buried an owner cannot trust that
        flight's answer, because the election ran without knowing: a retiring
        owner still answers its probe. An answer naming a written-off owner is
        refused, and if the flight did not know everything this caller knows,
        one more flight is run with it. At most two per call here, so a
        recovery cannot spin.

        **The election runs in a thread, and that is correctness rather than
        latency.** ``obtain_owner`` probes liveness through ``asyncio.run``, which
        raises inside a running loop, and its own ``except`` turns that into
        ``Reach.REFUSED``. Called in-loop it would therefore bury a *healthy*
        owner for the whole election rather than merely blocking.

        **Shielded, and this one is measured.** Cancelling the task that awaits
        ``asyncio.to_thread`` returns to the awaiter at once while the worker runs
        to completion regardless: driven directly, an awaiter cancelled at 0.1s
        and a worker that still finished its 1.5s of work. So a caller that gives
        up must not clear this guard, or the next failure starts a second election
        while the first is still running.
        """
        self.note_failure(failed_instance, classification)
        current = self._attachment.descriptor.instance_id
        if current != failed_instance and current not in self._unusable:
            # Somebody already replaced it. Nothing to do but use the answer.
            return self._attachment

        for _ in range(_ELECTIONS_PER_RECOVERY):
            flight = self._electing
            if flight is None:
                # Created and stored with no await in between, so two callers
                # cannot both get past this and start one each. The burial set
                # is a snapshot, handed to a thread.
                buried = frozenset(self._unusable)
                flight = _Flight(asyncio.create_task(self._elect(buried)), buried)
                self._electing = flight
            # Shielded so this caller's deadline cannot cancel the shared election.
            found = await asyncio.shield(flight.task)
            if found is not None and found.descriptor.instance_id in self._unusable:
                found = None
            if found is not None:
                if (
                    classification is OwnerFailure.TOKEN_REJECTED
                    and found.descriptor.instance_id == failed_instance
                ):
                    # A token is minted per instance and never changes, so the
                    # same instance comes back with the same token that was just
                    # refused. Another attempt would only be refused again.
                    return None
                return found
            if self._unusable.keys() <= flight.buried:
                # The election knew everything this caller knows. Its answer
                # stands, and asking again would get the same one.
                return None
        return None

    async def _elect(self, buried: frozenset[str]) -> Attachment | None:
        """Run one election and adopt what it finds, unless it is written off."""
        from linkedin_mcp_server.daemon_election import obtain_owner

        try:
            outcome = await asyncio.to_thread(
                partial(
                    obtain_owner,
                    self.auth_root,
                    self.profile,
                    self.config,
                    buried=buried,
                )
            )
        except Exception:
            logger.warning(
                "Could not elect a replacement for the shared browser owner",
                exc_info=True,
            )
            return None
        finally:
            # Only after the worker has ended, and only if this is still the
            # registered flight: a caller that timed out may already have left,
            # and a later election must not be cleared by an earlier one.
            flight = self._electing
            if flight is not None and flight.task is asyncio.current_task():
                self._electing = None

        found = outcome.attachment_lookup.attachment
        if not outcome.worth_connecting or found is None or found.control_only:
            logger.warning(
                "No shared browser owner could be established (%s)",
                outcome.attachment_lookup.state.value,
            )
            return None

        # Against the latest set, not the snapshot the election ran with: an
        # owner written off while it ran must not become the one every new call
        # is sent to.
        if found.descriptor.instance_id in self._unusable:
            logger.info("The election found an owner already written off")
            return None

        if found.descriptor.instance_id != self._attachment.descriptor.instance_id:
            logger.info("Attached to a replacement shared browser owner")
            self._attachment = found
        return found

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

        # Which call this request belongs to, if it belongs to one, and the owner
        # that call was bound to. Read together and never separately: a call that
        # dialled a replacement while its heartbeats stayed with the departing
        # owner would be cancelled by the owner actually running it.
        bound = _call_being_made.get()
        # A call keeps the owner it was bound to, which its heartbeat middleware
        # already checked. Anything else, a listing above all, gets the owner a
        # new call would get, including the refusal when that one is written off.
        attachment = (
            bound.attachment if bound is not None else self.attachment_for_a_call()
        )
        _refuse_control_only(attachment)
        # Verbatim, never rebuilt from host and port: the descriptor's own URL
        # already carries the MCP path and brackets an IPv6 literal, and FastMCP
        # deliberately does not rewrite the path it is given.
        url = attachment.descriptor.url
        # Read together with the URL, never separately. They are one credential
        # pair for one owner, and a token kept across an address change would
        # authenticate against a process it was never issued for.
        token = attachment.token

        return _tells_which_owner_failed()(
            StreamableHttpTransport(
                url,
                headers={CALL_HEADER: bound.call_id} if bound is not None else None,
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
            # So a failure says which owner it was against. Read from the same
            # attachment as the URL and the token, so all three describe one owner
            # even while a replacement is being adopted concurrently.
            instance_id=attachment.descriptor.instance_id,
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
    # builds while listing, and a tool outlives the listing that built it.
    # Reassigning the provider's factory later would not reach them, so the
    # object has to stay the same one and resolve inside.
    #
    # ProxyClient rather than a plain Client, and that is not a preference
    # either: a plain client installs no progress handler, so the progress every
    # browser-backed tool reports is silently dropped. Measured both ways.
    return ProxyProvider(
        partial(backend.open_client, timeout=timeout),
        cache_ttl=_NO_COMPONENT_CACHE,
    )


class FrontendOwnerRecoveryMiddleware(Middleware):
    """Find a replacement owner when this one is gone, and repeat what is safe.

    Installed only on a proxy, and after the auth-repair middleware so that one
    stays outermost: a call and the replay a sign-in triggers each get their own
    liveness wrapper, rather than one wrapper around both.

    Discovery is covered as well as calling, and it is the half a client hits
    first: many list before they call. A recovery that only covered
    ``tools/call`` would leave a client whose owner died unable to list, and
    therefore unable to make the call that would have triggered the recovery:
    dead in exactly the way this exists to prevent.

    All four listings, not only tools, although this role serves nothing but
    tools. A client's opening exchange asks for resources and prompts too, the
    provider is asked in order to answer "none", and with
    ``provider_error_strategy = "raise"`` a departed owner turns that answer into
    an error the user sees.

    Reads and renders are left out, and the reason is a fact about this
    repository rather than about the protocol: no owner here registers a resource
    or a prompt, so the listings in front of them find nothing and a client
    cannot reach ``resources/read`` or ``prompts/get`` at all. The day one is
    registered, ``read_resource_mcp`` and ``get_prompt_mcp`` need the same
    treatment as the listings, along with ``on_read_resource`` and
    ``on_get_prompt`` here.

    **What one tool call can cost, counted.** This middleware makes at most two
    attempts per invocation. Each attempt is one
    heartbeat preflight and at most one dispatch; only the first attempt's
    failure recovers, joining at most :data:`_ELECTIONS_PER_RECOVERY`
    elections, and the second's is recorded without electing. The auth-repair
    middleware outside invokes this at most twice per client call, once and one
    read-only replay. So one client call is at most four preflights, four
    dispatches and four election joins, and never a third dispatch from one
    invocation.
    """

    def __init__(self, backend: DaemonProxyBackend) -> None:
        self._backend = backend

    async def _repeat_the_listing(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        try:
            return await call_next(context)
        except Exception as exc:
            failure = unreachable_owner_in(exc)
            if failure is None:
                raise
            logger.info("Listing failed against a departed owner; looking for another")
            replacement = await self._backend.recover(
                failure.instance_id, classification=failure.classification
            )
            if replacement is None:
                raise
            # Unconditional, because listing changes nothing on LinkedIn. There is
            # no effect a second one could repeat.
            return await call_next(context)

    async def on_list_tools(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        return await self._repeat_the_listing(context, call_next)

    async def on_list_resources(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        return await self._repeat_the_listing(context, call_next)

    async def on_list_resource_templates(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        return await self._repeat_the_listing(context, call_next)

    async def on_list_prompts(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        return await self._repeat_the_listing(context, call_next)

    def _report_an_unknown_outcome(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        failure: OwnerUnreachableError,
    ) -> ToolResult:
        """Say that nobody here knows whether the call acted.

        The one case where the honest answer is to report the failure. Nothing in
        the protocol says whether the departed owner had already sent the
        connection request or the message, and asking costs one retry while
        guessing wrong sends it twice. The user knows which happened; this
        process does not.

        A result rather than a raise, and what decides that is the payload rather
        than the masking. Two raises have to be told apart, both measured against
        fastmcp 3.4.7 with the masking this server switches on (``server.py``,
        ``mask_error_details=True``). Re-raising the failure that arrived says
        nothing: ``OwnerUnreachableError`` is a plain ``Exception``, so the call
        below has already replaced it with ``ToolError("Error calling tool
        'send_message'")`` (``fastmcp/server/server.py:1342-1358``), and that is
        the whole of what the client gets on the one call that most needs to
        hear a retry can deliver the message twice. A ``ToolError`` raised
        *here* keeps its own text: the masking sits below this middleware, and
        even there a ``FastMCPError`` — which ``ToolError`` is — is re-raised
        untouched (``:1327-1331``). What neither raise carries is ``status`` and
        ``retry_safe``, the two fields a client can act on without reading
        prose, and they are why this answer is a result.

        ``is_error`` stays true so a client that reads no structured content
        still sees a failure rather than a success carrying no data, the way
        ``daemon_auth`` answers a sign-in.

        Keeping it true is also what lets this payload ignore the tool's declared
        output schema: an error result travels as a whole ``CallToolResult``,
        which ``mcp.server.lowlevel.server`` returns unchanged instead of
        validating it. A success-shaped dict would be checked against the schema
        of whichever tool was called, and this one is a stand-in for all of them.
        """
        logger.info(
            "Owner lost mid-call; reporting an unknown outcome rather "
            "than repeating a call that could change something"
        )
        answer = unknown_outcome(tool=context.message.name, reason=str(failure))
        return ToolResult(
            content=[mt.TextContent(type="text", text=answer["message"])],
            structured_content=answer,
            is_error=True,
        )

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except Exception as exc:
            failure = unreachable_owner_in(exc)
            if failure is None:
                raise

            # Before deciding whether to repeat anything: a replacement is worth
            # having for the *next* call even when this one cannot be repeated.
            # The classification goes with it, so an owner that refused the call
            # is written off before any election is joined.
            replacement = await self._backend.recover(
                failure.instance_id, classification=failure.classification
            )

            # Asked before the replacement is looked at, because the answer does
            # not depend on it: whether the departed owner already acted is
            # unknown either way, and an election that found nobody makes it no
            # more knowable. The cost of the new order is that
            # `a_repeat_could_change_something` now also runs when the election
            # failed; it catches its own exceptions and answers `True`, so a
            # lookup against a dead backend lands on the cautious side.
            #
            # Not bounded by the owner's tool timeout, which bounds the forwarded
            # call that has already failed and never enclosed this middleware.
            # Several separate budgets do bound it: the lookup goes through
            # `fastmcp.get_tool`, and with the component cache off
            # (`_NO_COMPONENT_CACHE`) that is a fresh forwarded listing carrying
            # the client's own deadline (`create_proxy_provider`, `tool_timeout +
            # _TIMEOUT_MARGIN_SECONDS`), after a `recover` above that may already
            # have spent `daemon_election.DEFAULT_ELECTION_SECONDS`. And a
            # cancellation is not one of the exceptions that lands on the
            # cautious side: `CancelledError` is a `BaseException`, so it passes
            # that `except Exception` and leaves through here, which is what
            # should happen to it.
            #
            # Kept rather than dropped, because the second `except` below needs
            # the same answer and by then there is nobody left to give it. It
            # stays `None` when the short-circuit means the question was never
            # put, which is the one case with nothing to remember.
            could_change_something: bool | None = None
            if not failure.nothing_was_sent:
                could_change_something = await a_repeat_could_change_something(context)
                if could_change_something:
                    return self._report_an_unknown_outcome(context, failure)

            if replacement is None:
                raise

            logger.info("Attached to a replacement owner; running the call again")
            try:
                return await call_next(context)
            except Exception as again:
                # The replacement can go away exactly like the first owner, and
                # by then the repeat may have acted: it was made only because
                # nothing left this process the first time, which says nothing
                # about the second. Left to escape, it reaches the client as
                # `Error calling tool 'send_message'` and nothing else, which is
                # the #891 damage one round later — a client told nothing repeats
                # a message that may already be delivered.
                #
                # Classified again rather than attempted again. Exactly two
                # attempts: no recursion here and no second `recover`, because a
                # third attempt would be a guess about an owner that has now
                # failed twice, and the question it raises — whether the second
                # attempt acted — is the one nothing here can answer.
                #
                # Recorded, though, without electing: an owner the repeat found
                # retiring or wrong must not receive the next call either, and
                # the next call's own recovery is what finds a replacement.
                repeat = unreachable_owner_in(again)
                if repeat is not None:
                    self._backend.note_failure(
                        repeat.instance_id, repeat.classification
                    )
                if repeat is not None and not repeat.nothing_was_sent:
                    # The answer the first attempt already has, rather than the
                    # same question put to an owner that has just died. Both
                    # readings are about one tool and the tool did not change;
                    # what changed is who is left to answer. The lookup goes
                    # through `fastmcp.get_tool`, which with the component cache
                    # off is a forwarded listing against exactly that departed
                    # owner, so it raises and `a_repeat_could_change_something`
                    # answers `True` on the cautious side. Caution about a
                    # question already answered is not caution: it turns a read
                    # repeated *because* the first lookup called it read-only
                    # into an `outcome_unknown` carrying `retry_safe: False`,
                    # which sends the user to look on LinkedIn for an effect a
                    # read cannot have had.
                    if could_change_something is None:
                        # Nothing was remembered, because the first pass
                        # short-circuited on `nothing_was_sent` and never asked.
                        # Here the lookup is the only source there is, and its
                        # failing open is still the right side to fail on: an
                        # unreadable annotation is not a promise of safety.
                        could_change_something = await a_repeat_could_change_something(
                            context
                        )
                    if could_change_something:
                        return self._report_an_unknown_outcome(context, repeat)
                raise


class _NotAnOwnerAnswer(Exception):
    """The cause on a preflight that was answered, but not with a go-ahead."""

    def __init__(self, status: int, classification: OwnerFailure) -> None:
        super().__init__(
            f"the heartbeat preflight was answered with HTTP {status} "
            f"({classification.value})"
        )


class _OwnerRefusedTheCall(Exception):
    """The cause on a call the owner signed as never having reached a tool."""

    def __init__(self, classification: OwnerFailure) -> None:
        super().__init__(f"the owner refused the call ({classification.value})")


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    """The response body as a JSON object, or ``None`` for anything else."""
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _signed_by(marker: object, attachment: Attachment, kind: str) -> bool:
    """Whether *marker* is this owner's own refusal of the given *kind*.

    Every field is checked against the owner this call was bound to. The
    protocol is compared as an exact ``int`` because ``True == 1`` in Python,
    and a marker from another owner, another protocol or of another shape is
    not proof that this call never ran.
    """
    if not isinstance(marker, dict):
        return False
    protocol = marker.get("protocol")
    return (
        marker.get("daemon") == kind
        and type(protocol) is int
        and protocol == PROTOCOL_VERSION
        and marker.get("instance") == attachment.descriptor.instance_id
    )


def _classify_preflight(
    response: httpx.Response, attachment: Attachment
) -> OwnerFailure | None:
    """What a heartbeat preflight's answer means, or ``None`` to go ahead.

    Total: every status has an outcome, and only one of them dispatches. A 200
    must carry the body this owner's route sends, so a stranger on the port that
    answers everything with 200 is not taken for the owner.
    """
    status = response.status_code
    if status == 200:
        body = _json_object(response)
        if body is not None and isinstance(body.get("watched"), bool):
            return None
        return OwnerFailure.UNEXPECTED_STATUS
    if status == 401:
        return OwnerFailure.TOKEN_REJECTED
    if status == 404:
        return OwnerFailure.ROUTE_MISSING
    if status == 409:
        if _signed_by(_json_object(response), attachment, RETIRING):
            return OwnerFailure.RETIRING
        return OwnerFailure.UNEXPECTED_STATUS
    if 500 <= status <= 599:
        return OwnerFailure.OWNER_ERROR
    return OwnerFailure.UNEXPECTED_STATUS


_REFUSALS = {
    RETIRING: OwnerFailure.RETIRING,
    UNMARKED_CALL: OwnerFailure.UNMARKED_REFUSED,
}


def _owner_refused(result: object, attachment: Attachment) -> OwnerFailure | None:
    """The owner's own refusal on *result*, if it is one it signed for this call.

    Anything else is tool data, whatever it looks like, and proves nothing
    about whether the tool ran.
    """
    if not isinstance(result, ToolResult) or not result.is_error:
        return None
    marker = (result.meta or {}).get(REFUSAL_KEY)
    for kind, classification in _REFUSALS.items():
        if _signed_by(marker, attachment, kind):
            return classification
    return None


class FrontendCallHeartbeatMiddleware(Middleware):
    """Mark every call, and say for as long as this frontend waits that it still is.

    The other half of `daemon_liveness`. Cancellation does not cross the hop, so
    an owner cannot tell a call somebody wants from one whose client has gone;
    this is the frontend answering that question over and over until it stops
    caring, at which point the answer stops arriving and the owner draws the
    obvious conclusion.

    **No call leaves unmarked.** The first beat is a preflight, and only a
    validated 200 dispatches the call. Every other answer, and every failure to
    get one, raises :class:`OwnerUnreachableError` with ``nothing_was_sent``
    true and a classification, before the call is handed on. The same holds for
    an owner that answered the call itself with its signed refusal: the tool
    never ran, and recovery may repeat it.

    Innermost of the three a proxy installs, and that is what makes a replay
    behave: the recovery middleware sits outside, so a call it runs again enters
    here a second time and gets its own identifier and its own heartbeats
    against whichever owner it is now talking to.
    """

    def __init__(self, backend: DaemonProxyBackend) -> None:
        self._backend = backend

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        call_id = new_call_id()
        # Captured once, and every beat for this call goes here even if another
        # call adopts a replacement owner meanwhile. Beating at the new owner
        # would name a call it has never heard of, while the owner actually
        # running this one stopped being told about it. Refused here if the
        # current owner was written off or proved for control only.
        attachment = self._backend.attachment_for_a_call()

        refused = await self._preflight(attachment, call_id)
        if refused is not None:
            raise refused

        beating = asyncio.create_task(self._keep_saying(attachment, call_id))
        marked = _call_being_made.set(_CallBinding(call_id, attachment))
        try:
            result = await call_next(context)
        finally:
            _call_being_made.reset(marked)
            # In a finally, and unconditionally: a task left running would go on
            # keeping a finished call alive in the owner's tracker, which is the
            # exact state this exists to prevent.
            beating.cancel()

        classification = _owner_refused(result, attachment)
        if classification is not None:
            logger.info(
                "The shared browser owner refused the call before running it (%s)",
                classification.value,
            )
            raise OwnerUnreachableError(
                instance_id=attachment.descriptor.instance_id,
                nothing_was_sent=True,
                cause=_OwnerRefusedTheCall(classification),
                classification=classification,
            )
        return result

    async def _preflight(
        self, attachment: Attachment, call_id: str
    ) -> OwnerUnreachableError | None:
        """Beat once before dispatching, and say why not if the call must not go.

        Its not-sent answer is about the tool request, which this makes certain
        has not left, and not about the preflight, which may well have arrived.
        A connection that was never opened is ``UNREACHABLE``; one that opened
        and then failed is ``OWNER_ERROR``. Neither buries the owner: a restart
        or a stall looks like either, and the election's own probe decides.
        """
        try:
            response = await self._beat(attachment, call_id)
        except Exception as exc:
            logger.info(
                "The shared browser owner did not answer the call preflight",
                exc_info=True,
            )
            return OwnerUnreachableError(
                instance_id=attachment.descriptor.instance_id,
                nothing_was_sent=True,
                cause=exc,
            )
        classification = _classify_preflight(response, attachment)
        if classification is None:
            return None
        logger.info(
            "The shared browser owner refused the call preflight (HTTP %s, %s)",
            response.status_code,
            classification.value,
        )
        return OwnerUnreachableError(
            instance_id=attachment.descriptor.instance_id,
            nothing_was_sent=True,
            cause=_NotAnOwnerAnswer(response.status_code, classification),
            classification=classification,
        )

    async def _keep_saying(self, attachment: Attachment, call_id: str) -> None:
        """Beat until cancelled, and never let a failed beat end the run.

        A beat that fails is not evidence the call is over: the owner may be
        momentarily busy, and giving up here would expire a call that is running
        perfectly well. The frontend stops saying it is waiting only when it
        stops waiting, which is what the cancellation in the caller's `finally`
        expresses.
        """
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                await self._beat(attachment, call_id)
            except Exception:
                logger.debug("A heartbeat did not arrive", exc_info=True)

    @staticmethod
    async def _beat(attachment: Attachment, call_id: str) -> httpx.Response:
        """One heartbeat, returning the owner's answer with its body read.

        The address is the published one with its path replaced, rather than
        rebuilt from host and port: the descriptor's URL already brackets an
        IPv6 literal, and rebuilding is how that bracket gets lost.

        The owner's own client factory, for the reason it exists: httpx honours
        ``HTTP_PROXY`` even for loopback unless ``NO_PROXY`` happens to say
        otherwise, and this request carries the bearer token.
        """
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(attachment.descriptor.url)
        url = urlunsplit((parts.scheme, parts.netloc, HEARTBEAT_PATH, "", ""))
        async with daemon_owner.direct_async_http_client(
            headers={
                "Authorization": f"Bearer {attachment.token}",
                CALL_HEADER: call_id,
            },
            timeout=httpx.Timeout(HEARTBEAT_SECONDS),
        ) as client:
            # Not streamed, so the body is read before the client closes and the
            # response stays readable after it.
            return await client.post(url)
