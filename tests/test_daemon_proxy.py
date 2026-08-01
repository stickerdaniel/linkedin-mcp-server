"""How a frontend reaches the owner it was told to forward to.

Every test here pins something that is invisible in a passing round trip and
expensive when it is wrong: a bearer token taking a detour through the user's
proxy, a long call that hangs instead of failing, progress that silently stops
arriving, or a dead owner that looks like a server with no tools.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

import httpx
import mcp.types as mt
import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.client.transports import (
    ClientTransport,
    FastMCPTransport,
    StreamableHttpTransport,
)
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
    replacement without reading a configuration singleton whose first read parses
    `sys.argv`.
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
        raise httpx.ConnectError(f"nothing is listening on {self.url}")
        yield  # noqa: W0101 - unreachable, and an async generator needs one


class _GoesAwayAfterInitialize(FastMCPTransport):
    """An owner that answers the initialize and is gone before the next request.

    The window that neither the connect nor the tool call can see, and the only
    reason the listing boundaries are wrapped at all.
    """

    @asynccontextmanager
    async def connect_session(self, **kwargs: Any) -> AsyncIterator[Any]:
        async with super().connect_session(**kwargs) as session:
            yield _AnsweredOnceAndStopped(session)


class _AnsweredOnceAndStopped:
    """A session that completes the handshake and then answers nothing.

    Deliberately not the exception a real owner produces, and reality has three
    shapes rather than one. Measured with the client the provider builds: an
    owner already gone at connect time fails in `__aenter__` as `RuntimeError`
    over `httpx.ConnectError`; one that goes away in this window, after the
    initialize and before the request on that same session, raises
    `anyio.BrokenResourceError` or `ClosedResourceError` depending on timing; one
    that goes away with a request outstanding comes back as an `McpError` the
    session invented. The first and third have their own tests.

    This double stands in for the middle one, and raises something outside all
    three on purpose, so what it pins is the boundary itself: the listing answers
    "nothing was sent" from *where* the failure happened. A version that read the
    cause chain instead would pass against the real shapes and fail here.
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def list_tools(self, *_args: Any, **_kwargs: Any):
        raise httpx.RemoteProtocolError("the owner closed the connection")


class _AnswersWhenTold(FastMCPTransport):
    """An owner whose listing is held open for as long as a test needs it."""

    def __init__(self, server: FastMCP) -> None:
        super().__init__(server)
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    @asynccontextmanager
    async def connect_session(self, **kwargs: Any) -> AsyncIterator[Any]:
        async with super().connect_session(**kwargs) as session:
            yield _WaitsBeforeListing(session, self.reached, self.release)


class _WaitsBeforeListing:
    """A session that reports it was asked, then waits to be let go."""

    def __init__(
        self, session: Any, reached: asyncio.Event, release: asyncio.Event
    ) -> None:
        self._session = session
        self._reached = reached
        self._release = release

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def list_tools(self, *args: Any, **kwargs: Any):
        self._reached.set()
        await self._release.wait()
        return await self._session.list_tools(*args, **kwargs)


def _reach_owners_in_process(monkeypatch: pytest.MonkeyPatch, owner_at) -> None:
    """Reach in-process owners, but only at the address production chose.

    Only the socket is stood in for. The URL is still built by production code
    from whatever attachment the backend currently holds, and the client wrapped
    around it is the one `open_client` built, so a failure is classified by the
    production client rather than raised in the shape a test wanted.

    *owner_at* answers with a `FastMCP` to reach, a transport to use as it is, or
    `None` for an address nobody is serving.
    """
    from fastmcp.client import transports

    def transport_for(url: str, **_ignored: Any) -> ClientTransport:
        reached = owner_at(url)
        if reached is None:
            return _NothingIsListening(url)
        if isinstance(reached, ClientTransport):
            return reached
        return FastMCPTransport(reached)

    monkeypatch.setattr(transports, "StreamableHttpTransport", transport_for)


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

        request = httpx.Request("POST", attachment.descriptor.url)
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


class TestKeepingTheTokenOffTheNetwork:
    """A loopback hop must not become a request to somebody else's proxy."""

    def test_the_environment_proxy_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # httpx honours HTTP_PROXY even for 127.0.0.1 unless NO_PROXY happens to
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
            timeout=httpx.Timeout(5.0),
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
        assert read_timeout is not None
        return read_timeout.total_seconds()

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
            timeout=httpx.Timeout(30.0, read=self._request_deadline(client)),
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
        assert served.annotations.readOnlyHint is False, (
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
        assert served.annotations.readOnlyHint is False, (
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
        # exception), and a long scrape with no progress looks indistinguishable
        # from a hung one. A plain Client in the factory drops these silently.
        owner = FastMCP("owner")

        @owner.tool
        async def scrape(ctx: Context) -> dict[str, bool]:
            await ctx.report_progress(progress=50, total=100, message="halfway")
            return {"ok": True}

        proxy = FastMCP("proxy", providers=[ProxyProvider(lambda: ProxyClient(owner))])
        seen: list[tuple[float, float | None, str | None]] = []

        async def record(progress: float, total: float | None, message: str | None):
            seen.append((progress, total, message))

        async with Client(proxy, progress_handler=record) as client:
            await client.call_tool("scrape", {})

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
        assert tool.annotations.readOnlyHint is True


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
    def _context(*, read_only: bool | None) -> MagicMock:
        """A call context whose tool declares *read_only*, or declares nothing."""
        tool = MagicMock()
        tool.annotations = (
            None if read_only is None else MagicMock(readOnlyHint=read_only)
        )
        context = MagicMock()
        context.message.name = "do_the_thing"
        context.fastmcp_context.fastmcp.get_tool = AsyncMock(return_value=tool)
        return context

    @staticmethod
    async def _run(
        backend: DaemonProxyBackend,
        context: MagicMock,
        *,
        nothing_was_sent: bool,
        instance_id: str,
    ) -> tuple[bool, int]:
        """Drive one failing call through the middleware.

        Returns whether it succeeded and how many times the call was attempted.
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
                    cause=httpx.ConnectError("gone"),
                )
            return "the result"

        middleware = FrontendOwnerRecoveryMiddleware(backend)
        try:
            await middleware.on_call_tool(context, call_next)  # ty: ignore
        except OwnerUnreachableError:
            return False, attempts
        return True, attempts

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

    async def test_a_mutating_call_is_not_repeated_when_it_may_have_run(
        self, _recovering
    ):
        # The failure the user pays for. `daemon_auth` already recorded the
        # measurement behind the rule: a client answered with an error at 0.66s
        # and the effect landing 0.7s later.
        backend, failed = _recovering
        succeeded, attempts = await self._run(
            backend,
            self._context(read_only=False),
            nothing_was_sent=False,
            instance_id=failed,
        )

        assert not succeeded, "a call that may already have run was repeated"
        assert attempts == 1
        # And the replacement was still adopted, for the next call.
        assert backend.attachment.descriptor.instance_id != failed

    async def test_a_mutating_call_is_repeated_when_nothing_was_sent(self, _recovering):
        # The only reason the dispatch question is worth asking. Without it every
        # write tool would keep failing across an upgrade.
        backend, failed = _recovering
        succeeded, attempts = await self._run(
            backend,
            self._context(read_only=False),
            nothing_was_sent=True,
            instance_id=failed,
        )

        assert succeeded
        assert attempts == 2

    async def test_a_read_only_call_is_repeated_even_when_it_may_have_run(
        self, _recovering
    ):
        # Repeating a read costs a page load and nothing else.
        backend, failed = _recovering
        succeeded, attempts = await self._run(
            backend,
            self._context(read_only=True),
            nothing_was_sent=False,
            instance_id=failed,
        )

        assert succeeded
        assert attempts == 2

    async def test_an_unannotated_call_is_treated_as_mutating(self, _recovering):
        # A tool that declares nothing has not promised anything, and the default
        # has to be the safe one: this is what keeps a tool added later from
        # being replayed because nobody remembered to annotate it.
        backend, failed = _recovering
        succeeded, attempts = await self._run(
            backend,
            self._context(read_only=None),
            nothing_was_sent=False,
            instance_id=failed,
        )

        assert not succeeded
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


class _AnswersWithAnError(FastMCPTransport):
    """An owner whose listing or call comes back as a JSON-RPC error.

    The code is the whole point. A client cannot tell from the type whether the
    owner said no or whether the session gave up waiting and wrote the error
    itself, and only the second is a departure.
    """

    def __init__(
        self, server: FastMCP, *, code: int, message: str, fails: str = "list"
    ) -> None:
        super().__init__(server)
        self._code = code
        self._message = message
        self._fails = fails

    @asynccontextmanager
    async def connect_session(self, **kwargs: Any) -> AsyncIterator[Any]:
        async with super().connect_session(**kwargs) as session:
            yield _FailsOneRequest(session, self._code, self._message, self._fails)


class _FailsOneRequest:
    """A session that answers one kind of request with a JSON-RPC error."""

    def __init__(self, session: Any, code: int, message: str, fails: str) -> None:
        self._session = session
        self._code = code
        self._message = message
        self._fails = fails

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def _refuse(self):
        from mcp.shared.exceptions import McpError

        return McpError(mt.ErrorData(code=self._code, message=self._message))

    async def list_tools(self, *args: Any, **kwargs: Any):
        if self._fails == "list":
            raise self._refuse()
        return await self._session.list_tools(*args, **kwargs)

    async def call_tool(self, *args: Any, **kwargs: Any):
        if self._fails == "call":
            raise self._refuse()
        return await self._session.call_tool(*args, **kwargs)


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
            (
                httpx.codes.REQUEST_TIMEOUT,
                "Timed out while waiting for response to ListToolsRequest",
            ),
            (mt.CONNECTION_CLOSED, "Connection closed"),
            (32600, "Session terminated"),
        ],
        ids=["timed out", "connection closed", "session terminated"],
    )
    async def test_a_request_that_never_came_back_is_a_departure(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded, code: int, message: str
    ):
        """What a departure looks like *during* a request, rather than between.

        An owner killed between requests fails to connect, because streamable
        HTTP opens a fresh connection each time. One that goes away with a
        request outstanding does not: the client session waits, gives up, and
        writes an `McpError` itself. So "the owner is gone" and "the owner said
        no" arrive as the same type, and reading the type alone leaves the
        frontend attached to a process that is not there.

        Each code here is one the client invents. What this pins is the rule, not
        its premise: nothing in the protocol reserves these integers, and the
        reason reading them is safe is that the owner is this same package and
        constructs no JSON-RPC error at all. That argument lives with the rule.
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

    async def test_a_mutating_call_that_timed_out_is_not_repeated(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded
    ):
        """A timeout is a departure and still no licence to run the call again.

        The two halves of the decision come apart here. The owner is gone, so a
        replacement is adopted for the next call; but a call that timed out may
        have been queued, may have held the profile lease, may have sent the
        connection request. Nothing in the protocol says which, so the failure is
        reported rather than guessed at.
        """
        backend, elected, _replacement, elections = _upgraded
        ran = 0

        def with_a_mutating_tool() -> FastMCP:
            owner = FastMCP("owner")

            @owner.tool(name="send_connection_request")
            async def send() -> str:
                nonlocal ran
                ran += 1
                return "sent"

            return owner

        # The departed owner still answers the listing, so the failure comes from
        # the call boundary rather than from the lookup in front of it.
        gone = _AnswersWithAnError(
            with_a_mutating_tool(),
            code=httpx.codes.REQUEST_TIMEOUT,
            message="Timed out while waiting for response to CallToolRequest",
            fails="call",
        )
        after = with_a_mutating_tool()
        _reach_owners_in_process(
            monkeypatch,
            lambda url: gone if url == elected.descriptor.url else after,
        )

        async with Client(self._proxy(backend)) as client:
            # Reported rather than repeated. The text is the server's masked one,
            # so what is pinned here is that the call failed, not how it read.
            with pytest.raises(Exception, match="send_connection_request"):
                await client.call_tool("send_connection_request", {})

        assert ran == 0, "a call that may already have run was sent again"
        assert elections() == 1
        assert (
            backend.attachment.descriptor.instance_id != elected.descriptor.instance_id
        )
