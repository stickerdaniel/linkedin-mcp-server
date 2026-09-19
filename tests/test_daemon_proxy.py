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
from dataclasses import dataclass
from functools import partial
from typing import Any, NoReturn
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

import httpx
import mcp.types as mt
import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
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

    * `httpx.ConnectError`, off the socket.
    * `RuntimeError("Client failed to connect: ...")`, raised `from` it in
      `fastmcp/client/client.py:622-624` (fastmcp 3.4.7) whenever the session
      task ended in anything but an `McpError` or an `HTTPStatusError`.
    * `OwnerUnreachableError`, raised `from` that by `_saying_which_owner` in
      `daemon_proxy`, which is where the owner's identity and the dispatch
      answer are attached.
    * `ToolError("Error calling tool ...")`, raised `from` that at
      `fastmcp/server/server.py:1357` under the `mask_error_details=True` this
      server switches on, and the outermost thing a middleware is handed.

    So the tag sits three links down and finding it is a walk, which is why
    `unreachable_owner_in` exists rather than an `isinstance`. A second failure
    raised bare leaves that walk unrun on the way back out.
    """
    from linkedin_mcp_server.daemon_proxy import OwnerUnreachableError

    try:
        try:
            try:
                raise httpx.ConnectError("gone as well")
            except httpx.ConnectError as connect:
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
            None if read_only is None else MagicMock(readOnlyHint=read_only)
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
                    cause=httpx.ConnectError("gone"),
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
                    cause=httpx.ConnectError("gone as well"),
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
        saying it knows nothing, not a scraping outcome, and no daemon module
        imports from `scraping/`. The cost of that is two places naming the same
        keys, so the names are pinned against their source here rather than
        left to drift until a client reads one of them and not the other.
        """
        from linkedin_mcp_server.daemon_proxy import _unknown_outcome
        from linkedin_mcp_server.scraping.contracts import message_action_result

        reported = _unknown_outcome(tool="send_message", reason="the owner went away")
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
        connect') -> httpx.ConnectError` and the tag is three links down.
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
        effect a scrape cannot have had, and a safely repeatable read declared
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
        never acted.
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
                cause=httpx.ConnectError("gone"),
            )

        middleware = FrontendOwnerRecoveryMiddleware(backend)
        with pytest.raises(OwnerUnreachableError, match="did not answer"):
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


class _DiesAsTheCallsSessionCloses(FastMCPTransport):
    """An owner whose session fails to close, after a call was made on it.

    The two halves of the displacement, in the order that does the damage.
    `ProxyTool.run` makes its call inside `async with client`
    (`fastmcp/server/providers/proxy.py:181`), and `Client._disconnect` awaits
    the session task under `suppress(asyncio.CancelledError)`
    (`fastmcp/client/client.py:672-676`), so an ordinary exception from that
    task leaves the context manager after the call has already decided its
    outcome, and replaces whatever that was.

    *refusing* is the code and message the call comes back with, or `None` for a
    call that succeeds. With a failure in flight it is the tag that gets
    replaced, and with none it is the result.

    Only a session that was asked to call dies while closing. Every upstream
    operation opens a client of its own, so a transport that killed every
    session would fail the lookup in front of the call and the call would never
    be reached.
    """

    def __init__(
        self, server: FastMCP, *, refusing: tuple[int, str] | None = None
    ) -> None:
        super().__init__(server)
        self._refusing = refusing

    @asynccontextmanager
    async def connect_session(self, **kwargs: Any) -> AsyncIterator[Any]:
        async with super().connect_session(**kwargs) as session:
            calling = _CallsOnThisSession(session, self._refusing)
            yield calling
        if calling.calls:
            raise httpx.ReadError("the owner went away as the session closed")


class _CallsOnThisSession:
    """A session that records the calls it was asked for, and may refuse them."""

    def __init__(self, session: Any, refusing: tuple[int, str] | None) -> None:
        self._session = session
        self._refusing = refusing
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def call_tool(self, *args: Any, **kwargs: Any):
        from mcp.shared.exceptions import McpError

        self.calls += 1
        if self._refusing is None:
            return await self._session.call_tool(*args, **kwargs)
        code, message = self._refusing
        raise McpError(mt.ErrorData(code=code, message=message))


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
            code=httpx.codes.REQUEST_TIMEOUT,
            message="Timed out while waiting for response to CallToolRequest",
            fails="call",
        )
        after = self._mutating_owner(ran)
        _reach_owners_in_process(
            monkeypatch,
            lambda url: gone if url == elected.descriptor.url else after,
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

    async def test_a_tag_the_closing_session_replaced_is_still_found(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded
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
                httpx.codes.REQUEST_TIMEOUT,
                "Timed out while waiting for response to CallToolRequest",
            ),
        )
        after = self._mutating_owner(ran)
        _reach_owners_in_process(
            monkeypatch,
            lambda url: gone if url == elected.descriptor.url else after,
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

    async def test_an_answer_survives_a_session_that_fails_to_close(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded
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
        _reach_owners_in_process(monkeypatch, lambda _url: dying)

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
