"""How a frontend reaches the owner it was told to forward to.

Every test here pins something that is invisible in a passing round trip and
expensive when it is wrong: a bearer token taking a detour through the user's
proxy, a long call that hangs instead of failing, progress that silently stops
arriving, or a dead owner that looks like a server with no tools.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from unittest.mock import AsyncMock, MagicMock

import httpx2
import mcp.types as mt
import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from fastmcp.tools import ToolResult
from owner_hop import Forward, Interceptor, hop_of, method_of
from owner_hop import reach_owners_in_process as _reach_owners_in_process

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon.discovery import Attachment
from linkedin_mcp_server.daemon.descriptor import build, new_instance_id, new_token
from linkedin_mcp_server.daemon import descriptor as daemon_descriptor
from linkedin_mcp_server.daemon.forwarder import (
    HOP_PROTOCOL_VERSION,
    CallBinding,
    OwnerForwarder,
    _call_being_made,
    bound_to,
)
from linkedin_mcp_server.daemon.proxy import DaemonProxyBackend


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
    from linkedin_mcp_server.daemon.discovery import OwnerLookup, OwnerState
    from linkedin_mcp_server.daemon.election import ElectionOutcome

    return ElectionOutcome(
        OwnerLookup(state=OwnerState.ATTACHABLE, attachment=attachment),
        started_owner=True,
    )


def _forwarding(backend: DaemonProxyBackend, *, timeout: float = 5.0) -> FastMCP:
    """A server whose only provider is the forwarder, and nothing else of a proxy.

    `provider_error_strategy = "raise"` as the proxy role sets it, so a failing
    owner is an error rather than a server with no tools.
    """
    proxy = FastMCP("proxy", providers=[OwnerForwarder(backend, timeout=timeout)])
    proxy.provider_error_strategy = "raise"
    return proxy


class _GoesAwayAfterNegotiating(Interceptor):
    """An owner that answers the negotiation and is gone before the next request.

    The window that neither the connect nor the tool call can see: the hop was
    established, so the listing fails inside a connection that worked. The
    listing never reaches the owner here; its connection is dropped instead.
    """

    async def handle(
        self, message: dict[str, Any] | None, request: httpx2.Request, forward: Forward
    ) -> httpx2.Response:
        if method_of(message) == "tools/list":
            raise httpx2.RemoteProtocolError("the owner closed the connection")
        return await forward()


class _AnswersWhenTold(Interceptor):
    """An owner whose listing is held open for as long as a test needs it."""

    def __init__(self, owner: FastMCP) -> None:
        super().__init__(owner)
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def handle(
        self, message: dict[str, Any] | None, request: httpx2.Request, forward: Forward
    ) -> httpx2.Response:
        if method_of(message) == "tools/list":
            self.reached.set()
            await self.release.wait()
        return await forward()


class TestReachingTheOwner:
    """The address and the credential, both used exactly as published.

    Read off the requests that arrive at a hand-written owner, so what is
    pinned is what production sent rather than how it built the client.
    """

    @staticmethod
    async def _listed_through(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attachment: Attachment
    ) -> tuple[_OwnerOverHttp, list[str]]:
        owner = _OwnerOverHttp()
        reached: list[str] = []

        def owner_at(url: str) -> httpx2.MockTransport:
            reached.append(url)
            return httpx2.MockTransport(owner)

        _reach_owners_in_process(monkeypatch, owner_at)
        await OwnerForwarder(_backend(attachment, tmp_path), timeout=2.0).list_tools()
        return owner, reached

    async def test_the_published_url_is_used_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Rebuilding it from host and port loses the MCP path, and FastMCP does
        # not add one back: it deliberately serves whatever path it is given.
        attachment = _attachment(tmp_path)

        _owner, reached = await self._listed_through(tmp_path, monkeypatch, attachment)

        assert reached and set(reached) == {attachment.descriptor.url}
        assert attachment.descriptor.url.endswith("/mcp")

    async def test_an_ipv6_owner_keeps_its_brackets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Unbracketed, the colons in the address run into the one before the
        # port and the whole URL parses as a bad port.
        attachment = _attachment(tmp_path, host="::1")

        _owner, reached = await self._listed_through(tmp_path, monkeypatch, attachment)

        assert reached and all("[::1]" in url for url in reached)

    async def test_the_owners_token_is_sent_as_a_bearer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The owner compares the token after the `Bearer ` scheme, so a raw
        # header value would be rejected by the endpoint it was minted for.
        attachment = _attachment(tmp_path)

        owner, _reached = await self._listed_through(tmp_path, monkeypatch, attachment)

        assert owner.requests
        for request in owner.requests:
            assert request.headers["Authorization"] == f"Bearer {attachment.token}"

    async def test_every_operation_opens_a_hop_of_its_own(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A shared connection would be reused after the owner it was opened
        # against had gone, and an upgrade replaces the owner under a running
        # frontend. Each operation negotiates afresh, and leaves nothing open
        # behind it: every exchange is one POST.
        owner = _OwnerOverHttp()
        _reach_owners_in_process(monkeypatch, lambda _url: httpx2.MockTransport(owner))
        forwarder = OwnerForwarder(
            _backend(_attachment(tmp_path), tmp_path), timeout=2.0
        )

        await forwarder.list_tools()
        await forwarder.call("send_connection_request", {})

        assert owner.methods == [
            "server/discover",
            "tools/list",
            "server/discover",
            "tools/call",
        ]
        assert [r.method for r in owner.requests] == ["POST"] * 4


class TestCallingTheOwner:
    @pytest.mark.parametrize(
        ("deadline", "answered"), [(0.2, False), (3.0, True)], ids=["cut", "kept"]
    )
    async def test_each_request_waits_on_the_forwarding_deadline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        deadline: float,
        answered: bool,
    ):
        """The forwarder's timeout is what a request on the hop waits for.

        Set at the MCP layer and as the HTTP read timeout from one value: with
        it on the HTTP client alone, a call that outlived the read timeout never
        returned at all.
        """
        from linkedin_mcp_server.daemon.forwarder import OwnerUnreachableError

        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            await asyncio.sleep(0.6)
            return "sent"

        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        forwarder = OwnerForwarder(
            _backend(_attachment(tmp_path), tmp_path), timeout=deadline
        )

        if answered:
            result = await asyncio.wait_for(
                forwarder.call("send_connection_request", {}), timeout=10
            )
            assert result.is_error is False
            return
        with pytest.raises(OwnerUnreachableError) as failed:
            await asyncio.wait_for(
                forwarder.call("send_connection_request", {}), timeout=10
            )
        assert getattr(failed.value.__cause__, "code", None) == mt.REQUEST_TIMEOUT
        assert failed.value.nothing_was_sent is False

    async def test_an_answer_in_hand_outlives_a_session_that_ends_after_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Nothing that can fail runs between the owner's answer and the caller.

        The SDK's own result check lists the tools again on the same session
        whenever it has no output schema cached for the tool, which on a fresh
        hop is every time. That listing would be a second request after a call
        that has already acted, and its failure would stand in for the answer.
        Here every request after the tool request on its session is refused.
        """
        ran: list[str] = []
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> dict[str, str]:
            ran.append("sent")
            return {"status": "sent"}

        after_the_call = _GoneAfterTheCall(owner)
        _reach_owners_in_process(monkeypatch, lambda _url: after_the_call)
        forwarder = OwnerForwarder(
            _backend(_attachment(tmp_path), tmp_path), timeout=5.0
        )

        result = await forwarder.call("send_connection_request", {})

        assert result.is_error is False
        assert result.structured_content == {"status": "sent"}
        assert ran == ["sent"]
        assert after_the_call.refused == [], "a request followed the answer"

    def test_the_sdk_boundary_keeps_the_expected_signature(self):
        # The hop's session overrides the one method every request enters,
        # and passes each argument on by position, so both have to agree with
        # the SDK's own on names, order and defaults.
        from mcp import ClientSession

        from linkedin_mcp_server.daemon.forwarder import _HopSession

        claimed = inspect.signature(_HopSession.send_request)
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
        assert list(
            inspect.signature(_HopSession.validate_tool_result).parameters
        ) == list(inspect.signature(ClientSession.validate_tool_result).parameters)


class _GoneAfterTheCall(Interceptor):
    """An owner that refuses every request made after its tool call."""

    def __init__(self, owner: FastMCP) -> None:
        super().__init__(owner)
        self.called = False
        self.refused: list[str | None] = []

    async def handle(
        self, message: dict[str, Any] | None, request: httpx2.Request, forward: Forward
    ) -> httpx2.Response:
        method = method_of(message)
        if message is not None and self.called:
            self.refused.append(method)
            raise httpx2.ConnectError("the owner left after answering the call")
        if method == "tools/call":
            self.called = True
        return await forward()


class TestKeepingTheTokenOffTheNetwork:
    """A loopback hop must not become a request to somebody else's proxy."""

    async def test_the_environment_proxy_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # httpx2 honours HTTP_PROXY even for 127.0.0.1 unless NO_PROXY happens to
        # say otherwise. The owner reproduced this against a capture proxy: a
        # loopback request arrived there complete with the bearer token. This
        # server also has a *legitimate* proxy setting for LinkedIn's own
        # traffic, which is exactly why the two must not be confused. An
        # environment proxy mounts a transport of its own in front of the one
        # given, so a client that trusted it would never reach the owner here.
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        owner = _OwnerOverHttp()
        _reach_owners_in_process(monkeypatch, lambda _url: httpx2.MockTransport(owner))

        listed = await OwnerForwarder(
            _backend(_attachment(tmp_path), tmp_path), timeout=2.0
        ).list_tools()

        assert [tool.name for tool in listed] == ["send_connection_request"]
        assert owner.methods == ["server/discover", "tools/list"]


class TestTheForwardingDeadline:
    """Why the timeout is an argument and not a default."""

    def test_it_outlasts_the_owners_own_tool_timeout(self):
        # Equal would race the owner's error response, turning a diagnosable
        # "tool timed out" into an unexplained transport failure. Shorter would
        # abort calls the owner would have finished.
        from linkedin_mcp_server.daemon.proxy import call_budget

        assert call_budget(42.0) > 42.0

    async def test_a_call_that_outlives_the_deadline_fails_rather_than_hangs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The regression this argument exists for. Without an MCP-level timeout
        # the equivalent call hung indefinitely; `asyncio.wait_for` here is only
        # a guard so a regression fails the suite instead of stalling it.
        owner = FastMCP("owner")

        @owner.tool
        async def slow() -> dict[str, bool]:
            await asyncio.sleep(30)
            return {"ok": True}

        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        proxy = _forwarding(_backend(_attachment(tmp_path), tmp_path), timeout=0.2)

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
            "linkedin_mcp_server.daemon.election.obtain_owner",
            lambda *_args, **_kwargs: _elected(replacement),
        )

        provider = OwnerForwarder(backend, timeout=1.0)
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

        A cache written after its listing completes, outside any lock, is
        refilled by a listing already in flight against the departing owner,
        after it was cleared at adoption. Nothing available at adoption time
        closes that window. Keeping no cache does close it.
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
            "linkedin_mcp_server.daemon.election.obtain_owner",
            lambda *_args, **_kwargs: _elected(replacement),
        )

        provider = OwnerForwarder(backend, timeout=1.0)
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

        monkeypatch.setattr("linkedin_mcp_server.daemon.election.obtain_owner", elect)

        daemon_descriptor.publish(auth_root, elected.descriptor, elected.token)
        backend = _backend(elected, tmp_path)
        provider = OwnerForwarder(backend, timeout=1.0)
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

        monkeypatch.setattr("linkedin_mcp_server.daemon.election.obtain_owner", elect)

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

        monkeypatch.setattr("linkedin_mcp_server.daemon.election.obtain_owner", elect)

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

        monkeypatch.setattr("linkedin_mcp_server.daemon.election.obtain_owner", elect)

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

    async def test_a_dead_owner_is_an_error_not_an_empty_tool_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # FastMCP's default is to log a failing provider and carry on. For a
        # server whose only provider this is, that turns a dead owner into a
        # client that sees no tools and no reason why.
        from mcp import MCPError

        _reach_owners_in_process(monkeypatch, lambda _url: None)
        proxy = _forwarding(_backend(_attachment(tmp_path), tmp_path))

        async with Client(proxy) as client:
            with pytest.raises(MCPError):
                await client.list_tools()

    async def test_progress_from_the_owner_reaches_the_clients_handler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Eighteen of the nineteen tools report progress (`close_session` is the
        # exception), and a long read with no progress looks indistinguishable
        # from a hung one.
        owner = FastMCP("owner")

        @owner.tool
        async def read_page(ctx: Context) -> dict[str, bool]:
            await ctx.report_progress(progress=50, total=100, message="halfway")
            return {"ok": True}

        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        proxy = _forwarding(_backend(_attachment(tmp_path), tmp_path))
        seen: list[tuple[float, float | None, str | None]] = []

        async def record(progress: float, total: float | None, message: str | None):
            seen.append((progress, total, message))

        async with Client(proxy, progress_handler=record) as client:
            await client.call_tool("read_page", {})

        assert seen == [(50.0, 100.0, "halfway")]

    async def test_a_round_trip_preserves_everything_a_result_carries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
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

        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        proxy = _forwarding(_backend(_attachment(tmp_path), tmp_path))

        async with Client(proxy) as client:
            result = await client.call_tool(
                "report", {}, raise_on_error=False, meta={"marker": "carried"}
            )

        assert received["meta"]["marker"] == "carried"
        assert result.is_error is True
        assert result.structured_content == {"the": "structured half"}
        assert any("the text half" in getattr(c, "text", "") for c in result.content)

    async def test_meta_and_progress_cross_the_production_proxy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The caller's `_meta` and the owner's progress, through the real proxy.

        The frontend server, its middleware and the forwarder, with only the
        socket replaced. The caller's marker and trace context are read at the
        owner by value, progress the owner reports is read at the caller's
        handler, and the tool request is read where it arrives, on the hop's
        pinned protocol version.
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

        recorded = _RecordsRequests(owner)
        _reach_owners_in_process(monkeypatch, lambda _url: recorded)
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
        (call,) = recorded.of("tools/call")
        assert call.headers["mcp-protocol-version"] == HOP_PROTOCOL_VERSION
        (meta,) = received
        assert meta.get("marker") == "carried", meta
        # The same trace, whichever span the proxy stamped as the parent.
        assert meta.get("traceparent", "").split("-")[1:2] == [trace_id], meta
        assert (7.0, 10.0, "forwarded") in seen, seen

    async def test_the_owners_tool_schema_survives_the_hop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A client picks tools by title and annotations, so losing them changes
        # which tool an agent chooses even though every call still works.
        owner = self._owner()
        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        proxy = _forwarding(_backend(_attachment(tmp_path), tmp_path))

        async with Client(proxy) as client:
            (tool,) = await client.list_tools()

        assert tool.name == "get_person_profile"
        assert tool.title == "Get Person Profile"
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True


class _RecordsRequests(Interceptor):
    """An in-process owner, and every request that reached it, in order."""

    def __init__(self, owner: FastMCP) -> None:
        super().__init__(owner)
        self.requests: list[tuple[str | None, httpx2.Request]] = []

    def of(self, method: str) -> list[httpx2.Request]:
        return [request for seen, request in self.requests if seen == method]

    async def handle(
        self, message: dict[str, Any] | None, request: httpx2.Request, forward: Forward
    ) -> httpx2.Response:
        self.requests.append((method_of(message), request))
        return await forward()


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

    * `httpx2.ConnectError`, off the socket, inside the POST task that
      `mcp/client/streamable_http.py` runs for each request.
    * The `ExceptionGroup` that task's failure becomes as the transport's task
      group closes around the hop (anyio), wrapped once more by the session's.
    * `OwnerUnreachableError`, raised `from` that by `_Hop.fail` in
      `daemon.forwarder`, which is where the owner's identity and the dispatch
      answer are attached.
    * `ToolError("Error calling tool ...")`, raised `from` that at
      `fastmcp/server/server.py:1564` under the `mask_error_details=True` this
      server switches on, and the outermost thing a middleware is handed.

    So the tag sits a link down and finding it is a walk, which is why
    `unreachable_owner_in` exists rather than an `isinstance`. A second failure
    raised bare leaves that walk unrun on the way back out.
    """
    from linkedin_mcp_server.daemon.forwarder import OwnerUnreachableError

    try:
        try:
            raise ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [
                    ExceptionGroup(
                        "unhandled errors in a TaskGroup",
                        [httpx2.ConnectError("gone as well")],
                    )
                ],
            )
        except ExceptionGroup as closing:
            raise OwnerUnreachableError(
                instance_id=instance_id,
                nothing_was_sent=nothing_was_sent,
                cause=closing,
            ) from closing
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
        from linkedin_mcp_server.daemon.proxy import (
            FrontendCallMiddleware,
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

        middleware = FrontendCallMiddleware(backend, budget=30.0)
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
            "linkedin_mcp_server.daemon.election.obtain_owner",
            lambda *_a, **_k: _elected(replacement),
        )
        return _backend(elected, tmp_path), elected.descriptor.instance_id

    @pytest.fixture
    def _alone(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """A backend whose election finds nobody and cannot start one either."""
        elected = _attachment(tmp_path)

        def elect(*_a: Any, **_k: Any):
            raise RuntimeError("no owner could be started")

        monkeypatch.setattr("linkedin_mcp_server.daemon.election.obtain_owner", elect)
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
        from linkedin_mcp_server.daemon.proxy import unreachable_owner_in

        assert isinstance(answer, _Escaped), f"{why}: answered {answer!r}"
        assert unreachable_owner_in(answer.error) is not None, (
            f"a different failure escaped the recovery: {answer.error!r}"
        )
        return answer.error

    async def test_a_mutating_call_is_not_repeated_when_it_may_have_run(
        self, _recovering
    ):
        # The failure the user pays for. `daemon.auth_repair` already recorded the
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
        assert "thread_id" not in reported

    async def test_the_unknown_outcome_speaks_the_send_contracts_vocabulary(self):
        """The two halves of the payload are keys a send already uses.

        The builder lives in `daemon.proxy` on purpose: this is the transport
        saying it knows nothing, not a page-read outcome, and no daemon module
        imports from `linkedin/`. The cost of that is two places naming the same
        keys, so the names are pinned against their source here rather than
        left to drift until a client reads one of them and not the other.
        """
        from linkedin_mcp_server.daemon.liveness import unknown_outcome
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
        from linkedin_mcp_server.daemon.proxy import (
            FrontendCallMiddleware,
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

        middleware = FrontendCallMiddleware(backend, budget=30.0)
        with pytest.raises(ToolError, match="could not reach a new one"):
            await middleware.on_call_tool(
                self._context(read_only=True),
                call_next,  # ty: ignore
            )

        assert attempts == 1

    async def test_a_failure_from_something_else_is_left_alone(self, _recovering):
        # Only an unreachable owner is this middleware's business. Swallowing or
        # retrying anything else would hide a real tool error behind a recovery.
        from linkedin_mcp_server.daemon.proxy import FrontendCallMiddleware

        backend, _failed = _recovering
        attempts = 0

        async def call_next(_context: Any) -> Any:
            nonlocal attempts
            attempts += 1
            raise ValueError("the tool itself failed")

        middleware = FrontendCallMiddleware(backend, budget=30.0)
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


def _answers_with_an_error(
    server: FastMCP, *, code: int, message: str, fails: str = "list"
) -> FastMCP:
    """An owner whose listing or call comes back as a JSON-RPC error it sent.

    Sent by the owner, so it is the owner's answer whatever its code: whether
    an error came from the owner or was written by the SDK is read off the
    wire, never off the pair. `TestTheSdksOwnHttpErrors` makes the SDK write
    its own.
    """
    server.add_middleware(_OwnerRefuses(code=code, message=message, fails=fails))
    return server


class _StandsIn(Interceptor):
    """An owner whose answer to one method is replaced by an HTTP stand-in.

    The request never reaches the owner: the response is what the owner's side
    of the exchange sent instead, and the SDK writes its own error for it.
    *hold* keeps the request waiting instead, until the hop gives up on it.
    """

    def __init__(
        self,
        owner: FastMCP,
        *,
        method: str,
        respond: Callable[[dict[str, Any]], httpx2.Response] | None = None,
    ) -> None:
        super().__init__(owner)
        self.method = method
        self.respond = respond
        self.stood_in = 0

    async def handle(
        self, message: dict[str, Any] | None, request: httpx2.Request, forward: Forward
    ) -> httpx2.Response:
        if message is None or method_of(message) != self.method:
            return await forward()
        self.stood_in += 1
        if self.respond is None:
            await asyncio.Event().wait()
            raise AssertionError("the request was never given up on")
        return self.respond(message)


class _DiesAsTheCallsHopCloses(Interceptor):
    """An owner whose hop fails to close, after a call was made on it.

    The displacement in the order that does the damage: the call has an
    outcome, answered or failed, and only then does closing the hop fail.
    Left to escape, that failure replaces whatever the outcome was.

    *respond* replaces the call's answer with a stand-in, or is `None` for a
    call that runs and answers. Only a hop that was asked to call dies while
    closing, so the lookup in front of the call still works; *closing_failures*
    says whether the failure this exists for was actually raised.
    """

    def __init__(
        self,
        owner: FastMCP,
        *,
        respond: Callable[[dict[str, Any]], httpx2.Response] | None = None,
    ) -> None:
        super().__init__(owner)
        self.respond = respond
        self.closing_failures = 0

    async def handle(
        self, message: dict[str, Any] | None, request: httpx2.Request, forward: Forward
    ) -> httpx2.Response:
        if method_of(message) == "tools/call" and self.respond is not None:
            assert message is not None
            return self.respond(message)
        return await forward()

    async def closed(self, methods: list[str]) -> None:
        if "tools/call" in methods:
            self.closing_failures += 1
            raise httpx2.ReadError("the owner went away as the hop closed")


class _DiscoveryFailsAfterMutation(Interceptor):
    """An owner that answers a call, then cannot answer a listing on its hop."""

    def __init__(self, owner: FastMCP) -> None:
        super().__init__(owner)
        self.called: set[int] = set()
        self.discovery_attempts = 0

    async def handle(
        self, message: dict[str, Any] | None, request: httpx2.Request, forward: Forward
    ) -> httpx2.Response:
        # Only the hop that made the call loses its owner. The lookup in
        # front of the call has a hop of its own, and that one is not what
        # this is about.
        hop = hop_of(request)
        method = method_of(message)
        if method == "tools/call":
            self.called.add(hop)
        elif method == "tools/list" and hop in self.called:
            self.discovery_attempts += 1
            raise httpx2.ConnectError("the owner left after answering the call")
        return await forward()


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

        monkeypatch.setattr("linkedin_mcp_server.daemon.election.obtain_owner", elect)
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

    async def test_an_answer_is_not_replaced_by_schema_discovery(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded
    ):
        """A completed mutation must not be rediscovered and sent again."""
        backend, elected, _replacement, elections = _upgraded
        ran: list[str] = []
        before = _DiscoveryFailsAfterMutation(self._mutating_owner(ran))
        after = self._mutating_owner(ran)
        _reach_owners_in_process(
            monkeypatch,
            lambda url: before if url == elected.descriptor.url else after,
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
        assert before.called, "the call never reached the owner under test"

    async def test_an_owner_that_dies_after_the_negotiation_is_still_recovered(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded
    ):
        """The boundary neither the connect nor the call can see.

        The listing runs inside a connection that was established, so a departure
        between the negotiation and the list request raises out of neither. Every
        other recovery test kills the owner earlier and passes with the listing
        boundaries unwrapped.
        """
        backend, elected, _replacement, elections = _upgraded
        dying = _GoesAwayAfterNegotiating(self._owner())
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

        A forwarded call first looks its tool up, on a hop of its own that is
        already bound to the call. Here that hop loses its owner under the
        lookup, before a tool request exists. Nothing that could act has left,
        so the one run the user asked for belongs to the replacement.
        """
        from linkedin_mcp_server.daemon.core.call import Dispatch
        from linkedin_mcp_server.daemon.liveness import CALL_HEADER

        backend, elected, _replacement, elections = _upgraded
        original_runs: list[str] = []
        replacement_runs: list[str] = []
        at_the_failure: list[tuple[bool, Dispatch | None]] = []

        class EndsItsHopAtTheLookup(Interceptor):
            async def handle(
                self,
                message: dict[str, Any] | None,
                request: httpx2.Request,
                forward: Forward,
            ) -> httpx2.Response:
                if method_of(message) == "tools/list":
                    bound = _call_being_made.get()
                    at_the_failure.append(
                        (
                            CALL_HEADER in request.headers,
                            None if bound is None else bound.dispatch.state,
                        )
                    )
                    raise httpx2.ReadError("the owner's connection was lost")
                return await forward()

        def owner_recording(runs: list[str]) -> FastMCP:
            owner = FastMCP("owner")

            @owner.tool(name="send_connection_request")
            async def send(message: str) -> str:
                runs.append(message)
                return message

            return owner

        original = EndsItsHopAtTheLookup(owner_recording(original_runs))
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
        assert at_the_failure == [(True, Dispatch.UNSENT)]
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
        refusing = _answers_with_an_error(
            self._owner(), code=mt.INTERNAL_ERROR, message="the owner refused"
        )
        _reach_owners_in_process(monkeypatch, lambda _url: refusing)

        async with Client(self._proxy(backend)) as client:
            with pytest.raises(Exception, match="refused"):
                await client.list_tools()

        assert elections() == 0
        assert (
            backend.attachment.descriptor.instance_id == elected.descriptor.instance_id
        )

    @pytest.mark.parametrize(
        ("code", "message"),
        [
            (mt.REQUEST_TIMEOUT, "Request 'tools/list' timed out"),
            (mt.CONNECTION_CLOSED, "Connection closed"),
            (mt.INVALID_REQUEST, "Session terminated"),
        ],
        ids=["timed out", "connection closed", "session terminated"],
    )
    async def test_an_owner_that_sends_a_pair_the_sdk_also_writes_elects_nothing(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded, code: int, message: str
    ):
        """Whether an error is the owner's is read off the wire, not the pair.

        These are pairs the SDK writes itself when an owner goes away with a
        request outstanding. Sent by the owner, each is still an answer from a
        process that is there, and nothing is elected for it.
        """
        backend, elected, _replacement, elections = _upgraded
        refusing = _answers_with_an_error(self._owner(), code=code, message=message)
        _reach_owners_in_process(monkeypatch, lambda _url: refusing)

        async with Client(self._proxy(backend)) as client:
            with pytest.raises(Exception):  # noqa: B017 - the owner's own error
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

    @pytest.mark.parametrize("stand_in", ["sse ended", "404", "500", "timed out"])
    async def test_a_request_that_never_came_back_is_a_departure(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded, stand_in: str
    ):
        """What a departure looks like *during* a request, rather than between.

        An owner killed between requests fails to connect, because streamable
        HTTP opens a fresh connection each time. One that goes away with a
        request outstanding does not: the SDK writes an `MCPError` itself, of
        the same type as a real answer. Each stand-in here is a real HTTP
        exchange the SDK turns into one ("SSE stream ended without a
        response", "Session terminated", a 500, its own deadline), so reading
        the type alone would leave the frontend attached to a process that is
        not there.
        """
        backend, elected, _replacement, elections = _upgraded
        if stand_in == "timed out":
            # The budget is the tool timeout with no margin, so the held
            # listing is given up on after a second rather than thirty-one.
            monkeypatch.setattr(
                "linkedin_mcp_server.daemon.proxy._TIMEOUT_MARGIN_SECONDS", 0.0
            )
        gone = _StandsIn(
            self._owner(),
            method="tools/list",
            respond=None if stand_in == "timed out" else _STAND_INS[stand_in],
        )
        after = self._owner("the_replacements_tool")
        _reach_owners_in_process(
            monkeypatch,
            lambda url: gone if url == elected.descriptor.url else after,
        )

        async with Client(self._proxy(backend)) as client:
            listed = {tool.name for tool in await client.list_tools()}

        assert listed == {"the_replacements_tool"}
        assert elections() == 1
        assert gone.stood_in == 1

    @pytest.mark.parametrize("stand_in", ["sse ended", "500"])
    async def test_a_mutating_call_whose_answer_never_came_is_not_repeated(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded, stand_in: str
    ):
        """A lost answer is a departure and still no licence to run the call again.

        The two halves of the decision come apart here. The owner is gone, so a
        replacement is adopted for the next call; but a call whose request left
        may have been queued, may have held the profile lease, may have sent the
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
        gone = _StandsIn(
            self._mutating_owner(ran),
            method="tools/call",
            respond=_STAND_INS[stand_in],
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

        assert gone.stood_in == 1
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

    async def test_a_loss_the_closing_hop_fails_after_is_still_found(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded
    ):
        """The owner-loss failure a departing owner's own cleanup would bury.

        The call is classified as a departure and tagged, and then the hop
        closes and fails doing so. Left to escape, that failure takes the
        tag's place; the recovery then finds no failure to act on, re-raises,
        and masking hands the client `Error calling tool
        'send_connection_request'` about a call that may have reached LinkedIn.
        That is the #891 damage on the path the #1008 answers never reach.

        Every other test here lets the hop close cleanly, and all of them pass
        with the closing boundary unguarded.
        """
        backend, elected, _replacement, elections = _upgraded
        ran: list[str] = []
        gone = _DiesAsTheCallsHopCloses(
            self._mutating_owner(ran), respond=_STAND_INS["sse ended"]
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
        assert gone.closing_failures == 1, "the hop closed without failing"

    async def test_an_answer_survives_a_hop_that_fails_to_close(
        self, monkeypatch: pytest.MonkeyPatch, _upgraded
    ):
        """The same displacement with nothing in flight: the result is replaced.

        The owner ran the tool and returned its result, and the hop then fails
        while closing. Left to escape, that failure replaces the answer, so
        masking turns a call that *did* act into `Error calling tool
        'send_connection_request'` — which a client reads as a call to make
        again, for the one tool where that sends a second connection request.
        """
        backend, _elected, _replacement, elections = _upgraded
        ran: list[str] = []
        dying = _DiesAsTheCallsHopCloses(self._mutating_owner(ran))
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
        assert dying.closing_failures == 1, "the hop closed without failing"

    async def test_a_caller_that_gives_up_at_the_close_stays_cancelled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The one failure at this boundary that is not the hop's: a cancel.

        The guard catches `Exception`, and the whole distance between that and
        `BaseException` sits here: a `CancelledError` is a caller giving up
        rather than the owner's hop failing, and a guard that returned for one
        would tell a caller that had already walked away that its call went
        through. Delivered with the answer in hand, as the hop closes.
        """
        ran: list[str] = []

        class GivesUpAsTheHopCloses(Interceptor):
            async def closed(self, methods: list[str]) -> None:
                if "tools/call" in methods:
                    giving_up = asyncio.current_task()
                    assert giving_up is not None
                    giving_up.cancel()
                    await asyncio.sleep(0)

        owner = GivesUpAsTheHopCloses(self._mutating_owner(ran))
        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        forwarder = OwnerForwarder(
            _backend(_attachment(tmp_path), tmp_path), timeout=5.0
        )
        went_on: list[str] = []

        async def gives_up_with_the_answer_in_hand() -> None:
            await forwarder.call("send_connection_request", {})
            went_on.append("the close answered a caller that had gone")

        operation = asyncio.create_task(gives_up_with_the_answer_in_hand())
        with pytest.raises(asyncio.CancelledError):
            await operation

        assert went_on == [], "a caller that gave up was carried on regardless"
        assert ran == ["sent"], "the owner did not run the call exactly once"


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
    """An owner's own refusal of a call, as `daemon.liveness` returns it."""
    from linkedin_mcp_server.daemon.liveness import REFUSAL_KEY

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
        from linkedin_mcp_server.daemon.proxy import (
            FrontendCallHeartbeatMiddleware,
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
        from linkedin_mcp_server.daemon.proxy import (
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
        from linkedin_mcp_server.daemon.proxy import (
            FrontendCallHeartbeatMiddleware,
            FrontendCallMiddleware,
        )

        failed = _attachment(tmp_path)
        replacement = _attachment(tmp_path, port=failed.descriptor.port + 1)
        failed_id = failed.descriptor.instance_id
        backend = _backend(failed, tmp_path)
        told_to_bury: list[set[str]] = []

        def elect(*_args: Any, buried: Any = frozenset(), **_kwargs: Any):
            told_to_bury.append(set(buried))
            return _elected(replacement)

        monkeypatch.setattr("linkedin_mcp_server.daemon.election.obtain_owner", elect)
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

        result = await FrontendCallMiddleware(backend, budget=30.0).on_call_tool(
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
        from linkedin_mcp_server.daemon.proxy import FrontendCallHeartbeatMiddleware

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
        from linkedin_mcp_server.daemon.proxy import OwnerUnreachableError

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
                protocol=daemon_descriptor.PROTOCOL_VERSION + 1,
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
            "another protocol",
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
        from linkedin_mcp_server.daemon.proxy import OwnerFailure

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

        monkeypatch.setattr("linkedin_mcp_server.daemon.election.obtain_owner", elect)

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
        from linkedin_mcp_server.daemon.proxy import OwnerFailure

        failed = _attachment(tmp_path)
        backend = _backend(failed, tmp_path)
        elections: list[set[str]] = []

        def elect(*_args: Any, buried: Any = frozenset(), **_kwargs: Any):
            elections.append(set(buried))
            raise RuntimeError("nobody could be started")

        monkeypatch.setattr("linkedin_mcp_server.daemon.election.obtain_owner", elect)

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
        from linkedin_mcp_server.daemon.proxy import OwnerFailure

        owner = _attachment(tmp_path)
        backend = _backend(owner, tmp_path)
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon.election.obtain_owner",
            lambda *_a, **_k: _elected(owner),
        )

        found = await backend.recover(
            owner.descriptor.instance_id, classification=OwnerFailure.TOKEN_REJECTED
        )

        assert found is None

    async def test_a_written_off_owner_gets_no_listing_and_no_new_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.daemon.proxy import (
            FrontendCallHeartbeatMiddleware,
            OwnerFailure,
            OwnerUnreachableError,
        )

        owner = _attachment(tmp_path)
        backend = _backend(owner, tmp_path)
        backend.note_failure(owner.descriptor.instance_id, OwnerFailure.RETIRING)
        reached: list[str] = []
        _reach_owners_in_process(monkeypatch, lambda url: reached.append(url))

        with pytest.raises(OwnerUnreachableError) as listing:
            await OwnerForwarder(backend, timeout=1.0).list_tools()
        assert listing.value.nothing_was_sent is True
        assert listing.value.classification is OwnerFailure.RETIRING
        assert reached == [], "a written-off owner was sent a listing"

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

    async def test_a_bound_call_not_yet_sent_gets_no_hop_to_a_written_off_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A binding is made before the hop is opened. Until the request has
        # been sent, the owner it names is asked about again, and a burial that
        # landed meanwhile refuses the hop with a not-sent failure naming that
        # owner rather than quietly swapping in another one.
        from linkedin_mcp_server.daemon.proxy import (
            OwnerFailure,
            OwnerUnreachableError,
        )

        owner = _attachment(tmp_path)
        backend = _backend(owner, tmp_path)
        reached: list[str] = []
        _reach_owners_in_process(monkeypatch, lambda url: reached.append(url))
        with bound_to(CallBinding("v1." + "a" * 32, owner)):
            backend.note_failure(owner.descriptor.instance_id, OwnerFailure.RETIRING)
            with pytest.raises(OwnerUnreachableError) as refused:
                await OwnerForwarder(backend, timeout=1.0).list_tools()

        assert refused.value.nothing_was_sent is True
        assert refused.value.instance_id == owner.descriptor.instance_id
        assert refused.value.classification is OwnerFailure.RETIRING
        assert reached == []

    async def test_a_second_attempts_burial_is_kept_for_the_next_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The second failure is not recovered from, but what it learned about
        # the replacement is not thrown away either.
        from linkedin_mcp_server.daemon.proxy import (
            FrontendCallMiddleware,
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

        monkeypatch.setattr("linkedin_mcp_server.daemon.election.obtain_owner", elect)
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

        with pytest.raises(ToolError, match="could not reach a new one"):
            await FrontendCallMiddleware(backend, budget=30.0).on_call_tool(
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
            "linkedin_mcp_server.daemon.election.obtain_owner",
            lambda *_a, **_k: _elected(self._control_only(tmp_path)),
        )

        assert await backend.recover(failed.descriptor.instance_id) is None
        assert backend.attachment is failed

    async def test_a_bound_call_cannot_open_a_hop_to_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        backend = _backend(_attachment(tmp_path), tmp_path)
        reached: list[str] = []
        _reach_owners_in_process(monkeypatch, lambda url: reached.append(url))
        with bound_to(CallBinding("v1." + "b" * 32, self._control_only(tmp_path))):
            with pytest.raises(ValueError, match="control only"):
                await OwnerForwarder(backend, timeout=1.0).list_tools()
        assert reached == []

    async def test_a_call_is_not_preflighted_or_sent_to_one(self, tmp_path: Path):
        from linkedin_mcp_server.daemon.proxy import FrontendCallHeartbeatMiddleware

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
    """The composed bound, counted through the two middlewares a proxy installs.

    The call's own outermost, then the heartbeat. The longest sequence that
    can still end: the first invocation fails once and is recovered, its
    repeat comes back asking for a sign-in, and the read-only replay fails
    twice. Every preflight, dispatch and election is counted
    separately. The bound is at most four preflights, four dispatches and four
    election joins; fewer elections run when no burial news arrives while one
    is in flight, or when flights are shared.
    """

    async def test_without_late_burial_news_a_call_costs_two_elections(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.daemon import auth_repair as daemon_auth
        from linkedin_mcp_server.daemon.auth_repair import MARKER_KEY, MARKER_VERSION
        from linkedin_mcp_server.daemon.proxy import (
            FrontendCallHeartbeatMiddleware,
            FrontendCallMiddleware,
            OwnerUnreachableError,
        )

        owners = [_attachment(tmp_path, port=51300 + n) for n in range(4)]
        backend = _backend(owners[0], tmp_path)
        elections = 0

        def elect(*_args: Any, **_kwargs: Any):
            nonlocal elections
            elections += 1
            return _elected(owners[elections])

        monkeypatch.setattr("linkedin_mcp_server.daemon.election.obtain_owner", elect)

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

        async def through_the_heartbeat(context: Any) -> Any:
            return await heartbeat.on_call_tool(context, dispatch)  # ty: ignore

        tool = MagicMock()
        tool.annotations = MagicMock(read_only_hint=True)
        context = MagicMock()
        context.message.name = "get_person_profile"
        context.fastmcp_context.fastmcp.get_tool = AsyncMock(return_value=tool)

        with pytest.raises(ToolError, match="could not reach a new one"):
            await FrontendCallMiddleware(backend, budget=30.0).on_call_tool(
                context,
                through_the_heartbeat,
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
        from linkedin_mcp_server.daemon import auth_repair as daemon_auth
        from linkedin_mcp_server.daemon.auth_repair import MARKER_KEY, MARKER_VERSION
        from linkedin_mcp_server.daemon.proxy import (
            FrontendCallHeartbeatMiddleware,
            FrontendCallMiddleware,
            OwnerFailure,
            OwnerUnreachableError,
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

        monkeypatch.setattr("linkedin_mcp_server.daemon.election.obtain_owner", elect)

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

        async def through_the_heartbeat(context: Any) -> Any:
            return await heartbeat.on_call_tool(context, dispatch)  # ty: ignore

        tool = MagicMock()
        tool.annotations = MagicMock(read_only_hint=True)
        context = MagicMock()
        context.message.name = "get_person_profile"
        context.fastmcp_context.fastmcp.get_tool = AsyncMock(return_value=tool)

        with pytest.raises(ToolError, match="could not reach a new one"):
            await FrontendCallMiddleware(backend, budget=30.0).on_call_tool(
                context,
                through_the_heartbeat,
            )

        assert len(preflights) == 4
        assert len(dispatches) == 4
        assert told_to_bury == [0, 1, 1, 2]
        assert dispatches == preflights
        # No dispatch went to an owner already written off when it was sent.
        assert written_off_at_dispatch == [False] * 4


class _HeldElection:
    """An election that runs until a test lets it finish, counted.

    Runs in the worker thread `DaemonProxyBackend.recover` hands it to, so a
    caller that stops waiting cannot stop it either, which is the property the
    tests below lean on.
    """

    def __init__(self, found: Attachment) -> None:
        self.found = found
        self.started = 0
        self.finished = 0
        self.release = threading.Event()

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        self.started += 1
        self.release.wait(timeout=30)
        self.finished += 1
        return _elected(self.found)


class TestOneDeadlinePerCall:
    """A call's wait ends at one deadline, counted from its arrival.

    Whatever it is waiting on at the time: here, a recovery that cannot finish
    inside the budget. The call is answered by then, with the answer its
    dispatch state allows, while the election goes on for whoever needs it next.
    """

    #: The budget these calls get, from `tool_timeout` with the margin zeroed.
    BUDGET = 1.0
    #: How late an answer may be and still count as on time. Generous, because
    #: what it separates is the budget from the election's thirty seconds.
    SLACK = 2.0

    @pytest.fixture(autouse=True)
    def _no_margin(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon.proxy._TIMEOUT_MARGIN_SECONDS", 0.0
        )

    @classmethod
    def _proxy(cls, backend: DaemonProxyBackend) -> FastMCP:
        from linkedin_mcp_server.server import create_mcp_server
        from linkedin_mcp_server.server_role import ServerRole

        return create_mcp_server(
            role=ServerRole.PROXY, proxy_backend=backend, tool_timeout=cls.BUDGET
        )

    @staticmethod
    async def _let_it_finish(backend: DaemonProxyBackend, election: _HeldElection):
        election.release.set()
        flight = backend._electing
        if flight is not None:
            await flight.task

    async def test_a_call_that_never_left_is_told_so_within_its_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        gone = _attachment(tmp_path)
        backend = _backend(gone, tmp_path)
        election = _HeldElection(_attachment(tmp_path, port=gone.descriptor.port + 1))
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon.election.obtain_owner", election
        )
        # Nothing answers anywhere: the preflight is refused, so nothing is sent.
        _reach_owners_in_process(monkeypatch, lambda _url: None)
        loop = asyncio.get_running_loop()

        try:
            async with Client(self._proxy(backend)) as client:
                began = loop.time()
                result = await client.call_tool(
                    "send_connection_request", {}, raise_on_error=False
                )
                took = loop.time() - began
            still_electing = election.finished == 0
        finally:
            await self._let_it_finish(backend, election)

        assert result.is_error is True
        assert result.structured_content is None, (
            "a call never sent was reported unknown"
        )
        assert "could not reach a new one" in result.content[0].text
        assert took < self.BUDGET + self.SLACK, f"answered after {took:.2f}s"
        assert still_electing, "the call waited for the election to end"
        assert election.started == 1

    async def test_a_call_that_may_have_run_is_unknown_within_its_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        original = _OwnerOverHttp()
        original.fail["tools/call"] = original.after_running(_STAND_INS["500"])
        replacement = _OwnerOverHttp()
        elected = _attachment(tmp_path)
        standby = _attachment(tmp_path, port=elected.descriptor.port + 1)
        election = _HeldElection(standby)
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon.election.obtain_owner", election
        )
        owners = {_address(elected): original, _address(standby): replacement}
        _serve_over_http(monkeypatch, owners.__getitem__)
        backend = _backend(elected, tmp_path)
        loop = asyncio.get_running_loop()

        try:
            async with Client(self._proxy(backend)) as client:
                began = loop.time()
                result = await client.call_tool(
                    "send_connection_request", {}, raise_on_error=False
                )
                took = loop.time() - began
            still_electing = election.finished == 0
        finally:
            await self._let_it_finish(backend, election)

        assert original.effects == 1
        assert replacement.effects == 0, "a call that had run was sent again"
        assert result.is_error is True
        assert result.structured_content is not None
        assert result.structured_content["status"] == "outcome_unknown"
        assert result.structured_content["retry_safe"] is False
        assert took < self.BUDGET + self.SLACK, f"answered after {took:.2f}s"
        assert still_electing, "the call waited for the election to end"

    async def test_a_request_the_deadline_cuts_off_is_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The owner took the request and is still running it at the deadline.

        Only the dispatch record says the request left; the owner said
        nothing. The call is told the outcome is unknown, never that it was not
        sent, and a slow owner is not a lost one: nobody is elected.
        """
        running = asyncio.Event()
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            running.set()
            await asyncio.Event().wait()
            return "never"

        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        elections: list[str] = []
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon.election.obtain_owner",
            lambda *_a, **_k: elections.append("elected"),
        )
        backend = _backend(_attachment(tmp_path), tmp_path)
        loop = asyncio.get_running_loop()

        async with Client(self._proxy(backend)) as client:
            began = loop.time()
            result = await client.call_tool(
                "send_connection_request", {}, raise_on_error=False
            )
            took = loop.time() - began

        assert running.is_set(), "the request never reached the owner"
        assert result.is_error is True
        assert result.structured_content is not None
        assert result.structured_content["status"] == "outcome_unknown"
        assert took < self.BUDGET + self.SLACK, f"answered after {took:.2f}s"
        assert elections == []

    async def test_one_caller_leaves_while_the_election_it_started_goes_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Three callers, one election, and each told what fits how far it got.

        The first starts the election and runs out of time inside it. The
        second joins while the first is still waiting, the third only after it
        has gone. Leaving must not cancel the election or clear it, or the
        third would start a second one; and the election's late answer must
        still serve the two that stayed.
        """
        from linkedin_mcp_server.daemon.proxy import (
            FrontendCallMiddleware,
            OwnerUnreachableError,
        )

        gone = _attachment(tmp_path)
        replacement = _attachment(tmp_path, port=gone.descriptor.port + 1)
        backend = _backend(gone, tmp_path)
        election = _HeldElection(replacement)
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon.election.obtain_owner", election
        )
        failed_on_the_old_owner: list[str] = []

        def caller(name: str, *, sent: bool, read_only: bool) -> Any:
            tool = MagicMock()
            tool.annotations = MagicMock(read_only_hint=read_only)
            context = MagicMock()
            context.message.name = name
            context.fastmcp_context.fastmcp.get_tool = AsyncMock(return_value=tool)
            attempts = 0

            async def call_next(_context: Any) -> Any:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    failed_on_the_old_owner.append(name)
                    raise OwnerUnreachableError(
                        instance_id=gone.descriptor.instance_id,
                        nothing_was_sent=not sent,
                        cause=httpx2.ConnectError("gone"),
                    )
                return f"{name} on the replacement"

            return context, call_next

        async def call(budget: float, name: str, **how: bool) -> Any:
            context, call_next = caller(name, **how)
            try:
                return await FrontendCallMiddleware(
                    backend, budget=budget
                ).on_call_tool(context, call_next)
            except Exception as escaped:
                return _Escaped(escaped)

        loop = asyncio.get_running_loop()
        began = loop.time()
        try:
            first = asyncio.create_task(call(0.5, "first", sent=False, read_only=False))
            await _until_true(lambda: election.started == 1)
            second = asyncio.create_task(
                call(30.0, "second", sent=False, read_only=False)
            )
            await _until_true(lambda: "second" in failed_on_the_old_owner)
            first_answer = await asyncio.wait_for(first, timeout=5)
            first_took = loop.time() - began
            third = asyncio.create_task(call(30.0, "third", sent=True, read_only=False))
            await _until_true(lambda: "third" in failed_on_the_old_owner)
            assert election.finished == 0, "the election ended before the test let it"
            election.release.set()
            second_answer, third_answer = await asyncio.wait_for(
                asyncio.gather(second, third), timeout=10
            )
        finally:
            election.release.set()

        assert election.started == 1, "a caller that left let a second election start"
        assert isinstance(first_answer, _Escaped)
        assert isinstance(first_answer.error, ToolError)
        assert "could not reach a new one" in str(first_answer.error)
        assert first_took < 0.5 + self.SLACK, f"the first waited {first_took:.2f}s"
        assert second_answer == "second on the replacement"
        assert third_answer.structured_content["status"] == "outcome_unknown"
        assert backend.attachment.descriptor.instance_id == (
            replacement.descriptor.instance_id
        )


class _HeldAtConnect(Interceptor):
    """An owner whose negotiation is held until a test lets it proceed.

    The window between a call's binding and its send: the hop is being opened
    and negotiated, and another call can write the owner off meanwhile.
    """

    def __init__(self, owner: FastMCP) -> None:
        super().__init__(owner)
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def handle(
        self, message: dict[str, Any] | None, request: httpx2.Request, forward: Forward
    ) -> httpx2.Response:
        if method_of(message) == "server/discover":
            self.reached.set()
            await self.release.wait()
        return await forward()


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

        from linkedin_mcp_server.daemon import election as daemon_election
        from linkedin_mcp_server.daemon.proxy import (
            FrontendCallHeartbeatMiddleware,
            FrontendCallMiddleware,
            OwnerFailure,
            OwnerUnreachableError,
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
            await FrontendCallMiddleware(backend, budget=30.0).on_call_tool(
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
        # The call is bound and its client is negotiating when another
        # call writes the owner off. The client is real and the owner is an
        # in-process server, so a request that got through would run its tool.
        from linkedin_mcp_server.daemon.proxy import (
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

        forwarder = OwnerForwarder(backend, timeout=5.0)

        async def dispatch(_context: Any) -> Any:
            return await forwarder.call("send_connection_request", {})

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

    async def test_a_burial_before_the_negotiation_sends_it_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The owner is written off as the hop opens its transport, before the
        # negotiation goes out: not even that reaches it.
        from linkedin_mcp_server.daemon import forwarder as forwarder_module
        from linkedin_mcp_server.daemon.proxy import OwnerFailure, OwnerUnreachableError

        owner = FastMCP("owner")
        reached = _RecordsRequests(owner)
        _reach_owners_in_process(monkeypatch, lambda _url: reached)
        attachment = _attachment(tmp_path)
        backend = _backend(attachment, tmp_path)
        routed = forwarder_module.owner_transport
        loop = asyncio.get_running_loop()

        def opened() -> httpx2.AsyncBaseTransport:
            loop.call_soon(
                backend.note_failure,
                attachment.descriptor.instance_id,
                OwnerFailure.RETIRING,
            )
            return routed()

        monkeypatch.setattr(forwarder_module, "owner_transport", opened)

        with pytest.raises(OwnerUnreachableError) as refused:
            await OwnerForwarder(backend, timeout=5.0).call(
                "send_connection_request", {}
            )

        assert reached.requests == []
        assert refused.value.nothing_was_sent is True
        assert refused.value.classification is OwnerFailure.RETIRING

    async def test_a_request_already_sent_keeps_its_owner_and_its_heartbeats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The positive control. The request reached the owner and is running
        # when the owner is written off and a replacement adopted. Moving its
        # heartbeats, or refusing it now, would get a live call cancelled by the
        # owner running it.
        from linkedin_mcp_server.daemon.proxy import (
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
        monkeypatch.setattr("linkedin_mcp_server.daemon.proxy.HEARTBEAT_SECONDS", 0.05)
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

        forwarder = OwnerForwarder(backend, timeout=5.0)

        async def dispatch(_context: Any) -> Any:
            return await forwarder.call("send_connection_request", {})

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

    A check made before an await is older than the send by that await, and a
    burial queued in it runs first. These queue the burial as the owner's
    answer to the negotiation arrives, after every check the hop made while
    opening, with a real in-process owner, so a request that got past the last
    check would run its tool.
    """

    @staticmethod
    def _owner(ran: list[str]) -> FastMCP:
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            ran.append("sent")
            return "sent"

        return owner

    @pytest.mark.parametrize("buried", [True, False], ids=["buried", "control"])
    async def test_a_burial_queued_as_the_negotiation_answers_stops_the_send(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, buried: bool
    ):
        from linkedin_mcp_server.daemon.proxy import (
            FrontendCallHeartbeatMiddleware,
            OwnerFailure,
            OwnerUnreachableError,
        )

        ran: list[str] = []
        old = _attachment(tmp_path)
        backend = _backend(old, tmp_path)
        loop = asyncio.get_running_loop()

        class BuriedAsItIsEntered(Interceptor):
            async def handle(
                self,
                message: dict[str, Any] | None,
                request: httpx2.Request,
                forward: Forward,
            ) -> httpx2.Response:
                response = await forward()
                if buried and method_of(message) == "server/discover":
                    loop.call_soon(
                        backend.note_failure,
                        old.descriptor.instance_id,
                        OwnerFailure.RETIRING,
                    )
                return response

        owner = BuriedAsItIsEntered(self._owner(ran))
        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        heartbeat = FrontendCallHeartbeatMiddleware(backend)
        forwarder = OwnerForwarder(backend, timeout=5.0)

        async def dispatch(_context: Any) -> Any:
            return await forwarder.call("send_connection_request", {})

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

    async def test_a_call_already_sent_is_not_refused_on_its_own_hop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Once the bound call's tool request is out, its owner is fixed, and
        # its answer is taken from that owner even after it was written off:
        # refusing it would abandon a call the owner may still be running for
        # this frontend.
        from linkedin_mcp_server.daemon.core.call import Dispatch
        from linkedin_mcp_server.daemon.proxy import OwnerFailure

        ran: list[str] = []
        attachment = _attachment(tmp_path)
        backend = _backend(attachment, tmp_path)

        class BuriedOnceSent(Interceptor):
            async def handle(
                self,
                message: dict[str, Any] | None,
                request: httpx2.Request,
                forward: Forward,
            ) -> httpx2.Response:
                if method_of(message) == "tools/call":
                    backend.note_failure(
                        attachment.descriptor.instance_id, OwnerFailure.RETIRING
                    )
                return await forward()

        owner = BuriedOnceSent(self._owner(ran))
        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        binding = CallBinding("a-call", attachment)

        with bound_to(binding):
            result = await OwnerForwarder(backend, timeout=5.0).call(
                "send_connection_request", {}
            )

        assert result.is_error is False
        assert ran == ["sent"]
        assert binding.dispatch.state is Dispatch.ANSWERED

    async def test_a_listing_to_an_owner_written_off_during_setup_is_not_sent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # An unbound listing picks its owner when the hop opens. An owner
        # written off while that hop is being negotiated gets no listing: a
        # same-configuration older build stays attachable until it turns over,
        # and its tool schemas may differ from its replacement's.
        from fastmcp.server.middleware import Middleware as ServerMiddleware

        from linkedin_mcp_server.daemon.proxy import (
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

        listing_task = asyncio.create_task(
            OwnerForwarder(backend, timeout=5.0).list_tools()
        )
        await asyncio.wait_for(held.reached.wait(), timeout=5)
        backend.note_failure(attachment.descriptor.instance_id, OwnerFailure.RETIRING)
        held.release.set()

        with pytest.raises(OwnerUnreachableError) as refused:
            await asyncio.wait_for(listing_task, timeout=5)

        assert listed == []
        assert refused.value.nothing_was_sent is True
        assert refused.value.instance_id == attachment.descriptor.instance_id
        assert refused.value.classification is OwnerFailure.RETIRING


def _leaves(failure: BaseException) -> list[type[BaseException]]:
    """The type of every exception under *failure*, through causes and groups."""
    found: list[type[BaseException]] = []
    pending: list[BaseException] = [failure]
    while pending:
        current = pending.pop()
        found.append(type(current))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return found


class _WitnessesTheRecord(Interceptor):
    """An in-process owner that notes the bound call's dispatch state per request.

    Read in the task that posts the request, which carries the frontend's
    binding: the state as each request leaves, before the owner acts on it.
    Requests only: a notification is posted by a task of its own, and may
    leave after the session has already taken the next request.
    """

    def __init__(self, owner: FastMCP) -> None:
        super().__init__(owner)
        self.seen: list[tuple[str, Any]] = []

    async def handle(
        self, message: dict[str, Any] | None, request: httpx2.Request, forward: Forward
    ) -> httpx2.Response:
        method = method_of(message)
        if method is not None and message is not None and "id" in message:
            bound = _call_being_made.get()
            self.seen.append((method, None if bound is None else bound.dispatch.state))
        return await forward()


class TestTheDispatchRecord:
    """How far the tool request got, recorded where the session takes it.

    Only the tool request moves the record, and only forward: a listing or the
    negotiation changes nothing and stays repeatable, and nothing that happens
    after a request may have left can say it did not.
    """

    async def test_only_the_tool_request_moves_the_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.daemon.core.call import Dispatch

        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            return "sent"

        witness = _WitnessesTheRecord(owner)
        _reach_owners_in_process(monkeypatch, lambda _url: witness)
        attachment = _attachment(tmp_path)
        forwarder = OwnerForwarder(_backend(attachment, tmp_path), timeout=5.0)
        binding = CallBinding("a-call", attachment)

        with bound_to(binding):
            await forwarder.list_tools()
            assert binding.dispatch.state is Dispatch.UNSENT, "a listing moved it"
            result = await forwarder.call("send_connection_request", {})

        assert result.is_error is False
        assert binding.dispatch.state is Dispatch.ANSWERED
        assert witness.seen == [
            ("server/discover", Dispatch.UNSENT),
            ("tools/list", Dispatch.UNSENT),
            ("server/discover", Dispatch.UNSENT),
            ("tools/call", Dispatch.POSSIBLY_SENT),
        ]

    async def test_the_production_proxy_moves_it_for_the_call_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Through the whole proxy, where the lookup shares the call's record.

        The lookup in front of the call is a hop of its own, bound to the same
        call, and must leave the record as it found it.
        """
        from linkedin_mcp_server.daemon.core.call import Dispatch
        from linkedin_mcp_server.server import create_mcp_server
        from linkedin_mcp_server.server_role import ServerRole

        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send(message: str) -> str:
            return message

        witness = _WitnessesTheRecord(owner)
        _reach_owners_in_process(monkeypatch, lambda _url: witness)
        backend = _backend(_attachment(tmp_path), tmp_path)
        proxy = create_mcp_server(
            role=ServerRole.PROXY, proxy_backend=backend, tool_timeout=5.0
        )

        async with Client(proxy) as client:
            result = await client.call_tool(
                "send_connection_request", {"message": "hi"}
            )

        assert result.data == "hi"
        bound = [(method, state) for method, state in witness.seen if state]
        assert ("tools/list", Dispatch.UNSENT) in bound, witness.seen
        assert [m for m, _state in bound].count("tools/call") == 1, witness.seen
        assert ("tools/call", Dispatch.POSSIBLY_SENT) in bound, witness.seen

    async def test_a_connect_error_after_the_request_left_is_no_proof(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A connection failure that follows the send says nothing about it.

        The owner ran the call and the connection then failed with a connect
        error. Read as "no connection was made", it would let a call that ran
        be sent again.
        """
        from linkedin_mcp_server.daemon.core.call import Dispatch
        from linkedin_mcp_server.daemon.forwarder import OwnerUnreachableError

        ran: list[str] = []
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            ran.append("sent")
            return "sent"

        class LostOnceItRan(Interceptor):
            async def handle(
                self,
                message: dict[str, Any] | None,
                request: httpx2.Request,
                forward: Forward,
            ) -> httpx2.Response:
                response = await forward()
                if method_of(message) == "tools/call":
                    await response.aread()
                    raise httpx2.ConnectError("could not reach the owner again")
                return response

        lost = LostOnceItRan(owner)
        _reach_owners_in_process(monkeypatch, lambda _url: lost)
        attachment = _attachment(tmp_path)
        binding = CallBinding("a-call", attachment)

        with bound_to(binding), pytest.raises(OwnerUnreachableError) as failed:
            await OwnerForwarder(_backend(attachment, tmp_path), timeout=5.0).call(
                "send_connection_request", {}
            )

        assert ran == ["sent"]
        assert binding.dispatch.state is Dispatch.POSSIBLY_SENT
        assert failed.value.nothing_was_sent is False
        # The fault this pins was really delivered: a connect error, under the
        # failure the tag was made from.
        assert httpx2.ConnectError in _leaves(failed.value), _leaves(failed.value)

    async def test_what_a_call_sent_outlives_its_hop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A later hop of the same attempt still knows the call went out.

        The call ran and answered, and the next request bound to that attempt
        finds nobody. Nothing reaching the owner at that point is not evidence
        that nothing was sent: this attempt did send a tool request, and the
        failure has to say it may have arrived.
        """
        from linkedin_mcp_server.daemon.forwarder import OwnerUnreachableError

        ran: list[str] = []
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            ran.append("sent")
            return "sent"

        gone: list[bool] = []
        _reach_owners_in_process(monkeypatch, lambda _url: None if gone else owner)
        attachment = _attachment(tmp_path)
        forwarder = OwnerForwarder(_backend(attachment, tmp_path), timeout=5.0)

        with bound_to(CallBinding("a-call", attachment)):
            await forwarder.call("send_connection_request", {})
            gone.append(True)
            with pytest.raises(OwnerUnreachableError) as refused:
                await forwarder.list_tools()

        assert ran == ["sent"]
        assert refused.value.nothing_was_sent is False

    async def test_a_caller_that_gives_up_mid_call_stays_cancelled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Giving up while the owner works is a cancellation, not an owner loss.

        Converted into a tagged failure it would be recovered from, and a
        caller that had walked away would be answered or, worse, repeated for.
        """
        from linkedin_mcp_server.daemon.core.call import Dispatch

        started = asyncio.Event()
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            started.set()
            await asyncio.Event().wait()
            return "never"

        _reach_owners_in_process(monkeypatch, lambda _url: owner)
        attachment = _attachment(tmp_path)
        forwarder = OwnerForwarder(_backend(attachment, tmp_path), timeout=5.0)
        binding = CallBinding("a-call", attachment)

        async def call() -> Any:
            with bound_to(binding):
                return await forwarder.call("send_connection_request", {})

        calling = asyncio.create_task(call())
        await asyncio.wait_for(started.wait(), timeout=5)
        calling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await calling

        assert binding.dispatch.state is Dispatch.POSSIBLY_SENT


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


def _no_such_method(body: dict[str, Any]) -> httpx2.Response:
    """The owner's own unknown-method answer, at the status this era maps it to."""
    error = {"code": mt.METHOD_NOT_FOUND, "message": "Method not found"}
    return httpx2.Response(
        404, json={"jsonrpc": "2.0", "id": body["id"], "error": error}
    )


def _discovered_as(*versions: str) -> Callable[[dict[str, Any]], httpx2.Response]:
    """A ``server/discover`` result listing *versions*, whatever was proposed."""

    def answer(body: dict[str, Any]) -> httpx2.Response:
        result = {
            "ttlMs": 0,
            "cacheScope": "private",
            "resultType": "complete",
            "supportedVersions": list(versions),
            "capabilities": {"tools": {"listChanged": False}},
        }
        return httpx2.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    return answer


def _unsupported_version(
    body: dict[str, Any], proposed: str, served: tuple[str, ...]
) -> httpx2.Response:
    """A FastMCP owner's answer to ``server/discover`` at a version it does not serve."""
    return httpx2.Response(
        400,
        json={
            "jsonrpc": "2.0",
            "id": body["id"],
            "error": {
                "code": mt.UNSUPPORTED_PROTOCOL_VERSION,
                "message": "Unsupported protocol version",
                "data": {"supported": list(served), "requested": proposed},
            },
        },
    )


class _OwnerOverHttp:
    """An owner at the far end of the SDK's own HTTP client, answering by hand.

    Installed as an ``httpx2.MockTransport`` under the forwarder's own HTTP
    client, so everything between the forwarder and the bytes is real: the
    SDK's streamable HTTP transport and session, and the forwarder's hop. The
    owner is what stands in, and it is small enough to fail on command: *fail*
    maps a JSON-RPC method to the response it gets. No socket is opened and no
    process dies here: these are fault injections into the SDK's HTTP
    handling. Losing a real owner process is witnessed by the loopback owners
    in ``tests/test_daemon_election.py``.

    *effects* counts the tool calls this owner ran. *versions* are the protocol
    versions it serves; ``server/discover`` proposing any other is refused the
    way a real owner refuses it. *methods* is every JSON-RPC request that
    arrived, in order. The answers are a FastMCP owner's on the 2026-07-28
    wire, trimmed: each request one POST, no session id.
    """

    def __init__(
        self,
        *,
        fail: dict[str, Callable[[dict[str, Any]], Any]] | None = None,
        versions: tuple[str, ...] = (HOP_PROTOCOL_VERSION,),
    ) -> None:
        self.fail = dict(fail or {})
        self.versions = versions
        self.effects = 0
        self.requests: list[httpx2.Request] = []
        self.methods: list[str] = []
        self.bodies: list[dict[str, Any]] = []
        #: Called as a tool request arrives, before it runs.
        self.on_call: Callable[[], None] | None = None

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        from linkedin_mcp_server.daemon.liveness import HEARTBEAT_PATH

        self.requests.append(request)
        if request.url.path == HEARTBEAT_PATH:
            return httpx2.Response(200, json={"watched": False})
        if request.method != "POST":
            return httpx2.Response(405)
        body = json.loads(request.content)
        self.bodies.append(body)
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
        if method == "server/discover":
            proposed = body["params"]["_meta"][mt.PROTOCOL_VERSION_META_KEY]
            if proposed not in self.versions:
                return _unsupported_version(body, proposed, self.versions)
            return _discovered_as(*self.versions)(body)
        if method == "tools/list":
            tool = {
                "name": "send_connection_request",
                "inputSchema": {"type": "object"},
            }
            result: dict[str, Any] = {
                "ttlMs": 0,
                "cacheScope": "private",
                "resultType": "complete",
                "tools": [tool],
            }
        elif method == "tools/call":
            self.effects += 1
            result = {
                "content": [{"type": "text", "text": "sent"}],
                "isError": False,
                "resultType": "complete",
            }
        else:
            return _no_such_method(body)
        return httpx2.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
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

    The hop's transport is the forwarder's one seam, and the heartbeat
    preflight goes through ``daemon.owner.direct_async_http_client`` itself,
    wrapped rather than replaced, so its forced ``trust_env=False`` stays on
    the path. A client given a transport of its own keeps it.
    """
    from linkedin_mcp_server.daemon import forwarder
    from linkedin_mcp_server.daemon import owner as daemon_owner

    real = daemon_owner.direct_async_http_client

    async def route(request: httpx2.Request) -> httpx2.Response:
        return await owner_at(f"{request.url.host}:{request.url.port}")(request)

    def factory(**kwargs: Any) -> httpx2.AsyncClient:
        kwargs.setdefault("transport", httpx2.MockTransport(route))
        return real(**kwargs)

    monkeypatch.setattr(daemon_owner, "direct_async_http_client", factory)
    monkeypatch.setattr(
        forwarder, "owner_transport", lambda: httpx2.MockTransport(route)
    )


def _address(attachment: Attachment) -> str:
    return f"{attachment.descriptor.host}:{attachment.descriptor.port}"


class TestTheSdksOwnHttpErrors:
    """SDK v2 answers a failed HTTP exchange with an error it writes itself.

    V1 raised on a non-2xx status and failed the whole transport; v2 turns it
    into a JSON-RPC error for that one request (SDK ``docs/migration.md:2178``),
    which reaches the hop as the same ``MCPError`` a real answer does. Driven
    through the real SDK transport, because the stand-in is only ever written
    there: a hand-made ``MCPError`` would pin the rule and not the provenance
    it is about.

    Each phase separately. Entry and listing may always be repeated and say
    so; once the tool request entered the session nothing here proves it did
    not arrive, so the call says it may have.
    """

    @staticmethod
    async def _fail_at(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        phase: str,
        respond: Callable[[dict[str, Any]], Any],
    ) -> tuple[BaseException, _OwnerOverHttp]:
        methods = {
            "entry": ("server/discover",),
            "list": ("tools/list",),
            "call": ("tools/call",),
        }
        owner = _OwnerOverHttp(fail=dict.fromkeys(methods[phase], respond))
        _serve_over_http(monkeypatch, lambda _address: owner)
        forwarder = OwnerForwarder(
            _backend(_attachment(tmp_path), tmp_path), timeout=2.0
        )
        with pytest.raises(Exception) as failed:
            if phase == "call":
                await forwarder.call("send_connection_request", {})
            else:
                await forwarder.list_tools()
        return failed.value, owner

    @pytest.mark.parametrize("phase", ["entry", "list", "call"])
    @pytest.mark.parametrize("stand_in", sorted(_STAND_INS))
    async def test_a_stand_in_is_not_taken_for_the_owners_answer(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        phase: str,
        stand_in: str,
    ):
        from linkedin_mcp_server.daemon.forwarder import (
            OwnerFailure,
            OwnerUnreachableError,
        )

        failure, owner = await self._fail_at(
            tmp_path, monkeypatch, phase, _STAND_INS[stand_in]
        )

        assert isinstance(failure, OwnerUnreachableError), (
            f"the SDK's own {stand_in} error passed as the owner's answer: {failure!r}"
        )
        assert failure.classification is OwnerFailure.OWNER_ERROR
        # The boundary answers the dispatch question, never the stand-in: only
        # the call had sent a request, and nothing says it did not arrive.
        assert failure.nothing_was_sent is (phase != "call")
        assert owner.effects == 0

    @pytest.mark.parametrize("phase", ["entry", "list", "call"])
    async def test_a_bare_404_is_a_stand_in_and_not_an_unknown_method(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
    ):
        """A 404 without a JSON-RPC body is the SDK's, whatever its code says.

        This era has no session, so the SDK writes ``METHOD_NOT_FOUND`` "Not
        Found" for it: the code a server uses to say it has no listing of a
        kind, and the one that refuses the hop's version at entry.
        """
        from linkedin_mcp_server.daemon.forwarder import OwnerUnreachableError

        failure, _owner = await self._fail_at(
            tmp_path, monkeypatch, phase, _STAND_INS["404"]
        )

        assert isinstance(failure, OwnerUnreachableError), failure
        assert str(failure.__cause__) == "Not Found"

    @pytest.mark.parametrize("phase", ["entry", "list", "call"])
    async def test_a_real_refusal_in_an_error_status_is_the_owners_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
    ):
        """A JSON-RPC error body survives a non-2xx status, and is an answer.

        Its code is the one the SDK writes for a lost session, so what keeps
        this an answer is that the owner was seen sending it.
        """
        from mcp import MCPError

        failure, owner = await self._fail_at(
            tmp_path, monkeypatch, phase, _a_genuine_refusal
        )

        assert isinstance(failure, MCPError), (
            f"a real refusal was retagged: {failure!r}"
        )
        assert failure.code == mt.INVALID_REQUEST
        assert failure.message == "Some other refusal"
        assert owner.effects == 0

    async def test_a_call_that_outlived_the_deadline_is_not_the_owners_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The session's own deadline: ``REQUEST_TIMEOUT``, not HTTP 408."""
        from linkedin_mcp_server.daemon.forwarder import OwnerUnreachableError

        async def never(_body: dict[str, Any]) -> httpx2.Response:
            await asyncio.sleep(30)
            raise AssertionError("the call was not given up on")

        owner = _OwnerOverHttp(fail={"tools/call": never})
        _serve_over_http(monkeypatch, lambda _address: owner)
        forwarder = OwnerForwarder(
            _backend(_attachment(tmp_path), tmp_path), timeout=0.3
        )

        with pytest.raises(OwnerUnreachableError) as failed:
            await forwarder.call("send_connection_request", {})

        assert getattr(failed.value.__cause__, "code", None) == mt.REQUEST_TIMEOUT
        assert failed.value.nothing_was_sent is False

    @pytest.mark.parametrize("stand_in", ["500", "sse ended"])
    async def test_an_unbound_call_that_ran_is_not_reported_unsent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stand_in: str
    ):
        """A hop with no call bound to it still knows what it sent.

        The binding belongs to a call the heartbeat middleware is watching, and
        a hop can be opened without one. Its absence says nobody else is
        keeping count, never that no tool request went out: the owner here ran
        it before its answer was lost.
        """
        from linkedin_mcp_server.daemon.forwarder import OwnerUnreachableError

        owner = _OwnerOverHttp()
        owner.fail["tools/call"] = owner.after_running(_STAND_INS[stand_in])
        _serve_over_http(monkeypatch, lambda _address: owner)
        forwarder = OwnerForwarder(
            _backend(_attachment(tmp_path), tmp_path), timeout=2.0
        )

        with pytest.raises(OwnerUnreachableError) as failed:
            await forwarder.call("send_connection_request", {})

        assert owner.effects == 1
        assert failed.value.nothing_was_sent is False


class TestTheHopsProtocolVersion:
    """The hop asks for one protocol version and accepts no other.

    The external client may negotiate whatever it likes with the frontend;
    only this internal hop is pinned, so a dependency upgrade cannot move what
    the frontend and its owner speak to each other.
    """

    async def test_the_hop_asks_for_the_pinned_version_and_speaks_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The owner also serves an era newer than the pin, and so does the SDK
        # the frontend runs, as a fresh `uvx` resolve may bring one: the SDK
        # adopts the newest era both sides list. The hop still speaks the pin.
        import mcp.client.session as sdk_session

        newer = (*sdk_session.MODERN_PROTOCOL_VERSIONS, "2099-01-01")
        monkeypatch.setattr(sdk_session, "MODERN_PROTOCOL_VERSIONS", newer)
        owner = _OwnerOverHttp(versions=(HOP_PROTOCOL_VERSION, "2099-01-01"))
        _serve_over_http(monkeypatch, lambda _address: owner)

        result = await OwnerForwarder(
            _backend(_attachment(tmp_path), tmp_path), timeout=2.0
        ).call("send_connection_request", {})

        assert result.is_error is False
        assert owner.methods == ["server/discover", "tools/call"]
        (discover,) = [b for b in owner.bodies if b.get("method") == "server/discover"]
        proposed = discover["params"]["_meta"][mt.PROTOCOL_VERSION_META_KEY]
        assert proposed == HOP_PROTOCOL_VERSION == "2026-07-28"
        (call,) = [r for r in owner.requests if b'"tools/call"' in r.content]
        assert call.headers["mcp-protocol-version"] == HOP_PROTOCOL_VERSION
        sent = json.loads(call.content)["params"]["_meta"]
        assert sent[mt.PROTOCOL_VERSION_META_KEY] == HOP_PROTOCOL_VERSION
        assert owner.effects == 1

    @pytest.mark.parametrize(
        "refusing",
        [
            pytest.param(
                lambda: _OwnerOverHttp(versions=("2025-06-18",)), id="2025-06-18"
            ),
            pytest.param(
                lambda: _OwnerOverHttp(versions=("2025-11-25",)), id="2025-11-25"
            ),
            # An owner that has no `server/discover` at all.
            pytest.param(
                lambda: _OwnerOverHttp(fail={"server/discover": _no_such_method}),
                id="no discover",
            ),
            # Not an answer a FastMCP owner gives, which refuses a version it
            # does not serve; the hop does not take the version on trust.
            pytest.param(
                lambda: _OwnerOverHttp(
                    fail={"server/discover": _discovered_as("2025-11-25")}
                ),
                id="listed without it",
            ),
        ],
    )
    async def test_an_owner_without_the_pinned_version_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        refusing: Callable[[], _OwnerOverHttp],
    ):
        """An owner that does not serve the pinned version is not spoken to.

        No tool request follows: the refusal comes before anything the owner
        could act on, so the call says nothing was sent, and it is a failure
        to reach the owner rather than an answer the caller is handed.
        """
        from linkedin_mcp_server.daemon.forwarder import (
            OwnerUnreachableError,
            _HopVersionRefused,
        )

        owner = refusing()
        _serve_over_http(monkeypatch, lambda _address: owner)

        with pytest.raises(OwnerUnreachableError) as refused:
            await OwnerForwarder(
                _backend(_attachment(tmp_path), tmp_path), timeout=2.0
            ).call("send_connection_request", {})

        assert refused.value.nothing_was_sent is True
        assert isinstance(refused.value.__cause__, _HopVersionRefused)
        assert owner.methods == ["server/discover"]
        assert owner.effects == 0

    async def test_an_owner_with_no_way_in_is_not_entered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.daemon.forwarder import OwnerUnreachableError

        owner = _OwnerOverHttp(fail={"server/discover": _STAND_INS["500"]})
        _serve_over_http(monkeypatch, lambda _address: owner)

        with pytest.raises(OwnerUnreachableError) as failed:
            await OwnerForwarder(
                _backend(_attachment(tmp_path), tmp_path), timeout=2.0
            ).call("send_connection_request", {})

        assert failed.value.nothing_was_sent is True
        assert owner.methods == ["server/discover"]
        assert owner.effects == 0

    async def test_an_unsupported_listing_is_an_empty_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """An owner with no resources says so, and that is not a departure.

        Its unknown-method answer is a real JSON-RPC error, which the forwarder
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
            "linkedin_mcp_server.daemon.election.obtain_owner",
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
            "linkedin_mcp_server.daemon.election.obtain_owner",
            lambda *_a, **_k: _elected(standby),
        )
        owners = {_address(elected): original, _address(standby): replacement}
        _serve_over_http(monkeypatch, owners.__getitem__)
        return _backend(elected, tmp_path)

    async def test_a_call_that_never_left_runs_once_on_the_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Gone after the lookup, before the call's own hop could connect.

        The call's hop fails its negotiation, before any tool request exists,
        so the one run the user asked for belongs to the replacement.
        """
        original = _OwnerOverHttp()
        replacement = _OwnerOverHttp()

        def gone_after_the_lookup(body: dict[str, Any]) -> httpx2.Response:
            del original.fail["tools/list"]
            original.fail["server/discover"] = _refused
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
        assert "tools/call" not in original.methods

    @pytest.mark.parametrize("stand_in", ["500", "sse ended"])
    async def test_a_call_that_ran_is_reported_and_never_repeated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stand_in: str
    ):
        """The owner ran the call, and its answer was lost on the way back."""
        original = _OwnerOverHttp()
        original.fail["tools/call"] = original.after_running(_STAND_INS[stand_in])
        replacement = _OwnerOverHttp()
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


class TestTheHeadersTheOwnerReceives:
    """What actually arrives at the owner, read off the outbound request.

    The frontend is served over HTTP here, so the caller's request carries
    headers of its own, the frontend's bearer among them. None of them crosses
    the hop: the owner's bearer and the bound call id are the hop's own, and
    they have to be on the request itself.
    """

    async def test_the_owners_credential_and_the_bound_call_arrive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from fastmcp.utilities.tests import asgi_client

        from linkedin_mcp_server.daemon.liveness import CALL_HEADER, HEARTBEAT_PATH
        from linkedin_mcp_server.server import create_mcp_server
        from linkedin_mcp_server.server_role import ServerRole

        owner = _OwnerOverHttp()
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
                "x-caller-note": "not forwarded",
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
        assert "x-caller-note" not in call.headers
        assert call.headers["authorization"] == f"Bearer {attachment.token}"
        assert call.headers[CALL_HEADER] == beat.headers[CALL_HEADER]
        assert call.headers[CALL_HEADER] != "forged-by-the-caller"
        for request in owner.requests:
            assert "frontend-credential" not in str(request.headers), request.url
        # The lookup inside the call names the call too.
        named = [
            r.content
            for r in owner.requests
            if r.headers.get(CALL_HEADER) == call.headers[CALL_HEADER]
        ]
        assert any(b'"tools/list"' in content for content in named), named
        # The version the hop spoke, as the owner sees it on the request.
        assert call.headers["mcp-protocol-version"] == HOP_PROTOCOL_VERSION


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

from linkedin_mcp_server.daemon.liveness import HEARTBEAT_PATH
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
    while its tool is running, on the hop's pinned protocol version, with the
    production frontend, its recovery and its forwarder in front of it. The owner itself is
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
                "linkedin_mcp_server.daemon.election.obtain_owner", elect
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
                assert effect == f"{HOP_PROTOCOL_VERSION} hello"
                assert not calling.done(), calling
                original.stop()
                result = await asyncio.wait_for(calling, timeout=20)
        finally:
            for owner in owners:
                owner.stop()

        assert self._runs(tmp_path / "original") == [f"{HOP_PROTOCOL_VERSION} hello"]
        assert self._runs(tmp_path / "replacement") == [], "the call was sent again"
        assert result.is_error is True
        assert result.structured_content is not None
        assert result.structured_content["status"] == "outcome_unknown"
        assert result.structured_content["retry_safe"] is False
        assert elections == [1]


class TestAnOwnerProcessKilledBeforeTheCall:
    """A stand-in owner process killed after its preflight answered, before the send.

    The same stand-in processes as `TestAnOwnerProcessKilledMidCall`. The
    preflight is answered for the departed owner, as one that left between
    its heartbeat and the call would have answered it, so what refuses the
    call is the operating system on the dead port. Nothing left this process,
    which the forwarder reads off the refused connection, so the mutating
    call runs once, on the replacement.
    """

    async def test_the_refused_call_runs_once_on_the_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.daemon.proxy import FrontendCallHeartbeatMiddleware
        from linkedin_mcp_server.server import create_mcp_server
        from linkedin_mcp_server.server_role import ServerRole

        spawn = TestAnOwnerProcessKilledMidCall._spawn
        runs = TestAnOwnerProcessKilledMidCall._runs
        owners: list[_OwnerProcess] = []
        try:
            original, attachment = spawn(tmp_path / "original", "answer", owners)
            _replacement, standby = spawn(tmp_path / "replacement", "answer", owners)
            elections: list[object] = []

            def elect(*_args: Any, **_kwargs: Any) -> Any:
                elections.append(1)
                return _elected(standby)

            async def beat(_attachment: Attachment, _call_id: str) -> httpx2.Response:
                return httpx2.Response(200, json={"watched": False})

            monkeypatch.setattr(
                "linkedin_mcp_server.daemon.election.obtain_owner", elect
            )
            monkeypatch.setattr(
                FrontendCallHeartbeatMiddleware, "_beat", staticmethod(beat)
            )
            original.stop()
            backend = _backend(attachment, tmp_path / "original")
            proxy = create_mcp_server(
                role=ServerRole.PROXY, proxy_backend=backend, tool_timeout=5.0
            )

            async with Client(proxy) as client:
                result = await asyncio.wait_for(
                    client.call_tool(
                        "send_connection_request",
                        {"message": "hello"},
                        raise_on_error=False,
                    ),
                    timeout=30,
                )
        finally:
            for owner in owners:
                owner.stop()

        assert runs(tmp_path / "original") == []
        assert runs(tmp_path / "replacement") == [f"{HOP_PROTOCOL_VERSION} hello"]
        assert result.is_error is False, result
        assert result.structured_content == {"status": "sent"}
        assert elections == [1]
