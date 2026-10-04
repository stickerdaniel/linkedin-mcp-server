"""How a frontend reaches the owner it was told to forward to.

Every test here pins something that is invisible in a passing round trip and
expensive when it is wrong: a bearer token taking a detour through the user's
proxy, a long call that hangs instead of failing, progress that silently stops
arriving, or a dead owner that looks like a server with no tools.
"""

from __future__ import annotations

import asyncio
import datetime
import inspect
import json
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, NoReturn
from unittest.mock import AsyncMock, MagicMock

import httpx2
import mcp.types as mt
import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.client.transports import (
    ClientTransport,
    FastMCPTransport,
    StreamableHttpTransport,
)
from fastmcp.server.middleware import Middleware
from fastmcp.server.providers.proxy import ProxyClient, ProxyProvider
from fastmcp.tools import ToolResult

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon import Attachment
from linkedin_mcp_server.daemon_descriptor import build, new_instance_id, new_token
from linkedin_mcp_server import daemon_descriptor
from linkedin_mcp_server.daemon_proxy import (
    DaemonProxyBackend,
    create_proxy_provider,
)


def _backend(attachment: Attachment, tmp_path: Path) -> DaemonProxyBackend:
    """The state object the proxy layer is built from.

    The election's inputs travel with the answer, so a later change can find a
    replacement without substituting defaults from a configuration singleton.
    """
    profile = tmp_path / "profile"
    return DaemonProxyBackend(
        attachment=attachment,
        auth_root=profile.parent,
        profile=profile,
        config=AppConfig(),
    )


def _attachment(
    tmp_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 51234,
    path: str = "/mcp",
) -> Attachment:
    profile = tmp_path / "profile"
    profile.mkdir(exist_ok=True)
    config = AppConfig()
    config.browser.user_data_dir = str(profile)
    token = new_token()
    descriptor = build(
        instance_id=new_instance_id(),
        package_version="4.20.1",
        runtime_id="test-runtime",
        profile=profile,
        host=host,
        port=port,
        path=path,
        token=token,
        config=config,
        log_path=tmp_path / "owner.log",
    )
    return Attachment(descriptor=descriptor, token=token)


def _elected(attachment: Attachment):
    """What `obtain_owner` returns when it found *attachment*."""
    from linkedin_mcp_server.daemon import OwnerLookup, OwnerState
    from linkedin_mcp_server.daemon_election import ElectionOutcome

    return ElectionOutcome(
        OwnerLookup(state=OwnerState.ATTACHABLE, attachment=attachment),
        started_owner=True,
    )


class _NothingIsListening(ClientTransport):
    """An address with nothing behind it, the way a departed owner leaves one."""

    def __init__(self, url: str) -> None:
        self.url = url

    @asynccontextmanager
    async def connect_session(self, **_kwargs: Any) -> AsyncIterator[Any]:
        raise httpx2.ConnectError(f"nothing is listening on {self.url}")
        yield  # noqa: W0101 - unreachable, and an async generator needs one


class _OnTheRealSession(FastMCPTransport):
    """An in-process owner whose client session is changed in place, never wrapped.

    The client has to get the session it asked for: the claim on a tool request
    lives in that session's class (`ClaimsTheToolRequest`), and a client handed
    anything else refuses it at entry. A wrapper would also miss what it claims
    to watch, because the SDK's `call_tool` reaches `send_request` through its
    own `self`. So a fault is set as an attribute of the real session, where
    the SDK's calls through `self` and fastmcp's listings both find it — the
    way fastmcp itself replaces `send_discover` while it negotiates.

    Set before the negotiation, which uses neither method changed here.
    """

    @asynccontextmanager
    async def connect_session(self, **kwargs: Any) -> AsyncIterator[Any]:
        async with super().connect_session(**kwargs) as session:
            self.adjust(session)
            yield session

    def adjust(self, session: Any) -> None:
        """Change *session* in place, before anything is asked of it."""


class _GoesAwayAfterInitialize(_OnTheRealSession):
    """An owner that answers the negotiation and is gone before the next request.

    The window that neither the connect nor the tool call can see, and the only
    reason the listing boundaries are tagged at all.

    Deliberately not the exception a real owner produces, and reality has three
    shapes rather than one. Measured with the client the provider builds: an
    owner already gone at connect time fails in `__aenter__` as `RuntimeError`
    over `httpx2.ConnectError`; one that goes away in this window, after the
    negotiation and before the request on that same session, raises
    `anyio.BrokenResourceError` or `ClosedResourceError` depending on timing; one
    that goes away with a request outstanding comes back as an `MCPError` the
    session invented. The first and third have their own tests.

    This double stands in for the middle one, and raises something outside all
    three on purpose, so what it pins is the boundary itself: the listing answers
    "nothing was sent" from *where* the failure happened. A version that read the
    cause chain instead would pass against the real shapes and fail here.
    """

    def adjust(self, session: Any) -> None:
        async def list_tools(*_args: Any, **_kwargs: Any) -> NoReturn:
            raise httpx2.RemoteProtocolError("the owner closed the connection")

        session.list_tools = list_tools


class _AnswersWhenTold(_OnTheRealSession):
    """An owner whose listing is held open for as long as a test needs it."""

    def __init__(self, server: FastMCP) -> None:
        super().__init__(server)
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    def adjust(self, session: Any) -> None:
        list_tools = session.list_tools

        async def held(*args: Any, **kwargs: Any) -> Any:
            self.reached.set()
            await self.release.wait()
            return await list_tools(*args, **kwargs)

        session.list_tools = held


def _reach_owners_in_process(
    monkeypatch: pytest.MonkeyPatch, owner_at, *, legacy_only: bool = False
) -> None:
    """Reach in-process owners, but only at the address production chose.

    Only the socket is stood in for. The URL is still built by production code
    from whatever attachment the backend currently holds, and the client wrapped
    around it is the one `open_client` built, so a failure is classified by the
    production client rather than raised in the shape a test wanted.

    *owner_at* answers with a `FastMCP` to reach, a transport to use as it is, or
    `None` for an address nobody is serving.

    The heartbeat preflight goes to the same place. An in-process owner has no
    HTTP routes, so it answers the preflight the way the owner's own route
    answers a call it has not registered yet; an address nobody serves refuses
    the connection, which is what a departed owner's port does.

    *legacy_only* keeps every owner reached here to the handshake era, the way
    an owner that cannot serve the 2026-07-28 one does; left false, an
    in-process owner negotiates that era.
    """
    from fastmcp.client import transports

    from linkedin_mcp_server.daemon_proxy import FrontendCallHeartbeatMiddleware

    def transport_for(url: str, **_ignored: Any) -> ClientTransport:
        reached = owner_at(url)
        if reached is None:
            return _NothingIsListening(url)
        transport = (
            reached
            if isinstance(reached, ClientTransport)
            else FastMCPTransport(reached)
        )
        if legacy_only:
            transport.legacy_only = True
        return transport

    async def beat(attachment: Attachment, _call_id: str) -> httpx2.Response:
        if owner_at(attachment.descriptor.url) is None:
            raise httpx2.ConnectError(
                f"nothing is listening on {attachment.descriptor.url}"
            )
        return httpx2.Response(200, json={"watched": False})

    monkeypatch.setattr(transports, "StreamableHttpTransport", transport_for)
    monkeypatch.setattr(FrontendCallHeartbeatMiddleware, "_beat", staticmethod(beat))


class TestReachingTheOwner:
    """The address and the credential, both used exactly as published."""

    def test_the_published_url_is_used_verbatim(self, tmp_path: Path):
        # Rebuilding it from host and port loses the MCP path, and FastMCP does
        # not add one back: it deliberately serves whatever path it is given.
        attachment = _attachment(tmp_path)
        client = _backend(attachment, tmp_path).open_client(timeout=1.0)

        assert isinstance(client.transport, StreamableHttpTransport)
        assert client.transport.url == attachment.descriptor.url
        assert client.transport.url.endswith("/mcp")

    def test_an_ipv6_owner_keeps_its_brackets(self, tmp_path: Path):
        # Unbracketed, the colons in the address run into the one before the
        # port and the whole URL parses as a bad port.
        attachment = _attachment(tmp_path, host="::1")
        client = _backend(attachment, tmp_path).open_client(timeout=1.0)

        assert isinstance(client.transport, StreamableHttpTransport)
        assert "[::1]" in client.transport.url

    def test_the_owners_token_is_sent_as_a_bearer(self, tmp_path: Path):
        # The owner compares the token after the `Bearer ` scheme, so a raw
        # header value would be rejected by the endpoint it was minted for.
        attachment = _attachment(tmp_path)
        client = _backend(attachment, tmp_path).open_client(timeout=1.0)

        request = httpx2.Request("POST", attachment.descriptor.url)
        assert client.transport.auth is not None
        signed = next(client.transport.auth.auth_flow(request))

        assert signed.headers["Authorization"] == f"Bearer {attachment.token}"

    def test_a_fresh_client_is_built_for_every_operation(self, tmp_path: Path):
        # The provider opens and closes a client around each upstream call, so a
        # single shared session would be reused after its context had exited —
        # and would outlive the owner it was opened against.
        factory = partial(
            _backend(_attachment(tmp_path), tmp_path).open_client, timeout=1.0
        )

        assert factory() is not factory()

    def test_it_forwards_with_a_proxy_client(self, tmp_path: Path):
        # Not a plain Client. That one installs no progress handler, so every
        # progress update the tools report would be dropped on the way through.
        client = _backend(_attachment(tmp_path), tmp_path).open_client(timeout=1.0)

        assert isinstance(client, ProxyClient)


#: Both eras the owner hop can negotiate with an in-process owner, with the
#: protocol each must actually land on. The handshake era is reached the way a
#: client reaches it with an owner that cannot serve the other one: through a
#: transport that says so, which `mode="auto"` honours without probing.
_HOP_ERAS = pytest.mark.parametrize(
    ("legacy_only", "protocol"),
    [(False, "2026-07-28"), (True, "2025-11-25")],
    ids=["2026-07-28 era", "handshake era"],
)


def _sessions_opened(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every owner session built from here on, kept after it closes.

    Read off the constructor of the session class the client installs, so a
    client that installed some other class shows up as no sessions at all.
    """
    from linkedin_mcp_server.daemon_proxy import ClaimsTheToolRequest

    opened: list[Any] = []
    construct = ClaimsTheToolRequest.__init__

    def recording(self: Any, *args: Any, **kwargs: Any) -> None:
        construct(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(ClaimsTheToolRequest, "__init__", recording)
    return opened


class TestCallingTheOwner:
    @_HOP_ERAS
    @pytest.mark.parametrize(
        ("timeout", "asked", "waited"),
        [
            (2.5, 2.5, 2.5),
            (datetime.timedelta(seconds=2.5), 2.5, 2.5),
            (3, 3.0, 3.0),
            (None, None, 72.0),
        ],
        ids=["float", "timedelta", "int", "the forwarding deadline"],
    )
    async def test_meta_timeout_and_progress_reach_the_sdk(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        legacy_only: bool,
        protocol: str,
        timeout: Any,
        asked: float | None,
        waited: float,
    ):
        """What `call_tool_mcp` hands the SDK, read where the SDK takes it.

        The deadline in seconds as a float, because the dispatcher adds it to
        a float clock; none at all when the caller named none, so the request
        waits on the session's own, which is the forwarding deadline this
        backend's provider sets (`tool_timeout` 42 plus the margin). The
        caller's `_meta` with the injected trace context, both by value. And
        the progress handler, proved by progress the owner actually reported
        arriving at it.
        """
        owner = FastMCP("owner")

        @owner.tool
        async def report(ctx: Context) -> str:
            await ctx.report_progress(progress=7, total=10, message="forwarded")
            return "sent"

        transport = _RecordsCallRequest(owner)
        transport.legacy_only = legacy_only
        _reach_owners_in_process(monkeypatch, lambda _url: transport)
        provider = create_proxy_provider(
            _backend(_attachment(tmp_path), tmp_path), tool_timeout=42.0
        )
        client = provider.client_factory()
        assert isinstance(client, ProxyClient)
        injected: list[dict[str, Any] | None] = []

        def inject(meta: dict[str, Any] | None) -> dict[str, Any]:
            injected.append(meta)
            return {**(meta or {}), "traceparent": "00-trace-span-01"}

        monkeypatch.setattr(
            "linkedin_mcp_server.daemon_proxy.inject_trace_context", inject
        )
        seen_progress: list[tuple[float, float | None, str | None]] = []

        async def record(
            progress: float, total: float | None, message: str | None
        ) -> None:
            seen_progress.append((progress, total, message))

        async with client:
            assert client.protocol_version == protocol
            result = await client.call_tool_mcp(
                "report",
                {},
                timeout=timeout,
                progress_handler=record,
                meta={"marker": "carried"},
            )

        assert result.is_error is False
        assert injected == [{"marker": "carried"}]
        assert type(transport.request) is mt.CallToolRequest
        # As the request entered the SDK, before it stamps anything of its own.
        sent = transport.request.model_dump(by_alias=True, exclude_none=True)
        assert sent["params"]["_meta"] == {
            "marker": "carried",
            "traceparent": "00-trace-span-01",
        }
        assert transport.timeout == asked
        assert type(transport.timeout) is type(asked)
        assert transport.deadline == waited
        assert transport.progress_callback is record
        assert seen_progress == [(7.0, 10.0, "forwarded")]

    async def test_the_clients_progress_handler_reaches_send_request(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        owner = FastMCP("owner")

        @owner.tool
        async def report(ctx: Context) -> str:
            await ctx.report_progress(progress=7, total=10, message="forwarded")
            return "sent"

        transport = _RecordsCallRequest(owner)
        _reach_owners_in_process(monkeypatch, lambda _url: transport)
        client = _backend(_attachment(tmp_path), tmp_path).open_client(timeout=1.0)
        seen: list[tuple[float, float | None, str | None]] = []

        async def record(
            progress: float, total: float | None, message: str | None
        ) -> None:
            seen.append((progress, total, message))

        async with client:
            client._progress_handler = record
            result = await client.call_tool_mcp("report", {})

        assert result.is_error is False
        assert transport.progress_callback is record
        assert seen == [(7.0, 10.0, "forwarded")]

    async def test_an_answer_in_hand_outlives_the_session_that_brought_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Nothing that can fail runs between the owner's answer and the caller.

        The session here is gone from the moment the answer arrived: every
        monitored request after it fails the way the installed monitor fails
        one on an ended session. FastMCP's own `call_tool_mcp` makes a second
        such request once the answer is in, to drive a multi-round call this
        owner never makes, and that failure would stand in for an answer to a
        call that has already acted.
        """
        from mcp import MCPError

        ran: list[str] = []
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            ran.append("sent")
            return "sent"

        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        client = _backend(_attachment(tmp_path), tmp_path).open_client(timeout=5.0)
        monitor = ProxyClient._await_with_session_monitoring
        answered: list[object] = []

        async def ended_once_answered(self: Any, coro: Any) -> Any:
            if answered:
                coro.close()
                raise MCPError(code=mt.CONNECTION_CLOSED, message="Connection closed")
            answer = await monitor(self, coro)
            answered.append(answer)
            return answer

        monkeypatch.setattr(
            ProxyClient, "_await_with_session_monitoring", ended_once_answered
        )

        async with client:
            result = await client.call_tool_mcp("send_connection_request", {})

        assert result.is_error is False
        assert ran == ["sent"]

    def test_the_sdk_boundary_keeps_the_expected_signature(self, tmp_path: Path):
        from fastmcp.client.mixins.tools import ClientToolsMixin
        from mcp import ClientSession

        from linkedin_mcp_server.daemon_proxy import ClaimsTheToolRequest

        client = _backend(_attachment(tmp_path), tmp_path).open_client(timeout=1.0)
        boundary = inspect.signature(type(client).call_tool_mcp)
        upstream = inspect.signature(ClientToolsMixin.call_tool_mcp)

        assert (
            list(boundary.parameters)
            == list(upstream.parameters)
            == [
                "self",
                "name",
                "arguments",
                "progress_handler",
                "timeout",
                "meta",
            ]
        )
        for name, parameter in boundary.parameters.items():
            assert parameter.kind is upstream.parameters[name].kind
            assert parameter.default == upstream.parameters[name].default

        # The claim overrides the one method every tool request enters, and
        # passes each argument on by position, so both have to agree with the
        # SDK's own on names, order and defaults.
        claimed = inspect.signature(ClaimsTheToolRequest.send_request)
        send_request = inspect.signature(ClientSession.send_request)
        assert (
            list(claimed.parameters)
            == list(send_request.parameters)
            == [
                "self",
                "request",
                "result_type",
                "request_read_timeout_seconds",
                "metadata",
                "progress_callback",
            ]
        )
        for signature in (claimed, send_request):
            assert all(
                parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                for parameter in signature.parameters.values()
            )
            assert [
                parameter.default for parameter in signature.parameters.values()
            ] == [
                inspect.Parameter.empty,
                inspect.Parameter.empty,
                inspect.Parameter.empty,
                None,
                None,
                None,
            ]


class TestKeepingTheTokenOffTheNetwork:
    """A loopback hop must not become a request to somebody else's proxy."""

    def test_the_environment_proxy_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # httpx2 honours HTTP_PROXY even for 127.0.0.1 unless NO_PROXY happens to
        # say otherwise. The owner reproduced this against a capture proxy: a
        # loopback request arrived there complete with the bearer token. This
        # server also has a *legitimate* proxy setting for LinkedIn's own
        # traffic, which is exactly why the two must not be confused.
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.delenv("NO_PROXY", raising=False)

        client = _backend(_attachment(tmp_path), tmp_path).open_client(timeout=1.0)
        http_client = client.transport.httpx_client_factory(
            headers=None, auth=None, follow_redirects=True
        )

        assert http_client.trust_env is False

    def test_the_factory_survives_the_extra_arguments_fastmcp_passes(
        self, tmp_path: Path
    ):
        # FastMCP's transport passes `follow_redirects` on top of the documented
        # client-factory protocol. A factory accepting only the three declared
        # parameters failed at connect time with an unexpected keyword.
        client = _backend(_attachment(tmp_path), tmp_path).open_client(timeout=1.0)

        http_client = client.transport.httpx_client_factory(
            headers={"x": "y"},
            auth=None,
            follow_redirects=True,
            timeout=httpx2.Timeout(5.0),
        )

        assert http_client.trust_env is False


class TestTheForwardingDeadline:
    """Why the timeout is an argument and not a default."""

    @staticmethod
    def _client_of(provider: ProxyProvider) -> ProxyClient:
        """The client the provider would open, narrowed from its async-capable type."""
        client = provider.client_factory()
        assert isinstance(client, ProxyClient)
        return client

    @classmethod
    def _request_deadline(cls, client: ProxyClient) -> float:
        """The timeout the MCP session actually waits on, in seconds."""
        read_timeout = client._session_kwargs["read_timeout_seconds"]
        assert isinstance(read_timeout, float)
        return read_timeout

    def test_it_outlasts_the_owners_own_tool_timeout(self, tmp_path: Path):
        # Equal would race the owner's error response, turning a diagnosable
        # "tool timed out" into an unexplained transport failure. Shorter would
        # abort calls the owner would have finished.
        provider = create_proxy_provider(
            _backend(_attachment(tmp_path), tmp_path), tool_timeout=42.0
        )

        assert self._request_deadline(self._client_of(provider)) > 42.0

    def test_it_is_set_at_the_mcp_layer_and_not_only_on_the_http_client(
        self, tmp_path: Path
    ):
        # Measured: with the deadline only on the HTTP client, a call that
        # outlives the read timeout never returns at all. What produces an error
        # is the MCP-level timeout, and setting that also raises the HTTP read
        # timeout, so one value covers both layers.
        provider = create_proxy_provider(
            _backend(_attachment(tmp_path), tmp_path), tool_timeout=42.0
        )
        client = self._client_of(provider)

        assert self._request_deadline(client) == 72.0

        # The transport derives the HTTP read timeout from that same value.
        http_client = client.transport.httpx_client_factory(
            headers=None,
            auth=None,
            follow_redirects=True,
            timeout=httpx2.Timeout(30.0, read=self._request_deadline(client)),
        )
        assert http_client.timeout.read == 72.0

    async def test_a_call_that_outlives_the_deadline_fails_rather_than_hangs(
        self, tmp_path: Path
    ):
        # The regression this argument exists for. Without an MCP-level timeout
        # the equivalent call hung indefinitely; `asyncio.wait_for` here is only
        # a guard so a regression fails the suite instead of stalling it.
        owner = FastMCP("owner")

        @owner.tool
        async def slow() -> dict[str, bool]:
            await asyncio.sleep(30)
            return {"ok": True}

        proxy = FastMCP(
            "proxy",
            providers=[ProxyProvider(lambda: ProxyClient(owner, timeout=0.2))],
        )
        proxy.provider_error_strategy = "raise"

        async with Client(proxy) as client:
            with pytest.raises(Exception, match="[Tt]ime"):
                await asyncio.wait_for(client.call_tool("slow", {}), timeout=10)


class TestServingTheOwnersTools:
    """What survives the hop, and what a dead owner looks like."""

    @staticmethod
    def _owner(*, read_only: bool = True, also: str | None = None) -> FastMCP:
        owner = FastMCP("owner")

        @owner.tool(
            title="Get Person Profile",
            annotations={"readOnlyHint": read_only},
            tags={"person"},
        )
        async def get_person_profile(linkedin_username: str) -> dict[str, str]:
            return {"username": linkedin_username}

        if also is not None:

            @owner.tool(name=also)
            async def only_this_owner_has_this() -> str:
                return "here"

        return owner

    async def test_a_lookup_never_serves_a_departed_owners_annotations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """What decides this is the replay rule, not freshness.

        `a_repeat_could_change_something` reads `readOnlyHint` off the tool the
        provider hands back, so a component cache that outlives its owner is what
        would authorise repeating a call the new owner declares as mutating. An
        upgrade is exactly when a tool's annotations can change.
        """
        elected = _attachment(tmp_path)
        replacement = _attachment(tmp_path, port=elected.descriptor.port + 1)
        backend = _backend(elected, tmp_path)
        before = self._owner(read_only=True, also="only_the_old_owner_had_this")
        after = self._owner(read_only=False)

        _reach_owners_in_process(
            monkeypatch,
            lambda url: before if url == elected.descriptor.url else after,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon_election.obtain_owner",
            lambda *_args, **_kwargs: _elected(replacement),
        )

        provider = create_proxy_provider(backend, tool_timeout=1.0)
        # Warms whatever the provider keeps, which is the point: the lookups
        # below are the ones a cache would answer without asking anybody.
        await provider.list_tools()

        assert await backend.recover(elected.descriptor.instance_id) is not None

        served = await provider.get_tool("get_person_profile")
        assert served is not None and served.annotations is not None
        assert served.annotations.read_only_hint is False, (
            "the departed owner's annotation decided a replay against its successor"
        )
        assert await provider.get_tool("only_the_old_owner_had_this") is None

    async def test_a_listing_still_in_flight_cannot_restore_the_old_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Why adoption does not simply clear the caches.

        `ProxyProvider` writes each cache after its listing completes and outside
        any lock, so a listing already in flight against the departing owner
        refills a cache that was cleared while it ran. Nothing available at
        adoption time closes that window, because the write is in code this
        repository does not own. Keeping no cache does close it.
        """
        elected = _attachment(tmp_path)
        replacement = _attachment(tmp_path, port=elected.descriptor.port + 1)
        backend = _backend(elected, tmp_path)
        held = _AnswersWhenTold(self._owner(read_only=True))
        after = self._owner(read_only=False)

        _reach_owners_in_process(
            monkeypatch,
            lambda url: held if url == elected.descriptor.url else after,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon_election.obtain_owner",
            lambda *_args, **_kwargs: _elected(replacement),
        )

        provider = create_proxy_provider(backend, tool_timeout=1.0)
        in_flight = asyncio.create_task(provider.list_tools())
        await asyncio.wait_for(held.reached.wait(), timeout=5)

        assert await backend.recover(elected.descriptor.instance_id) is not None

        # Only now does the old owner's answer arrive, and land in the provider.
        held.release.set()
        await asyncio.wait_for(in_flight, timeout=5)

        served = await provider.get_tool("get_person_profile")
        assert served is not None and served.annotations is not None
        assert served.annotations.read_only_hint is False, (
            "a listing that outlived its owner put that owner's components back"
        )

    async def test_a_replacement_owner_is_found_and_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The failure this whole round exists to remove.

        The test this replaces pinned the *limitation*: it published a
        replacement and asserted the proxy still went to the old address. Its own
        docstring said a re-resolution test would need the resolver to be
        something a test can inject, which is what `DaemonProxyBackend` now is.

        Only the socket is stood in for. A client reaches the owner when the
        address it carries is the one currently published, and finds nothing
        otherwise, so what decides the outcome is the address production code
        chose rather than anything this test set.
        """
        owner = self._owner()
        auth_root = tmp_path / "state"
        elected = _attachment(tmp_path)
        replacement = _attachment(tmp_path, port=elected.descriptor.port + 1)

        # Published state is keyed by auth root but *stored* under the account's
        # own private directory, so a tmp_path auth root alone does not isolate
        # anything. Caught by counting entries before and after a run.
        monkeypatch.setattr(daemon_descriptor, "_account_home", lambda: tmp_path)

        def owner_at(url: str) -> FastMCP | None:
            published = daemon_descriptor.read(auth_root)
            if published is None or url != published.url:
                return None
            return owner

        _reach_owners_in_process(monkeypatch, owner_at)

        # The election finds the replacement, the way a real one does once the
        # departed owner's lock is free.
        def elect(*_args, **_kwargs):
            daemon_descriptor.publish(
                auth_root, replacement.descriptor, replacement.token
            )
            return _elected(replacement)

        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", elect)

        daemon_descriptor.publish(auth_root, elected.descriptor, elected.token)
        backend = _backend(elected, tmp_path)
        provider = create_proxy_provider(backend, tool_timeout=1.0)
        assert {t.name for t in await provider.list_tools()} == {"get_person_profile"}

        # The owner goes away without publishing anything, as a crash does.
        daemon_descriptor.publish(auth_root, replacement.descriptor, replacement.token)

        recovered = await backend.recover(elected.descriptor.instance_id)
        assert recovered is not None
        assert backend.attachment.descriptor.url == replacement.descriptor.url
        # And the token moved with the address rather than being kept.
        assert backend.attachment.token == replacement.token
        assert {t.name for t in await provider.list_tools()} == {"get_person_profile"}

    async def test_a_late_failure_from_the_old_owner_elects_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The test that actually pins the generation check.

        Concurrent failures cannot: released together they all join the one
        flight and produce one election with the check removed. What breaks
        without it is a *late* failure, from a call that opened its client before
        the replacement was adopted and fails afterwards still naming the old
        owner. Without the check that failure elects again, against an owner that
        is already answering.
        """
        elected = _attachment(tmp_path)
        replacement = _attachment(tmp_path, port=elected.descriptor.port + 1)
        backend = _backend(elected, tmp_path)

        elections = 0

        def elect(*_args, **_kwargs):
            nonlocal elections
            elections += 1
            return _elected(replacement)

        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", elect)

        assert await backend.recover(elected.descriptor.instance_id) is not None
        assert elections == 1

        # The same failure arrives again, from a call that was already in flight.
        again = await backend.recover(elected.descriptor.instance_id)

        assert elections == 1, "a late failure elected a second time"
        assert again is not None
        assert again.descriptor.url == replacement.descriptor.url

    async def test_concurrent_failures_share_one_election(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        elected = _attachment(tmp_path)
        replacement = _attachment(tmp_path, port=elected.descriptor.port + 1)
        backend = _backend(elected, tmp_path)

        elections = 0
        holding = threading.Event()

        def elect(*_args, **_kwargs):
            nonlocal elections
            elections += 1
            # Held so every caller is waiting at once rather than arriving after
            # the first has already finished, which would pass without a guard.
            holding.wait(timeout=5)
            return _elected(replacement)

        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", elect)

        failed = elected.descriptor.instance_id
        waiting = [asyncio.create_task(backend.recover(failed)) for _ in range(5)]
        for _ in range(20):
            await asyncio.sleep(0)
        holding.set()
        results = await asyncio.gather(*waiting)

        assert elections == 1
        assert all(
            r is not None and r.descriptor.url == replacement.descriptor.url
            for r in results
        )

    async def test_a_caller_that_gives_up_does_not_free_the_election(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Cancelling a caller must not let the next failure elect again.

        `asyncio.to_thread` outlives the cancellation of whoever awaited it:
        driven directly, an awaiter cancelled at 0.1s and a worker that still
        finished its 1.5s of work. So an unshielded await that gets cancelled
        would clear the guard while an election was still running.
        """
        elected = _attachment(tmp_path)
        replacement = _attachment(tmp_path, port=elected.descriptor.port + 1)
        backend = _backend(elected, tmp_path)

        elections = 0
        holding = threading.Event()

        def elect(*_args, **_kwargs):
            nonlocal elections
            elections += 1
            holding.wait(timeout=5)
            return _elected(replacement)

        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", elect)

        failed = elected.descriptor.instance_id
        gives_up = asyncio.create_task(backend.recover(failed))
        for _ in range(20):
            await asyncio.sleep(0)
        gives_up.cancel()
        with pytest.raises(asyncio.CancelledError):
            await gives_up

        # A second failure arrives while the first election is still running.
        second = asyncio.create_task(backend.recover(failed))
        for _ in range(20):
            await asyncio.sleep(0)
        holding.set()
        assert await second is not None

        assert elections == 1, "a cancelled caller freed the guard"

    async def test_a_dead_owner_is_an_error_not_an_empty_tool_list(self):
        # FastMCP's default is to log a failing provider and carry on. For a
        # server whose only provider this is, that turns a dead owner into a
        # client that sees no tools and no reason why.
        provider = ProxyProvider(lambda: ProxyClient("http://127.0.0.1:9/mcp"))
        proxy = FastMCP("proxy", providers=[provider])
        proxy.provider_error_strategy = "raise"

        async with Client(proxy) as client:
            with pytest.raises(Exception, match="connect"):
                await client.list_tools()

    async def test_progress_from_the_owner_reaches_the_clients_handler(self):
        # Eighteen of the nineteen tools report progress (`close_session` is the
        # exception), and a long read with no progress looks indistinguishable
        # from a hung one. A plain Client in the factory drops these silently.
        owner = FastMCP("owner")

        @owner.tool
        async def read_page(ctx: Context) -> dict[str, bool]:
            await ctx.report_progress(progress=50, total=100, message="halfway")
            return {"ok": True}

        proxy = FastMCP("proxy", providers=[ProxyProvider(lambda: ProxyClient(owner))])
        seen: list[tuple[float, float | None, str | None]] = []

        async def record(progress: float, total: float | None, message: str | None):
            seen.append((progress, total, message))

        async with Client(proxy, progress_handler=record) as client:
            await client.call_tool("read_page", {})

        assert seen == [(50.0, 100.0, "halfway")]

    async def test_a_round_trip_preserves_everything_a_result_carries(self):
        # The envelope is what a later change has to survive on: request `_meta`
        # is how an owner will label an auth failure, and a collapsed result
        # would lose the structured half every tool returns.
        owner = FastMCP("owner")
        received: dict[str, dict[str, object]] = {}

        @owner.tool
        async def report(ctx: Context) -> ToolResult:
            request_context = ctx.request_context
            assert request_context is not None
            received["meta"] = dict(request_context.meta or {})
            return ToolResult(
                content=[mt.TextContent(type="text", text="the text half")],
                structured_content={"the": "structured half"},
                is_error=True,
            )

        proxy = FastMCP("proxy", providers=[ProxyProvider(lambda: ProxyClient(owner))])

        async with Client(proxy) as client:
            result = await client.call_tool(
                "report", {}, raise_on_error=False, meta={"marker": "carried"}
            )

        assert received["meta"]["marker"] == "carried"
        assert result.is_error is True
        assert result.structured_content == {"the": "structured half"}
        assert any("the text half" in getattr(c, "text", "") for c in result.content)

    @_HOP_ERAS
    async def test_meta_and_progress_cross_the_production_proxy(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        legacy_only: bool,
        protocol: str,
    ):
        """The caller's `_meta` and the owner's progress, through the real proxy.

        The frontend server, its provider and the backend's own client, with
        only the socket replaced. On the 2026-07-28 era the provider calls the
        owner session itself and never reaches `call_tool_mcp`, so the tests of
        that method say nothing about this path: here the caller's marker and
        trace context are read at the owner by value, and progress the owner
        reports is read at the caller's handler.
        """
        from linkedin_mcp_server.server import create_mcp_server
        from linkedin_mcp_server.server_role import ServerRole

        received: list[dict[str, Any]] = []
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send(message: str, ctx: Context) -> str:
            request_context = ctx.request_context
            assert request_context is not None
            received.append(dict(request_context.meta or {}))
            await ctx.report_progress(progress=7, total=10, message="forwarded")
            return message

        sessions = _sessions_opened(monkeypatch)
        _reach_owners_in_process(
            monkeypatch, lambda _url: owner, legacy_only=legacy_only
        )
        proxy = create_mcp_server(
            role=ServerRole.PROXY,
            proxy_backend=_backend(_attachment(tmp_path), tmp_path),
            tool_timeout=5.0,
        )
        seen: list[tuple[float, float | None, str | None]] = []

        async def record(
            progress: float, total: float | None, message: str | None
        ) -> None:
            seen.append((progress, total, message))

        trace_id = "0af7651916cd43dd8448eb211c80319c"
        async with Client(proxy, progress_handler=record) as client:
            result = await client.call_tool(
                "send_connection_request",
                {"message": "hi"},
                meta={
                    "marker": "carried",
                    "traceparent": f"00-{trace_id}-b7ad6b7169203331-01",
                },
            )

        assert result.data == "hi"
        called = [s for s in sessions if s.tool_request_started]
        assert [s.protocol_version for s in called] == [protocol]
        (meta,) = received
        assert meta.get("marker") == "carried", meta
        # The same trace, whichever span the proxy stamped as the parent.
        assert meta.get("traceparent", "").split("-")[1:2] == [trace_id], meta
        assert (7.0, 10.0, "forwarded") in seen, seen

    async def test_the_owners_tool_schema_survives_the_hop(self):
        # A client picks tools by title and annotations, so losing them changes
        # which tool an agent chooses even though every call still works.
        owner = self._owner()
        proxy = FastMCP("proxy", providers=[ProxyProvider(lambda: ProxyClient(owner))])

        async with Client(proxy) as client:
            (tool,) = await client.list_tools()

        assert tool.name == "get_person_profile"
        assert tool.title == "Get Person Profile"
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True


@dataclass(frozen=True)
class _Escaped:
    """An exception the middleware let out, in the slot an answer would fill.

    A helper that spells a raise `None` cannot tell one from a middleware that
    returned `None`, and that difference is the whole of what the tests named
    "still raises" claim. With both spelled the same, replacing the final
    `raise` in `on_call_tool` with `return None` left every one of them green.
    """

    error: BaseException


def _fail_the_way_a_real_call_fails(
    *, instance_id: str, nothing_was_sent: bool
) -> NoReturn:
    """Raise an owner-loss failure in the shape that reaches the middleware.

    Every link is a real one, in the order the installed versions produce it,
    rather than the bare tag a helper can raise but nothing here can deliver:

    * `httpx2.ConnectError`, off the socket.
    * `RuntimeError("Client failed to connect: ...")`, raised `from` it in
      `fastmcp/client/client.py:1045-1048` (fastmcp 4.0.10, through
      `_connection_failure` at 235) whenever the session task ended in anything
      but an `MCPError` or an `HTTPStatusError`.
    * `OwnerUnreachableError`, raised `from` that by `_saying_which_owner` in
      `daemon_proxy`, which is where the owner's identity and the dispatch
      answer are attached.
    * `ToolError("Error calling tool ...")`, raised `from` that at
      `fastmcp/server/server.py:1564` under the `mask_error_details=True` this
      server switches on, and the outermost thing a middleware is handed.

    So the tag sits three links down and finding it is a walk, which is why
    `unreachable_owner_in` exists rather than an `isinstance`. A second failure
    raised bare leaves that walk unrun on the way back out.
    """
    from linkedin_mcp_server.daemon_proxy import OwnerUnreachableError

    try:
        try:
            try:
                raise httpx2.ConnectError("gone as well")
            except httpx2.ConnectError as connect:
                raise RuntimeError(f"Client failed to connect: {connect}") from connect
        except RuntimeError as connecting:
            raise OwnerUnreachableError(
                instance_id=instance_id,
                nothing_was_sent=nothing_was_sent,
                cause=connecting,
            ) from connecting
    except OwnerUnreachableError as tag:
        raise ToolError("Error calling tool 'do_the_thing'") from tag


class TestRepeatingOnlyWhatIsSafe:
    """Which calls a recovery may run again, and which it must merely report.

    The decision has two halves and both are load-bearing. The tool's own
    annotation says whether a repeat could change anything on LinkedIn, and the
    failure says whether the request had left this process. A mutating call is
    repeated only when nothing was sent, because nothing in the protocol says
    whether the departed owner had already done the thing.

    Driven at the middleware rather than through a whole proxy stack, so that
    what decides each outcome is the rule under test and not a transport that
    happened to fail in a particular way.
    """

    @staticmethod
    def _context(
        *, read_only: bool | None, then_unreachable: bool = False
    ) -> MagicMock:
        """A call context whose tool declares *read_only*, or declares nothing.

        *then_unreachable* arms a second lookup to fail the way one fails
        against an owner that has just gone: `fastmcp.get_tool` is a forwarded
        listing with the component cache off, so it needs somebody alive to
        answer. It is armed rather than expected, and a test uses it to show
        that the second lookup is never reached.
        """
        tool = MagicMock()
        tool.annotations = (
            None if read_only is None else MagicMock(read_only_hint=read_only)
        )
        context = MagicMock()
        context.message.name = "do_the_thing"
        context.fastmcp_context.fastmcp.get_tool = (
            AsyncMock(side_effect=[tool, RuntimeError("the replacement is gone too")])
            if then_unreachable
            else AsyncMock(return_value=tool)
        )
        return context

    @staticmethod
    async def _run(
        backend: DaemonProxyBackend,
        context: MagicMock,
        *,
        nothing_was_sent: bool,
        instance_id: str,
        the_repeat_sent_nothing: bool | None = None,
        the_repeat_arrives_wrapped: bool = False,
    ) -> tuple[Any, int]:
        """Drive one failing call through the middleware.

        Returns what the middleware answered and how many times the call was
        attempted, with an `_Escaped` in place of the answer when it raised
        instead of answering. Spelling that raise `None` was the same value a
        middleware can return, so the two could not be told apart: `return None`
        in place of the final `raise` in `on_call_tool` passed every test here.
        The answer itself is kept rather than reduced to a pass/fail because the
        branch that cannot repeat a call now reports it: "did not succeed" no
        longer distinguishes a payload naming the unknown outcome from a masked
        raise that names nothing.

        *the_repeat_sent_nothing* is the replacement dying too: `None` for a
        repeat that succeeds, and otherwise the second failure's own
        `nothing_was_sent`. A repeat that always answers "the result" is the only
        thing this helper could express before, and it cannot show what happens
        to a failure on the way back out.

        *the_repeat_arrives_wrapped* gives that second failure the chain a real
        one carries, with the tag three links under a `ToolError` instead of
        raised bare.
        """
        from linkedin_mcp_server.daemon_proxy import (
            FrontendOwnerRecoveryMiddleware,
            OwnerUnreachableError,
        )

        attempts = 0

        async def call_next(_context: Any) -> Any:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OwnerUnreachableError(
                    instance_id=instance_id,
                    nothing_was_sent=nothing_was_sent,
                    cause=httpx2.ConnectError("gone"),
                )
            if the_repeat_sent_nothing is not None:
                # The replacement's identity, not the failed owner's: this is a
                # second owner going away, not the first failing late.
                the_replacement = f"{instance_id}-replacement"
                if the_repeat_arrives_wrapped:
                    _fail_the_way_a_real_call_fails(
                        instance_id=the_replacement,
                        nothing_was_sent=the_repeat_sent_nothing,
                    )
                raise OwnerUnreachableError(
                    instance_id=the_replacement,
                    nothing_was_sent=the_repeat_sent_nothing,
                    cause=httpx2.ConnectError("gone as well"),
                )
            return "the result"

        middleware = FrontendOwnerRecoveryMiddleware(backend)
        try:
            answer = await middleware.on_call_tool(context, call_next)  # ty: ignore
        except Exception as escaped:
            # Anything, rather than `OwnerUnreachableError` alone: a failure
            # that arrived wrapped leaves wrapped too, and the narrow catch
            # would let that one past this helper instead of recording it as
            # the raise it is.
            return _Escaped(escaped), attempts
        return answer, attempts

    @pytest.fixture
    def _recovering(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """A backend whose election always finds a replacement."""
        elected = _attachment(tmp_path)
        replacement = _attachment(tmp_path, port=elected.descriptor.port + 1)
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon_election.obtain_owner",
            lambda *_a, **_k: _elected(replacement),
        )
        return _backend(elected, tmp_path), elected.descriptor.instance_id

    @pytest.fixture
    def _alone(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """A backend whose election finds nobody and cannot start one either."""
        elected = _attachment(tmp_path)

        def elect(*_a: Any, **_k: Any):
            raise RuntimeError("no owner could be started")

        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", elect)
        return _backend(elected, tmp_path), elected.descriptor.instance_id

    @staticmethod
    def _reported(answer: Any) -> dict[str, Any]:
        """The structured payload of an answer that reports an unknown outcome."""
        assert not isinstance(answer, _Escaped), (
            f"the call was reported by raising, not by result: {answer.error!r}"
        )
        assert answer.is_error is True
        assert answer.structured_content is not None
        return answer.structured_content

    @staticmethod
    def _escaped(answer: Any, why: str) -> BaseException:
        """The owner-loss failure the middleware raised, rather than an answer.

        Both halves are asserted. That the call left through a raise at all is
        what `answer is None` could not say, since a middleware returning
        `None` reads exactly the same; and that what escaped is still the
        owner-loss failure, rather than something the recovery itself broke on
        while deciding what to do with it.
        """
        from linkedin_mcp_server.daemon_proxy import unreachable_owner_in

        assert isinstance(answer, _Escaped), f"{why}: answered {answer!r}"
        assert unreachable_owner_in(answer.error) is not None, (
            f"a different failure escaped the recovery: {answer.error!r}"
        )
        return answer.error

    async def test_a_mutating_call_is_not_repeated_when_it_may_have_run(
        self, _recovering
    ):
        # The failure the user pays for. `daemon_auth` already recorded the
        # measurement behind the rule: a client answered with an error at 0.66s
        # and the effect landing 0.7s later.
        backend, failed = _recovering
        answer, attempts = await self._run(
            backend,
            self._context(read_only=False),
            nothing_was_sent=False,
            instance_id=failed,
        )

        assert attempts == 1, "a call that may already have run was repeated"
        # Reported rather than raised, because a raise reaches the client as the
        # tool's name and nothing else. The status and `retry_safe` are the whole
        # answer: they are what tells a client to look before calling again.
        reported = self._reported(answer)
        assert reported["status"] == "outcome_unknown"
        assert reported["retry_safe"] is False
        assert "do_the_thing" in reported["message"]
        # And the replacement was still adopted, for the next call.
        assert backend.attachment.descriptor.instance_id != failed

    async def test_an_unknown_outcome_says_nothing_about_what_was_sent(
        self, _recovering
    ):
        # Absent, not null. `sent` is precisely the thing nobody here knows, and
        # a null answers the question a client asked with a "no".
        backend, failed = _recovering
        answer, _attempts = await self._run(
            backend,
            self._context(read_only=False),
            nothing_was_sent=False,
            instance_id=failed,
        )

        reported = self._reported(answer)
        assert "sent" not in reported
        assert "recipient_selected" not in reported
        assert "url" not in reported

    async def test_the_unknown_outcome_speaks_the_send_contracts_vocabulary(self):
        """The two halves of the payload are keys a send already uses.

        The builder lives in `daemon_proxy` on purpose: this is the transport
        saying it knows nothing, not a page-read outcome, and no daemon module
        imports from `linkedin/`. The cost of that is two places naming the same
        keys, so the names are pinned against their source here rather than
        left to drift until a client reads one of them and not the other.
        """
        from linkedin_mcp_server.daemon_liveness import unknown_outcome
        from linkedin_mcp_server.linkedin.contracts import message_action_result

        reported = unknown_outcome(tool="send_message", reason="the owner went away")
        a_send = message_action_result("https://www.linkedin.com/in/x/", "sent", "ok")

        assert set(reported) <= set(a_send), (
            "an owner-loss result must not invent keys a send does not have"
        )
        assert {"status", "message", "retry_safe"} <= set(reported)

    async def test_a_mutating_call_is_repeated_when_nothing_was_sent(self, _recovering):
        # The only reason the dispatch question is worth asking. Without it every
        # write tool would keep failing across an upgrade.
        backend, failed = _recovering
        answer, attempts = await self._run(
            backend,
            self._context(read_only=False),
            nothing_was_sent=True,
            instance_id=failed,
        )

        assert answer == "the result"
        assert attempts == 2

    async def test_a_replacement_that_dies_too_is_reported(self, _recovering):
        """The repeat can act, and an escaping failure is #891 one round later.

        The first attempt was repeated only because nothing had left this
        process; that says nothing about the second, which the replacement may
        have taken and run before going away itself. Escaping, it is flattened
        to `Error calling tool 'do_the_thing'` — and a client told only that
        sends the message again.
        """
        backend, failed = _recovering
        answer, attempts = await self._run(
            backend,
            self._context(read_only=False),
            nothing_was_sent=True,
            instance_id=failed,
            the_repeat_sent_nothing=False,
        )

        assert attempts == 2, "a repeat that may have run was attempted again"
        reported = self._reported(answer)
        assert reported["status"] == "outcome_unknown"
        assert reported["retry_safe"] is False

    async def test_a_second_failure_is_found_through_its_wrapping(self, _recovering):
        """The chain a second failure really carries, rather than the bare tag.

        Masking sits below this middleware, so what comes back from the repeat
        is `ToolError -> OwnerUnreachableError -> RuntimeError('Client failed to
        connect') -> httpx2.ConnectError` and the tag is three links down.
        Reading the exception's own type instead of walking its causes passes
        every other test here, because every other one raises the tag bare, and
        loses exactly this call: the mutating repeat a replacement may already
        have sent before dying.
        """
        backend, failed = _recovering
        answer, attempts = await self._run(
            backend,
            self._context(read_only=False),
            nothing_was_sent=True,
            instance_id=failed,
            the_repeat_sent_nothing=False,
            the_repeat_arrives_wrapped=True,
        )

        assert attempts == 2
        reported = self._reported(answer)
        assert reported["status"] == "outcome_unknown"
        assert reported["retry_safe"] is False

    async def test_a_repeat_that_never_left_either_still_raises(self, _recovering):
        """Two attempts, neither of which reached anybody: nothing to describe.

        `outcome_unknown` is a claim that something may have happened on
        LinkedIn. A repeat that provably never left the process makes no such
        claim, and reporting one would hand a client a `retry_safe` flag about a
        call nobody ever received.
        """
        backend, failed = _recovering
        answer, attempts = await self._run(
            backend,
            self._context(read_only=False),
            nothing_was_sent=True,
            instance_id=failed,
            the_repeat_sent_nothing=True,
        )

        self._escaped(answer, "a call that provably never left was called unknown")
        assert attempts == 2

    async def test_a_read_only_repeat_that_fails_stays_a_failure(self, _recovering):
        """A read has no outcome to be unknown about, on either attempt.

        The same rule as with no replacement at all: turning this into a result
        would dress a plain transport failure up as a LinkedIn answer.
        """
        backend, failed = _recovering
        answer, attempts = await self._run(
            backend,
            self._context(read_only=True),
            nothing_was_sent=False,
            instance_id=failed,
            the_repeat_sent_nothing=False,
        )

        self._escaped(answer, "a failed read was reported as an unknown outcome")
        assert attempts == 2

    async def test_a_read_is_not_reclassified_against_the_departed_owner(
        self, _recovering
    ):
        """The classification the first attempt read decides the second too.

        The same read as above, with the second lookup armed to fail. Asking
        again means asking the owner that has just gone: `fastmcp.get_tool` is a
        forwarded listing with the component cache off, it raises, and
        `a_repeat_could_change_something` catches that and answers `True` on the
        cautious side. The read would then come back as `outcome_unknown`
        carrying `retry_safe: False` — a client sent to look on LinkedIn for an
        effect a read cannot have had, and a safely repeatable read declared
        unrepeatable, for no reason but that the owner holding the answer died.

        So the armed failure is never reached. Which is what the await count
        says: the annotations belong to the tool, one live owner already read
        them, and an owner going away does not turn a read into a write.
        """
        backend, failed = _recovering
        context = self._context(read_only=True, then_unreachable=True)
        answer, attempts = await self._run(
            backend,
            context,
            nothing_was_sent=False,
            instance_id=failed,
            the_repeat_sent_nothing=False,
        )

        assert attempts == 2
        self._escaped(answer, "a failed read was reported as an unknown outcome")
        assert context.fastmcp_context.fastmcp.get_tool.await_count == 1, (
            "the classification was read again from the owner that had gone"
        )

    async def test_a_read_only_call_is_repeated_even_when_it_may_have_run(
        self, _recovering
    ):
        # Repeating a read costs a page load and nothing else.
        backend, failed = _recovering
        answer, attempts = await self._run(
            backend,
            self._context(read_only=True),
            nothing_was_sent=False,
            instance_id=failed,
        )

        assert answer == "the result"
        assert attempts == 2

    async def test_an_unannotated_call_is_treated_as_mutating(self, _recovering):
        # A tool that declares nothing has not promised anything, and the default
        # has to be the safe one: this is what keeps a tool added later from
        # being replayed because nobody remembered to annotate it.
        backend, failed = _recovering
        answer, attempts = await self._run(
            backend,
            self._context(read_only=None),
            nothing_was_sent=False,
            instance_id=failed,
        )

        assert attempts == 1
        assert self._reported(answer)["retry_safe"] is False

    async def test_a_call_that_may_have_run_is_reported_with_no_replacement_too(
        self, _alone
    ):
        """An election that found nobody makes the outcome no more knowable.

        The order the decision is taken in: the unsafe question is asked before
        the replacement is looked at, because the answer does not depend on it.
        Taken the other way around, the one call that needs the detail most
        loses it exactly when the host is down and nothing will elect.
        """
        backend, failed = _alone
        answer, attempts = await self._run(
            backend,
            self._context(read_only=False),
            nothing_was_sent=False,
            instance_id=failed,
        )

        assert attempts == 1
        assert self._reported(answer)["status"] == "outcome_unknown"
        assert backend.attachment.descriptor.instance_id == failed

    async def test_a_repeatable_call_needs_somewhere_to_repeat_it(self, _alone):
        """Nothing was sent, and there is nobody left to send it to.

        The dispatch question says a repeat would be *safe*, not that there is
        anywhere to run it: the owner it would go to is the one that has just
        gone away, so a repeat here is a second failure rather than a second
        chance. It stays a raise, because a call that provably never left has no
        unknown outcome to report either.
        """
        backend, failed = _alone
        answer, attempts = await self._run(
            backend,
            self._context(read_only=False),
            nothing_was_sent=True,
            instance_id=failed,
        )

        self._escaped(answer, "a transport failure was dressed up as an outcome")
        assert attempts == 1, "the call was repeated against the departed owner"
        assert backend.attachment.descriptor.instance_id == failed

    async def test_a_safe_call_with_no_replacement_still_raises(self, _alone):
        """Nothing to report and nowhere to run it: the failure stays a failure.

        A read that could be repeated has no unknown outcome to describe, so
        turning this into a result would dress a plain transport failure up as a
        LinkedIn answer and hand a client a `retry_safe` flag about a call that
        never acted. It is raised as a `ToolError` saying the owner is gone, so
        the masking below and the SDK above leave the reason readable.
        """
        from linkedin_mcp_server.daemon_proxy import (
            FrontendOwnerRecoveryMiddleware,
            OwnerUnreachableError,
        )

        backend, failed = _alone
        attempts = 0

        async def call_next(_context: Any) -> Any:
            nonlocal attempts
            attempts += 1
            raise OwnerUnreachableError(
                instance_id=failed,
                nothing_was_sent=False,
                cause=httpx2.ConnectError("gone"),
            )

        middleware = FrontendOwnerRecoveryMiddleware(backend)
        with pytest.raises(ToolError, match="could not be restarted"):
            await middleware.on_call_tool(
                self._context(read_only=True),
                call_next,  # ty: ignore
            )

        assert attempts == 1

    async def test_a_failure_from_something_else_is_left_alone(self, _recovering):
        # Only an unreachable owner is this middleware's business. Swallowing or
        # retrying anything else would hide a real tool error behind a recovery.
        from linkedin_mcp_server.daemon_proxy import FrontendOwnerRecoveryMiddleware

        backend, _failed = _recovering
        attempts = 0

        async def call_next(_context: Any) -> Any:
            nonlocal attempts
            attempts += 1
            raise ValueError("the tool itself failed")

        middleware = FrontendOwnerRecoveryMiddleware(backend)
        with pytest.raises(ValueError, match="the tool itself failed"):
            await middleware.on_call_tool(
                self._context(read_only=True),
                call_next,  # ty: ignore
            )

        assert attempts == 1


class _OwnerRefuses(Middleware):
    """The owner answering one kind of request with a JSON-RPC error of its own.

    Raised in the owner's outermost middleware, where the server sends an
    `MCPError` on with its code and message intact in both eras, rather than
    in a tool body, where fastmcp would shape it into a tool result first.
    """

    def __init__(self, *, code: int, message: str, fails: str) -> None:
        self._code = code
        self._message = message
        self._fails = fails

    def _refuse(self) -> NoReturn:
        from mcp import MCPError

        raise MCPError(code=self._code, message=self._message)

    async def on_list_tools(self, context: Any, call_next: Any) -> Any:
        if self._fails == "list":
            self._refuse()
        return await call_next(context)

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        if self._fails == "call":
            self._refuse()
        return await call_next(context)


class _AnswersWithAnError(FastMCPTransport):
    """An owner whose listing or call comes back as a JSON-RPC error.

    The code is the whole point. A client cannot tell from the type whether the
    owner said no or whether the session gave up waiting and wrote the error
    itself, and only the second is a departure. The owner sends these itself
    here, so what they pin is the rule applied to a pair, not where the pair
    came from: `TestTheSdksOwnHttpErrors` makes the SDK write its own.
    """

    def __init__(
        self, server: FastMCP, *, code: int, message: str, fails: str = "list"
    ) -> None:
        super().__init__(server)
        server.add_middleware(_OwnerRefuses(code=code, message=message, fails=fails))


class _DiesAsTheCallsSessionCloses(_OnTheRealSession):
    """An owner whose session fails to close, after a call was made on it.

    The two halves of the displacement, in the order that does the damage.
    `ProxyTool.run` makes its call inside `async with client`
    (`fastmcp/server/providers/proxy.py`), and `Client._disconnect` awaits the
    session task under `suppress(asyncio.CancelledError)`
    (`fastmcp/client/client.py`), so an ordinary exception from that task
    leaves the context manager after the call has already decided its outcome,
    and replaces whatever that was.

    *refusing* is the code and message the owner answers the call with, or
    `None` for a call that succeeds. With a failure in flight it is the tag
    that gets replaced, and with none it is the result.

    Only a session that was asked to call dies while closing. Every upstream
    operation opens a client of its own, so a transport that killed every
    session would fail the lookup in front of the call and the call would never
    be reached. The call is counted where the SDK takes it, on the real
    session, and *closing_failures* says whether the failure this exists for
    was actually raised: a count kept anywhere the call does not pass would
    leave it silently unraised.
    """

    def __init__(
        self, server: FastMCP, *, refusing: tuple[int, str] | None = None
    ) -> None:
        super().__init__(server)
        if refusing is not None:
            code, message = refusing
            server.add_middleware(
                _OwnerRefuses(code=code, message=message, fails="call")
            )
        self.closing_failures = 0

    @asynccontextmanager
    async def connect_session(self, **kwargs: Any) -> AsyncIterator[Any]:
        calls = 0
        async with super().connect_session(**kwargs) as session:
            send_request = session.send_request

            async def counting(request: Any, *args: Any, **kwargs: Any) -> Any:
                nonlocal calls
                if isinstance(request, mt.CallToolRequest):
                    calls += 1
                return await send_request(request, *args, **kwargs)

            session.send_request = counting
            yield session
        if calls:
            self.closing_failures += 1
            raise httpx2.ReadError("the owner went away as the session closed")


class _DiscoveryFailsAfterMutation(_OnTheRealSession):
    """An owner that answers a call, then cannot answer schema discovery."""

    def __init__(self, server: FastMCP, ran: list[str]) -> None:
        super().__init__(server)
        self._ran = ran
        self.discovery_attempts = 0

    def adjust(self, session: Any) -> None:
        # Only the session that made the call loses its owner. The frontend's
        # own client lists again after a call to read the output schema, on a
        # session of its own, and that one is not what this is about.
        send_request = session.send_request
        list_tools = session.list_tools
        called = False

        async def noting_the_call(request: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal called
            if isinstance(request, mt.CallToolRequest):
                called = True
            return await send_request(request, *args, **kwargs)

        async def gone_once_it_ran(*args: Any, **kwargs: Any) -> Any:
            if called and self._ran:
                self.discovery_attempts += 1
                raise httpx2.ConnectError("the owner left after answering the call")
            return await list_tools(*args, **kwargs)

        session.send_request = noting_the_call
        session.list_tools = gone_once_it_ran


class _RecordsCallRequest(_OnTheRealSession):
    """Record the tool request at two layers of the session that sends it.

    Where it enters the SDK (`send_request`, before the SDK stamps anything on
    it) for what the client asked for, and where the session hands it to its
    dispatcher for the deadline that request actually waits on, which is the
    session's own when the call names none.
    """

    def __init__(self, server: FastMCP) -> None:
        super().__init__(server)
        self.request: mt.CallToolRequest | None = None
        self.timeout: float | None = None
        self.progress_callback: Any = None
        self.deadline: object = "never reached"

    def adjust(self, session: Any) -> None:
        send_request = session.send_request
        dispatcher = session._dispatcher
        send_raw_request = dispatcher.send_raw_request

        async def entering(
            request: Any,
            result_type: Any,
            request_read_timeout_seconds: float | None = None,
            metadata: Any = None,
            progress_callback: Any = None,
        ) -> Any:
            if isinstance(request, mt.CallToolRequest):
                self.request = request
                self.timeout = request_read_timeout_seconds
                self.progress_callback = progress_callback
            return await send_request(
                request,
                result_type,
                request_read_timeout_seconds,
                metadata,
                progress_callback,
            )

        async def dispatched(
            method: str, params: Any, opts: Any = None, **kwargs: Any
        ) -> Any:
            if method == "tools/call":
                self.deadline = (opts or {}).get("timeout")
            return await send_raw_request(method, params, opts, **kwargs)

        session.send_request = entering
        dispatcher.send_raw_request = dispatched


class TestRecoveringThroughTheWholeProxy:
    """The path a real request takes, with only the socket stood in for.

    Every other test here drives one piece, and all of them stay green with the
    pieces unconnected: a classification that is never applied, a middleware that
    is never registered. What runs below is `create_mcp_server` in its proxy
    role, so a failure has to travel the real exception chain, through the
    middleware the server really installed, into an election, and back out as an
    answer a client can use.
    """

    @staticmethod
    def _owner(name: str = "get_person_profile") -> FastMCP:
        owner = FastMCP("owner")

        @owner.tool(name=name, annotations={"readOnlyHint": True})
        async def a_tool() -> str:
            return name

        return owner

    @staticmethod
    def _mutating_owner(ran: list[str]) -> FastMCP:
        """An owner with a write tool that records every run in *ran*.

        Unannotated on purpose: a tool that declares no `readOnlyHint` is what
        the recovery has to treat as something a repeat could change.
        """
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            ran.append("sent")
            return "sent"

        return owner

    @staticmethod
    def _proxy(backend: DaemonProxyBackend) -> FastMCP:
        from linkedin_mcp_server.server import create_mcp_server
        from linkedin_mcp_server.server_role import ServerRole

        return create_mcp_server(
            role=ServerRole.PROXY, proxy_backend=backend, tool_timeout=1.0
        )

    @pytest.fixture
    def _upgraded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """A backend whose owner is gone and whose election finds the new one.

        Returns the backend, the id of the owner that left, and a callable
        counting how many elections have been run.
        """
        elected = _attachment(tmp_path)
        replacement = _attachment(tmp_path, port=elected.descriptor.port + 1)
        backend = _backend(elected, tmp_path)
        elections = 0

        def elect(*_args, **_kwargs):
            nonlocal elections
            elections += 1
            return _elected(replacement)

        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", elect)
        return backend, elected, replacement, lambda: elections

    async def test_a_client_lists_again_after_its_owner_went_away(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded
    ):
        """The reproduced failure, run backwards.

        A proxy whose owner was stood down answered its next listing with
        `McpError: Client failed to connect`. Here the same departure ends in the
        replacement's tool list, without the proxy process restarting.
        """
        backend, elected, replacement, elections = _upgraded
        after = self._owner("the_replacements_tool")
        _reach_owners_in_process(
            monkeypatch,
            # Nothing is listening where the departed owner was.
            lambda url: None if url == elected.descriptor.url else after,
        )

        async with Client(self._proxy(backend)) as client:
            listed = {tool.name for tool in await client.list_tools()}

        assert listed == {"the_replacements_tool"}
        assert elections() == 1
        assert backend.attachment.descriptor.url == replacement.descriptor.url

    async def test_a_client_calls_a_tool_through_the_replacement(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded
    ):
        backend, elected, _replacement, elections = _upgraded
        after = self._owner()
        _reach_owners_in_process(
            monkeypatch,
            lambda url: None if url == elected.descriptor.url else after,
        )

        async with Client(self._proxy(backend)) as client:
            result = await client.call_tool("get_person_profile", {})

        assert result.data == "get_person_profile"
        assert elections() == 1

    @_HOP_ERAS
    async def test_an_answer_is_not_replaced_by_schema_discovery(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _upgraded,
        legacy_only: bool,
        protocol: str,
    ):
        """A completed mutation must not be rediscovered and sent again."""
        backend, elected, _replacement, elections = _upgraded
        ran: list[str] = []
        before = _DiscoveryFailsAfterMutation(self._mutating_owner(ran), ran)
        after = self._mutating_owner(ran)
        sessions = _sessions_opened(monkeypatch)
        _reach_owners_in_process(
            monkeypatch,
            lambda url: before if url == elected.descriptor.url else after,
            legacy_only=legacy_only,
        )

        async with Client(self._proxy(backend)) as client:
            result = await client.call_tool("send_connection_request", {})

        assert result.data == "sent"
        assert ran == ["sent"], (
            "schema discovery repeated a completed mutation "
            f"(elections={elections()}, discovery_attempts={before.discovery_attempts})"
        )
        assert elections() == 0, "schema discovery stood a replacement owner up"
        assert before.discovery_attempts == 0
        assert {session.protocol_version for session in sessions} == {protocol}

    async def test_an_owner_that_dies_after_the_handshake_is_still_recovered(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded
    ):
        """The boundary neither the connect nor the call can see.

        The listing runs inside a connection that was established, so a departure
        between the initialize and the list request raises out of neither. Every
        other recovery test kills the owner earlier and passes with the listing
        boundaries unwrapped.
        """
        backend, elected, _replacement, elections = _upgraded
        dying = _GoesAwayAfterInitialize(self._owner())
        after = self._owner("the_replacements_tool")
        _reach_owners_in_process(
            monkeypatch,
            lambda url: dying if url == elected.descriptor.url else after,
        )

        async with Client(self._proxy(backend)) as client:
            listed = {tool.name for tool in await client.list_tools()}

        assert listed == {"the_replacements_tool"}
        assert elections() == 1

    async def test_a_lookup_that_loses_its_session_runs_the_call_once_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded
    ):
        """The provider's lookup inside a mutating call ends before any tool request.

        A forwarded call first looks its tool up, on a session of its own that
        is already bound to the call. Here that session ends under the lookup,
        before a tool request exists. Nothing that could act has left, so the
        one run the user asked for belongs to the replacement.

        The client's session state is cleared by the time the failure is
        reported, so the no-send answer has to come from the session the client
        entered on. Read from the cleared state instead, the call looks as if
        it may have been sent, and the user is told to check LinkedIn for an
        action neither owner ran.
        """
        backend, elected, _replacement, elections = _upgraded
        original_runs: list[str] = []
        replacement_runs: list[str] = []
        at_the_failure: list[tuple[bool, bool]] = []
        clients: list[Any] = []
        open_client = backend.open_client

        def recording(*, timeout: float) -> Any:
            client = open_client(timeout=timeout)
            clients.append(client)
            return client

        monkeypatch.setattr(backend, "open_client", recording)

        class EndsItsSessionAtTheLookup(_OnTheRealSession):
            def adjust(self, session: Any) -> None:
                async def lost(*_args: Any, **_kwargs: Any) -> NoReturn:
                    (client,) = [
                        c for c in clients if c._session_state.session is session
                    ]
                    at_the_failure.append(
                        (client._binding is not None, session.tool_request_started)
                    )
                    # Ends the client's session runner the way a lost owner
                    # connection does, with this listing still waiting.
                    client._session_state.stop_event.set()
                    await asyncio.Event().wait()
                    raise AssertionError("the listing was never given up on")

                session.list_tools = lost

        def owner_recording(runs: list[str]) -> FastMCP:
            owner = FastMCP("owner")

            @owner.tool(name="send_connection_request")
            async def send(message: str) -> str:
                runs.append(message)
                return message

            return owner

        original = EndsItsSessionAtTheLookup(owner_recording(original_runs))
        replacement = owner_recording(replacement_runs)
        _reach_owners_in_process(
            monkeypatch,
            lambda url: original if url == elected.descriptor.url else replacement,
        )

        async with Client(self._proxy(backend)) as client:
            result = await asyncio.wait_for(
                client.call_tool(
                    "send_connection_request", {"message": "hi"}, raise_on_error=False
                ),
                timeout=10,
            )

        # The fault this pins really happened where it matters: inside the
        # bound call, before its tool request.
        assert at_the_failure == [(True, False)]
        assert original_runs == []
        assert replacement_runs == ["hi"], result
        assert result.is_error is False
        assert result.data == "hi"
        assert elections() == 1

    async def test_an_owner_that_answers_with_an_error_elects_nothing(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded
    ):
        """A process that returns a JSON-RPC error is reachable.

        Treating one as a departure would stand a healthy owner's replacement up
        for nothing, and hide whatever it was trying to say.
        """
        backend, elected, _replacement, elections = _upgraded
        _reach_owners_in_process(
            monkeypatch,
            lambda _url: _AnswersWithAnError(
                self._owner(), code=mt.INTERNAL_ERROR, message="the owner refused"
            ),
        )

        async with Client(self._proxy(backend)) as client:
            with pytest.raises(Exception, match="refused"):
                await client.list_tools()

        assert elections() == 0
        assert (
            backend.attachment.descriptor.instance_id == elected.descriptor.instance_id
        )

    @pytest.mark.parametrize(
        "listing", ["list_resources", "list_resource_templates", "list_prompts"]
    )
    async def test_the_other_listings_recover_too(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded, listing: str
    ):
        """A client's opening exchange asks for more than tools.

        This role serves none of these, so the answer is an empty list either
        way. The provider is still asked in order to give it, and with a departed
        owner and `provider_error_strategy = "raise"` that empty answer becomes
        an error the user sees. One case per listing, because a hook left off
        covers only itself.
        """
        backend, elected, _replacement, elections = _upgraded
        after = self._owner()
        _reach_owners_in_process(
            monkeypatch,
            lambda url: None if url == elected.descriptor.url else after,
        )

        async with Client(self._proxy(backend)) as client:
            assert await getattr(client, listing)() == []

        assert elections() == 1

    @pytest.mark.parametrize(
        ("code", "message"),
        [
            (mt.REQUEST_TIMEOUT, "Request 'tools/list' timed out"),
            (mt.CONNECTION_CLOSED, "Connection closed"),
            (mt.CONNECTION_CLOSED, "SSE stream ended without a response"),
            (mt.INVALID_REQUEST, "Session terminated"),
        ],
        ids=["timed out", "connection closed", "sse ended", "session terminated"],
    )
    async def test_a_request_that_never_came_back_is_a_departure(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded, code: int, message: str
    ):
        """What a departure looks like *during* a request, rather than between.

        An owner killed between requests fails to connect, because streamable
        HTTP opens a fresh connection each time. One that goes away with a
        request outstanding does not: the client session waits, gives up, and
        writes an `MCPError` itself. So "the owner is gone" and "the owner said
        no" arrive as the same type, and reading the type alone leaves the
        frontend attached to a process that is not there.

        Each pair here is one the client invents, in the words SDK v2 uses.
        What this pins is the rule, not its premise: nothing in the protocol
        reserves these codes, and the argument for reading them lives with the
        rule. The HTTP stand-ins are driven through a real transport in
        `TestTheSdksOwnHttpErrors`.
        """
        backend, elected, _replacement, elections = _upgraded
        gone = _AnswersWithAnError(self._owner(), code=code, message=message)
        after = self._owner("the_replacements_tool")
        _reach_owners_in_process(
            monkeypatch,
            lambda url: gone if url == elected.descriptor.url else after,
        )

        async with Client(self._proxy(backend)) as client:
            listed = {tool.name for tool in await client.list_tools()}

        assert listed == {"the_replacements_tool"}
        assert elections() == 1

    @_HOP_ERAS
    async def test_a_mutating_call_that_timed_out_is_not_repeated(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _upgraded,
        legacy_only: bool,
        protocol: str,
    ):
        """A timeout is a departure and still no licence to run the call again.

        The two halves of the decision come apart here. The owner is gone, so a
        replacement is adopted for the next call; but a call that timed out may
        have been queued, may have held the profile lease, may have sent the
        connection request. Nothing in the protocol says which, so the failure is
        reported rather than guessed at.

        Driven through the real server rather than the middleware because
        `mask_error_details` is what makes the shape matter: the whole path is
        the only place that shows a raise arriving as the tool's name and
        nothing else, and this result surviving with its payload intact.
        """
        backend, elected, _replacement, elections = _upgraded
        ran: list[str] = []
        # The departed owner still answers the listing, so the failure comes from
        # the call boundary rather than from the lookup in front of it.
        gone = _AnswersWithAnError(
            self._mutating_owner(ran),
            code=mt.REQUEST_TIMEOUT,
            message="Request 'tools/call' timed out",
            fails="call",
        )
        after = self._mutating_owner(ran)
        sessions = _sessions_opened(monkeypatch)
        _reach_owners_in_process(
            monkeypatch,
            lambda url: gone if url == elected.descriptor.url else after,
            legacy_only=legacy_only,
        )

        async with Client(self._proxy(backend)) as client:
            # Reported rather than repeated, and reported in full: a raise from
            # the middleware would leave `Error calling tool
            # 'send_connection_request'` and no way to tell it from a tool that
            # simply failed, which is the difference between a client checking
            # LinkedIn and a client calling again.
            result = await client.call_tool(
                "send_connection_request", {}, raise_on_error=False
            )

        assert result.is_error is True
        assert result.structured_content is not None
        assert result.structured_content["status"] == "outcome_unknown"
        assert result.structured_content["retry_safe"] is False
        assert "sent" not in result.structured_content
        assert any(
            "Check LinkedIn before calling again" in getattr(block, "text", "")
            for block in result.content
        )
        assert ran == [], "a call that may already have run was sent again"
        assert elections() == 1
        assert (
            backend.attachment.descriptor.instance_id != elected.descriptor.instance_id
        )
        assert {session.protocol_version for session in sessions} == {protocol}

    @_HOP_ERAS
    async def test_a_tag_the_closing_session_replaced_is_still_found(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _upgraded,
        legacy_only: bool,
        protocol: str,
    ):
        """The owner-loss failure a departing owner's own cleanup buries.

        The call is classified as a departure and tagged, and then the client
        closes: the session task's own exception leaves `__aexit__` and takes the
        tag's place, which survives in `__context__` where nothing looks. The
        recovery then finds no failure to act on, re-raises, and masking hands
        the client `Error calling tool 'send_connection_request'` about a call
        that may have reached LinkedIn. That is the #891 damage on the path the
        #1008 answers never reach.

        Every other test here lets the client close cleanly, and all of them pass
        with the closing boundary unguarded.
        """
        backend, elected, _replacement, elections = _upgraded
        ran: list[str] = []
        gone = _DiesAsTheCallsSessionCloses(
            self._mutating_owner(ran),
            refusing=(
                mt.REQUEST_TIMEOUT,
                "Request 'tools/call' timed out",
            ),
        )
        after = self._mutating_owner(ran)
        sessions = _sessions_opened(monkeypatch)
        _reach_owners_in_process(
            monkeypatch,
            lambda url: gone if url == elected.descriptor.url else after,
            legacy_only=legacy_only,
        )

        async with Client(self._proxy(backend)) as client:
            result = await client.call_tool(
                "send_connection_request", {}, raise_on_error=False
            )

        assert result.is_error is True
        assert result.structured_content is not None, (
            "the owner loss reached the client as a failure carrying nothing"
        )
        assert result.structured_content["status"] == "outcome_unknown"
        assert result.structured_content["retry_safe"] is False
        assert ran == [], "a call that may already have run was sent again"
        assert elections() == 1
        assert gone.closing_failures == 1, "the session closed without failing"
        assert {session.protocol_version for session in sessions} == {protocol}

    @_HOP_ERAS
    async def test_an_answer_survives_a_session_that_fails_to_close(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _upgraded,
        legacy_only: bool,
        protocol: str,
    ):
        """The same displacement with nothing in flight: the result is replaced.

        The owner ran the tool and returned its result, and the client then fails
        while closing a session it will never use again. Left to escape, that
        failure replaces the answer, so masking turns a call that *did* act into
        `Error calling tool 'send_connection_request'` — which a client reads as
        a call to make again, for the one tool where that sends a second
        connection request.
        """
        backend, _elected, _replacement, elections = _upgraded
        ran: list[str] = []
        dying = _DiesAsTheCallsSessionCloses(self._mutating_owner(ran))
        sessions = _sessions_opened(monkeypatch)
        _reach_owners_in_process(
            monkeypatch, lambda _url: dying, legacy_only=legacy_only
        )

        async with Client(self._proxy(backend)) as client:
            result = await client.call_tool(
                "send_connection_request", {}, raise_on_error=False
            )

        assert result.is_error is False, (
            "a call the owner answered was reported as a failure"
        )
        assert result.data == "sent"
        assert ran == ["sent"], "the owner did not run the call exactly once"
        assert elections() == 0, "a closing failure stood a replacement up"
        assert dying.closing_failures == 1, "the session closed without failing"
        assert {session.protocol_version for session in sessions} == {protocol}

    async def test_a_caller_that_gives_up_at_the_close_stays_cancelled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The one failure at this boundary that is not the session's: a cancel.

        The guard catches `Exception`, and the whole distance between that and
        `BaseException` sits here: a `CancelledError` is a caller giving up
        rather than the owner's session failing, and a guard that returned for
        one would tell a caller that had already walked away that its call went
        through.

        Where such a cancellation can reach this boundary was measured against
        this client, and it is one window. Delivered any earlier it is already
        in flight at `__aexit__`, where a falsy return changes nothing about it.
        Delivered while `Client._disconnect` awaits the session task — the
        disconnect timeout, a close that hangs, the forced cancel that follows
        it — it is absorbed by that method's own
        `suppress(asyncio.CancelledError)` (`client.py`) and never arrives at
        all. What is left is the window below: the answer is in hand and the
        close has not started, so the delivery lands on the first checkpoint
        inside `_disconnect`, acquiring `_session_state.lock`.

        Driven against the client `open_client` builds rather than the whole
        server, because only the operation's own task can give up in that
        window, and in the server that task is inside `ProxyTool.run`.
        """
        ran: list[str] = []
        _reach_owners_in_process(monkeypatch, lambda _url: self._mutating_owner(ran))
        client = _backend(_attachment(tmp_path), tmp_path).open_client(timeout=1.0)
        went_on: list[str] = []

        async def gives_up_with_the_answer_in_hand() -> None:
            async with client:
                await client.call_tool_mcp("send_connection_request", {})
                giving_up = asyncio.current_task()
                assert giving_up is not None
                giving_up.cancel()
            went_on.append("the close answered a caller that had gone")

        operation = asyncio.create_task(gives_up_with_the_answer_in_hand())
        with pytest.raises(asyncio.CancelledError):
            await operation

        assert went_on == [], "a caller that gave up was carried on regardless"
        assert ran == ["sent"], "the owner did not run the call exactly once"
        # The cancelled close never reached the session task. Clearing up after
        # the caller, not part of what this pins.
        await client.close()


def _answers(**by_instance: Any):
    """A heartbeat preflight that answers per owner, the way each owner would.

    Keyed by instance id. A value is an `httpx2.Response` to return or an
    exception to raise, so every row of the classification table is the real
    classifier reading a real response object.
    """

    async def beat(attachment: Attachment, _call_id: str) -> httpx2.Response:
        answer = by_instance[attachment.descriptor.instance_id]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return beat


def _watched() -> httpx2.Response:
    """What an owner's heartbeat route says about a call it has not seen yet."""
    return httpx2.Response(200, json={"watched": False})


def _retiring(instance_id: str) -> httpx2.Response:
    """What a retiring owner signs, as its own heartbeat route sends it."""
    return httpx2.Response(
        409,
        json={
            "daemon": "retiring",
            "protocol": daemon_descriptor.PROTOCOL_VERSION,
            "instance": instance_id,
        },
    )


def _signed_refusal(kind: str, instance_id: str, *, protocol: object = None) -> Any:
    """An owner's own refusal of a call, as `daemon_liveness` returns it."""
    from linkedin_mcp_server.daemon_liveness import REFUSAL_KEY

    return ToolResult(
        content=[mt.TextContent(type="text", text="refused")],
        meta={
            REFUSAL_KEY: {
                "daemon": kind,
                "protocol": (
                    daemon_descriptor.PROTOCOL_VERSION if protocol is None else protocol
                ),
                "instance": instance_id,
            }
        },
        is_error=True,
    )


#: Every row of the preflight table: what came back, how it is classified, and
#: whether the owner is written off for it. An exception is a preflight that
#: got no answer at all.
_PREFLIGHT_ROWS: list[tuple[str, Callable[[str], Any], str, bool]] = [
    (
        "connect refused",
        lambda _i: httpx2.ConnectError("refused"),
        "unreachable",
        False,
    ),
    ("connect timeout", lambda _i: httpx2.ConnectTimeout("slow"), "unreachable", False),
    ("read timeout", lambda _i: httpx2.ReadTimeout("silent"), "owner_error", False),
    (
        "protocol error",
        lambda _i: httpx2.RemoteProtocolError("garbled"),
        "owner_error",
        False,
    ),
    ("500", lambda _i: httpx2.Response(500), "owner_error", False),
    ("503", lambda _i: httpx2.Response(503), "owner_error", False),
    ("401", lambda _i: httpx2.Response(401), "token_rejected", False),
    ("404", lambda _i: httpx2.Response(404), "route_missing", True),
    ("409 retiring", _retiring, "retiring", True),
    (
        "409 for another owner",
        lambda _i: _retiring(new_instance_id()),
        "unexpected_status",
        True,
    ),
    (
        "409 of another protocol",
        lambda i: httpx2.Response(
            409, json={"daemon": "retiring", "protocol": True, "instance": i}
        ),
        "unexpected_status",
        True,
    ),
    ("409 unsigned", lambda _i: httpx2.Response(409), "unexpected_status", True),
    (
        "302",
        lambda _i: httpx2.Response(302, headers={"location": "/"}),
        "unexpected_status",
        True,
    ),
    ("400", lambda _i: httpx2.Response(400), "unexpected_status", True),
    ("403", lambda _i: httpx2.Response(403), "unexpected_status", True),
    ("405", lambda _i: httpx2.Response(405), "unexpected_status", True),
    ("415", lambda _i: httpx2.Response(415), "unexpected_status", True),
    ("429", lambda _i: httpx2.Response(429), "unexpected_status", True),
    ("418", lambda _i: httpx2.Response(418), "unexpected_status", True),
    (
        "200 not JSON",
        lambda _i: httpx2.Response(200, text="<html>hello</html>"),
        "unexpected_status",
        True,
    ),
    (
        "200 without the heartbeat body",
        lambda _i: httpx2.Response(200, json={"ok": True}),
        "unexpected_status",
        True,
    ),
]


class TestThePreflightDecides:
    """No call leaves without a validated go-ahead from the owner it goes to.

    One test per row of the preflight table, each driven through the real
    heartbeat middleware with only the socket stood in for. What each pins is
    the contract bullet that a failed preflight dispatches nothing, and that
    its classification decides whether the owner is written off.
    """

    @staticmethod
    def _context() -> MagicMock:
        context = MagicMock()
        context.message.name = "get_person_profile"
        return context

    async def test_a_validated_answer_dispatches_a_marked_call(self, tmp_path: Path):
        from linkedin_mcp_server.daemon_proxy import (
            FrontendCallHeartbeatMiddleware,
            _call_being_made,
        )

        attachment = _attachment(tmp_path)
        middleware = FrontendCallHeartbeatMiddleware(_backend(attachment, tmp_path))
        middleware._beat = _answers(**{attachment.descriptor.instance_id: _watched()})
        marked: list[str | None] = []

        async def call_next(_context: Any) -> str:
            bound = _call_being_made.get()
            marked.append(None if bound is None else bound.call_id)
            return "the result"

        assert await middleware.on_call_tool(self._context(), call_next) == "the result"  # ty: ignore
        assert len(marked) == 1 and marked[0] is not None, "the call went unmarked"

    @pytest.mark.parametrize(
        ("answer", "classification", "buries"),
        [row[1:] for row in _PREFLIGHT_ROWS],
        ids=[row[0] for row in _PREFLIGHT_ROWS],
    )
    async def test_a_failed_preflight_dispatches_nothing(
        self,
        tmp_path: Path,
        answer: Callable[[str], Any],
        classification: str,
        buries: bool,
    ):
        from linkedin_mcp_server.daemon_proxy import (
            FrontendCallHeartbeatMiddleware,
            OwnerUnreachableError,
        )

        attachment = _attachment(tmp_path)
        instance = attachment.descriptor.instance_id
        middleware = FrontendCallHeartbeatMiddleware(_backend(attachment, tmp_path))
        middleware._beat = _answers(**{instance: answer(instance)})
        dispatched: list[str] = []

        async def call_next(_context: Any) -> str:
            dispatched.append("the tool call")
            return "the result"

        with pytest.raises(OwnerUnreachableError) as refused:
            await middleware.on_call_tool(self._context(), call_next)  # ty: ignore

        assert dispatched == []
        # About the tool request, which never left, not the preflight exchange.
        assert refused.value.nothing_was_sent is True
        assert refused.value.instance_id == instance
        assert refused.value.classification.value == classification
        assert refused.value.classification.buries is buries

    @pytest.mark.parametrize(
        ("answer", "classification", "buries"),
        [row[1:] for row in _PREFLIGHT_ROWS],
        ids=[row[0] for row in _PREFLIGHT_ROWS],
    )
    async def test_recovery_carries_the_classification_into_the_election(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        answer: Callable[[str], Any],
        classification: str,
        buries: bool,
    ):
        """The classification travels on the failure and decides the burial.

        A burying row writes the owner off before the election runs, and the
        election is told to pass over it. Every row ends with the call made
        once, on the replacement, and never on the owner that failed.
        """
        from linkedin_mcp_server.daemon_proxy import (
            FrontendCallHeartbeatMiddleware,
            FrontendOwnerRecoveryMiddleware,
            _call_being_made,
        )

        failed = _attachment(tmp_path)
        replacement = _attachment(tmp_path, port=failed.descriptor.port + 1)
        failed_id = failed.descriptor.instance_id
        backend = _backend(failed, tmp_path)
        told_to_bury: list[set[str]] = []

        def elect(*_args: Any, buried: Any = frozenset(), **_kwargs: Any):
            told_to_bury.append(set(buried))
            return _elected(replacement)

        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", elect)
        heartbeat = FrontendCallHeartbeatMiddleware(backend)
        heartbeat._beat = _answers(
            **{
                failed_id: answer(failed_id),
                replacement.descriptor.instance_id: _watched(),
            }
        )
        dispatched_to: list[str] = []

        async def dispatch(_context: Any) -> str:
            bound = _call_being_made.get()
            assert bound is not None
            dispatched_to.append(bound.attachment.descriptor.instance_id)
            return "the result"

        async def inner(context: Any) -> Any:
            return await heartbeat.on_call_tool(context, dispatch)  # ty: ignore

        result = await FrontendOwnerRecoveryMiddleware(backend).on_call_tool(
            self._context(),
            inner,
        )

        assert result == "the result"
        assert dispatched_to == [replacement.descriptor.instance_id]
        assert told_to_bury == [{failed_id} if buries else set()]
        assert (failed_id in backend._unusable) is buries
        assert backend.attachment.descriptor.instance_id == (
            replacement.descriptor.instance_id
        )


class TestAnOwnersOwnRefusal:
    """An owner that refused the call before it ran said so, and signed it."""

    @staticmethod
    async def _call(tmp_path: Path, result: Any) -> Any:
        from linkedin_mcp_server.daemon_proxy import FrontendCallHeartbeatMiddleware

        attachment = _attachment(tmp_path)
        middleware = FrontendCallHeartbeatMiddleware(_backend(attachment, tmp_path))
        middleware._beat = _answers(**{attachment.descriptor.instance_id: _watched()})

        async def call_next(_context: Any) -> Any:
            return result(attachment.descriptor.instance_id)

        context = MagicMock()
        context.message.name = "send_message"
        try:
            return await middleware.on_call_tool(context, call_next)  # ty: ignore
        except Exception as escaped:
            return _Escaped(escaped)

    @pytest.mark.parametrize(
        ("kind", "classification"),
        [("retiring", "retiring"), ("unmarked_call", "unmarked_refused")],
    )
    async def test_a_signed_refusal_is_a_call_that_never_ran(
        self, tmp_path: Path, kind: str, classification: str
    ):
        from linkedin_mcp_server.daemon_proxy import OwnerUnreachableError

        answer = await self._call(
            tmp_path, lambda instance: _signed_refusal(kind, instance)
        )

        assert isinstance(answer, _Escaped)
        assert isinstance(answer.error, OwnerUnreachableError)
        assert answer.error.nothing_was_sent is True
        assert answer.error.classification.value == classification
        assert answer.error.classification.buries

    @pytest.mark.parametrize(
        "forged",
        [
            lambda _instance: _signed_refusal("retiring", new_instance_id()),
            lambda instance: _signed_refusal("retiring", instance, protocol=True),
            lambda instance: _signed_refusal(
                "retiring",
                instance,
                protocol=daemon_descriptor.PROTOCOL_VERSION - 1,
            ),
            lambda instance: _signed_refusal("some_new_refusal", instance),
            lambda instance: ToolResult(
                content=[mt.TextContent(type="text", text="done")],
                meta=_signed_refusal("retiring", instance).meta,
                is_error=False,
            ),
        ],
        ids=[
            "another owner",
            "a boolean protocol",
            "an older protocol",
            "an unknown kind",
            "not an error",
        ],
    )
    async def test_anything_else_is_tool_data(self, tmp_path: Path, forged: Any):
        # Only the owner this call went to can prove the call never ran. A
        # marker from anyone else, or in any other shape, says nothing about
        # dispatch, and treating it as proof would let a mutating call be sent
        # twice.
        answer = await self._call(tmp_path, forged)

        assert isinstance(answer, ToolResult), f"treated as a refusal: {answer!r}"


async def _until_true(condition: Callable[[], bool], *, seconds: float = 5.0) -> None:
    """Yield to other tasks and threads until *condition* holds."""
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("the condition never held")
        await asyncio.sleep(0.01)


class TestWritingAnOwnerOff:
    """Durable burial, enforced wherever an attachment is used."""

    async def test_a_burial_learned_during_an_election_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The latest burial decides, not the one the running election knew.

        One call fails in a way that says nothing about the owner and starts an
        election. A second learns the owner is retiring and joins it. A
        retiring owner still answers the probe, so that election finds it
        again; its answer must not be adopted, and the second caller gets one
        more election that knows.
        """
        from linkedin_mcp_server.daemon_proxy import OwnerFailure

        retiring = _attachment(tmp_path)
        replacement = _attachment(tmp_path, port=retiring.descriptor.port + 1)
        retiring_id = retiring.descriptor.instance_id
        backend = _backend(retiring, tmp_path)
        holding = threading.Event()
        told_to_bury: list[set[str]] = []

        def elect(*_args: Any, buried: Any = frozenset(), **_kwargs: Any):
            told_to_bury.append(set(buried))
            if len(told_to_bury) == 1:
                holding.wait(timeout=5)
                return _elected(retiring)
            return _elected(replacement)

        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", elect)

        first = asyncio.create_task(
            backend.recover(retiring_id, classification=OwnerFailure.OWNER_ERROR)
        )
        await _until_true(lambda: bool(told_to_bury))
        joined = asyncio.create_task(
            backend.recover(retiring_id, classification=OwnerFailure.RETIRING)
        )
        for _ in range(20):
            await asyncio.sleep(0)
        holding.set()
        results = await asyncio.gather(first, joined)

        assert told_to_bury == [set(), {retiring_id}]
        assert all(
            r is not None
            and r.descriptor.instance_id == replacement.descriptor.instance_id
            for r in results
        ), "a caller was handed back the owner it had just found retiring"
        assert backend.attachment.descriptor.instance_id == (
            replacement.descriptor.instance_id
        )

    async def test_an_election_that_knew_everything_is_not_repeated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The bound on the rule above: a second election only for news the
        # first did not have. Finding nobody with full knowledge is an answer.
        from linkedin_mcp_server.daemon_proxy import OwnerFailure

        failed = _attachment(tmp_path)
        backend = _backend(failed, tmp_path)
        elections: list[set[str]] = []

        def elect(*_args: Any, buried: Any = frozenset(), **_kwargs: Any):
            elections.append(set(buried))
            raise RuntimeError("nobody could be started")

        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", elect)

        found = await backend.recover(
            failed.descriptor.instance_id, classification=OwnerFailure.RETIRING
        )

        assert found is None
        assert elections == [{failed.descriptor.instance_id}]

    async def test_a_rejected_token_does_not_come_back_as_the_same_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A token is minted per instance and never changes, so the same owner
        # found again carries the token it just refused. Retrying would be
        # refused the same way; the recovery ends instead.
        from linkedin_mcp_server.daemon_proxy import OwnerFailure

        owner = _attachment(tmp_path)
        backend = _backend(owner, tmp_path)
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon_election.obtain_owner",
            lambda *_a, **_k: _elected(owner),
        )

        found = await backend.recover(
            owner.descriptor.instance_id, classification=OwnerFailure.TOKEN_REJECTED
        )

        assert found is None

    async def test_a_written_off_owner_gets_no_listing_and_no_new_call(
        self, tmp_path: Path
    ):
        from linkedin_mcp_server.daemon_proxy import (
            FrontendCallHeartbeatMiddleware,
            OwnerFailure,
            OwnerUnreachableError,
        )

        owner = _attachment(tmp_path)
        backend = _backend(owner, tmp_path)
        backend.note_failure(owner.descriptor.instance_id, OwnerFailure.RETIRING)

        with pytest.raises(OwnerUnreachableError) as listing:
            backend.open_client(timeout=1.0)
        assert listing.value.nothing_was_sent is True
        assert listing.value.classification is OwnerFailure.RETIRING

        middleware = FrontendCallHeartbeatMiddleware(backend)
        preflights: list[str] = []

        async def beat(*_args: Any) -> httpx2.Response:
            preflights.append("preflight")
            return _watched()

        middleware._beat = beat  # ty: ignore[invalid-assignment]
        dispatched: list[str] = []

        async def call_next(_context: Any) -> str:
            dispatched.append("the tool call")
            return "the result"

        with pytest.raises(OwnerUnreachableError):
            await middleware.on_call_tool(MagicMock(), call_next)  # ty: ignore
        assert preflights == [] and dispatched == []

    async def test_a_bound_call_not_yet_sent_gets_no_client_for_a_written_off_owner(
        self, tmp_path: Path
    ):
        # A binding is made before the client is built. Until the request has
        # been sent, the owner it names is asked about again, and a burial that
        # landed meanwhile refuses the client with a not-sent failure naming
        # that owner rather than quietly swapping in another one.
        from linkedin_mcp_server.daemon_proxy import (
            OwnerFailure,
            OwnerUnreachableError,
            _call_being_made,
            _CallBinding,
        )

        owner = _attachment(tmp_path)
        backend = _backend(owner, tmp_path)
        marked = _call_being_made.set(_CallBinding("v1." + "a" * 32, owner))
        try:
            backend.note_failure(owner.descriptor.instance_id, OwnerFailure.RETIRING)
            with pytest.raises(OwnerUnreachableError) as refused:
                backend.open_client(timeout=1.0)
        finally:
            _call_being_made.reset(marked)

        assert refused.value.nothing_was_sent is True
        assert refused.value.instance_id == owner.descriptor.instance_id
        assert refused.value.classification is OwnerFailure.RETIRING

    async def test_a_second_attempts_burial_is_kept_for_the_next_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The second failure is not recovered from, but what it learned about
        # the replacement is not thrown away either.
        from linkedin_mcp_server.daemon_proxy import (
            FrontendOwnerRecoveryMiddleware,
            OwnerFailure,
            OwnerUnreachableError,
        )

        failed = _attachment(tmp_path)
        replacement = _attachment(tmp_path, port=failed.descriptor.port + 1)
        backend = _backend(failed, tmp_path)
        elections = 0

        def elect(*_args: Any, **_kwargs: Any):
            nonlocal elections
            elections += 1
            return _elected(replacement)

        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", elect)
        attempts = 0

        async def call_next(_context: Any) -> Any:
            nonlocal attempts
            attempts += 1
            owner = failed if attempts == 1 else replacement
            raise OwnerUnreachableError(
                instance_id=owner.descriptor.instance_id,
                nothing_was_sent=True,
                cause=RuntimeError("refused"),
                classification=OwnerFailure.RETIRING,
            )

        with pytest.raises(ToolError, match="could not be restarted"):
            await FrontendOwnerRecoveryMiddleware(backend).on_call_tool(
                MagicMock(),
                call_next,  # ty: ignore
            )

        assert attempts == 2
        assert elections == 1, "the second failure elected again"
        assert replacement.descriptor.instance_id in backend._unusable


class TestControlOnlyNeverRunsATool:
    """A pair proved for control is refused wherever a call could be sent."""

    @staticmethod
    def _control_only(tmp_path: Path) -> Attachment:
        import dataclasses

        return dataclasses.replace(_attachment(tmp_path), control_only=True)

    def test_a_backend_cannot_be_built_around_one(self, tmp_path: Path):
        with pytest.raises(ValueError, match="control only"):
            _backend(self._control_only(tmp_path), tmp_path)

    async def test_an_election_that_returns_one_is_not_adopted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        failed = _attachment(tmp_path)
        backend = _backend(failed, tmp_path)
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon_election.obtain_owner",
            lambda *_a, **_k: _elected(self._control_only(tmp_path)),
        )

        assert await backend.recover(failed.descriptor.instance_id) is None
        assert backend.attachment is failed

    def test_a_bound_call_cannot_open_a_client_to_one(self, tmp_path: Path):
        from linkedin_mcp_server.daemon_proxy import _call_being_made, _CallBinding

        backend = _backend(_attachment(tmp_path), tmp_path)
        marked = _call_being_made.set(
            _CallBinding("v1." + "b" * 32, self._control_only(tmp_path))
        )
        try:
            with pytest.raises(ValueError, match="control only"):
                backend.open_client(timeout=1.0)
        finally:
            _call_being_made.reset(marked)

    async def test_a_call_is_not_preflighted_or_sent_to_one(self, tmp_path: Path):
        from linkedin_mcp_server.daemon_proxy import FrontendCallHeartbeatMiddleware

        backend = _backend(_attachment(tmp_path), tmp_path)
        backend._attachment = self._control_only(tmp_path)
        middleware = FrontendCallHeartbeatMiddleware(backend)
        preflights: list[str] = []

        async def beat(*_args: Any) -> httpx2.Response:
            preflights.append("preflight")
            return _watched()

        middleware._beat = beat  # ty: ignore[invalid-assignment]
        dispatched: list[str] = []

        async def call_next(_context: Any) -> str:
            dispatched.append("the tool call")
            return "the result"

        with pytest.raises(ValueError, match="control only"):
            await middleware.on_call_tool(MagicMock(), call_next)  # ty: ignore
        assert preflights == [] and dispatched == []


class TestWhatOneCallCanCost:
    """The composed bound, counted through the three middlewares a proxy installs.

    Auth repair outermost, then recovery, then the heartbeat. The longest
    sequence that can still end: the first invocation fails once and is
    recovered, its repeat comes back asking for a sign-in, and the read-only
    replay fails twice. Every preflight, dispatch and election is counted
    separately. The bound is at most four preflights, four dispatches and four
    election joins; fewer elections run when no burial news arrives while one
    is in flight, or when flights are shared.
    """

    async def test_without_late_burial_news_a_call_costs_two_elections(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import daemon_auth
        from linkedin_mcp_server.daemon_auth import (
            MARKER_KEY,
            MARKER_VERSION,
            FrontendAuthRepairMiddleware,
        )
        from linkedin_mcp_server.daemon_proxy import (
            FrontendCallHeartbeatMiddleware,
            FrontendOwnerRecoveryMiddleware,
            OwnerUnreachableError,
            _call_being_made,
        )

        owners = [_attachment(tmp_path, port=51300 + n) for n in range(4)]
        backend = _backend(owners[0], tmp_path)
        elections = 0

        def elect(*_args: Any, **_kwargs: Any):
            nonlocal elections
            elections += 1
            return _elected(owners[elections])

        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", elect)

        async def repaired(*_args: Any) -> None:
            return None

        monkeypatch.setattr(daemon_auth, "_repair_auth_locally", repaired)

        preflights: list[str] = []

        async def beat(attachment: Attachment, _call_id: str) -> httpx2.Response:
            preflights.append(attachment.descriptor.instance_id)
            return _watched()

        heartbeat = FrontendCallHeartbeatMiddleware(backend)
        heartbeat._beat = beat  # ty: ignore[invalid-assignment]
        dispatches: list[str] = []

        async def dispatch(_context: Any) -> Any:
            bound = _call_being_made.get()
            assert bound is not None
            dispatches.append(bound.attachment.descriptor.instance_id)
            if len(dispatches) == 2:
                # The repeat reaches an owner whose session has expired.
                return ToolResult(
                    content=[mt.TextContent(type="text", text="sign in")],
                    meta={
                        MARKER_KEY: {
                            "v": MARKER_VERSION,
                            "reason": "stale",
                            "replayable": True,
                            "browser_open": False,
                            "generation": None,
                        }
                    },
                    is_error=True,
                )
            raise OwnerUnreachableError(
                instance_id=bound.attachment.descriptor.instance_id,
                nothing_was_sent=True,
                cause=httpx2.ConnectError("gone"),
            )

        recovery = FrontendOwnerRecoveryMiddleware(backend)

        async def through_the_heartbeat(context: Any) -> Any:
            return await heartbeat.on_call_tool(context, dispatch)  # ty: ignore

        async def through_recovery(context: Any) -> Any:
            return await recovery.on_call_tool(context, through_the_heartbeat)

        tool = MagicMock()
        tool.annotations = MagicMock(read_only_hint=True)
        context = MagicMock()
        context.message.name = "get_person_profile"
        context.fastmcp_context.fastmcp.get_tool = AsyncMock(return_value=tool)

        with pytest.raises(ToolError, match="could not be restarted"):
            await FrontendAuthRepairMiddleware(tool_timeout=30.0).on_call_tool(
                context,
                through_recovery,
            )

        assert len(preflights) == 4
        assert len(dispatches) == 4
        assert elections == 2
        # Every dispatch followed a preflight to the same owner, in order.
        assert dispatches == preflights

    async def test_burial_news_during_each_election_raises_it_to_four(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The same sequence, with an owner written off while each recovery waits.

        Each recovery may run one more election for news its first election did
        not know, and auth repair can enter recovery twice, so four is the
        ceiling. The news arrives from another thread, the way the event loop
        would deliver another call's burial while the election runs.
        """
        from linkedin_mcp_server import daemon_auth
        from linkedin_mcp_server.daemon_auth import (
            MARKER_KEY,
            MARKER_VERSION,
            FrontendAuthRepairMiddleware,
        )
        from linkedin_mcp_server.daemon_proxy import (
            FrontendCallHeartbeatMiddleware,
            FrontendOwnerRecoveryMiddleware,
            OwnerFailure,
            OwnerUnreachableError,
            _call_being_made,
        )

        owners = [_attachment(tmp_path, port=51400 + n) for n in range(3)]
        backend = _backend(owners[0], tmp_path)
        loop = asyncio.get_running_loop()
        told_to_bury: list[int] = []

        def elect(*_args: Any, buried: Any = frozenset(), **_kwargs: Any):
            told_to_bury.append(len(buried))
            number = len(told_to_bury)
            if number in (1, 3):
                # The owner this election is about to return is found retiring
                # by another call while it runs.
                owner = owners[(number - 1) // 2]
                learned = threading.Event()

                def learn() -> None:
                    backend.note_failure(
                        owner.descriptor.instance_id, OwnerFailure.RETIRING
                    )
                    learned.set()

                loop.call_soon_threadsafe(learn)
                assert learned.wait(timeout=5)
                return _elected(owner)
            return _elected(owners[number // 2])

        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", elect)

        async def repaired(*_args: Any) -> None:
            return None

        monkeypatch.setattr(daemon_auth, "_repair_auth_locally", repaired)
        preflights: list[str] = []

        async def beat(attachment: Attachment, _call_id: str) -> httpx2.Response:
            preflights.append(attachment.descriptor.instance_id)
            return _watched()

        heartbeat = FrontendCallHeartbeatMiddleware(backend)
        heartbeat._beat = beat  # ty: ignore[invalid-assignment]
        dispatches: list[str] = []
        written_off_at_dispatch: list[bool] = []

        async def dispatch(_context: Any) -> Any:
            bound = _call_being_made.get()
            assert bound is not None
            dispatches.append(bound.attachment.descriptor.instance_id)
            written_off_at_dispatch.append(
                bound.attachment.descriptor.instance_id in backend._unusable
            )
            if len(dispatches) == 2:
                return ToolResult(
                    content=[mt.TextContent(type="text", text="sign in")],
                    meta={
                        MARKER_KEY: {
                            "v": MARKER_VERSION,
                            "reason": "stale",
                            "replayable": True,
                            "browser_open": False,
                            "generation": None,
                        }
                    },
                    is_error=True,
                )
            raise OwnerUnreachableError(
                instance_id=bound.attachment.descriptor.instance_id,
                nothing_was_sent=True,
                cause=httpx2.ConnectError("gone"),
            )

        recovery = FrontendOwnerRecoveryMiddleware(backend)

        async def through_the_heartbeat(context: Any) -> Any:
            return await heartbeat.on_call_tool(context, dispatch)  # ty: ignore

        async def through_recovery(context: Any) -> Any:
            return await recovery.on_call_tool(context, through_the_heartbeat)

        tool = MagicMock()
        tool.annotations = MagicMock(read_only_hint=True)
        context = MagicMock()
        context.message.name = "get_person_profile"
        context.fastmcp_context.fastmcp.get_tool = AsyncMock(return_value=tool)

        with pytest.raises(ToolError, match="could not be restarted"):
            await FrontendAuthRepairMiddleware(tool_timeout=30.0).on_call_tool(
                context,
                through_recovery,
            )

        assert len(preflights) == 4
        assert len(dispatches) == 4
        assert told_to_bury == [0, 1, 1, 2]
        assert dispatches == preflights
        # No dispatch went to an owner already written off when it was sent.
        assert written_off_at_dispatch == [False] * 4


class _HeldAtConnect(FastMCPTransport):
    """An owner whose connection is held open until a test lets it proceed.

    The window between a call's binding and its send: the client is being built
    and initialized, and another call can write the owner off meanwhile.
    """

    def __init__(self, server: FastMCP) -> None:
        super().__init__(server)
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    @asynccontextmanager
    async def connect_session(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.reached.set()
        await self.release.wait()
        async with super().connect_session(**kwargs) as session:
            yield session


class TestBurialAfterTheLastCheck:
    """An owner written off between a call's checks and its send gets no request.

    Every check before an await is stale after it. These pin the two awaits a
    new request crosses after choosing its owner, the preflight and the client
    setup, and the control that a request already sent keeps its owner.
    """

    @staticmethod
    def _context() -> MagicMock:
        context = MagicMock()
        context.message.name = "send_connection_request"
        return context

    async def test_a_burial_during_a_successful_preflight_stops_the_dispatch(
        self, tmp_path: Path
    ):
        # The first call's preflight is held. Meanwhile a second call finds the
        # same owner retiring, writes it off and recovers to a replacement.
        # The first preflight then returns its go-ahead, which is older than
        # that news.
        from unittest.mock import patch

        from linkedin_mcp_server import daemon_election
        from linkedin_mcp_server.daemon_proxy import (
            FrontendCallHeartbeatMiddleware,
            FrontendOwnerRecoveryMiddleware,
            OwnerFailure,
            OwnerUnreachableError,
            _call_being_made,
        )

        old = _attachment(tmp_path)
        new = _attachment(tmp_path, port=old.descriptor.port + 1)
        backend = _backend(old, tmp_path)
        waiting, release = asyncio.Event(), asyncio.Event()

        first = FrontendCallHeartbeatMiddleware(backend)

        async def held_beat(_attachment: Attachment, _call_id: str) -> httpx2.Response:
            waiting.set()
            await release.wait()
            return _watched()

        first._beat = held_beat  # ty: ignore[invalid-assignment]
        dispatched: list[str] = []

        async def dispatch(_context: Any) -> str:
            bound = _call_being_made.get()
            assert bound is not None
            dispatched.append(bound.attachment.descriptor.instance_id)
            return "the result"

        call = asyncio.create_task(first.on_call_tool(self._context(), dispatch))  # ty: ignore
        await asyncio.wait_for(waiting.wait(), timeout=5)

        second = FrontendCallHeartbeatMiddleware(backend)
        second._beat = _answers(
            **{
                old.descriptor.instance_id: _retiring(old.descriptor.instance_id),
                new.descriptor.instance_id: _watched(),
            }
        )

        async def inner(context: Any) -> Any:
            return await second.on_call_tool(context, dispatch)  # ty: ignore

        with patch.object(daemon_election, "obtain_owner", return_value=_elected(new)):
            await FrontendOwnerRecoveryMiddleware(backend).on_call_tool(
                self._context(),
                inner,
            )
        assert old.descriptor.instance_id in backend._unusable

        release.set()
        with pytest.raises(OwnerUnreachableError) as refused:
            await asyncio.wait_for(call, timeout=5)

        assert dispatched == [new.descriptor.instance_id]
        assert refused.value.nothing_was_sent is True
        assert refused.value.instance_id == old.descriptor.instance_id
        assert refused.value.classification is OwnerFailure.RETIRING

    async def test_a_burial_during_client_setup_stops_the_send(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The call is bound and its client is being initialized when another
        # call writes the owner off. The client is real and the owner is an
        # in-process server, so a request that got through would run its tool.
        from linkedin_mcp_server.daemon_proxy import (
            FrontendCallHeartbeatMiddleware,
            OwnerFailure,
            OwnerUnreachableError,
        )

        ran: list[str] = []
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            ran.append("sent")
            return "sent"

        held = _HeldAtConnect(owner)
        _reach_owners_in_process(monkeypatch, lambda _url: held)
        attachment = _attachment(tmp_path)
        backend = _backend(attachment, tmp_path)
        heartbeat = FrontendCallHeartbeatMiddleware(backend)

        async def dispatch(_context: Any) -> Any:
            client = backend.open_client(timeout=5.0)
            async with client:
                return await client.call_tool_mcp("send_connection_request", {})

        call = asyncio.create_task(heartbeat.on_call_tool(self._context(), dispatch))  # ty: ignore
        await asyncio.wait_for(held.reached.wait(), timeout=5)
        backend.note_failure(attachment.descriptor.instance_id, OwnerFailure.RETIRING)
        held.release.set()

        with pytest.raises(OwnerUnreachableError) as refused:
            await asyncio.wait_for(call, timeout=5)

        assert ran == []
        assert refused.value.nothing_was_sent is True
        assert refused.value.instance_id == attachment.descriptor.instance_id
        assert refused.value.classification is OwnerFailure.RETIRING

    async def test_a_request_already_sent_keeps_its_owner_and_its_heartbeats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The positive control. The request reached the owner and is running
        # when the owner is written off and a replacement adopted. Moving its
        # heartbeats, or refusing it now, would get a live call cancelled by the
        # owner running it.
        from linkedin_mcp_server.daemon_proxy import (
            FrontendCallHeartbeatMiddleware,
            OwnerFailure,
        )

        started, finish = asyncio.Event(), asyncio.Event()
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            started.set()
            await finish.wait()
            return "sent"

        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        monkeypatch.setattr("linkedin_mcp_server.daemon_proxy.HEARTBEAT_SECONDS", 0.05)
        beats: list[str] = []

        async def beat(attachment: Attachment, _call_id: str) -> httpx2.Response:
            beats.append(attachment.descriptor.instance_id)
            return _watched()

        monkeypatch.setattr(
            FrontendCallHeartbeatMiddleware, "_beat", staticmethod(beat)
        )
        old = _attachment(tmp_path)
        backend = _backend(old, tmp_path)
        heartbeat = FrontendCallHeartbeatMiddleware(backend)

        async def dispatch(_context: Any) -> Any:
            client = backend.open_client(timeout=5.0)
            async with client:
                return await client.call_tool_mcp("send_connection_request", {})

        call = asyncio.create_task(heartbeat.on_call_tool(self._context(), dispatch))  # ty: ignore
        await asyncio.wait_for(started.wait(), timeout=5)
        backend.note_failure(old.descriptor.instance_id, OwnerFailure.RETIRING)
        backend._attachment = _attachment(tmp_path, port=old.descriptor.port + 1)
        beats_at_burial = len(beats)
        await asyncio.sleep(0.3)
        finish.set()
        result = await asyncio.wait_for(call, timeout=5)

        assert result.is_error is False
        assert len(beats) - beats_at_burial >= 2, "the heartbeats stopped"
        assert set(beats) == {old.descriptor.instance_id}


class TestTheSendBoundary:
    """The last burial check runs where the session takes the request.

    fastmcp's session monitor runs each request as a task of its own, so a
    check made before handing it the request is older than the send by at
    least one turn of the loop, and a burial already queued runs in that turn.
    These queue the burial exactly there, with the real monitor and a real
    in-process owner, and add no await of their own.
    """

    async def test_a_request_the_monitor_never_starts_is_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The installed monitor refuses before starting its coroutine once the
        # session has ended, and closes what it was given: the wrapper. The
        # request the wrapper holds must be closed too, not left unawaited, and
        # a request that never started was never sent.
        from linkedin_mcp_server.daemon_proxy import OwnerUnreachableError

        ran: list[str] = []
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            ran.append("sent")
            return "sent"

        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        client = _backend(_attachment(tmp_path), tmp_path).open_client(timeout=5.0)

        async def refuse_unstarted(_self: Any, coro: Any) -> NoReturn:
            coro.close()
            raise RuntimeError("the session has already ended")

        async with client:
            monkeypatch.setattr(
                ProxyClient, "_await_with_session_monitoring", refuse_unstarted
            )
            pending = client.session.call_tool("send_connection_request", {})
            with pytest.raises(OwnerUnreachableError) as refused:
                await client._await_with_session_monitoring(pending)
            started = getattr(client.session, "tool_request_started", None)

        assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED
        assert started is False
        assert refused.value.nothing_was_sent is True
        assert isinstance(refused.value.__cause__, RuntimeError)
        assert ran == []

    @pytest.mark.parametrize("buried", [True, False], ids=["buried", "control"])
    async def test_a_burial_queued_as_the_monitor_takes_the_send_stops_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, buried: bool
    ):
        from linkedin_mcp_server.daemon_proxy import (
            FrontendCallHeartbeatMiddleware,
            OwnerFailure,
            OwnerUnreachableError,
        )

        ran: list[str] = []
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            ran.append("sent")
            return "sent"

        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        old = _attachment(tmp_path)
        backend = _backend(old, tmp_path)
        heartbeat = FrontendCallHeartbeatMiddleware(backend)
        loop = asyncio.get_running_loop()

        async def dispatch(_context: Any) -> Any:
            client = backend.open_client(timeout=5.0)
            async with client:
                # The monitored path, not the one it takes with no session task.
                assert client._session_state.session_task is not None
                monitor = client._await_with_session_monitoring

                async def burial_queued_first(coro: Any, **kwargs: Any) -> Any:
                    if buried:
                        loop.call_soon(
                            backend.note_failure,
                            old.descriptor.instance_id,
                            OwnerFailure.RETIRING,
                        )
                    return await monitor(coro, **kwargs)

                monkeypatch.setattr(
                    client, "_await_with_session_monitoring", burial_queued_first
                )
                return await client.call_tool_mcp("send_connection_request", {})

        context = MagicMock()
        context.message.name = "send_connection_request"
        if not buried:
            result = await heartbeat.on_call_tool(context, dispatch)  # ty: ignore
            assert ran == ["sent"]
            assert getattr(result, "is_error", None) is False
            return

        with pytest.raises(OwnerUnreachableError) as refused:
            await heartbeat.on_call_tool(context, dispatch)  # ty: ignore

        assert ran == [], "the request went to an owner written off before the send"
        assert refused.value.nothing_was_sent is True
        assert refused.value.instance_id == old.descriptor.instance_id
        assert refused.value.classification is OwnerFailure.RETIRING

    async def test_a_call_already_sent_is_not_refused_on_its_own_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Once the bound call's tool request is out, its owner is fixed, and a
        # later request on the same client goes to that owner even after it was
        # written off: refusing it would abandon a call the owner may still be
        # running for this frontend.
        from linkedin_mcp_server.daemon_proxy import (
            OwnerFailure,
            _call_being_made,
            _CallBinding,
        )

        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            return "sent"

        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        attachment = _attachment(tmp_path)
        backend = _backend(attachment, tmp_path)
        binding = _CallBinding("a-call", attachment)
        marked = _call_being_made.set(binding)
        try:
            client = backend.open_client(timeout=5.0)
        finally:
            _call_being_made.reset(marked)

        async with client:
            await client.call_tool_mcp("send_connection_request", {})
            backend.note_failure(
                attachment.descriptor.instance_id, OwnerFailure.RETIRING
            )
            listed = await client.list_tools()

        assert binding.dispatch.sent is True
        assert [tool.name for tool in listed] == ["send_connection_request"]

    async def test_a_listing_to_an_owner_written_off_during_setup_is_not_sent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # An unbound listing picks its owner when the client is built. An owner
        # written off while that client is being initialized gets no listing:
        # a same-configuration older build stays attachable until it turns
        # over, and its tool schemas may differ from its replacement's.
        from fastmcp.server.middleware import Middleware as ServerMiddleware

        from linkedin_mcp_server.daemon_proxy import (
            OwnerFailure,
            OwnerUnreachableError,
        )

        listed: list[str] = []

        class CountsListings(ServerMiddleware):
            async def on_list_tools(self, context: Any, call_next: Any) -> Any:
                listed.append("tools/list")
                return await call_next(context)

        owner = FastMCP("owner")
        owner.add_middleware(CountsListings())

        @owner.tool
        async def get_person_profile() -> str:
            return "profile"

        held = _HeldAtConnect(owner)
        _reach_owners_in_process(monkeypatch, lambda _url: held)
        attachment = _attachment(tmp_path)
        backend = _backend(attachment, tmp_path)
        client = backend.open_client(timeout=5.0)

        async def listing() -> Any:
            async with client:
                return await client.list_tools_mcp()

        listing_task = asyncio.create_task(listing())
        await asyncio.wait_for(held.reached.wait(), timeout=5)
        backend.note_failure(attachment.descriptor.instance_id, OwnerFailure.RETIRING)
        held.release.set()

        with pytest.raises(OwnerUnreachableError) as refused:
            await asyncio.wait_for(listing_task, timeout=5)

        assert listed == []
        assert refused.value.nothing_was_sent is True
        assert refused.value.instance_id == attachment.descriptor.instance_id
        assert refused.value.classification is OwnerFailure.RETIRING


def _causes(failure: BaseException) -> list[type[BaseException]]:
    """The type of every exception in *failure*'s ``__cause__`` chain."""
    chain: list[type[BaseException]] = []
    current: BaseException | None = failure
    while current is not None:
        chain.append(type(current))
        current = current.__cause__
    return chain


class _HandsOverAWrapper(FastMCPTransport):
    """A transport that gives the client something other than the session it built.

    What a transport that ignored the requested session class would amount
    to, and what a wrapping test double used to be. Setting the binding on
    this object would succeed and claim nothing.
    """

    def __init__(self, server: FastMCP) -> None:
        super().__init__(server)
        self.closed = False

    @asynccontextmanager
    async def connect_session(self, **kwargs: Any) -> AsyncIterator[Any]:
        try:
            async with super().connect_session(**kwargs) as session:
                yield _Delegating(session)
        finally:
            self.closed = True


class _Delegating:
    def __init__(self, session: Any) -> None:
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


class TestTheSessionClaimsTheToolRequest:
    """The tool request is claimed where the SDK takes it, in either era.

    On the 2026-07-28 era the provider calls the session itself and never
    reaches the client's `call_tool_mcp`, so the claim has to live on the
    session, and only the tool request may make it: a listing or the
    negotiation changes nothing and stays repeatable.
    """

    @staticmethod
    def _bound_client(backend: DaemonProxyBackend, attachment: Attachment) -> Any:
        """A client opened the way a forwarded call opens one, and its binding."""
        from linkedin_mcp_server.daemon_proxy import _call_being_made, _CallBinding

        binding = _CallBinding("a-call", attachment)
        marked = _call_being_made.set(binding)
        try:
            return backend.open_client(timeout=5.0), binding
        finally:
            _call_being_made.reset(marked)

    @pytest.mark.parametrize("over", ["memory", "http"])
    @_HOP_ERAS
    async def test_the_client_talks_through_the_session_that_claims(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        over: str,
        legacy_only: bool,
        protocol: str,
    ):
        """The session the client ends up with, and what it has claimed when.

        Read where the owner takes the call, before it acts: in the tool body
        of an in-process owner, and as the request arrives at one over HTTP.
        """
        from linkedin_mcp_server.daemon_proxy import ClaimsTheToolRequest

        attachment = _attachment(tmp_path)
        backend = _backend(attachment, tmp_path)
        seen_by_the_owner: list[tuple[bool, bool]] = []

        def witness() -> None:
            seen_by_the_owner.append(
                (binding.dispatch.sent, client.session.tool_request_started)
            )

        if over == "memory":
            owner = FastMCP("owner")

            @owner.tool(name="send_connection_request")
            async def send() -> str:
                witness()
                return "sent"

            _reach_owners_in_process(
                monkeypatch, lambda _url: owner, legacy_only=legacy_only
            )
        else:
            http_owner = _OwnerOverHttp(
                era="handshake" if legacy_only else "2026-07-28"
            )
            http_owner.on_call = witness
            _serve_over_http(monkeypatch, lambda _address: http_owner)
        client, binding = self._bound_client(backend, attachment)

        async with client:
            session = client.session
            assert type(session) is ClaimsTheToolRequest
            assert session.binding is binding
            # The option layer the proxy set is kept, not rebuilt around the
            # session class: forwarding is what carries the caller's headers.
            assert client._transport_options.forward_incoming_headers is True
            assert client.protocol_version == protocol
            assert not binding.dispatch.sent, "the negotiation claimed the call"
            assert not session.tool_request_started
            await client.list_tools()
            assert not binding.dispatch.sent, "a listing claimed the call"
            assert not session.tool_request_started
            result = await client.call_tool_mcp("send_connection_request", {})

        assert result.is_error is False
        assert seen_by_the_owner == [(True, True)]

    @_HOP_ERAS
    async def test_the_provider_claims_the_call_on_either_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        legacy_only: bool,
        protocol: str,
    ):
        """Through the whole proxy, where the era decides the provider's path.

        On the handshake era `ProxyTool.run` goes through `call_tool_mcp`; on
        the 2026-07-28 era it calls the session directly. Both have to arrive
        claimed, and every listing around them unclaimed.
        """
        from linkedin_mcp_server.server import create_mcp_server
        from linkedin_mcp_server.server_role import ServerRole

        sessions = _sessions_opened(monkeypatch)
        at_the_effect: list[list[tuple[bool, bool | None]]] = []
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send(message: str) -> str:
            at_the_effect.append(
                [
                    (
                        session.tool_request_started,
                        None
                        if session.binding is None
                        else session.binding.dispatch.sent,
                    )
                    for session in sessions
                ]
            )
            return message

        _reach_owners_in_process(
            monkeypatch, lambda _url: owner, legacy_only=legacy_only
        )
        backend = _backend(_attachment(tmp_path), tmp_path)
        proxy = create_mcp_server(
            role=ServerRole.PROXY, proxy_backend=backend, tool_timeout=5.0
        )

        async with Client(proxy) as client:
            result = await client.call_tool(
                "send_connection_request", {"message": "hi"}
            )

        assert result.data == "hi"
        # One session took the tool request, and it was claimed for the bound
        # call before the owner acted. Every other one only listed, the
        # provider's own lookup inside the same call among them.
        (seen,) = at_the_effect
        assert [started for started, _sent in seen].count(True) == 1, seen
        assert (True, True) in seen, seen
        called = [s for s in sessions if s.tool_request_started]
        assert [s.protocol_version for s in called] == [protocol]
        assert len(sessions) > len(called), "no listing went through a session"

    async def test_a_session_the_client_did_not_ask_for_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Refused at entry, and the connection it came on is closed.

        A client whose transport ignored the session class would send its
        tool request unclaimed. It fails before the caller can use it, and a
        failed entry gets no exit of its own, so it closes what it opened.
        """
        ran: list[str] = []
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            ran.append("sent")
            return "sent"

        transport = _HandsOverAWrapper(owner)
        _reach_owners_in_process(monkeypatch, lambda _url: transport)
        attachment = _attachment(tmp_path)
        client, _binding = self._bound_client(
            _backend(attachment, tmp_path), attachment
        )

        with pytest.raises(TypeError, match="claims its tool requests"):
            async with client:
                await client.call_tool_mcp("send_connection_request", {})

        assert transport.closed, "the refused connection was left open"
        assert ran == []

    async def test_a_session_that_died_of_a_failed_connect_is_no_proof(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A connect error the session died of says nothing about the request.

        The owner is acting on the call when the session task ends in a
        connect error, and the monitor reports that error as the cause. It is
        about some later exchange on the session, not the tool request, and
        read as proof it would let a call that ran be sent again.

        The session task is stood in for at the one place the monitor reads
        it, because a real transport fails the outstanding request first and
        the monitor then never sees the connect error at all.
        """
        from linkedin_mcp_server.daemon_proxy import OwnerUnreachableError

        ran: list[str] = []
        died: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            ran.append("sent")
            died.set_exception(httpx2.ConnectError("could not reach the owner again"))
            await asyncio.Event().wait()
            return "never"

        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        attachment = _attachment(tmp_path)
        client, binding = self._bound_client(_backend(attachment, tmp_path), attachment)

        async with client:
            running = client._session_state.session_task
            client._session_state.session_task = died
            try:
                with pytest.raises(OwnerUnreachableError) as failed:
                    await client.call_tool_mcp("send_connection_request", {})
            finally:
                client._session_state.session_task = running

        assert ran == ["sent"]
        assert binding.dispatch.sent is True
        assert failed.value.nothing_was_sent is False
        # The fault this pins was really delivered: a connect error, in the
        # chain the tag was made from.
        assert httpx2.ConnectError in _causes(failed.value), _causes(failed.value)

    async def test_what_a_session_sent_outlives_the_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A request after the session ended still knows the call went out.

        The call ran and answered, and then the session ended. The client
        clears its session state as it does, and a monitored request made
        afterwards is refused before it starts. Nothing being left to read at
        that point is not evidence that nothing was sent: this client did send
        a tool request, and the refusal has to say it may have arrived.
        """
        from linkedin_mcp_server.daemon_proxy import OwnerUnreachableError

        ran: list[str] = []
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            ran.append("sent")
            return "sent"

        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        client = _backend(_attachment(tmp_path), tmp_path).open_client(timeout=5.0)

        async def later() -> str:
            return "never reached"

        async with client:
            await client.call_tool_mcp("send_connection_request", {})
            running = client._session_state.session_task
            assert running is not None
            client._session_state.stop_event.set()
            await asyncio.wait_for(asyncio.wait([running]), timeout=5)
            assert client._session_state.session is None
            with pytest.raises(OwnerUnreachableError) as refused:
                await client._await_with_session_monitoring(later())

        assert ran == ["sent"]
        assert refused.value.nothing_was_sent is False

    async def test_a_caller_that_gives_up_mid_call_stays_cancelled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Giving up while the owner works is a cancellation, not an owner loss.

        Converted into a tagged failure it would be recovered from, and a
        caller that had walked away would be answered or, worse, repeated for.
        """
        started = asyncio.Event()
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            started.set()
            await asyncio.Event().wait()
            return "never"

        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        attachment = _attachment(tmp_path)
        client, binding = self._bound_client(_backend(attachment, tmp_path), attachment)

        async def call() -> Any:
            async with client:
                return await client.call_tool_mcp("send_connection_request", {})

        calling = asyncio.create_task(call())
        await asyncio.wait_for(started.wait(), timeout=5)
        calling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await calling

        assert binding.dispatch.sent is True


#: How each SDK-written stand-in is provoked: what the owner's side of the HTTP
#: exchange sends back. ``mcp/client/streamable_http.py`` at mcp 2.2.0 turns
#: every one of these into an error it writes itself; none is a JSON-RPC error
#: the owner sent.
_STAND_INS: dict[str, Callable[[dict[str, Any]], httpx2.Response]] = {
    "401": lambda _body: httpx2.Response(401, text="unauthorized"),
    "500": lambda _body: httpx2.Response(500, text="failed"),
    "404": lambda _body: httpx2.Response(404, text="missing"),
    "202": lambda _body: httpx2.Response(202),
    "malformed json": lambda _body: httpx2.Response(
        200, content=b"not-json", headers={"content-type": "application/json"}
    ),
    "sse ended": lambda _body: httpx2.Response(
        200, content=b"", headers={"content-type": "text/event-stream"}
    ),
}


def _a_genuine_refusal(body: dict[str, Any]) -> httpx2.Response:
    """A real JSON-RPC error body at a non-2xx status, which the SDK forwards.

    ``INVALID_REQUEST``, the code the SDK also writes for "Session terminated",
    so only the message tells the two apart.
    """
    return httpx2.Response(
        400,
        json={
            "jsonrpc": "2.0",
            "id": body["id"],
            "error": {"code": mt.INVALID_REQUEST, "message": "Some other refusal"},
        },
    )


def _refused(_body: dict[str, Any]) -> NoReturn:
    """An owner whose port no longer accepts connections."""
    raise httpx2.ConnectError("the owner's port refused the connection")


class _OwnerOverHttp:
    """An owner at the far end of the SDK's own HTTP client, answering by hand.

    Installed as an ``httpx2.MockTransport`` under the production client
    factory, so everything between ``call_tool_mcp`` and the bytes is real:
    the SDK's streamable HTTP transport and session, FastMCP's client and the
    owner-tagging subclass. The owner is what stands in, and it is small enough
    to fail on command: *fail* maps a JSON-RPC method to the response it gets.
    No socket is opened and no process dies here: these are fault injections
    into the SDK's HTTP handling. Losing a real owner process is witnessed by
    the loopback owners in ``tests/test_daemon_election.py``.

    *effects* counts the tool calls this owner ran. *session* decides whether
    the initialize hands out a session id, which is what decides whether the
    SDK reads a 404 as a lost session or as an unknown method.

    *era* is what the owner speaks. ``"2026-07-28"`` answers ``server/discover``
    with a result that passes the strict schema of that version, and stamps
    every later result the way that version requires; a result missing those
    fields is not evidence of the era, and a client quietly takes the
    handshake instead (``fastmcp.client.client._conformant_discover_only``).
    ``"handshake"`` answers ``server/discover`` as an unknown method, which is
    what an owner of that era sends. *methods* is every JSON-RPC method that
    arrived, in order.
    """

    def __init__(
        self,
        *,
        fail: dict[str, Callable[[dict[str, Any]], Any]] | None = None,
        session: bool = True,
        era: str = "2026-07-28",
    ) -> None:
        self.fail = dict(fail or {})
        self.session = session
        self.era = era
        self.effects = 0
        self.requests: list[httpx2.Request] = []
        self.methods: list[str] = []
        #: Called as a tool request arrives, before it runs.
        self.on_call: Callable[[], None] | None = None

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        import json

        from linkedin_mcp_server.daemon_liveness import HEARTBEAT_PATH

        self.requests.append(request)
        if request.url.path == HEARTBEAT_PATH:
            return httpx2.Response(200, json={"watched": False})
        if request.method != "POST":
            return httpx2.Response(405)
        body = json.loads(request.content)
        if "id" not in body:
            return httpx2.Response(202)
        self.methods.append(body["method"])
        if body["method"] == "tools/call" and self.on_call is not None:
            self.on_call()
        failing = self.fail.get(body["method"])
        if failing is not None:
            answer = failing(body)
            return await answer if inspect.isawaitable(answer) else answer
        return self.answer(body)

    def answer(self, body: dict[str, Any]) -> httpx2.Response:
        """What a healthy owner sends back for *body*."""
        method = body["method"]
        modern = self.era == "2026-07-28"
        headers: dict[str, str] = {}
        if method == "server/discover" and modern:
            result: dict[str, Any] = {
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "owner", "version": "1"},
                "resultType": "complete",
                "ttlMs": 0,
                "cacheScope": "private",
            }
        elif method == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "owner", "version": "1"},
            }
            if self.session:
                headers["mcp-session-id"] = "owner-session"
        elif method == "tools/list":
            tool = {
                "name": "send_connection_request",
                "inputSchema": {"type": "object"},
            }
            result = {"tools": [tool]}
            if modern:
                result.update(resultType="complete", ttlMs=0, cacheScope="private")
        elif method == "tools/call":
            self.effects += 1
            result = {"content": [{"type": "text", "text": "sent"}]}
            if modern:
                result["resultType"] = "complete"
        else:
            error = {"code": mt.METHOD_NOT_FOUND, "message": "Method not found"}
            return httpx2.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "error": error}
            )
        return httpx2.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": result},
            headers=headers,
        )

    def after_running(
        self, then: Callable[[dict[str, Any]], httpx2.Response]
    ) -> Callable[[dict[str, Any]], httpx2.Response]:
        """Run the call, then answer it with *then* instead of its result."""

        def answer(body: dict[str, Any]) -> httpx2.Response:
            self.effects += 1
            return then(body)

        return answer


def _serve_over_http(
    monkeypatch: pytest.MonkeyPatch, owner_at: Callable[[str], _OwnerOverHttp]
) -> None:
    """Send every loopback request to the owner *owner_at* names for its address.

    Through ``daemon_owner.direct_async_http_client`` itself, wrapped rather
    than replaced, so its forced ``trust_env=False`` stays on the path. The
    heartbeat preflight takes the same factory and arrives here too.
    """
    from linkedin_mcp_server import daemon_owner

    real = daemon_owner.direct_async_http_client

    async def route(request: httpx2.Request) -> httpx2.Response:
        return await owner_at(f"{request.url.host}:{request.url.port}")(request)

    def factory(**kwargs: Any) -> httpx2.AsyncClient:
        return real(transport=httpx2.MockTransport(route), **kwargs)

    monkeypatch.setattr(daemon_owner, "direct_async_http_client", factory)


def _address(attachment: Attachment) -> str:
    return f"{attachment.descriptor.host}:{attachment.descriptor.port}"


#: Both eras an owner at the far end of HTTP may speak, and the protocol the
#: hop must land on with each. Written out rather than read back, so an owner
#: stand-in that silently fell back could not pass as the modern case.
_OWNER_ERAS = pytest.mark.parametrize(
    ("era", "protocol"),
    [("2026-07-28", "2026-07-28"), ("handshake", "2025-11-25")],
    ids=["2026-07-28 owner", "handshake owner"],
)


class TestTheSdksOwnHttpErrors:
    """SDK v2 answers a failed HTTP exchange with an error it writes itself.

    V1 raised on a non-2xx status and failed the whole transport; v2 turns it
    into a JSON-RPC error for that one request (SDK ``docs/migration.md:2178``),
    which reaches this client as the same ``MCPError`` a real answer does.
    Driven through the real SDK transport, because the stand-in is only ever
    written there: a hand-made ``MCPError`` would pin the rule and not the
    provenance it is about.

    Each phase separately, against an owner of each era. Entry fails only when
    every way in does: on this client that is the discovery probe and the
    handshake it falls back to. Entry and listing may always be repeated and
    say so; once the tool request was claimed nothing here proves it did not
    arrive, so the call says it may have.
    """

    @staticmethod
    async def _fail_at(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        phase: str,
        respond: Callable[[dict[str, Any]], Any],
        *,
        era: str,
        session: bool = True,
    ) -> tuple[BaseException, _OwnerOverHttp, str | None]:
        methods = {
            "entry": ("server/discover", "initialize"),
            "list": ("tools/list",),
            "call": ("tools/call",),
        }
        owner = _OwnerOverHttp(
            fail=dict.fromkeys(methods[phase], respond), session=session, era=era
        )
        _serve_over_http(monkeypatch, lambda _address: owner)
        client = _backend(_attachment(tmp_path), tmp_path).open_client(timeout=2.0)
        protocol: str | None = None
        with pytest.raises(Exception) as failed:
            async with client:
                protocol = client.protocol_version
                if phase == "list":
                    await client.list_tools_mcp()
                elif phase == "call":
                    await client.call_tool_mcp("send_connection_request", {})
        return failed.value, owner, protocol

    @_OWNER_ERAS
    @pytest.mark.parametrize("phase", ["entry", "list", "call"])
    @pytest.mark.parametrize("stand_in", sorted(_STAND_INS))
    async def test_a_stand_in_is_not_taken_for_the_owners_answer(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        phase: str,
        stand_in: str,
        era: str,
        protocol: str,
    ):
        from linkedin_mcp_server.daemon_proxy import (
            OwnerFailure,
            OwnerUnreachableError,
        )

        failure, owner, negotiated = await self._fail_at(
            tmp_path, monkeypatch, phase, _STAND_INS[stand_in], era=era
        )

        assert isinstance(failure, OwnerUnreachableError), (
            f"the SDK's own {stand_in} error passed as the owner's answer: {failure!r}"
        )
        assert failure.classification is OwnerFailure.OWNER_ERROR
        # The boundary answers the dispatch question, never the stand-in: only
        # the call had claimed a request, and nothing says it did not arrive.
        assert failure.nothing_was_sent is (phase != "call")
        assert owner.effects == 0
        assert negotiated == (None if phase == "entry" else protocol)

    @pytest.mark.parametrize("phase", ["list", "call"])
    @pytest.mark.parametrize(
        ("era", "session", "written"),
        [
            ("handshake", True, "Session terminated"),
            ("handshake", False, "Not Found"),
            ("2026-07-28", False, "Not Found"),
        ],
        ids=["with a session", "before a session", "the 2026-07-28 era"],
    )
    async def test_a_404_is_a_stand_in_with_a_session_and_without(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        phase: str,
        era: str,
        session: bool,
        written: str,
    ):
        """The same status, two stand-ins, and neither is the owner's answer.

        With a session the SDK writes ``INVALID_REQUEST`` "Session terminated";
        without one, ``METHOD_NOT_FOUND`` "Not Found", whose code is also how a
        server says it has no listing of a kind. The 2026-07-28 era has no
        session at all, so only the second can happen there.
        """
        from linkedin_mcp_server.daemon_proxy import OwnerUnreachableError

        failure, _owner, _protocol = await self._fail_at(
            tmp_path, monkeypatch, phase, _STAND_INS["404"], era=era, session=session
        )

        assert isinstance(failure, OwnerUnreachableError), failure
        assert str(failure.__cause__) == written

    @_OWNER_ERAS
    @pytest.mark.parametrize("phase", ["entry", "list", "call"])
    async def test_a_real_refusal_in_an_error_status_is_the_owners_answer(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        phase: str,
        era: str,
        protocol: str,
    ):
        """A JSON-RPC error body survives a non-2xx status, and is an answer.

        Its code is the one the SDK writes for a lost session, so what keeps
        this an answer is the message, compared whole.
        """
        from mcp import MCPError

        failure, owner, _protocol = await self._fail_at(
            tmp_path, monkeypatch, phase, _a_genuine_refusal, era=era
        )

        assert isinstance(failure, MCPError), (
            f"a real refusal was retagged: {failure!r}"
        )
        assert failure.code == mt.INVALID_REQUEST
        assert failure.message == "Some other refusal"
        assert owner.effects == 0

    @_OWNER_ERAS
    async def test_a_call_that_outlived_the_deadline_is_not_the_owners_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, era: str, protocol: str
    ):
        """The session's own deadline: ``REQUEST_TIMEOUT``, not HTTP 408."""
        from linkedin_mcp_server.daemon_proxy import OwnerUnreachableError

        async def never(_body: dict[str, Any]) -> httpx2.Response:
            await asyncio.sleep(30)
            raise AssertionError("the call was not given up on")

        owner = _OwnerOverHttp(fail={"tools/call": never}, era=era)
        _serve_over_http(monkeypatch, lambda _address: owner)
        client = _backend(_attachment(tmp_path), tmp_path).open_client(timeout=0.3)
        negotiated: str | None = None

        with pytest.raises(OwnerUnreachableError) as failed:
            async with client:
                negotiated = client.protocol_version
                await client.call_tool_mcp("send_connection_request", {})

        assert getattr(failed.value.__cause__, "code", None) == mt.REQUEST_TIMEOUT
        assert failed.value.nothing_was_sent is False
        assert negotiated == protocol

    @_OWNER_ERAS
    @pytest.mark.parametrize("stand_in", ["500", "sse ended"])
    async def test_an_unbound_call_that_ran_is_not_reported_unsent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stand_in: str,
        era: str,
        protocol: str,
    ):
        """A client with no call bound to it still knows what it sent.

        The binding belongs to a call the heartbeat middleware is watching, and
        a client can be opened without one. Its absence says nobody else is
        keeping count, never that no tool request went out: the owner here ran
        it before its answer was lost.
        """
        from linkedin_mcp_server.daemon_proxy import OwnerUnreachableError

        owner = _OwnerOverHttp(era=era)
        owner.fail["tools/call"] = owner.after_running(_STAND_INS[stand_in])
        _serve_over_http(monkeypatch, lambda _address: owner)
        client = _backend(_attachment(tmp_path), tmp_path).open_client(timeout=2.0)
        negotiated: str | None = None

        with pytest.raises(OwnerUnreachableError) as failed:
            async with client:
                negotiated = client.protocol_version
                await client.call_tool_mcp("send_connection_request", {})

        assert owner.effects == 1
        assert failed.value.nothing_was_sent is False
        assert negotiated == protocol


class TestHowTheHopIsNegotiated:
    """Which era the owner hop lands on, and what it sends to get there.

    The client probes ``server/discover`` and takes the 2026-07-28 era from an
    owner that answers it properly, and the handshake from one that does not.
    Only a modern-only owner with no version in common is a refusal.
    """

    @staticmethod
    async def _call_through(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: _OwnerOverHttp
    ) -> tuple[str | None, mt.CallToolResult]:
        _serve_over_http(monkeypatch, lambda _address: owner)
        client = _backend(_attachment(tmp_path), tmp_path).open_client(timeout=2.0)
        async with client:
            protocol = client.protocol_version
            result = await client.call_tool_mcp("send_connection_request", {})
        return protocol, result

    async def test_a_modern_owner_is_spoken_to_in_its_era(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        owner = _OwnerOverHttp()

        protocol, result = await self._call_through(tmp_path, monkeypatch, owner)

        assert protocol == "2026-07-28"
        assert result.is_error is False
        assert owner.methods == ["server/discover", "tools/call"]
        assert owner.effects == 1

    async def test_a_discovery_short_of_the_strict_schema_is_not_the_modern_era(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Why the stand-in owner stamps what it does.

        A discovery result missing the fields the 2026-07-28 schema requires
        of it still parses, and the client falls back to the handshake rather
        than adopt an era every later answer would fail. An owner stand-in
        written that way would test the handshake while claiming the other.
        """
        owner = _OwnerOverHttp()

        def lax(body: dict[str, Any]) -> httpx2.Response:
            result = {
                key: value
                for key, value in json.loads(owner.answer(body).content)[
                    "result"
                ].items()
                if key not in {"resultType", "ttlMs", "cacheScope"}
            }
            return httpx2.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
            )

        owner.fail["server/discover"] = lax

        protocol, result = await self._call_through(tmp_path, monkeypatch, owner)

        assert protocol == "2025-11-25"
        assert result.is_error is False
        assert owner.methods[:2] == ["server/discover", "initialize"]

    @pytest.mark.parametrize("refusal", ["500", "an unknown method"])
    async def test_a_refused_discovery_falls_back_to_the_handshake(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str
    ):
        """A discovery that fails is not an entry that fails.

        The handshake is tried next, and an owner that answers it is entered
        in that era: the only kind of owner an older build is.
        """
        owner = _OwnerOverHttp(era="handshake")
        if refusal == "500":
            owner.era = "2026-07-28"
            owner.fail["server/discover"] = _STAND_INS["500"]

        protocol, result = await self._call_through(tmp_path, monkeypatch, owner)

        assert protocol == "2025-11-25"
        assert result.is_error is False
        assert owner.methods[:2] == ["server/discover", "initialize"]
        assert owner.effects == 1

    async def test_an_owner_with_no_way_in_is_not_entered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.daemon_proxy import OwnerUnreachableError

        owner = _OwnerOverHttp(
            fail={
                "server/discover": _STAND_INS["500"],
                "initialize": _STAND_INS["500"],
            }
        )

        with pytest.raises(OwnerUnreachableError) as failed:
            await self._call_through(tmp_path, monkeypatch, owner)

        assert failed.value.nothing_was_sent is True
        assert owner.methods == ["server/discover", "initialize"]
        assert owner.effects == 0

    async def test_a_modern_only_owner_sharing_no_version_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The one discovery answer that ends the connection instead.

        An owner that names only modern versions this client does not speak
        has said it cannot be talked to at all, and falling back to a
        handshake it does not serve would only hide that. It is its answer,
        so it is not retagged as a departure.
        """
        from mcp import MCPError

        def only_a_later_version(body: dict[str, Any]) -> httpx2.Response:
            error = {
                "code": mt.UNSUPPORTED_PROTOCOL_VERSION,
                "message": "Unsupported protocol version",
                "data": {"supported": ["2099-01-01"], "requested": "2026-07-28"},
            }
            return httpx2.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "error": error}
            )

        owner = _OwnerOverHttp(fail={"server/discover": only_a_later_version})

        with pytest.raises(MCPError) as refused:
            await self._call_through(tmp_path, monkeypatch, owner)

        assert refused.value.code == mt.UNSUPPORTED_PROTOCOL_VERSION
        assert owner.methods == ["server/discover"]
        assert owner.effects == 0

    async def test_an_unsupported_listing_is_an_empty_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """An owner with no resources says so, and that is not a departure.

        Its unknown-method answer is a real JSON-RPC error, which the provider
        turns into an empty list; the SDK's own "Not Found" for a 404 shares
        the code and is the departure instead.
        """
        from linkedin_mcp_server.server import create_mcp_server
        from linkedin_mcp_server.server_role import ServerRole

        owner = _OwnerOverHttp()
        _serve_over_http(monkeypatch, lambda _address: owner)
        elected = _attachment(tmp_path)
        backend = _backend(elected, tmp_path)
        elections: list[object] = []
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon_election.obtain_owner",
            lambda *_a, **_k: elections.append(1),
        )
        proxy = create_mcp_server(
            role=ServerRole.PROXY, proxy_backend=backend, tool_timeout=1.0
        )

        async with Client(proxy) as client:
            assert await client.list_resources() == []
            assert await client.list_prompts() == []

        assert "resources/list" in owner.methods
        assert elections == []
        assert backend.attachment is elected


class TestWhatTheOwnerDidBeforeItWentAway:
    """Effects counted on both owners, through the whole proxy and the SDK's HTTP client.

    The recovery acts on one question, whether the tool request can have
    reached the owner, and a counter on each owner answers it without trusting
    the code under test.
    """

    @staticmethod
    def _proxy(backend: DaemonProxyBackend) -> FastMCP:
        from linkedin_mcp_server.server import create_mcp_server
        from linkedin_mcp_server.server_role import ServerRole

        return create_mcp_server(
            role=ServerRole.PROXY, proxy_backend=backend, tool_timeout=1.0
        )

    @staticmethod
    def _two_owners(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        original: _OwnerOverHttp,
        replacement: _OwnerOverHttp,
    ) -> DaemonProxyBackend:
        elected = _attachment(tmp_path)
        standby = _attachment(tmp_path, port=elected.descriptor.port + 1)
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon_election.obtain_owner",
            lambda *_a, **_k: _elected(standby),
        )
        owners = {_address(elected): original, _address(standby): replacement}
        _serve_over_http(monkeypatch, owners.__getitem__)
        return _backend(elected, tmp_path)

    @_OWNER_ERAS
    async def test_a_call_that_never_left_runs_once_on_the_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, era: str, protocol: str
    ):
        """Gone after the lookup, before the call's own client could connect.

        The call's client fails its negotiation, before any tool request
        exists, so the one run the user asked for belongs to the replacement.
        """
        original = _OwnerOverHttp(era=era)
        replacement = _OwnerOverHttp(era=era)
        sessions = _sessions_opened(monkeypatch)

        def gone_after_the_lookup(body: dict[str, Any]) -> httpx2.Response:
            del original.fail["tools/list"]
            original.fail["server/discover"] = _refused
            original.fail["initialize"] = _refused
            return original.answer(body)

        original.fail["tools/list"] = gone_after_the_lookup
        backend = self._two_owners(tmp_path, monkeypatch, original, replacement)

        async with Client(self._proxy(backend)) as client:
            result = await client.call_tool(
                "send_connection_request", {}, raise_on_error=False
            )

        assert result.is_error is False, result
        assert original.effects == 0
        assert replacement.effects == 1, "the call that never left was not run"
        called = [s for s in sessions if s.tool_request_started]
        assert [s.protocol_version for s in called] == [protocol]

    @_OWNER_ERAS
    @pytest.mark.parametrize("stand_in", ["500", "sse ended"])
    async def test_a_call_that_ran_is_reported_and_never_repeated(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stand_in: str,
        era: str,
        protocol: str,
    ):
        """The owner ran the call, and its answer was lost on the way back."""
        original = _OwnerOverHttp(era=era)
        original.fail["tools/call"] = original.after_running(_STAND_INS[stand_in])
        replacement = _OwnerOverHttp(era=era)
        sessions = _sessions_opened(monkeypatch)
        backend = self._two_owners(tmp_path, monkeypatch, original, replacement)

        async with Client(self._proxy(backend)) as client:
            result = await client.call_tool(
                "send_connection_request", {}, raise_on_error=False
            )

        assert original.effects == 1
        assert replacement.effects == 0, "a call that had run was sent again"
        assert result.is_error is True
        assert result.structured_content is not None
        assert result.structured_content["status"] == "outcome_unknown"
        assert result.structured_content["retry_safe"] is False
        called = [s for s in sessions if s.tool_request_started]
        assert [s.protocol_version for s in called] == [protocol]


class TestTheHeadersTheOwnerReceives:
    """What actually arrives at the owner, read off the outbound request.

    The frontend is served over HTTP here, so FastMCP forwards the caller's
    headers onto the owner hop, the frontend's own bearer among them. The
    owner's bearer and the bound call id have to win on the request itself;
    the transport's constructor arguments say nothing about that.
    """

    @_OWNER_ERAS
    async def test_the_owners_credential_and_the_bound_call_arrive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, era: str, protocol: str
    ):
        from fastmcp.utilities.tests import asgi_client

        from linkedin_mcp_server.daemon_liveness import CALL_HEADER, HEARTBEAT_PATH
        from linkedin_mcp_server.server import create_mcp_server
        from linkedin_mcp_server.server_role import ServerRole

        owner = _OwnerOverHttp(era=era)
        _serve_over_http(monkeypatch, lambda _address: owner)
        attachment = _attachment(tmp_path)
        proxy = create_mcp_server(
            role=ServerRole.PROXY,
            proxy_backend=_backend(attachment, tmp_path),
            tool_timeout=1.0,
        )

        async with asgi_client(
            proxy,
            headers={
                "Authorization": "Bearer frontend-credential",
                CALL_HEADER: "forged-by-the-caller",
                "x-caller-note": "forwarded",
            },
        ) as client:
            result = await client.call_tool("send_connection_request", {})

        assert result.is_error is False
        (call,) = [
            request
            for request in owner.requests
            if request.url.path != HEARTBEAT_PATH and b'"tools/call"' in request.content
        ]
        (beat,) = [r for r in owner.requests if r.url.path == HEARTBEAT_PATH]
        # Forwarding is live on this path, which is what makes the two
        # absences below mean anything: an ordinary caller header arrives.
        assert call.headers.get("x-caller-note") == "forwarded"
        assert call.headers["authorization"] == f"Bearer {attachment.token}"
        assert call.headers[CALL_HEADER] == beat.headers[CALL_HEADER]
        assert call.headers[CALL_HEADER] != "forged-by-the-caller"
        for request in owner.requests:
            assert "frontend-credential" not in str(request.headers), request.url
        # The era the hop spoke, as the owner sees it on the request. On the
        # 2026-07-28 era the owner routes and validates on these headers, and
        # there is no session to name.
        assert call.headers["mcp-protocol-version"] == protocol
        if era == "2026-07-28":
            assert call.headers["mcp-method"] == "tools/call"
            assert call.headers["mcp-name"] == "send_connection_request"
            assert "mcp-session-id" not in call.headers


#: A stand-in owner process: a real HTTP server with the owner's bearer check
#: and heartbeat route, and one mutating tool that appends one record per run
#: to a file, naming the protocol the request arrived on and its argument.
#: "hold" keeps the call running until the process is killed.
#:
#: It reports to its parent in whole lines on stdout, each flushed only once
#: what it announces is complete: ``READY <json>`` once it serves, and
#: ``EFFECT <record>`` once that record is written and the file closed. A line
#: without its newline is never a frame, so a child descheduled halfway
#: through a write cannot be read as having said anything.
_OWNER_PROCESS = """
import asyncio, json, socket, sys
from pathlib import Path

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from starlette.responses import JSONResponse

from linkedin_mcp_server.daemon_liveness import HEARTBEAT_PATH
from linkedin_mcp_server.server import _StaticTokenAuth

root, token, behaviour = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
mcp = FastMCP("owner", auth=_StaticTokenAuth(token))


def say(frame):
    sys.stdout.write(frame + "\\n")
    sys.stdout.flush()


@mcp.tool(annotations={"destructiveHint": True})
async def send_connection_request(message: str) -> dict[str, str]:
    record = get_http_headers().get("mcp-protocol-version", "") + " " + message
    with (root / "effects.txt").open("a") as effects:
        effects.write(record + "\\n")
    say("EFFECT " + record)
    if behaviour == "hold":
        await asyncio.Event().wait()
    return {"status": "sent"}


@mcp.custom_route(HEARTBEAT_PATH, methods=["POST"])
async def heartbeat(request):
    if request.headers.get("authorization") != f"Bearer {token}":
        return JSONResponse({}, status_code=401)
    return JSONResponse({"watched": False})


async def main():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    config = uvicorn.Config(
        mcp.http_app(path="/mcp"), log_level="error", access_log=False
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        if serving.done():
            await serving
            raise RuntimeError("the owner stopped before it started")
        await asyncio.sleep(0.01)
    say("READY " + json.dumps({"port": listener.getsockname()[1]}))
    await serving


asyncio.run(main())
"""


class _OwnerProcess:
    """A running stand-in owner, and the frames it has reported so far.

    A reader thread drains the child's output, keeps every line in the log for
    a failure message, and queues each whole frame. Waiting for one is a
    blocking read with a deadline, never a fixed pause, and the child's exit
    ends the wait at once.
    """

    def __init__(self, root: Path, behaviour: str, token: str) -> None:
        import os
        import queue
        import subprocess
        import sys
        import threading

        self.root = root
        script = root / "owner.py"
        script.write_text(_OWNER_PROCESS, encoding="utf-8")
        self._frames: queue.Queue[str | None] = queue.Queue()
        self._log: list[str] = []
        self.process = subprocess.Popen(
            [sys.executable, str(script), str(root), token, behaviour],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={**os.environ, "USER_DATA_DIR": str(root / "profile")},
            text=True,
            encoding="utf-8",
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        stream = self.process.stdout
        assert stream is not None
        for line in stream:
            self._log.append(line)
            if line.endswith("\n") and line.startswith(("READY ", "EFFECT ")):
                self._frames.put(line[:-1])
        self._frames.put(None)

    def wait_for(self, kind: str, *, seconds: float) -> str:
        """The payload of the next *kind* frame, or a failure naming why not."""
        import queue
        import time

        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"no {kind} frame from the owner: {self.log()}"
            try:
                frame = self._frames.get(timeout=remaining)
            except queue.Empty:
                continue
            assert frame is not None, f"the owner exited before {kind}: {self.log()}"
            if frame.startswith(kind + " "):
                return frame[len(kind) + 1 :]

    def log(self) -> str:
        return "".join(self._log)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        self._reader.join(timeout=10)


class TestAnOwnerProcessKilledMidCall:
    """A stand-in owner process killed after it took a mutating call.

    What is real here is the transport and the process loss: a separate
    process serving HTTP on a loopback socket, killed by the operating system
    while its tool is running, on the 2026-07-28 era, with the production
    frontend, its recovery and its client in front of it. The owner itself is
    a stand-in rather than the owner's own server assembly (no call
    admission, lease or signed refusals; a minimal heartbeat route), and the
    election is replaced by a known replacement: those halves are witnessed
    in `TestAForwardedCallThroughTheRealOwner` and the election tests. The
    only question here is the one the recovery acts on, answered by counting
    complete run records on each owner.
    """

    @staticmethod
    def _spawn(
        root: Path, behaviour: str, owners: list[_OwnerProcess]
    ) -> tuple[_OwnerProcess, Attachment]:
        import dataclasses

        root.mkdir(parents=True)
        token = new_token()
        owner = _OwnerProcess(root, behaviour, token)
        owners.append(owner)
        port = json.loads(owner.wait_for("READY", seconds=30))["port"]
        attachment = dataclasses.replace(_attachment(root, port=port), token=token)
        return owner, attachment

    @staticmethod
    def _runs(root: Path) -> list[str]:
        """Every complete run record, and nothing a write left half done."""
        effects = root / "effects.txt"
        if not effects.exists():
            return []
        return effects.read_text(encoding="utf-8").split("\n")[:-1]

    async def test_the_call_is_reported_as_unknown_and_never_repeated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.server import create_mcp_server
        from linkedin_mcp_server.server_role import ServerRole

        owners: list[_OwnerProcess] = []
        try:
            original, attachment = self._spawn(tmp_path / "original", "hold", owners)
            _replacement, standby = self._spawn(
                tmp_path / "replacement", "answer", owners
            )
            elections: list[object] = []

            def elect(*_args: Any, **_kwargs: Any) -> Any:
                elections.append(1)
                return _elected(standby)

            monkeypatch.setattr(
                "linkedin_mcp_server.daemon_election.obtain_owner", elect
            )
            backend = _backend(attachment, tmp_path / "original")
            proxy = create_mcp_server(
                role=ServerRole.PROXY, proxy_backend=backend, tool_timeout=5.0
            )

            async with Client(proxy) as client:
                calling = asyncio.create_task(
                    client.call_tool(
                        "send_connection_request",
                        {"message": "hello"},
                        raise_on_error=False,
                    )
                )
                # Killed only once the owner has reported a complete record of
                # the run, so the kill lands on a call that has acted.
                effect = await asyncio.to_thread(
                    original.wait_for, "EFFECT", seconds=15
                )
                assert effect == "2026-07-28 hello"
                assert not calling.done(), calling
                original.stop()
                result = await asyncio.wait_for(calling, timeout=20)
        finally:
            for owner in owners:
                owner.stop()

        assert self._runs(tmp_path / "original") == ["2026-07-28 hello"]
        assert self._runs(tmp_path / "replacement") == [], "the call was sent again"
        assert result.is_error is True
        assert result.structured_content is not None
        assert result.structured_content["status"] == "outcome_unknown"
        assert result.structured_content["retry_safe"] is False
        assert elections == [1]
