"""Forwarding the owner's tools to a frontend, through public MCP and FastMCP APIs.

A FastMCP :class:`~fastmcp.server.providers.Provider` that lists the owner's
tools and runs each call through an ``mcp.ClientSession`` over streamable HTTP.
It overrides only the documented ``Provider._list_tools`` and the public
:meth:`~mcp.ClientSession.send_request` and
:meth:`~mcp.ClientSession.validate_tool_result`, and reads no private attribute
or SDK error message of either library, because ``uvx`` resolves both fresh
(``tests/test_daemon_forwarder.py`` holds that line).

- One hop per operation: every listing and call opens its own client and
  session against the owner the backend names at that moment, because an
  upgrade replaces the owner under a running frontend.
- The hop speaks one protocol version (:data:`HOP_PROTOCOL_VERSION`); the
  external host still negotiates any version with the frontend.
- How far a tool request got is read off the wire (:class:`DispatchRecord`,
  :class:`_Wire`), never off an exception: the SDK folds HTTP failures into
  ``MCPError``\\ s of its own.
- No component cache: ``readOnlyHint`` decides whether a call may be repeated
  against a replacement owner, so a lookup lists afresh.
"""

from __future__ import annotations

import contextvars
import enum
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, NoReturn, TypeVar, cast

import httpx2
import mcp.types as mt
from fastmcp.server.dependencies import get_context
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool, ToolResult
from fastmcp.utilities.components import get_fastmcp_metadata
from fastmcp.utilities.tasks import TaskConfig
from mcp import ClientSession, MCPError
from mcp.client.streamable_http import streamable_http_client
from pydantic import Field
from pydantic.json_schema import SkipJsonSchema

from linkedin_mcp_server import __version__
from linkedin_mcp_server.daemon import owner as daemon_owner
from linkedin_mcp_server.daemon.core.call import Dispatch
from linkedin_mcp_server.daemon.liveness import CALL_HEADER

if TYPE_CHECKING:
    from types import TracebackType

    from fastmcp.server.context import Context
    from fastmcp.utilities.components import FastMCPComponent
    from mcp.client.session import LoggingFnT
    from mcp.shared.dispatcher import ProgressFnT
    from mcp.shared.message import ClientMessageMetadata
    from pydantic import BaseModel, TypeAdapter

    from linkedin_mcp_server.daemon.discovery import Attachment
    from linkedin_mcp_server.daemon.proxy import DaemonProxyBackend

logger = logging.getLogger(__name__)

_ResultT = TypeVar("_ResultT", bound="BaseModel")

#: The one MCP protocol version the frontend speaks to its owner. A literal
#: rather than the SDK's latest, so a dependency upgrade cannot change it.
#: Stateless, because the handshake eras stream through sse-starlette, which
#: ends every stream once any uvicorn server in the process shuts down, and an
#: owner standing down would lose its last answers
#: (docs/decisions/2026-10-12-daemon-core-and-forwarder.md).
HOP_PROTOCOL_VERSION = "2026-07-28"

#: Who the frontend says it is to the owner.
_CLIENT_INFO = mt.Implementation(
    name="mcp-server-linkedin-frontend", version=__version__
)

#: Request ``_meta`` keys that describe the frontend's own connection to its
#: host. They never cross the hop, which is a connection of its own.
_CONNECTION_META_KEYS = frozenset(
    {
        mt.PROTOCOL_VERSION_META_KEY,
        mt.CLIENT_INFO_META_KEY,
        mt.CLIENT_CAPABILITIES_META_KEY,
    }
)


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

        A retiring owner still answers the election's probe, so without this
        it would be found and refused again until it left.
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

    Raised where the boundary is known, never diagnosed later. *instance_id*
    names the failed owner, so a late failure does not repeat a recovery that
    already happened. *nothing_was_sent* is the whole replay question: with no
    dispatch acknowledgement, only a mutating call that never left may be
    repeated. *classification* decides whether
    :meth:`DaemonProxyBackend.recover` writes the owner off; by default a
    connection never established is ``UNREACHABLE``.
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

    Walked along ``__cause__`` because what reaches a middleware is a wrapper:
    with ``mask_error_details`` FastMCP raises ``ToolError`` from whatever the
    tool raised. Never along ``__context__``, which Python sets on any
    exception raised while another is handled.
    """
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, OwnerUnreachableError):
            return current
        current = current.__cause__
    return None


def _no_connection_was_established(exc: BaseException) -> bool:
    """Whether the cause chain proves no connection was opened.

    ``ConnectError`` and ``ConnectTimeout`` are the two that mean the
    connection itself never existed. Only a classification: whether a tool
    request was sent is :class:`DispatchRecord`'s question.
    """
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, (httpx2.ConnectError, httpx2.ConnectTimeout)):
            return True
        current = current.__cause__
    return False


_ORDER = {Dispatch.UNSENT: 0, Dispatch.POSSIBLY_SENT: 1, Dispatch.ANSWERED: 2}


@dataclass
class DispatchRecord:
    """How far one attempt's tool request got, as far as this process can prove.

    Only moves forward, so a later observation can never turn a request that
    may have left into one that did not. ``UNSENT`` until the request enters
    the session, ``POSSIBLY_SENT`` from then on (entering is a superset of
    writing: a partial write is still ambiguous), ``ANSWERED`` once the owner's
    JSON-RPC response to it was read in full.
    """

    state: Dispatch = Dispatch.UNSENT

    def advance(self, to: Dispatch) -> None:
        if _ORDER[to] > _ORDER[self.state]:
            self.state = to

    @property
    def nothing_was_sent(self) -> bool:
        return self.state is Dispatch.UNSENT


@dataclass(frozen=True)
class CallBinding:
    """One call, and the owner it belongs to for as long as it runs.

    Read together: if the heartbeats and the request named different owners,
    the owner running the call would hear no beat and cancel it. Until
    *dispatch* leaves ``UNSENT`` the owner is checked before every request and
    a written-off owner gets nothing; after that the call keeps its owner.
    """

    call_id: str
    attachment: Attachment
    dispatch: DispatchRecord = field(default_factory=DispatchRecord)


#: The call the current task is making. Set by the heartbeat middleware around
#: the tool lookup and the call, which FastMCP runs inside it.
_call_being_made: contextvars.ContextVar[CallBinding | None] = contextvars.ContextVar(
    "linkedin_mcp_call", default=None
)


@contextmanager
def bound_to(binding: CallBinding) -> Iterator[None]:
    """Address every request this task makes to *binding*'s owner and call."""
    token = _call_being_made.set(binding)
    try:
        yield
    finally:
        _call_being_made.reset(token)


def owner_transport() -> httpx2.AsyncBaseTransport:
    """The HTTP transport a hop's bytes travel on: the loopback socket."""
    return httpx2.AsyncHTTPTransport()


class _Wire:
    """What one hop's HTTP exchanges showed, read off the bytes.

    Request ids are recorded as each POST leaves, and a response is attributed
    to a request only once it has been read whole: a JSON body at its end, an
    event-stream message at its terminating blank line.
    """

    def __init__(self, dispatch: DispatchRecord) -> None:
        self.dispatch = dispatch
        self.methods: dict[str, str] = {}
        #: Every ``(code, message)`` the owner itself answered a request with.
        self.errors: set[tuple[int, str]] = set()
        self.connect_failed = False
        self.responded = False

    def posted(self, body: bytes) -> str | None:
        """Record the request in a POST body; its id key if it is a request."""
        try:
            message = json.loads(body)
        except ValueError:
            return None
        if not isinstance(message, dict) or "id" not in message:
            return None
        method = message.get("method")
        if not isinstance(method, str):
            return None
        key = json.dumps(message["id"])
        self.methods[key] = method
        return key

    def heard(self, payload: bytes, posted: str | None) -> bool:
        """Attribute one complete JSON-RPC message, or batch, to its request.

        Whether one of them answers *posted*, the request of the POST it came
        back on.
        """
        try:
            message = json.loads(payload)
        except ValueError:
            return False
        answers_posted = False
        for item in message if isinstance(message, list) else [message]:
            if not isinstance(item, dict) or ("result" in item) == ("error" in item):
                continue
            ident = item.get("id")
            # A server may refuse a request before reading its id and answer
            # with a null one; the SDK then credits the POST that carried it.
            key = posted if ident is None and posted is not None else json.dumps(ident)
            method = self.methods.get(key)
            if method is None:
                continue
            answers_posted = answers_posted or (posted is not None and key == posted)
            error = item.get("error")
            if isinstance(error, dict):
                code, text = error.get("code"), error.get("message")
                if isinstance(code, int) and isinstance(text, str):
                    self.errors.add((code, text))
            if method == "tools/call":
                self.dispatch.advance(Dispatch.ANSWERED)
        return answers_posted

    def answered_with(self, error: MCPError) -> bool:
        """Whether *error* is one the owner sent, rather than one the SDK wrote."""
        return (error.code, error.message) in self.errors

    @property
    def classification(self) -> OwnerFailure:
        if self.connect_failed and not self.responded:
            return OwnerFailure.UNREACHABLE
        return OwnerFailure.OWNER_ERROR


_LINE_BREAK = re.compile(rb"\r\n|\r|\n")


class _Reader:
    """Find whole JSON-RPC messages in one response body as it streams past."""

    def __init__(self, wire: _Wire, posted: str | None, content_type: str) -> None:
        self.wire = wire
        self.posted = posted
        self.events = content_type.startswith("text/event-stream")
        self.whole = content_type.startswith("application/json")
        self.pending = b""
        self.after_cr = False
        self.data: list[bytes] = []
        #: A JSON body read to its end that answers the POST it came back on.
        #: Never set for an event stream, which the SDK reads before its end.
        self.answered = False

    def feed(self, chunk: bytes) -> None:
        if self.whole:
            self.pending += chunk
            return
        if not self.events or not chunk:
            return
        # A CRLF split across two chunks is one line break, not two.
        if self.after_cr and chunk.startswith(b"\n"):
            chunk = chunk[1:]
        self.after_cr = chunk.endswith(b"\r")
        *lines, self.pending = _LINE_BREAK.split(self.pending + chunk)
        for line in lines:
            if not line:
                if self.data:
                    self.wire.heard(b"\n".join(self.data), self.posted)
                    self.data.clear()
            elif line.startswith(b"data:"):
                self.data.append(line[5:].removeprefix(b" "))

    def end(self) -> None:
        # An event without its blank line is incomplete and is discarded, as
        # the SDK's own event-stream parser discards it.
        if self.whole:
            self.answered = self.wire.heard(self.pending, self.posted)


class _ObservedBody(httpx2.AsyncByteStream):
    def __init__(self, inner: Any, reader: _Reader) -> None:
        self.inner = inner
        self.reader = reader

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.inner:
            self.reader.feed(chunk)
            yield chunk
        self.reader.end()

    async def aclose(self) -> None:
        """Close the body, keeping an answer that already arrived whole.

        httpx2 closes a body before ``aread()`` returns it, and the SDK parses
        it only after that, so a failing close would turn an answer the owner
        sent in full into a lost one. Only then is the failure swallowed:
        read to its end, JSON, and answering this POST. The SDK still parses
        and validates it as usual. Cancellation is never swallowed.
        """
        try:
            await self.inner.aclose()
        except Exception:
            if not self.reader.answered:
                raise
            logger.debug("Closing a complete owner answer failed", exc_info=True)


class _WatchedTransport(httpx2.AsyncBaseTransport):
    """Pass every exchange of one hop through, noting what the wire showed."""

    def __init__(self, inner: httpx2.AsyncBaseTransport, wire: _Wire) -> None:
        self.inner = inner
        self.wire = wire

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        posted = (
            self.wire.posted(await request.aread())
            if request.method == "POST"
            else None
        )
        try:
            response = await self.inner.handle_async_request(request)
        except (httpx2.ConnectError, httpx2.ConnectTimeout):
            self.wire.connect_failed = True
            raise
        self.wire.responded = True
        reader = _Reader(
            self.wire, posted, response.headers.get("content-type", "").lower()
        )
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=_ObservedBody(response.stream, reader),
            request=request,
            extensions=response.extensions,
        )

    async def __aenter__(self) -> _WatchedTransport:
        # The client enters its transport, and the inner one may need that:
        # a pooled transport opens its pool there.
        await self.inner.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        await self.inner.__aexit__(exc_type, exc_value, traceback)

    async def aclose(self) -> None:
        await self.inner.aclose()


class _HopSession(ClientSession):
    """The owner session, which notes the tool request as it enters the SDK.

    ``send_request`` is where every request after the negotiation enters the
    SDK, the tool request included, with no await in front of it. *hop* is
    attached once the session exists and is this class's own attribute.
    """

    hop: _Hop | None = None

    async def send_request(
        self,
        request: mt.ClientRequest | mt.Request[Any, Any],
        result_type: type[_ResultT] | TypeAdapter[_ResultT],
        request_read_timeout_seconds: float | None = None,
        metadata: ClientMessageMetadata | None = None,
        progress_callback: ProgressFnT | None = None,
    ) -> _ResultT:
        hop = self.hop
        calling = isinstance(request, mt.CallToolRequest)
        if hop is not None:
            # The last point at which this process can still decline to send.
            # A check made before any earlier await says nothing about now:
            # another call may have written the owner off meanwhile.
            hop.refuse_if_written_off()
            if calling:
                hop.wire.dispatch.advance(Dispatch.POSSIBLY_SENT)
        result = await super().send_request(
            request,
            result_type,
            request_read_timeout_seconds,
            metadata,
            progress_callback,
        )
        if hop is not None and calling:
            hop.wire.dispatch.advance(Dispatch.ANSWERED)
        return result

    async def validate_tool_result(self, name: str, result: mt.CallToolResult) -> None:
        """Accept the owner's answer as it arrived.

        The SDK's check would first list tools on this fresh session, and a
        failure there would replace an answer to a call that may already have
        changed LinkedIn. The owner validates its output schemas, and the
        frontend's server checks them again.
        """
        return None


@dataclass
class _Hop:
    """One operation's connection to one owner."""

    attachment: Attachment
    wire: _Wire
    backend: DaemonProxyBackend

    def refuse_if_written_off(self) -> None:
        self.backend.refuse_if_written_off(
            self.attachment, sent=not self.wire.dispatch.nothing_was_sent
        )

    def fail(self, error: Exception) -> NoReturn:
        """Raise what *error* means to the caller: the owner's answer, or a loss.

        An ``MCPError`` the transport saw the owner send is its answer and
        leaves as it is. Everything else is a failure to reach the owner,
        tagged with whether the tool request can have left.
        """
        if isinstance(error, OwnerUnreachableError):
            raise error
        if isinstance(error, MCPError) and self.wire.answered_with(error):
            raise error
        raise OwnerUnreachableError(
            instance_id=self.attachment.descriptor.instance_id,
            nothing_was_sent=self.wire.dispatch.nothing_was_sent,
            cause=error,
            classification=self.wire.classification,
        ) from error


class _HopVersionRefused(RuntimeError):
    """The owner does not serve the pinned protocol version."""


#: What an owner answers `server/discover` with when it does not serve the
#: proposed version: the method unknown to it, or the version.
_NOT_SERVED = frozenset({mt.METHOD_NOT_FOUND, mt.UNSUPPORTED_PROTOCOL_VERSION})


async def pin_hop_version(session: ClientSession) -> None:
    """Settle on :data:`HOP_PROTOCOL_VERSION`, and nothing else, with the owner.

    Proposed with ``server/discover``, which sends nothing a tool could act
    on, and adopted only as that one version even when the owner lists newer
    ones beside it. Every hop and both endpoint probes
    (``daemon.owner.prove_hop``) negotiate here, so an owner is attached
    to only if it serves what every forwarded call will propose; the SDK's
    default would fall back to an older era that no call can use.

    Raises :class:`_HopVersionRefused` for a result without the version, and
    whatever the SDK raised for a refused or failed proposal.
    """
    raw = await session.send_discover(HOP_PROTOCOL_VERSION)
    result = mt.DiscoverResult.model_validate(raw)
    if HOP_PROTOCOL_VERSION not in result.supported_versions:
        raise _HopVersionRefused(
            f"The shared browser owner serves protocols "
            f"{result.supported_versions!r}, not {HOP_PROTOCOL_VERSION!r}"
        )
    session.adopt(
        result.model_copy(update={"supported_versions": [HOP_PROTOCOL_VERSION]})
    )


async def _pin(session: _HopSession, hop: _Hop) -> None:
    """:func:`pin_hop_version` on one hop, judged against what its wire showed."""
    # `send_discover` does not pass through `send_request`, so the check that
    # every other request gets there is made here.
    hop.refuse_if_written_off()
    try:
        await pin_hop_version(session)
    except MCPError as refused:
        if refused.code in _NOT_SERVED and hop.wire.answered_with(refused):
            raise _HopVersionRefused(
                f"The shared browser owner does not serve protocol "
                f"{HOP_PROTOCOL_VERSION!r}: {refused}"
            ) from refused
        raise


@dataclass
class _Decided:
    """An operation's outcome, kept while its hop closes."""

    value: Any = None
    error: Exception | None = None


class ForwardedTool(Tool):
    """One of the owner's tools, as this frontend serves it."""

    task_config: TaskConfig = TaskConfig(mode="forbidden")
    forwarder: Annotated[SkipJsonSchema[Any], Field(exclude=True)] = None

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        return await self.forwarder.call(self.name, arguments)


class OwnerForwarder(Provider):
    """Serve the owner's tools as if they were this server's own.

    *timeout* bounds each request on the hop, at the MCP layer and as the HTTP
    read timeout. It has to outlast the owner's own tool timeout, so a tool
    that times out is reported by the owner rather than cut off here.

    Resources and prompts keep the provider's empty defaults: the owner
    registers none, so no listing or lookup of either reaches it.
    """

    def __init__(self, backend: DaemonProxyBackend, *, timeout: float) -> None:
        super().__init__()
        self.backend = backend
        self.timeout = timeout

    async def _list_tools(self) -> Sequence[Tool]:
        async def every_page(session: ClientSession) -> list[mt.Tool]:
            tools: list[mt.Tool] = []
            cursor: str | None = None
            while True:
                page = await session.list_tools(
                    params=mt.PaginatedRequestParams(cursor=cursor)
                )
                tools.extend(page.tools)
                if page.next_cursor is None:
                    return tools
                cursor = page.next_cursor

        try:
            return [self._served(tool) for tool in await self._exchange(every_page)]
        except MCPError as answered:
            # The owner's own answer that it lists no tools, never the SDK's.
            if answered.code == mt.METHOD_NOT_FOUND:
                return []
            raise

    def _served(self, tool: mt.Tool) -> ForwardedTool:
        return ForwardedTool(
            name=tool.name,
            title=tool.title,
            description=tool.description,
            parameters=tool.input_schema,
            annotations=tool.annotations,
            output_schema=tool.output_schema,
            icons=tool.icons,
            meta=tool.meta,
            tags=get_fastmcp_metadata(tool.meta).get("tags", []),
            execution=tool.execution,
            forwarder=self,
        )

    async def get_tasks(self) -> Sequence[FastMCPComponent]:
        """None: a forwarded tool runs in the owner, never as a local task.

        The default lists every component while the server starts, which
        would reach for an owner before any call was bound.
        """
        return []

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Run *name* in the owner and hand back its result as it came."""
        context = _frontend_context()
        progress = context.report_progress if context is not None else None
        logged = _relay_logs(context) if context is not None else None
        result = await self._exchange(
            lambda session: session.call_tool(
                name,
                arguments,
                progress_callback=progress,
                meta=_forwardable_meta(context),
            ),
            logging_callback=logged,
        )
        # Rebuilt rather than kept raw (`ToolResult.from_mcp_result`): middleware
        # on this server edits the content, and a raw result would be served
        # as it arrived, dropping the edit.
        return ToolResult(
            content=result.content,
            structured_content=result.structured_content,
            meta=result.meta,
            is_error=result.is_error,
        )

    async def _exchange(
        self,
        operation: Callable[[ClientSession], Awaitable[Any]],
        *,
        logging_callback: LoggingFnT | None = None,
    ) -> Any:
        """Run *operation* on a fresh session with the owner, and judge the end.

        Addressed to the bound call's owner, or else to whichever owner a new
        call would get. Once the operation has an outcome, a failure while the
        hop closes is logged and dropped; only a cancellation still leaves. A
        failure before any outcome goes through :meth:`_Hop.fail`, since a tool
        request that entered the session may have left. A caller giving up
        reaches the owner through the SDK aborting the request's POST.
        """
        binding = _call_being_made.get()
        if binding is None:
            attachment = self.backend.attachment_for_a_call()
            dispatch = DispatchRecord()
            headers: dict[str, str] = {}
        else:
            attachment = binding.attachment
            dispatch = binding.dispatch
            headers = {CALL_HEADER: binding.call_id}
        hop = _Hop(attachment=attachment, wire=_Wire(dispatch), backend=self.backend)
        hop.refuse_if_written_off()
        # Read together, never separately: the URL and the token are one
        # credential pair for one owner.
        headers["Authorization"] = f"Bearer {attachment.token}"
        return await self._hop(hop, headers, operation, logging_callback)

    async def _hop(
        self,
        hop: _Hop,
        headers: dict[str, str],
        operation: Callable[[ClientSession], Awaitable[Any]],
        logging_callback: LoggingFnT | None,
    ) -> Any:
        attachment = hop.attachment
        decided: _Decided | None = None
        try:
            # The owner's own factory, which forces `trust_env=False`: httpx2
            # honours HTTP_PROXY even for loopback, and this hop carries the
            # bearer token.
            async with (
                daemon_owner.direct_async_http_client(
                    headers=headers,
                    timeout=httpx2.Timeout(30.0, read=self.timeout),
                    transport=_WatchedTransport(owner_transport(), hop.wire),
                ) as http,
                streamable_http_client(attachment.descriptor.url, http_client=http) as (
                    read,
                    write,
                ),
                _HopSession(
                    read,
                    write,
                    read_timeout_seconds=self.timeout,
                    logging_callback=logging_callback,
                    client_info=_CLIENT_INFO,
                    # This era sends a request's log messages only when the
                    # request asks for them. A call whose caller named a level
                    # forwards that one; any other asks for every level.
                    log_level="debug" if logging_callback is not None else None,
                ) as session,
            ):
                session.hop = hop
                try:
                    await _pin(session, hop)
                    decided = _Decided(value=await operation(session))
                except Exception as failed:
                    decided = _Decided(error=failed)
        except Exception as closing:
            if decided is None:
                hop.fail(closing)
            logger.debug(
                "The shared browser owner's session failed while closing, after "
                "the operation had already %s",
                "failed" if decided.error is not None else "answered",
                exc_info=closing,
            )
        assert decided is not None
        if decided.error is not None:
            hop.fail(decided.error)
        return decided.value


def _frontend_context() -> Context | None:
    """The frontend request this forward serves, if it serves one."""
    try:
        return get_context()
    except RuntimeError:
        return None


def _forwardable_meta(context: Context | None) -> mt.RequestParamsMeta | None:
    """The caller's request ``_meta``, without what describes its connection.

    The progress token is replaced by the SDK with one for the hop.
    """
    request = context.request_context if context is not None else None
    if request is None or not request.meta:
        return None
    forwarded = {
        key: value
        for key, value in request.meta.items()
        if key not in _CONNECTION_META_KEYS
    }
    return cast("mt.RequestParamsMeta", forwarded) if forwarded else None


def _relay_logs(context: Context) -> LoggingFnT:
    """Pass the owner's log messages for a call on to the frontend's client."""

    async def relay(params: mt.LoggingMessageNotificationParams) -> None:
        data = params.data
        if isinstance(data, dict):
            message, extra = data.get("msg"), data.get("extra")
        else:
            message, extra = data, None
        await context.log(
            message if isinstance(message, str) else json.dumps(message),
            level=params.level,
            logger_name=params.logger,
            extra=extra,
        )

    return relay
