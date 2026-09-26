"""Bounding work whose client has gone away.

Every test here pins something that is invisible when everything works: a call
nobody is waiting for that keeps driving the browser, a heartbeat that keeps a
finished call alive, or an owner that stops a call somebody *is* waiting for.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from linkedin_mcp_server.daemon_descriptor import PROTOCOL_VERSION
from linkedin_mcp_server.daemon_liveness import (
    CALL_HEADER,
    EXPIRY_SECONDS,
    HEARTBEAT_SECONDS,
    CallLiveness,
    OwnerCallLivenessMiddleware,
    call_id_in,
    new_call_id,
)


def _last_heard(liveness: CallLiveness, call_id: str, seconds_ago: float) -> None:
    """Pretend the last beat for *call_id* arrived *seconds_ago*."""
    liveness._waiting[call_id].last_heard -= seconds_ago


def _idle_only_body(*, drop: str | None = None, **changes: Any) -> bytes:
    """The idle-only retirement body for the owner "the-owner", as changed."""
    import json

    body: dict[str, Any] = {
        "only_if_idle": True,
        "protocol": PROTOCOL_VERSION,
        "instance": "the-owner",
    }
    body.update(changes)
    if drop is not None:
        del body[drop]
    return json.dumps(body).encode()


class TestReadingTheMarker:
    """What counts as a call this build can be asked about."""

    def test_a_minted_marker_reads_back(self):
        marker = new_call_id()
        assert call_id_in(marker) == marker

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "deadbeef",  # no version
            "v2." + "a" * 32,  # a shape from a build that does not exist yet
            "v1." + "a" * 31,  # too short
            "v1." + "a" * 33,  # too long
            "v1." + "z" * 32,  # not hex
        ],
        ids=[
            "absent",
            "empty",
            "unversioned",
            "newer version",
            "short",
            "long",
            "not hex",
        ],
    )
    def test_anything_else_is_no_marker(self, value: str | None):
        # Unreadable has to mean unmarked rather than "close enough": an
        # unmarked call is refused before it runs, and that is the safe
        # direction. This arrives in a header from anything that can reach the
        # port.
        assert call_id_in(value) is None

    def test_two_calls_do_not_share_an_identifier(self):
        # One process-wide id would mean expiring one call cancels another.
        assert len({new_call_id() for _ in range(50)}) == 50


class TestCountingWhoIsWaiting:
    """The owner's side of the question, without any transport in the way."""

    @staticmethod
    def _task() -> Any:
        task = MagicMock()
        task.cancel = MagicMock()
        return task

    def test_a_call_nobody_asks_for_is_cancelled(self):
        liveness = CallLiveness()
        task = self._task()
        liveness.watch("v1.a", task)

        # The entry is aged rather than the clock moved. `time.monotonic` is the
        # one the whole interpreter reads, and patching it moves time for
        # everything in the test session, including asyncio's own timers.
        _last_heard(liveness, "v1.a", EXPIRY_SECONDS + 0.1)

        assert liveness.cancel_the_abandoned() == ["v1.a"]
        task.cancel.assert_called_once()

    def test_a_call_somebody_is_waiting_for_is_left_alone(self):
        liveness = CallLiveness()
        heard, unheard = self._task(), self._task()
        liveness.watch("v1.heard", heard)
        liveness.watch("v1.unheard", unheard)

        # Both have been running well past the expiry; only one frontend is
        # still saying so.
        _last_heard(liveness, "v1.heard", EXPIRY_SECONDS + 5)
        _last_heard(liveness, "v1.unheard", EXPIRY_SECONDS + 5)
        assert liveness.heard("v1.heard")

        assert liveness.cancel_the_abandoned() == ["v1.unheard"]
        heard.cancel.assert_not_called()
        unheard.cancel.assert_called_once()

    def test_a_beat_for_an_unknown_call_changes_nothing(self):
        # A beat that arrives just after its call returned. Ordinary, not a
        # fault, and it must not resurrect an entry.
        liveness = CallLiveness()
        assert liveness.heard("v1.never-registered") is False

    def test_a_released_call_is_no_longer_watched(self):
        liveness = CallLiveness()
        task = self._task()
        liveness.watch("v1.a", task)
        liveness.release("v1.a")

        assert liveness.cancel_the_abandoned() == []
        task.cancel.assert_not_called()

    def test_a_stall_is_longer_than_the_owner_looking(self):
        # The threshold has to sit above the interval the owner polls at, or
        # every ordinary scan looks like a stall and nothing is ever expired.
        # Below the poll it is not a smaller margin, it is the feature switched
        # off, and nothing else here would notice.
        from linkedin_mcp_server.daemon_liveness import _STALL_SECONDS
        from linkedin_mcp_server.daemon_owner import _STAND_DOWN_POLL_SECONDS

        assert _STALL_SECONDS > _STAND_DOWN_POLL_SECONDS * 5

    def test_the_expiry_is_several_cadences(self):
        # The relationship rather than the numbers. Both were measured, but what
        # keeps a live call safe is that a single late beat cannot expire it.
        assert EXPIRY_SECONDS >= 3 * HEARTBEAT_SECONDS


class TestTheOwnerSideMiddleware:
    """What the owner does with a call, marked or not."""

    @staticmethod
    def _context() -> Any:
        context = MagicMock()
        context.message.name = "get_person_profile"
        return context

    @staticmethod
    def _headers(monkeypatch, headers: dict[str, str]) -> None:
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_http_headers", lambda **_kw: headers
        )

    @pytest.mark.parametrize(
        "headers",
        [{}, {CALL_HEADER: "v2." + "a" * 32}],
        ids=["no marker", "a marker this build cannot read"],
    )
    async def test_an_unmarked_call_is_refused_before_anything_runs(
        self, monkeypatch, headers: dict[str, str]
    ):
        # A call the owner cannot identify is one it cannot cancel once its
        # client goes. Refused, and refused before it is counted: an unmarked
        # call is neither work nor a reason to stay.
        from linkedin_mcp_server import daemon_liveness

        self._headers(monkeypatch, headers)
        liveness = daemon_liveness.get_liveness()
        liveness.serving_as("the-owner")
        dispatched: list[str] = []

        async def call_next(_context: Any) -> str:
            dispatched.append("ran")
            return "the result"

        result = await OwnerCallLivenessMiddleware().on_call_tool(
            self._context(),
            call_next,  # ty: ignore
        )

        assert dispatched == []
        assert result.is_error is True
        assert result.meta == {
            daemon_liveness.REFUSAL_KEY: {
                "daemon": "unmarked_call",
                "protocol": PROTOCOL_VERSION,
                "instance": "the-owner",
            }
        }
        assert liveness.calls_in_flight() == 0
        assert liveness._waiting == {}

    async def test_a_marked_call_is_watched_while_it_runs(self, monkeypatch):
        from linkedin_mcp_server import daemon_liveness

        marker = new_call_id()
        self._headers(monkeypatch, {CALL_HEADER: marker})
        liveness = daemon_liveness.get_liveness()

        async def call_next(_context: Any) -> str:
            assert marker in liveness._waiting
            return "the result"

        assert (
            await OwnerCallLivenessMiddleware().on_call_tool(
                self._context(),
                call_next,  # ty: ignore
            )
            == "the result"
        )
        # And released afterwards, or the tracker would grow for the life of the
        # process and expire identifiers nothing is running.
        assert marker not in liveness._waiting

    async def test_a_marked_call_is_released_when_it_fails(self, monkeypatch):
        from linkedin_mcp_server import daemon_liveness

        marker = new_call_id()
        self._headers(monkeypatch, {CALL_HEADER: marker})

        async def call_next(_context: Any) -> str:
            raise ValueError("the tool itself failed")

        with pytest.raises(ValueError):
            await OwnerCallLivenessMiddleware().on_call_tool(
                self._context(),
                call_next,  # ty: ignore
            )
        assert marker not in daemon_liveness.get_liveness()._waiting

    async def test_an_abandoned_call_is_stopped_and_says_so(self, monkeypatch):
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server import daemon_liveness

        marker = new_call_id()
        self._headers(monkeypatch, {CALL_HEADER: marker})
        liveness = daemon_liveness.get_liveness()
        started = asyncio.Event()

        async def call_next(_context: Any) -> str:
            started.set()
            await asyncio.sleep(3600)  # the scrape nobody is waiting for
            return "never reached"  # pragma: no cover

        running = asyncio.create_task(
            OwnerCallLivenessMiddleware().on_call_tool(
                self._context(),
                call_next,  # ty: ignore
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)

        # The owner's own loop notices, without waiting out the real expiry.
        _last_heard(liveness, marker, EXPIRY_SECONDS + 1)
        assert liveness.cancel_the_abandoned() == [marker]

        with pytest.raises(ToolError, match="stopped waiting"):
            await asyncio.wait_for(running, timeout=5)
        assert marker not in liveness._waiting

    async def test_a_cancellation_from_elsewhere_is_not_swallowed(self, monkeypatch):
        # Shutdown cancels every task. That is not an abandoned call, and
        # reporting it as one would tell whoever asked us to stop that the call
        # merely lost its client.
        marker = new_call_id()
        self._headers(monkeypatch, {CALL_HEADER: marker})

        async def call_next(_context: Any) -> str:
            await asyncio.sleep(3600)
            return "never reached"  # pragma: no cover

        running = asyncio.create_task(
            OwnerCallLivenessMiddleware().on_call_tool(
                self._context(),
                call_next,  # ty: ignore
            )
        )
        await asyncio.sleep(0.05)
        running.cancel()

        with pytest.raises(asyncio.CancelledError):
            await running


class TestTheFrontendSide:
    """Saying we are still waiting, and stopping the moment we are not."""

    @staticmethod
    def _backend(tmp_path: Path) -> Any:
        from test_daemon_proxy import _attachment, _backend

        return _backend(_attachment(tmp_path), tmp_path)

    @staticmethod
    def _middleware(backend: Any, beats: list[tuple[str, str]], status: int = 200):
        """The middleware with its one network call stood in for.

        Records which owner each beat was addressed to, which is the only way to
        see a beat that followed a replacement owner rather than staying with
        the one running the call. Answers with the body the owner's route sends
        for a call it has not registered yet, which every preflight is.
        """
        import httpx

        from linkedin_mcp_server.daemon_proxy import FrontendCallHeartbeatMiddleware

        middleware = FrontendCallHeartbeatMiddleware(backend)

        async def beat(attachment: Any, call_id: str) -> httpx.Response:
            beats.append((attachment.descriptor.instance_id, call_id))
            return httpx.Response(status, json={"watched": False})

        middleware._beat = beat  # ty: ignore[invalid-assignment]
        return middleware

    @staticmethod
    def _context() -> Any:
        context = MagicMock()
        context.message.name = "get_person_profile"
        return context

    async def test_the_first_beat_precedes_the_dispatch(self, tmp_path: Path):
        # Otherwise the owner registers a call it has never been told about, and
        # the window before the first beat is one where an expiry would fire
        # against a call that is perfectly alive.
        backend = self._backend(tmp_path)
        beats: list[tuple[str, str]] = []
        order: list[str] = []

        async def call_next(_context: Any) -> str:
            order.append(f"dispatch after {len(beats)} beats")
            return "the result"

        middleware = self._middleware(backend, beats)
        assert (
            await middleware.on_call_tool(
                self._context(),
                call_next,
            )
            == "the result"
        )
        assert order == ["dispatch after 1 beats"]

    async def test_an_owner_without_the_route_gets_no_call_and_no_beats(
        self, tmp_path: Path, quick_cadence: float
    ):
        # The reverse of what protocol 1 promised. A 404 used to be an owner
        # from before heartbeats and was served unheard; now it is not an owner
        # of this protocol at all, and a call it ran could never be cancelled.
        from linkedin_mcp_server.daemon_proxy import (
            OwnerFailure,
            OwnerUnreachableError,
        )

        backend = self._backend(tmp_path)
        beats: list[tuple[str, str]] = []
        middleware = self._middleware(backend, beats, status=404)
        dispatched: list[str] = []

        async def call_next(_context: Any) -> str:
            dispatched.append("ran")
            return "the result"

        with pytest.raises(OwnerUnreachableError) as refused:
            await middleware.on_call_tool(self._context(), call_next)

        assert dispatched == []
        assert refused.value.nothing_was_sent is True
        assert refused.value.classification is OwnerFailure.ROUTE_MISSING
        # Several cadences, and no beater was ever started.
        await asyncio.sleep(quick_cadence * 6)
        assert len(beats) == 1, "kept beating at an owner that got no call"

    async def test_the_marker_reaches_the_client_the_provider_builds(
        self, tmp_path: Path
    ):
        # The identifier has to arrive on the tool call as well as on the beats,
        # or the owner cannot connect the two.
        from fastmcp.client.transports import StreamableHttpTransport

        backend = self._backend(tmp_path)
        beats: list[tuple[str, str]] = []
        middleware = self._middleware(backend, beats)
        seen: list[str | None] = []

        async def call_next(_context: Any) -> str:
            client = backend.open_client(timeout=1.0)
            assert isinstance(client.transport, StreamableHttpTransport)
            seen.append(client.transport.headers.get(CALL_HEADER))
            return "the result"

        await middleware.on_call_tool(
            self._context(),
            call_next,
        )
        assert seen == [beats[0][1]], "the call and its beats named different calls"

    async def test_no_marker_is_left_behind_afterwards(self, tmp_path: Path):
        # A leaked context variable would stamp the next unrelated operation,
        # including a listing, with a call identifier that is over.
        from linkedin_mcp_server.daemon_proxy import _call_being_made

        backend = self._backend(tmp_path)
        middleware = self._middleware(backend, [])

        async def call_next(_context: Any) -> str:
            return "the result"

        await middleware.on_call_tool(
            self._context(),
            call_next,
        )
        assert _call_being_made.get() is None

    @pytest.fixture
    def quick_cadence(self, monkeypatch):
        """Beat every 50ms instead of every two seconds.

        Patched in `daemon_proxy`'s own namespace, which is where
        `_keep_saying` reads it. Without this a test would have to outlast a
        real period to see anything at all, and one that waits less than a
        period proves nothing: it stays green with the cancellation removed.
        """
        monkeypatch.setattr("linkedin_mcp_server.daemon_proxy.HEARTBEAT_SECONDS", 0.05)
        return 0.05

    @pytest.mark.parametrize("outcome", ["succeeds", "fails", "is cancelled"])
    async def test_the_beating_stops_however_the_call_ends(
        self, tmp_path: Path, outcome: str, quick_cadence: float
    ):
        # A leaked beater keeps a finished call alive in the owner's tracker,
        # which is the exact state this mechanism exists to prevent.
        backend = self._backend(tmp_path)
        beats: list[tuple[str, str]] = []
        middleware = self._middleware(backend, beats)
        entered = asyncio.Event()

        async def call_next(_context: Any) -> str:
            entered.set()
            if outcome == "fails":
                raise ValueError("the tool itself failed")
            if outcome == "is cancelled":
                await asyncio.sleep(3600)
            return "the result"

        running = asyncio.create_task(
            middleware.on_call_tool(
                self._context(),
                call_next,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        if outcome == "is cancelled":
            running.cancel()
        with (
            pytest.raises((ValueError, asyncio.CancelledError))
            if outcome != ("succeeds")
            else _nothing_raised()
        ):
            await running

        before = len(beats)
        # Several cadences. A beater still running would add to the list.
        await asyncio.sleep(quick_cadence * 6)
        assert len(beats) == before, "a heartbeat outlived the call it belonged to"

    async def test_beats_stay_with_the_owner_running_the_call(
        self, tmp_path: Path, quick_cadence: float
    ):
        # A call belongs to the owner it was dispatched to. Following the
        # backend to a replacement would beat at an owner that has never heard
        # of this call, while the one actually running it stops being told.
        from test_daemon_proxy import _attachment

        backend = self._backend(tmp_path)
        original = backend.attachment.descriptor.instance_id
        beats: list[tuple[str, str]] = []
        middleware = self._middleware(backend, beats)

        dialled: list[str] = []

        async def call_next(_context: Any) -> str:
            # Another call elects a replacement while this one is running. With
            # no component cache every call re-lists first, so this really is
            # reachable between a call's own two upstream operations.
            backend._attachment = _attachment(tmp_path, port=51999)
            await asyncio.sleep(quick_cadence * 4)
            # What the provider would open for this call, after the move.
            # Twice, because one call opens more than one client: with no
            # component cache FastMCP resolves the tool through its own listing
            # first and then runs it, each with a client of its own. A binding
            # that only held for the first would send the listing to one owner
            # and the call itself to another, and one client per test cannot
            # see that.
            dialled.append(backend.open_client(timeout=1.0)._instance_id)
            backend._attachment = _attachment(tmp_path, port=52000)
            dialled.append(backend.open_client(timeout=1.0)._instance_id)
            return "the result"

        await middleware.on_call_tool(
            self._context(),
            call_next,
        )
        assert len(beats) >= 2, "no beat followed the first one"
        assert {owner for owner, _ in beats} == {original}
        # The half that matters most, and the half a beats-only assertion misses
        # entirely: the request goes where the beats go. Split between two owners,
        # the one running the call never hears a beat and cancels it after the
        # expiry, so a live call is reported to the user as abandoned.
        assert dialled == [original, original], (
            "the call and its heartbeats named different owners"
        )


class _nothing_raised:
    """A ``pytest.raises`` stand-in for the case that must not raise."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return False


class TestTheRouteOnARealOwner:
    """The heartbeat as an actual HTTP request against the owner's own app.

    Built the way `daemon_owner._build_server` builds it, because the thing
    under test is a custom route and those are mounted *outside* the
    authentication middleware. Measured on 3.4.4 and recorded next to the
    stand-down route: an unauthenticated POST to a custom path is served. Every
    check on this route is therefore its own, and a test that never sends a
    request would not see it.
    """

    HOST = "127.0.0.1"
    TOKEN = "the-owners-token"

    @staticmethod
    def _owner_app(token: str, host: str, port: int):
        from linkedin_mcp_server.daemon_owner import create_owner_server
        from linkedin_mcp_server.config.schema import AppConfig

        return create_owner_server(
            config=AppConfig(), token=token, host=host, port=port, stand_down=None
        )

    @pytest.fixture
    def serving(self):
        """The owner's real ASGI app, driven without a socket.

        No uvicorn and no port: the owner's server object carries the app that
        would be served, and the question here is where the route sits relative
        to the authentication middleware, which is a property of that app. The
        real process also runs a lifespan that opens Chromium, and a route test
        has no business starting a browser.
        """
        from linkedin_mcp_server import daemon_liveness

        port = 51234
        server = self._owner_app(self.TOKEN, self.HOST, port)
        return (
            server.config.app,
            f"http://{self.HOST}:{port}",
            daemon_liveness.get_liveness(),
        )

    @staticmethod
    async def _post(
        app: Any, base: str, *, token: str | None, marker: str | None
    ) -> int:
        import httpx

        from linkedin_mcp_server.daemon_liveness import HEARTBEAT_PATH

        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if marker is not None:
            headers[CALL_HEADER] = marker
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=base, trust_env=False
        ) as client:
            response = await client.post(HEARTBEAT_PATH, headers=headers)
            return response.status_code

    async def test_a_beat_without_the_token_is_refused_and_does_not_count(
        self, serving
    ):
        # The failure this route could quietly have: anything on the machine, and
        # any page the user's browser visits, keeping a call alive that its own
        # frontend abandoned.
        app, base, liveness = serving
        marker = new_call_id()
        task = MagicMock()
        liveness.watch(marker, task)
        _last_heard(liveness, marker, EXPIRY_SECONDS + 1)

        assert await self._post(app, base, token=None, marker=marker) == 401
        assert (
            await self._post(app, base, token="the-wrong-token", marker=marker) == 401
        )

        assert liveness.cancel_the_abandoned() == [marker]

    async def test_a_beat_with_the_token_keeps_the_call(self, serving):
        app, base, liveness = serving
        marker = new_call_id()
        liveness.watch(marker, MagicMock())
        _last_heard(liveness, marker, EXPIRY_SECONDS + 1)

        assert await self._post(app, base, token=self.TOKEN, marker=marker) == 200

        assert liveness.cancel_the_abandoned() == []

    async def test_a_beat_naming_no_call_is_a_bad_request(self, serving):
        app, base, _liveness = serving
        assert await self._post(app, base, token=self.TOKEN, marker=None) == 400
        assert await self._post(app, base, token=self.TOKEN, marker="nonsense") == 400


class TestTheOwnersOwnLoop:
    """That anything expires at all, which no rule above establishes.

    Every test further up drives `cancel_the_abandoned` itself. If the owner
    never called it, all of them would still pass and no call would ever be
    stopped in production. This is the wiring, and it is the part that is easy
    to leave out.
    """

    async def test_the_serving_loop_expires_abandoned_calls(self):
        from linkedin_mcp_server import daemon_liveness
        from linkedin_mcp_server.daemon_owner import _serve_until_stopped

        liveness = daemon_liveness.get_liveness()
        marker = new_call_id()
        abandoned = asyncio.create_task(asyncio.sleep(3600))
        liveness.watch(marker, abandoned)
        _last_heard(liveness, marker, EXPIRY_SECONDS + 1)

        # A server that stops on its own once the loop has had a tick or two.
        async def serves() -> None:
            await asyncio.sleep(0.35)

        serving = asyncio.create_task(serves())
        await _serve_until_stopped(MagicMock(), serving, [], lock=None)

        assert abandoned.cancelled(), "the owner never expired an abandoned call"
        assert marker not in liveness._waiting


class TestGoingAwayWhenNobodyNeedsIt:
    """The owner's third reason to exit, after wedge and turnover.

    Without it the process holds the daemon lock for the machine's uptime,
    having closed the browser hours earlier. What the next election waits for is
    the process, not the Chromium it stopped running.
    """

    @staticmethod
    async def _run_loop(idle_timeout: float, *, ticks: float = 0.4) -> bool:
        """Run the owner's loop briefly. True if it decided to exit."""
        from linkedin_mcp_server.daemon_owner import _serve_until_stopped

        server = MagicMock()
        server.should_exit = False

        async def serves() -> None:
            await asyncio.sleep(ticks)

        serving = asyncio.create_task(serves())
        await _serve_until_stopped(server, serving, [], idle_timeout, lock=None)
        return bool(server.should_exit)

    async def test_an_owner_nobody_ever_called_still_exits(self):
        # Idleness counted from the first *call* rather than from the endpoint
        # going live would leave an owner that was started and then never used
        # running forever, which is the commonest way to reach this state: an
        # election starts one, and the frontend that asked dies before calling.
        from linkedin_mcp_server import daemon_liveness

        liveness = daemon_liveness.get_liveness()
        liveness.the_endpoint_is_live()
        liveness._quiet_since = liveness._quiet_since - 60  # ty: ignore

        assert await self._run_loop(idle_timeout=5.0) is True

    async def test_browser_setup_in_progress_holds_the_owner_open(self):
        from linkedin_mcp_server import bootstrap, daemon_liveness

        liveness = daemon_liveness.get_liveness()
        liveness.the_endpoint_is_live()
        liveness._quiet_since = liveness._quiet_since - 60  # ty: ignore

        async def pending_setup() -> None:
            await asyncio.Event().wait()

        setup = asyncio.create_task(pending_setup())
        bootstrap.get_bootstrap_state().setup_task = setup
        try:
            assert await self._run_loop(idle_timeout=0.05, ticks=0.2) is False
        finally:
            setup.cancel()
            with pytest.raises(asyncio.CancelledError):
                await setup
            bootstrap.get_bootstrap_state().setup_task = None

    async def test_unconsumed_setup_failure_holds_the_owner_then_lets_it_go(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Both halves, because each one alone is a different bug. A failure is
        # only reported on the *next* tool call, and the message it follows asks
        # for that call in a minute or two: an owner with a shorter configured
        # idle timeout that exits inside that window sends the retry to a fresh
        # owner, which starts setup over and hides the diagnostic again. And a
        # failure nobody ever comes back for must not pin the owner forever,
        # which is what makes the grace bounded rather than a second wedge.
        from linkedin_mcp_server import bootstrap, daemon_liveness, daemon_owner

        grace = 1.0
        monkeypatch.setattr(daemon_owner, "_SETUP_FAILURE_RETRY_GRACE_SECONDS", grace)

        async def failed_setup() -> None:
            raise RuntimeError("install failed")

        setup = asyncio.create_task(failed_setup())
        with pytest.raises(RuntimeError, match="install failed"):
            await setup
        bootstrap.get_bootstrap_state().setup_task = setup

        liveness = daemon_liveness.get_liveness()
        liveness.the_endpoint_is_live()
        liveness._quiet_since = liveness._quiet_since - 60  # ty: ignore
        # Where the quiet period starts in production: setup completion resets
        # the idle clock, so the grace is measured from the failure rather than
        # from whenever the endpoint was published.
        liveness.background_activity_finished()

        try:
            assert bootstrap.browser_setup_failure_pending()
            held = await self._run_loop(idle_timeout=0.05, ticks=0.3)
            quiet = liveness.quiet_for()
            assert quiet is not None and 0.05 <= quiet < grace
            assert held is False, "the owner exited before the retry could arrive"

            assert await self._run_loop(idle_timeout=0.05, ticks=grace) is True
        finally:
            bootstrap.get_bootstrap_state().setup_task = None

    async def test_it_does_not_exit_before_the_endpoint_is_published(self):
        # The clock has not started. An owner spends its first seconds importing
        # and launching Chromium, and a short timeout would otherwise fire
        # during startup, before the frontend that asked for it could call.
        from linkedin_mcp_server import daemon_liveness

        assert daemon_liveness.get_liveness().quiet_for() is None
        assert await self._run_loop(idle_timeout=0.05) is False

    async def test_a_call_in_flight_holds_the_door(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Driven through the middleware rather than by calling the tracker here.
        # An earlier version of this test registered the call itself and stayed
        # green when the middleware stopped counting calls at all, which is the
        # whole thing it exists to catch. Only marked calls are left to count:
        # an unmarked one is refused before it is admitted.
        from linkedin_mcp_server import daemon_liveness

        headers = {CALL_HEADER: new_call_id()}
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_http_headers", lambda **_kw: headers
        )
        liveness = daemon_liveness.get_liveness()
        liveness.the_endpoint_is_live()
        liveness._quiet_since = liveness._quiet_since - 3600  # ty: ignore

        running = asyncio.Event()

        async def call_next(_context: Any) -> str:
            running.set()
            await asyncio.sleep(3600)
            return "never reached"  # pragma: no cover

        context = MagicMock()
        context.message.name = "get_person_profile"
        call = asyncio.create_task(
            OwnerCallLivenessMiddleware().on_call_tool(
                context,
                call_next,  # ty: ignore
            )
        )
        await asyncio.wait_for(running.wait(), timeout=5)

        try:
            assert liveness.quiet_for() is None, "a running call read as idleness"
            assert await self._run_loop(idle_timeout=0.05) is False
        finally:
            call.cancel()

    async def test_the_clock_restarts_when_a_call_ends(self):
        from linkedin_mcp_server import daemon_liveness

        liveness = daemon_liveness.get_liveness()
        liveness.the_endpoint_is_live()
        liveness._quiet_since = liveness._quiet_since - 60  # ty: ignore

        liveness.call_started()
        liveness.call_finished()

        # Quiet again, but only just: the wait starts from the call rather than
        # from whenever the endpoint went live.
        quiet = liveness.quiet_for()
        assert quiet is not None and quiet < 1
        assert await self._run_loop(idle_timeout=5.0) is False

    async def test_the_clock_restarts_when_background_activity_finishes(self):
        from linkedin_mcp_server import daemon_liveness

        liveness = daemon_liveness.get_liveness()
        liveness.the_endpoint_is_live()
        liveness._quiet_since = liveness._quiet_since - 60  # ty: ignore

        liveness.background_activity_finished()

        quiet = liveness.quiet_for()
        assert quiet is not None and quiet < 1
        assert await self._run_loop(idle_timeout=5.0) is False

    async def test_a_zero_timeout_keeps_the_owner_forever(self):
        # The documented way to switch it off, and the same value that already
        # disables the browser's own idle close.
        from linkedin_mcp_server import daemon_liveness

        liveness = daemon_liveness.get_liveness()
        liveness.the_endpoint_is_live()
        liveness._quiet_since = liveness._quiet_since - 86400  # ty: ignore

        assert await self._run_loop(idle_timeout=0.0) is False


#: A frontend that elects an owner with a short idle timeout and then leaves.
#: Its own script rather than the election suite's, because that one builds an
#: `AppConfig()` directly and ignores the environment: an idle timeout set as a
#: variable never reached the owner, and the first version of the test below
#: watched an owner running on the 600 second default and called the feature
#: broken.
_ELECT_WITH_IDLE_TIMEOUT = """
import json
import sys
from pathlib import Path

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon_election import obtain_owner
from linkedin_mcp_server.profile_claim import ensure_profile_claim

profile = Path(sys.argv[1])
ensure_profile_claim(profile, claim_anyway=True)
config = AppConfig()
config.browser.user_data_dir = str(profile)
config.browser.browser_idle_timeout_seconds = float(sys.argv[2])

outcome = obtain_owner(profile.parent, profile, config, deadline_seconds=90)
attachment = outcome.attachment_lookup.attachment
print(json.dumps({
    "started": outcome.started_owner,
    "pid": attachment.descriptor.pid if attachment else None,
}))
"""


@pytest.mark.slow
class TestARealOwnerGoingAway:
    """The idle exit with a real detached process on the other end.

    Nothing in one interpreter can see this. The rules above are all driven by
    calling the tracker or the loop directly, so every one of them stays green
    with the clock never started at the publish site, and the owner would then
    hold the daemon lock for the machine's uptime having closed the browser
    hours before. That line is the whole feature, and this is the only test that
    touches it.
    """

    def test_an_idle_owner_exits(self, tmp_path):
        import json
        import os
        import subprocess
        import sys
        import time as clock

        from test_daemon_election import _REPO_ROOT, _alive, _stop

        profile = tmp_path / "state"
        profile.mkdir()

        # Short enough to watch, and comfortably longer than the startup it must
        # not count as quiet.
        idle = 4.0
        started = subprocess.run(
            [sys.executable, "-c", _ELECT_WITH_IDLE_TIMEOUT, str(profile), str(idle)],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
            cwd=_REPO_ROOT,
            timeout=180,
        )
        assert started.returncode == 0, started.stderr
        result = json.loads(started.stdout.strip().splitlines()[-1])
        owner = result["pid"]
        assert isinstance(owner, int) and _alive(owner), result

        try:
            deadline = clock.monotonic() + idle * 8
            while clock.monotonic() < deadline and _alive(owner):
                clock.sleep(0.25)
            assert not _alive(owner), (
                "an owner nobody called kept running past its idle timeout"
            )
        finally:
            _stop(owner)


class TestTheMarkerSurvivesTheRealClient:
    """That the header the middleware sets is still there when it goes out.

    Every other test stops at the transport object. The client factory in
    between is the owner's own, it is shared with the heartbeat requests, and it
    is free to rebuild the headers it is given: a version of it that dropped
    this one would leave the owner seeing every real tool call unmarked, with
    all the rules above still green.
    """

    def test_the_owners_client_factory_keeps_the_call_header(self):
        from linkedin_mcp_server.daemon_owner import direct_async_http_client

        marker = new_call_id()
        client = direct_async_http_client(headers={CALL_HEADER: marker})

        assert client.headers.get(CALL_HEADER) == marker

    async def test_the_header_reaches_the_wire(self):
        import httpx

        from linkedin_mcp_server.daemon_owner import direct_async_http_client

        marker = new_call_id()
        seen: dict[str, str] = {}

        def record(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200)

        client = direct_async_http_client(headers={CALL_HEADER: marker})
        client._transport = httpx.MockTransport(record)
        async with client:
            await client.post("http://127.0.0.1:1/mcp")

        assert seen.get(CALL_HEADER) == marker


class TestAnOwnerThatWasNotRunning:
    """A stall in the owner is not a frontend that stopped waiting.

    The expiry reads elapsed time, and elapsed time cannot tell "nobody asked
    for this call" from "this process was not scheduled to hear them". A laptop
    that slept, a machine under load, or a long synchronous stretch all look
    like abandonment on the tick that follows, and cancelling then stops calls
    whose frontends were beating throughout.
    """

    def test_a_late_scan_expires_nothing(self):
        liveness = CallLiveness()
        task = MagicMock()
        liveness.watch("v1.a", task)
        _last_heard(liveness, "v1.a", EXPIRY_SECONDS + 5)

        # A first scan establishes when the owner last looked, and a second one
        # arrives far too late for the gap to be anything but the owner itself.
        liveness._last_scan = liveness._last_scan or 0.0
        import time as clock

        liveness._last_scan = clock.monotonic() - 30

        assert liveness.cancel_the_abandoned() == []
        task.cancel.assert_not_called()

    def test_the_next_scan_after_a_stall_judges_normally(self):
        # The stall buys one cycle, not immunity: a call that is still unheard
        # by the following scan is expired as usual.
        import time as clock

        liveness = CallLiveness()
        task = MagicMock()
        liveness.watch("v1.a", task)
        _last_heard(liveness, "v1.a", EXPIRY_SECONDS + 5)
        liveness._last_scan = clock.monotonic() - 30
        assert liveness.cancel_the_abandoned() == []

        _last_heard(liveness, "v1.a", EXPIRY_SECONDS + 1)
        assert liveness.cancel_the_abandoned() == ["v1.a"]
        task.cancel.assert_called_once()


class TestTellingShutdownFromAbandonment:
    """Which cancellation this was, when both arrive at once."""

    async def test_a_shutdown_that_races_the_expiry_is_still_a_shutdown(
        self, monkeypatch
    ):
        # The ordering that makes this hard: the outer task is cancelled first,
        # and before that reaches the child the expiry scan removes the entry and
        # cancels the same child. Reading only "the child was cancelled and its
        # entry is gone" then reports a shutdown as an abandoned call.
        from linkedin_mcp_server import daemon_liveness

        marker = new_call_id()
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_http_headers",
            lambda **_kw: {CALL_HEADER: marker},
        )
        liveness = daemon_liveness.get_liveness()
        started = asyncio.Event()

        async def call_next(_context: Any) -> str:
            started.set()
            await asyncio.sleep(3600)
            return "never reached"  # pragma: no cover

        context = MagicMock()
        context.message.name = "get_person_profile"
        running = asyncio.create_task(
            OwnerCallLivenessMiddleware().on_call_tool(
                context,
                call_next,  # ty: ignore
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)

        running.cancel()
        _last_heard(liveness, marker, EXPIRY_SECONDS + 1)
        liveness.cancel_the_abandoned()

        with pytest.raises(asyncio.CancelledError):
            await running


class TestAnAbandonedCallStillExpiresUnderTheLoop:
    """That the stall rule protects the owner without disabling the expiry.

    The rule reads the gap between the owner's own scans, so a threshold set
    below the poll interval would classify every ordinary tick as a stall and
    nothing would ever be cancelled. Every other test here calls
    `cancel_the_abandoned` itself and never sees a second scan, so all of them
    stay green against exactly that.
    """

    async def test_a_call_that_goes_quiet_is_cancelled_by_the_running_loop(self):
        from linkedin_mcp_server import daemon_liveness
        from linkedin_mcp_server.daemon_owner import _serve_until_stopped

        liveness = daemon_liveness.get_liveness()
        marker = new_call_id()
        abandoned = asyncio.create_task(asyncio.sleep(3600))
        liveness.watch(marker, abandoned)

        # Driven by events rather than by sleeping for a plausible-looking time.
        # An earlier version aged the call after a fixed pause and raced the
        # loop's own lifetime under load.
        stop = asyncio.Event()

        async def serves() -> None:
            await stop.wait()

        serving = asyncio.create_task(serves())
        loop = asyncio.create_task(
            _serve_until_stopped(MagicMock(), serving, [], lock=None)
        )
        try:
            # The first scan has to be behind us, so the decision is taken on a
            # later tick with a real and short gap in front of it.
            while liveness._last_scan is None:
                await asyncio.sleep(0.01)
            _last_heard(liveness, marker, EXPIRY_SECONDS + 1)

            for _ in range(500):
                if abandoned.cancelled():
                    break
                await asyncio.sleep(0.01)
        finally:
            stop.set()
            await loop

        assert abandoned.cancelled(), "an abandoned call outlived the owner's loop"


def _mark_calls(monkeypatch: pytest.MonkeyPatch) -> str:
    """Make every call in this test arrive carrying one fresh marker."""
    marker = new_call_id()
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda **_kw: {CALL_HEADER: marker},
    )
    return marker


def _call_context() -> Any:
    context = MagicMock()
    context.message.name = "get_person_profile"
    # No request context, so the serializing middleware reports no progress.
    context.fastmcp_context = None
    return context


def _refused_as_retiring(result: Any, instance: str | None) -> bool:
    from linkedin_mcp_server.daemon_liveness import REFUSAL_KEY

    return getattr(result, "is_error", False) is True and (result.meta or {}).get(
        REFUSAL_KEY
    ) == {"daemon": "retiring", "protocol": PROTOCOL_VERSION, "instance": instance}


class TestAdmissionAndRetirementAreOneDecision:
    """Only two orderings exist: the call wins, or the retirement does.

    Each ordering is forced with a barrier rather than left to timing, and the
    refused side is checked for having left nothing behind: no count, no task,
    no downstream call and no profile lease.
    """

    async def test_a_call_admitted_first_makes_retirement_see_busy(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import daemon_liveness

        _mark_calls(monkeypatch)
        liveness = daemon_liveness.get_liveness()
        liveness.the_endpoint_is_live()
        running = asyncio.Event()
        release = asyncio.Event()

        async def call_next(_context: Any) -> str:
            running.set()
            await release.wait()
            return "the result"

        call = asyncio.create_task(
            OwnerCallLivenessMiddleware().on_call_tool(_call_context(), call_next)  # ty: ignore
        )
        await asyncio.wait_for(running.wait(), timeout=5)

        assert liveness.try_retire("idle") is False
        assert liveness.retiring is False

        release.set()
        assert await call == "the result"
        assert liveness.try_retire("idle") is True

    async def test_a_call_after_retirement_is_refused_and_leaves_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Driven through the real serializing middleware, so a refused call
        # that still reached anything below admission would be seen taking the
        # lock or the profile lease.
        from linkedin_mcp_server import daemon_liveness
        from linkedin_mcp_server.sequential_tool_middleware import (
            SequentialToolExecutionMiddleware,
        )

        _mark_calls(monkeypatch)
        liveness = daemon_liveness.get_liveness()
        liveness.serving_as("the-owner")
        liveness.the_endpoint_is_live()
        quiet_since = liveness._quiet_since
        leases = MagicMock(name="get_profile_lease")
        monkeypatch.setattr(
            "linkedin_mcp_server.sequential_tool_middleware.get_profile_lease", leases
        )
        sequential = SequentialToolExecutionMiddleware()
        ran: list[str] = []

        async def tool(_context: Any) -> str:
            ran.append("the tool")
            return "the result"

        async def call_next(context: Any) -> Any:
            return await sequential.on_call_tool(context, tool)  # ty: ignore

        tasks_before = len(asyncio.all_tasks())
        assert liveness.try_retire("idle") is True

        result = await OwnerCallLivenessMiddleware().on_call_tool(
            _call_context(), call_next
        )

        assert _refused_as_retiring(result, "the-owner")
        assert ran == []
        leases.assert_not_called()
        assert not sequential._lock.locked()
        assert liveness.calls_in_flight() == 0
        assert liveness._waiting == {}
        assert liveness._quiet_since == quiet_since, "a refused call touched the clock"
        assert len(asyncio.all_tasks()) == tasks_before, "a refused call left a task"

    async def test_a_request_not_yet_admitted_is_refused_by_a_retirement(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Already in HTTP handling, with its task scheduled, but not yet at the
        # admission check when the owner decides to go.
        from linkedin_mcp_server import daemon_liveness

        _mark_calls(monkeypatch)
        liveness = daemon_liveness.get_liveness()
        liveness.the_endpoint_is_live()
        ran: list[str] = []

        async def call_next(_context: Any) -> str:
            ran.append("the tool")
            return "the result"

        arriving = asyncio.create_task(
            OwnerCallLivenessMiddleware().on_call_tool(_call_context(), call_next)  # ty: ignore
        )
        assert liveness.try_retire("idle") is True

        result = await arriving

        assert _refused_as_retiring(result, None)
        assert ran == []
        assert liveness.calls_in_flight() == 0

    async def test_a_queued_call_counts_as_busy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Queued behind another call on the serializing lock, it holds no
        # browser yet. Idle would be the wrong answer: it was admitted, and its
        # client is waiting for it.
        from linkedin_mcp_server import daemon_liveness
        from linkedin_mcp_server.profile_lease import get_profile_lease
        from linkedin_mcp_server.sequential_tool_middleware import (
            SequentialToolExecutionMiddleware,
        )

        _mark_calls(monkeypatch)
        liveness = daemon_liveness.get_liveness()
        liveness.the_endpoint_is_live()
        lease = get_profile_lease(tmp_path / "profile")
        monkeypatch.setattr(
            "linkedin_mcp_server.sequential_tool_middleware.get_profile_lease",
            lambda: lease,
        )
        sequential = SequentialToolExecutionMiddleware()

        async def tool(_context: Any) -> str:
            return "the result"

        async def call_next(context: Any) -> Any:
            return await sequential.on_call_tool(context, tool)  # ty: ignore

        await sequential._lock.acquire()
        try:
            queued = asyncio.create_task(
                OwnerCallLivenessMiddleware().on_call_tool(_call_context(), call_next)
            )
            while liveness.calls_in_flight() == 0:
                await asyncio.sleep(0)
            await asyncio.sleep(0.05)
            assert not queued.done(), "the call was not queued behind the lock"
            assert lease._refs == 0, "the queued call already holds the profile"

            assert liveness.try_retire("idle") is False
        finally:
            sequential._lock.release()
        assert await asyncio.wait_for(queued, timeout=5) == "the result"

    async def test_a_call_arriving_at_the_idle_decision_is_refused_or_keeps_it(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The owner's own loop, with a call arriving in the idle decision.

        The call is scheduled from inside the loop's read of the idle clock,
        so it is ready to run at whatever point the loop next yields. With the
        check and the set in one step it never gets in before the owner has
        closed admission; with any `await` between them it is admitted by an
        owner that has already decided to exit.
        """
        from linkedin_mcp_server import daemon_liveness
        from linkedin_mcp_server.daemon_owner import _serve_until_stopped

        _mark_calls(monkeypatch)
        liveness = daemon_liveness.get_liveness()
        liveness.the_endpoint_is_live()
        liveness._quiet_since = liveness._quiet_since - 60  # ty: ignore
        ran: list[str] = []

        async def call_next(_context: Any) -> str:
            ran.append("the tool")
            return "the result"

        arriving: list[asyncio.Future[Any]] = []
        read_the_clock = liveness.quiet_for

        def a_call_arrives_as_the_clock_is_read() -> float | None:
            if not arriving:
                arriving.append(
                    asyncio.ensure_future(
                        OwnerCallLivenessMiddleware().on_call_tool(
                            _call_context(),
                            call_next,  # ty: ignore
                        )
                    )
                )
            return read_the_clock()

        monkeypatch.setattr(liveness, "quiet_for", a_call_arrives_as_the_clock_is_read)
        server = MagicMock()
        server.should_exit = False

        async def serves() -> None:
            while not server.should_exit:
                await asyncio.sleep(0.01)

        serving = asyncio.create_task(serves())
        await _serve_until_stopped(server, serving, [], 0.05, lock=None)
        result = await arriving[0]

        assert server.should_exit is True
        assert ran == [], "a call was admitted by an owner that had decided to exit"
        assert _refused_as_retiring(result, None)

    async def test_retirement_never_resets(self, monkeypatch: pytest.MonkeyPatch):
        from linkedin_mcp_server import daemon_liveness

        liveness = daemon_liveness.get_liveness()
        liveness.retire("turnover")
        liveness.retire("idle")
        liveness.call_started()
        liveness.call_finished()
        liveness.the_endpoint_is_live()

        assert liveness.retiring is True
        assert liveness.retire_reason == "turnover"
        assert liveness.try_retire("idle") is True


class TestEveryWayOutClosesAdmission:
    """One test per reason an owner stops, each through the gate."""

    @staticmethod
    def _stopped_server() -> tuple[Any, Any]:
        server = MagicMock()
        server.should_exit = False

        async def serves() -> None:
            while not server.should_exit:
                await asyncio.sleep(0.01)

        return server, asyncio.create_task(serves())

    async def test_idle(self):
        from linkedin_mcp_server import daemon_liveness
        from linkedin_mcp_server.daemon_owner import _serve_until_stopped

        liveness = daemon_liveness.get_liveness()
        liveness.the_endpoint_is_live()
        liveness._quiet_since = liveness._quiet_since - 60  # ty: ignore
        server, serving = self._stopped_server()

        await _serve_until_stopped(server, serving, [], 0.05, lock=None)

        assert server.should_exit is True
        assert liveness.retire_reason == "idle"

    async def test_turnover(self):
        from linkedin_mcp_server import daemon_liveness
        from linkedin_mcp_server.daemon_owner import _serve_until_stopped

        liveness = daemon_liveness.get_liveness()
        server, serving = self._stopped_server()

        await _serve_until_stopped(server, serving, ["asked"], lock=None)

        assert server.should_exit is True
        assert liveness.retire_reason == "turnover"

    async def test_wedged(self, monkeypatch: pytest.MonkeyPatch):
        from linkedin_mcp_server import daemon_liveness, daemon_owner

        monkeypatch.setattr(
            daemon_owner, "stand_down_reason", lambda: "the browser cannot be driven"
        )
        liveness = daemon_liveness.get_liveness()
        server, serving = self._stopped_server()

        await daemon_owner._serve_until_stopped(server, serving, [], lock=None)

        assert server.should_exit is True
        assert liveness.retire_reason == "wedged"

    def test_uncertain_publication(self, monkeypatch: pytest.MonkeyPatch):
        from linkedin_mcp_server import daemon_liveness, daemon_owner

        class Exited(BaseException):
            pass

        def exit_hard(_lock: object) -> None:
            raise Exited

        monkeypatch.setattr(daemon_owner, "_exit_hard", exit_hard)

        with pytest.raises(Exited):
            daemon_owner._exit_uncertain_publication("ambiguous", lock=None)

        assert daemon_liveness.get_liveness().retire_reason == "uncertain publication"


class TestStandingDownForANewerBuild:
    """Turnover closes admission at once and drains for a bounded time."""

    async def test_an_admitted_call_finishes_before_the_owner_stops(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import daemon_liveness
        from linkedin_mcp_server.daemon_owner import _serve_until_stopped

        _mark_calls(monkeypatch)
        liveness = daemon_liveness.get_liveness()
        running = asyncio.Event()
        release = asyncio.Event()

        async def call_next(_context: Any) -> str:
            running.set()
            await release.wait()
            return "the result"

        call = asyncio.create_task(
            OwnerCallLivenessMiddleware().on_call_tool(_call_context(), call_next)  # ty: ignore
        )
        await asyncio.wait_for(running.wait(), timeout=5)
        server = MagicMock()
        server.should_exit = False

        async def serves() -> None:
            while not server.should_exit:
                await asyncio.sleep(0.01)

        loop = asyncio.create_task(
            _serve_until_stopped(
                server, asyncio.create_task(serves()), ["asked"], lock=None
            )
        )
        await asyncio.sleep(0.3)

        assert liveness.retiring is True
        assert server.should_exit is False, "the owner stopped with a call running"

        release.set()
        assert await call == "the result"
        await asyncio.wait_for(loop, timeout=5)
        assert server.should_exit is True

    @staticmethod
    async def _cut_at_the_end_of_the_drain(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, queued: bool
    ) -> tuple[Any, list[str]]:
        """Stand down with one admitted call that outlives a short drain.

        Driven through the real serializing middleware and a real profile lease,
        because the line between a call that never ran and one that may have
        acted is drawn there. *queued* holds the serializing lock for the whole
        drain, so the call never reaches its tool body; otherwise the body
        starts and never finishes.
        """
        from linkedin_mcp_server import daemon_liveness, daemon_owner
        from linkedin_mcp_server.profile_lease import get_profile_lease
        from linkedin_mcp_server.sequential_tool_middleware import (
            SequentialToolExecutionMiddleware,
        )

        monkeypatch.setattr(daemon_owner, "_TURNOVER_DRAIN_SECONDS", 0.2)
        marker = _mark_calls(monkeypatch)
        lease = get_profile_lease(tmp_path / "profile")
        monkeypatch.setattr(
            "linkedin_mcp_server.sequential_tool_middleware.get_profile_lease",
            lambda: lease,
        )
        sequential = SequentialToolExecutionMiddleware()
        ran: list[str] = []
        body_started = asyncio.Event()

        async def tool(_context: Any) -> str:
            ran.append("the tool")
            body_started.set()
            await asyncio.sleep(3600)  # the send that will not finish in time
            return "never reached"  # pragma: no cover

        async def call_next(context: Any) -> Any:
            return await sequential.on_call_tool(context, tool)  # ty: ignore

        liveness = daemon_liveness.get_liveness()
        if queued:
            await sequential._lock.acquire()
        try:
            call = asyncio.create_task(
                OwnerCallLivenessMiddleware().on_call_tool(_call_context(), call_next)
            )
            if queued:
                while marker not in liveness._waiting:
                    await asyncio.sleep(0)
            else:
                await asyncio.wait_for(body_started.wait(), timeout=5)
            server = MagicMock()
            server.should_exit = False

            async def serves() -> None:
                while not server.should_exit:
                    await asyncio.sleep(0.01)

            await daemon_owner._serve_until_stopped(
                server, asyncio.create_task(serves()), ["asked"], lock=None
            )
            result = await asyncio.wait_for(call, timeout=5)
        finally:
            if queued:
                sequential._lock.release()

        assert server.should_exit is True
        assert liveness.calls_in_flight() == 0
        assert lease._refs == 0, "a cut call kept its profile reference"
        return result, ran

    async def test_a_call_whose_body_began_is_reported_as_unknown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        result, ran = await self._cut_at_the_end_of_the_drain(
            monkeypatch, tmp_path, queued=False
        )

        assert ran == ["the tool"]
        assert result.is_error is True
        assert result.structured_content["status"] == "outcome_unknown"
        assert result.structured_content["retry_safe"] is False

    async def test_a_call_still_queued_is_answered_as_not_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Cut while waiting for the serializing lock, it never reached the
        # browser. Calling that an unknown outcome would send the user to
        # LinkedIn to look for an effect that cannot exist, and forbid a retry
        # that is safe. It gets the signed retiring refusal instead, which a
        # frontend reads as not sent and may repeat on a replacement.
        from linkedin_mcp_server import daemon_liveness

        daemon_liveness.get_liveness().serving_as("the-owner")
        result, ran = await self._cut_at_the_end_of_the_drain(
            monkeypatch, tmp_path, queued=True
        )

        assert ran == []
        assert _refused_as_retiring(result, "the-owner")
        assert result.structured_content is None


class TestTheControlRoutes:
    """The heartbeat's 409 and the stand-down route's body rule, on the real app."""

    HOST = "127.0.0.1"
    TOKEN = "the-owners-token"

    def _app(self, stand_down: Any = None) -> Any:
        from linkedin_mcp_server.config.schema import AppConfig
        from linkedin_mcp_server.daemon_owner import create_owner_server

        return create_owner_server(
            config=AppConfig(),
            token=self.TOKEN,
            host=self.HOST,
            port=51234,
            stand_down=stand_down,
        ).config.app

    def _client(self, app: Any) -> Any:
        import httpx

        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=f"http://{self.HOST}:51234",
            trust_env=False,
        )

    async def test_a_retiring_owner_answers_an_unknown_call_with_a_signed_409(self):
        from linkedin_mcp_server import daemon_liveness
        from linkedin_mcp_server.daemon_liveness import HEARTBEAT_PATH

        liveness = daemon_liveness.get_liveness()
        liveness.serving_as("the-owner")
        running = new_call_id()
        liveness.watch(running, MagicMock())
        app = self._app()

        async with self._client(app) as client:

            async def beat(marker: str) -> Any:
                return await client.post(
                    HEARTBEAT_PATH,
                    headers={
                        "Authorization": f"Bearer {self.TOKEN}",
                        CALL_HEADER: marker,
                    },
                )

            before = await beat(new_call_id())
            liveness.retire("turnover")
            refused = await beat(new_call_id())
            still_running = await beat(running)

        assert (before.status_code, before.json()) == (200, {"watched": False})
        assert refused.status_code == 409
        assert refused.json() == {
            "daemon": "retiring",
            "protocol": PROTOCOL_VERSION,
            "instance": "the-owner",
        }
        # Its client is still waiting, so it is still heard.
        assert (still_running.status_code, still_running.json()) == (
            200,
            {"watched": True},
        )

    async def _stand_down(self, app: Any, body: Any = None) -> Any:
        from linkedin_mcp_server.daemon_owner import STAND_DOWN_PATH

        async with self._client(app) as client:
            return await client.post(
                STAND_DOWN_PATH,
                headers={"Authorization": f"Bearer {self.TOKEN}"},
                content=body,
            )

    @pytest.mark.parametrize(
        "body",
        [
            b'{"only_if_idle": true}',
            b"{}",
            b"not json",
            b" ",
            b"\xff\xfe",
            b"[]",
            b"null",
            b'"only_if_idle"',
            _idle_only_body(only_if_idle=False),
            _idle_only_body(only_if_idle=1),
            _idle_only_body(protocol=True),
            _idle_only_body(protocol=str(PROTOCOL_VERSION)),
            _idle_only_body(protocol=PROTOCOL_VERSION - 1),
            _idle_only_body(protocol=PROTOCOL_VERSION + 1),
            _idle_only_body(instance="another-owner"),
            _idle_only_body(instance=7),
            _idle_only_body(instance=None),
            _idle_only_body(force=True),
            _idle_only_body(drop="instance"),
            _idle_only_body(drop="protocol"),
        ],
        ids=[
            "idle only alone",
            "empty object",
            "malformed",
            "whitespace",
            "not utf-8",
            "array",
            "null",
            "string",
            "only_if_idle false",
            "only_if_idle as number",
            "protocol as bool",
            "protocol as text",
            "older protocol",
            "newer protocol",
            "another instance",
            "instance as number",
            "instance null",
            "an extra key",
            "no instance",
            "no protocol",
        ],
    )
    async def test_a_stand_down_with_any_other_body_changes_nothing(self, body: bytes):
        # Only the absent body is the unconditional stand-down, and only the
        # exact idle-only body is the other request. Everything else is refused
        # before any retirement state moves, and a failed request must never
        # become the unconditional one.
        from linkedin_mcp_server import daemon_liveness

        liveness = daemon_liveness.get_liveness()
        liveness.serving_as("the-owner")
        asked: list[str] = []
        app = self._app(stand_down=lambda: asked.append("asked"))

        response = await self._stand_down(app, body)

        assert response.status_code == 400
        assert asked == []
        assert liveness.retiring is False
        assert liveness.retire_reason is None

    async def test_an_idle_owner_retires_on_the_idle_only_request(self):
        # Retirement and turnover both, in the request: a call arriving after
        # the reply is refused, and the loop stands down on its next tick.
        from linkedin_mcp_server import daemon_liveness

        liveness = daemon_liveness.get_liveness()
        liveness.serving_as("the-owner")
        asked: list[str] = []
        app = self._app(stand_down=lambda: asked.append("asked"))

        response = await self._stand_down(app, _idle_only_body())

        assert response.status_code == 200
        assert response.json() == {
            "standing_down": True,
            "retiring": True,
            "instance": "the-owner",
        }
        assert asked == ["asked"]
        assert liveness.retiring is True
        assert liveness.retire_reason == "retire"

        async def work() -> str:
            return "ran"

        assert liveness.admit(new_call_id(), work) is None

    @pytest.mark.parametrize(
        "busy_with", ["running", "queued", "setup"], ids=["running", "queued", "setup"]
    )
    async def test_a_busy_owner_refuses_and_changes_nothing(
        self, busy_with: str, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import daemon_liveness, daemon_owner

        liveness = daemon_liveness.get_liveness()
        liveness.serving_as("the-owner")
        if busy_with == "running":
            liveness.watch(new_call_id(), MagicMock())
            liveness.call_started()
        elif busy_with == "queued":
            # Admitted, so counted, and still waiting behind another call for
            # the sequential lock: as busy as a running one.
            liveness.call_started()
        else:
            monkeypatch.setattr(daemon_owner, "browser_setup_in_progress", lambda: True)
        asked: list[str] = []
        app = self._app(stand_down=lambda: asked.append("asked"))

        response = await self._stand_down(app, _idle_only_body())

        assert response.status_code == 409
        assert response.json() == {"standing_down": False, "busy": True}
        assert asked == []
        assert liveness.retiring is False

        async def work() -> str:
            return "ran"

        admitted = liveness.admit(new_call_id(), work)
        assert admitted is not None, "a refused retirement closed admission"
        await admitted

    async def test_an_owner_already_draining_calls_still_answers_busy(self):
        # Retiring for another reason, a turnover here, with a call it is still
        # draining. The user agreed only to retiring an owner nobody is using,
        # so this is a refusal, not an acknowledgement of someone else's exit.
        from linkedin_mcp_server import daemon_liveness

        liveness = daemon_liveness.get_liveness()
        liveness.serving_as("the-owner")
        liveness.call_started()
        liveness.retire("turnover")
        asked: list[str] = []
        app = self._app(stand_down=lambda: asked.append("asked"))

        response = await self._stand_down(app, _idle_only_body())

        assert response.status_code == 409
        assert response.json() == {"standing_down": False, "busy": True}
        assert asked == []
        assert liveness.retire_reason == "turnover"

    async def test_a_call_cannot_slip_in_between_the_verdict_and_the_retirement(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # A call arrives on the very next loop step after the owner looked for
        # work. With nothing awaited between that look and the retirement it
        # arrives to a retiring owner and is refused; with anything awaited in
        # between it would be admitted by an owner that then says it is idle.
        from linkedin_mcp_server import daemon_liveness

        liveness = daemon_liveness.get_liveness()
        liveness.serving_as("the-owner")
        app = self._app(stand_down=lambda: None)
        admitted: list[bool] = []
        real_busy = liveness.busy

        async def work() -> str:
            return "ran"

        def arrive() -> None:
            admitted.append(liveness.admit(new_call_id(), work) is not None)

        def busy(**kwargs: Any) -> bool:
            verdict = real_busy(**kwargs)
            asyncio.get_running_loop().call_soon(arrive)
            return verdict

        monkeypatch.setattr(liveness, "busy", busy)

        response = await self._stand_down(app, _idle_only_body())
        await asyncio.sleep(0)

        assert admitted, "the arriving call never ran"
        assert response.status_code == 200
        # One arrival per look for work; every one of them must be refused.
        assert not any(admitted), "a call was admitted by an owner that retired"

    async def test_the_body_is_read_whole_before_anything_is_decided(self):
        # The body arrives in two parts with the owner suspended between them,
        # and a call is admitted meanwhile. Nothing may be decided while it is
        # still arriving: a decision taken on the first part would read a body
        # that has not finished as absent, and one taken before the call would
        # retire an owner that is now busy.
        from linkedin_mcp_server import daemon_liveness

        liveness = daemon_liveness.get_liveness()
        liveness.serving_as("the-owner")
        asked: list[str] = []
        app = self._app(stand_down=lambda: asked.append("asked"))
        arrived = asyncio.Event()
        more = asyncio.Event()
        whole = _idle_only_body()

        async def body() -> Any:
            yield b""
            arrived.set()
            await more.wait()
            yield whole

        sent = asyncio.create_task(self._stand_down(app, body()))
        await asyncio.wait_for(arrived.wait(), timeout=5)
        await asyncio.sleep(0.05)
        assert asked == [], "the stand-down was decided before the body ended"
        liveness.call_started()
        more.set()
        response = await asyncio.wait_for(sent, timeout=5)

        assert response.status_code == 409
        assert asked == []
        assert liveness.retiring is False

    async def test_a_stand_down_without_a_body_is_the_unconditional_one(self):
        from linkedin_mcp_server.daemon_owner import STAND_DOWN_PATH

        asked: list[str] = []
        app = self._app(stand_down=lambda: asked.append("asked"))

        async with self._client(app) as client:
            response = await client.post(
                STAND_DOWN_PATH, headers={"Authorization": f"Bearer {self.TOKEN}"}
            )

        assert response.status_code == 200
        assert asked == ["asked"]


class TestTheLastCheckBeforeBrowserWork:
    """After every wait a call can spend queued, and before the browser."""

    def test_a_direct_server_never_asks(self):
        from linkedin_mcp_server.daemon_liveness import abandoned_before_browser_work

        assert abandoned_before_browser_work() is False

    async def test_a_call_id_the_tracker_does_not_know_is_abandoned(self):
        # Not a fresh heartbeat: an absent entry was expired, or never admitted.
        from linkedin_mcp_server import daemon_liveness

        async def ask() -> bool:
            daemon_liveness._current_call.set(new_call_id())
            return daemon_liveness.abandoned_before_browser_work()

        assert await asyncio.create_task(ask()) is True

    async def test_an_entry_past_the_expiry_is_abandoned_before_the_scan(self):
        from linkedin_mcp_server import daemon_liveness

        liveness = daemon_liveness.get_liveness()
        fresh, stale = new_call_id(), new_call_id()
        liveness.watch(fresh, MagicMock())
        liveness.watch(stale, MagicMock())
        _last_heard(liveness, stale, EXPIRY_SECONDS + 1)

        async def ask(call_id: str) -> bool:
            daemon_liveness._current_call.set(call_id)
            return daemon_liveness.abandoned_before_browser_work()

        assert await asyncio.create_task(ask(fresh)) is False
        assert await asyncio.create_task(ask(stale)) is True

    @pytest.mark.parametrize("already_held", [False, True], ids=["fresh", "reentrant"])
    async def test_an_abandoned_call_returns_its_lease_and_never_starts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        already_held: bool,
    ):
        """Only the reference this call took goes back, and no browser work begins.

        Driven the way it happens: the call is admitted and queued behind the
        serializing lock, its client goes quiet while it waits, and it gets the
        lock and the profile before any expiry scan runs.
        """
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server import daemon_liveness
        from linkedin_mcp_server.drivers import browser
        from linkedin_mcp_server.profile_lease import get_profile_lease
        from linkedin_mcp_server.sequential_tool_middleware import (
            SequentialToolExecutionMiddleware,
        )

        marker = _mark_calls(monkeypatch)
        liveness = daemon_liveness.get_liveness()
        lease = get_profile_lease(tmp_path / "profile")
        if already_held:
            assert lease.try_acquire()
        refs_before = lease._refs
        monkeypatch.setattr(
            "linkedin_mcp_server.sequential_tool_middleware.get_profile_lease",
            lambda: lease,
        )
        counted: list[str] = []
        monkeypatch.setattr(browser, "note_call_started", lambda: counted.append("+"))
        monkeypatch.setattr(browser, "note_activity", lambda: counted.append("-"))
        sequential = SequentialToolExecutionMiddleware()
        ran: list[str] = []

        async def tool(_context: Any) -> str:
            ran.append("the tool")
            return "the result"

        async def call_next(context: Any) -> Any:
            return await sequential.on_call_tool(context, tool)  # ty: ignore

        await sequential._lock.acquire()
        call = asyncio.create_task(
            OwnerCallLivenessMiddleware().on_call_tool(_call_context(), call_next)
        )
        while marker not in liveness._waiting:
            await asyncio.sleep(0)
        _last_heard(liveness, marker, EXPIRY_SECONDS + 1)
        sequential._lock.release()

        with pytest.raises(ToolError, match="stopped waiting"):
            await asyncio.wait_for(call, timeout=5)

        assert ran == []
        assert counted == [], "the browser-call count moved for a call that never ran"
        assert lease._refs == refs_before
        assert (lease._fd is not None) is already_held
        if already_held:
            lease.release()

    async def test_a_call_cancelled_while_queued_takes_no_lease(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        from linkedin_mcp_server import daemon_liveness
        from linkedin_mcp_server.profile_lease import get_profile_lease
        from linkedin_mcp_server.sequential_tool_middleware import (
            SequentialToolExecutionMiddleware,
        )

        marker = _mark_calls(monkeypatch)
        liveness = daemon_liveness.get_liveness()
        lease = get_profile_lease(tmp_path / "profile")
        monkeypatch.setattr(
            "linkedin_mcp_server.sequential_tool_middleware.get_profile_lease",
            lambda: lease,
        )
        sequential = SequentialToolExecutionMiddleware()

        async def tool(_context: Any) -> str:
            return "the result"

        async def call_next(context: Any) -> Any:
            return await sequential.on_call_tool(context, tool)  # ty: ignore

        await sequential._lock.acquire()
        try:
            call = asyncio.create_task(
                OwnerCallLivenessMiddleware().on_call_tool(_call_context(), call_next)
            )
            while marker not in liveness._waiting:
                await asyncio.sleep(0)
            _last_heard(liveness, marker, EXPIRY_SECONDS + 1)
            assert liveness.cancel_the_abandoned() == [marker]
            with pytest.raises(Exception, match="stopped waiting"):
                await asyncio.wait_for(call, timeout=5)
        finally:
            sequential._lock.release()

        assert lease._refs == 0
        assert liveness.calls_in_flight() == 0
