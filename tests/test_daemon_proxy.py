"""How a frontend reaches the owner it was told to forward to.

Every test here pins something that is invisible in a passing round trip and
expensive when it is wrong: a bearer token taking a detour through the user's
proxy, a long call that hangs instead of failing, progress that silently stops
arriving, or a dead owner that looks like a server with no tools.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import mcp.types as mt
import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.providers.proxy import ProxyClient, ProxyProvider
from fastmcp.tools import ToolResult

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon import Attachment
from linkedin_mcp_server.daemon_descriptor import build, new_instance_id, new_token
from linkedin_mcp_server.daemon_proxy import (
    _COMPONENT_CACHE_SECONDS,
    _client_factory,
    create_proxy_provider,
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


class TestReachingTheOwner:
    """The address and the credential, both used exactly as published."""

    def test_the_published_url_is_used_verbatim(self, tmp_path: Path):
        # Rebuilding it from host and port loses the MCP path, and FastMCP does
        # not add one back: it deliberately serves whatever path it is given.
        attachment = _attachment(tmp_path)
        client = _client_factory(attachment, timeout=1.0)()

        assert isinstance(client.transport, StreamableHttpTransport)
        assert client.transport.url == attachment.descriptor.url
        assert client.transport.url.endswith("/mcp")

    def test_an_ipv6_owner_keeps_its_brackets(self, tmp_path: Path):
        # Unbracketed, the colons in the address run into the one before the
        # port and the whole URL parses as a bad port.
        attachment = _attachment(tmp_path, host="::1")
        client = _client_factory(attachment, timeout=1.0)()

        assert isinstance(client.transport, StreamableHttpTransport)
        assert "[::1]" in client.transport.url

    def test_the_owners_token_is_sent_as_a_bearer(self, tmp_path: Path):
        # The owner compares the token after the `Bearer ` scheme, so a raw
        # header value would be rejected by the endpoint it was minted for.
        attachment = _attachment(tmp_path)
        client = _client_factory(attachment, timeout=1.0)()

        request = httpx.Request("POST", attachment.descriptor.url)
        assert client.transport.auth is not None
        signed = next(client.transport.auth.auth_flow(request))

        assert signed.headers["Authorization"] == f"Bearer {attachment.token}"

    def test_a_fresh_client_is_built_for_every_operation(self, tmp_path: Path):
        # The provider opens and closes a client around each upstream call, so a
        # single shared session would be reused after its context had exited —
        # and would outlive the owner it was opened against.
        factory = _client_factory(_attachment(tmp_path), timeout=1.0)

        assert factory() is not factory()

    def test_it_forwards_with_a_proxy_client(self, tmp_path: Path):
        # Not a plain Client. That one installs no progress handler, so every
        # progress update the tools report would be dropped on the way through.
        client = _client_factory(_attachment(tmp_path), timeout=1.0)()

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

        client = _client_factory(_attachment(tmp_path), timeout=1.0)()
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
        client = _client_factory(_attachment(tmp_path), timeout=1.0)()

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
        provider = create_proxy_provider(_attachment(tmp_path), tool_timeout=42.0)

        assert self._request_deadline(self._client_of(provider)) > 42.0

    def test_it_is_set_at_the_mcp_layer_and_not_only_on_the_http_client(
        self, tmp_path: Path
    ):
        # Measured: with the deadline only on the HTTP client, a call that
        # outlives the read timeout never returns at all. What produces an error
        # is the MCP-level timeout, and setting that also raises the HTTP read
        # timeout, so one value covers both layers.
        provider = create_proxy_provider(_attachment(tmp_path), tool_timeout=42.0)
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
    def _owner() -> FastMCP:
        owner = FastMCP("owner")

        @owner.tool(
            title="Get Person Profile",
            annotations={"readOnlyHint": True},
            tags={"person"},
        )
        async def get_person_profile(linkedin_username: str) -> dict[str, str]:
            return {"username": linkedin_username}

        return owner

    async def test_a_lookup_after_a_listing_costs_no_extra_round_trip(self):
        # An owner's tool set never changes over its lifetime, so caching it is
        # free correctness-wise and saves a list round trip per call — measured
        # at 88ms against 122ms with the cache off.
        #
        # Asserted through the connection count rather than against the module's
        # own constant: comparing to the constant moves with it, so setting the
        # cache to zero passed that version of this test.
        owner = self._owner()
        connections = 0

        def counting_factory() -> ProxyClient:
            nonlocal connections
            connections += 1
            return ProxyClient(owner)

        provider = ProxyProvider(counting_factory, cache_ttl=_COMPONENT_CACHE_SECONDS)
        assert _COMPONENT_CACHE_SECONDS > 0

        await provider.list_tools()
        after_listing = connections
        await provider.get_tool("get_person_profile")

        assert connections == after_listing

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
