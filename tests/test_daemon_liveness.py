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
        # unmarked call is never cancelled, and that is the safe direction. This
        # arrives in a header from anything that can reach the port.
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

    async def test_an_unmarked_call_runs_and_is_never_watched(self, monkeypatch):
        # The old-frontend direction of the compatibility claim. A frontend from
        # before this existed sends no marker, and its calls must run exactly as
        # they always did rather than becoming cancellable by anybody.
        from linkedin_mcp_server import daemon_liveness

        self._headers(monkeypatch, {})
        liveness = daemon_liveness.get_liveness()

        async def call_next(_context: Any) -> str:
            assert liveness._waiting == {}, "an unmarked call was watched"
            return "the result"

        result = await OwnerCallLivenessMiddleware().on_call_tool(
            self._context(),
            call_next,  # ty: ignore
        )
        assert result == "the result"

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
        the one running the call.
        """
        from linkedin_mcp_server.daemon_proxy import FrontendCallHeartbeatMiddleware

        middleware = FrontendCallHeartbeatMiddleware(backend)

        async def beat(attachment: Any, call_id: str) -> int:
            beats.append((attachment.descriptor.instance_id, call_id))
            return status

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

    async def test_an_owner_without_the_route_is_served_anyway(
        self, tmp_path: Path, quick_cadence: float
    ):
        # The new-frontend direction of the compatibility claim. A 404 is an
        # owner from before heartbeats existed, and its calls must go ahead
        # unheard rather than fail.
        backend = self._backend(tmp_path)
        beats: list[tuple[str, str]] = []
        middleware = self._middleware(backend, beats, status=404)

        during: list[int] = []

        async def call_next(_context: Any) -> str:
            # Counted while the call is still running. Afterwards proves
            # nothing: the beater is cancelled when the call returns either
            # way, so a beater that should never have started looks identical
            # from outside.
            await asyncio.sleep(quick_cadence * 6)
            during.append(len(beats))
            return "the result"

        assert (
            await middleware.on_call_tool(
                self._context(),
                call_next,
            )
            == "the result"
        )
        assert during == [1], "kept beating at an owner with no such route"

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
        await _serve_until_stopped(MagicMock(), serving, [])

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
        await _serve_until_stopped(server, serving, [], idle_timeout)
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

    async def test_it_does_not_exit_before_the_endpoint_is_published(self):
        # The clock has not started. An owner spends its first seconds importing
        # and launching Chromium, and a short timeout would otherwise fire
        # during startup, before the frontend that asked for it could call.
        from linkedin_mcp_server import daemon_liveness

        assert daemon_liveness.get_liveness().quiet_for() is None
        assert await self._run_loop(idle_timeout=0.05) is False

    @pytest.mark.parametrize("marked", [True, False], ids=["marked", "unmarked"])
    async def test_a_call_in_flight_holds_the_door(
        self, marked: bool, monkeypatch: pytest.MonkeyPatch
    ):
        # Both cases matter and only one is obvious. An unmarked call cannot be
        # cancelled, and calling it idleness would exit in the middle of one,
        # cutting off exactly the older frontends this build promises to serve.
        #
        # Driven through the middleware rather than by calling the tracker here.
        # An earlier version of this test registered the call itself and stayed
        # green when the middleware stopped counting unmarked calls at all,
        # which is the whole thing it exists to catch.
        from linkedin_mcp_server import daemon_liveness

        headers = {CALL_HEADER: new_call_id()} if marked else {}
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

profile = Path(sys.argv[1])
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
        loop = asyncio.create_task(_serve_until_stopped(MagicMock(), serving, []))
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
