"""Carrying an auth failure from the owner to the process that can fix it.

The owner drives the browser and notices bad LinkedIn auth; it has no terminal, so
it cannot repair it. The frontend can. These tests cover the crossing between them
and the decisions made at each end, because every one of them is a way to either
lose the signal or act on it wrongly.
"""

from __future__ import annotations

import asyncio

from unittest.mock import AsyncMock, patch

import mcp.types as mt
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from fastmcp.server.providers.proxy import ProxyClient, ProxyProvider
from fastmcp.tools import ToolResult

from linkedin_mcp_server import daemon_auth
from linkedin_mcp_server.daemon_auth import (
    MARKER_KEY,
    MARKER_VERSION,
    FrontendAuthRepairMiddleware,
    OwnerAuthSignalMiddleware,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.exceptions import (
    AuthMissingOnOwnerError,
    AuthStaleOnOwnerError,
)


def _owner_that_fails_with(
    error: Exception, *, then: str = "scraped ok"
) -> tuple[FastMCP, list[int]]:
    """An owner whose tool fails with *error* once, then succeeds.

    Through the production middleware and through ``raise_tool_error``, which is
    how every real tool body ends. The second part matters more than it looks:
    ``mask_error_details`` replaces the text of anything that is not already a
    ``ToolError`` before the middleware ever sees it, so a test that raised the
    domain exception raw would assert against ``"Error calling tool 'scrape'"``
    and conclude the owner's wording is lost when it is not.
    """
    owner = FastMCP("owner", mask_error_details=True)
    calls: list[int] = []

    # Annotated read-only, like the scraping tools it stands for. The annotation
    # is load-bearing rather than decoration: only a tool that declares itself
    # read-only is ever replayed, because a replay the client never sees the
    # result of would repeat whatever the tool did.
    @owner.tool(annotations={"readOnlyHint": True})
    async def scrape() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise_tool_error(error, "scrape")
        return then

    owner.add_middleware(OwnerAuthSignalMiddleware())
    return owner, calls


def _proxy_to(
    owner: FastMCP, *, repairing: bool = True, tool_timeout: float | None = None
) -> FastMCP:
    """A frontend forwarding to *owner* in memory, wired as production does.

    *tool_timeout* is handed to the middleware the way ``create_mcp_server`` does,
    rather than left to the configuration: the repair budgets itself from what
    its server was built with, so a test that set only the config would be
    measuring a number the production path never reads.
    """
    front = FastMCP("front", mask_error_details=True)
    front.add_provider(ProxyProvider(lambda: ProxyClient(owner)))
    if repairing:
        front.add_middleware(
            FrontendAuthRepairMiddleware(
                **({} if tool_timeout is None else {"tool_timeout": tool_timeout})
            )
        )
    return front


class TestTheMarkerSurvivesTheHop:
    """A raise loses everything that identifies the failure; a result does not."""

    async def test_the_owner_marks_a_missing_session(self):
        owner, calls = _owner_that_fails_with(
            AuthMissingOnOwnerError("no session", nothing_ran_yet=True)
        )

        with patch(
            "linkedin_mcp_server.daemon_auth._browser_still_holds_the_profile",
            return_value=False,
        ):
            async with Client(owner) as client:
                result = await client.call_tool("scrape", raise_on_error=False)

        assert result.is_error is True
        assert result.meta is not None
        assert result.meta[MARKER_KEY] == {
            "v": MARKER_VERSION,
            "reason": "missing",
            "replayable": True,
            "browser_open": False,
            "generation": None,
        }

    async def test_the_marker_reaches_the_outer_client_through_the_proxy(self):
        # The property the whole design rests on. Measured before this existed: a
        # middleware that re-raised delivered meta=None and a generic message,
        # because mask_error_details had already discarded the type.
        owner, calls = _owner_that_fails_with(
            AuthStaleOnOwnerError("session stopped working", nothing_ran_yet=False)
        )

        with patch(
            "linkedin_mcp_server.daemon_auth._browser_still_holds_the_profile",
            return_value=False,
        ):
            # No repair middleware: this asserts what crosses the hop, which a
            # working frontend consumes.
            async with Client(_proxy_to(owner, repairing=False)) as client:
                result = await client.call_tool("scrape", raise_on_error=False)

        assert result.meta[MARKER_KEY]["reason"] == "stale"
        assert result.meta[MARKER_KEY]["replayable"] is False
        # The owner's own wording survives too, so a client that knows nothing
        # about markers still gets a usable message rather than a bare failure.
        assert "session stopped working" in result.content[0].text

    async def test_a_raising_client_still_gets_a_tool_error(self):
        # is_error is kept true deliberately. A success carrying no data would be
        # worse than a failure: a client would treat the empty result as an answer.
        owner, calls = _owner_that_fails_with(AuthMissingOnOwnerError("no session"))

        with patch(
            "linkedin_mcp_server.daemon_auth._browser_still_holds_the_profile",
            return_value=False,
        ):
            async with Client(owner) as client:
                with pytest.raises(ToolError, match="no session"):
                    await client.call_tool("scrape")

    async def test_an_unrelated_failure_is_left_alone(self):
        # The middleware must not turn every error into an invitation to log in.
        owner, _calls = _owner_that_fails_with(RuntimeError("the page moved"))

        async with Client(owner) as client:
            result = await client.call_tool("scrape", raise_on_error=False)

        assert result.is_error is True
        assert MARKER_KEY not in (result.meta or {})

    async def test_the_marker_is_found_through_the_wrapping_tool_error(self):
        # Nothing that leaves a tool is the original exception: raise_tool_error
        # wraps it. Reading `type(exc)` would see only ToolError, so the chain has
        # to be walked, and this asserts the walk rather than the ideal case.
        owner = FastMCP("owner", mask_error_details=True)

        @owner.tool
        async def scrape() -> str:
            try:
                raise_tool_error(
                    AuthMissingOnOwnerError("no session", nothing_ran_yet=True),
                    "scrape",
                )
            except Exception as exc:  # what a real tool body does next
                raise_tool_error(exc, "scrape")

        owner.add_middleware(OwnerAuthSignalMiddleware())

        with patch(
            "linkedin_mcp_server.daemon_auth._browser_still_holds_the_profile",
            return_value=False,
        ):
            async with Client(owner) as client:
                result = await client.call_tool("scrape", raise_on_error=False)

        assert result.meta[MARKER_KEY]["reason"] == "missing"


class TestTheFrontendActsOnTheMarker:
    """Repairing and replaying are separate decisions, and both can go wrong."""

    @pytest.fixture(autouse=True)
    def _a_real_config(self):
        """The replay reads its deadline from the configuration.

        Built rather than left to the default, because ``get_config`` parses
        ``sys.argv`` when nothing has been set, which under pytest is pytest's
        own command line.
        """
        from linkedin_mcp_server.config import set_config
        from linkedin_mcp_server.config.schema import AppConfig

        set_config(AppConfig())

    def _repair(self):
        """Watch the login without running one."""
        return patch(
            "linkedin_mcp_server.daemon_auth._repair_auth_locally",
            new_callable=AsyncMock,
        )

    def _profile_is_free(self):
        return patch(
            "linkedin_mcp_server.daemon_auth._browser_still_holds_the_profile",
            return_value=False,
        )

    async def test_a_replayable_failure_is_run_again_exactly_once(self):
        owner, calls = _owner_that_fails_with(
            AuthMissingOnOwnerError("no session", nothing_ran_yet=True)
        )

        with self._profile_is_free(), self._repair() as repair:
            async with Client(_proxy_to(owner)) as client:
                result = await client.call_tool("scrape", raise_on_error=False)

        repair.assert_awaited_once_with("missing", None)
        # Twice on the owner: the failure, then the replay. Not three times, which
        # is what a retry loop without a bound would give.
        assert len(calls) == 2
        assert result.is_error is False
        assert result.content[0].text == "scraped ok"

    async def test_a_call_that_had_already_started_is_never_run_again(self):
        # The one that protects LinkedIn state. Some of these tools send messages
        # and connection requests, so a replay could repeat a side effect the user
        # never asked for twice.
        owner, calls = _owner_that_fails_with(
            AuthStaleOnOwnerError("session died mid-scrape", nothing_ran_yet=False)
        )

        with self._profile_is_free(), self._repair() as repair:
            async with Client(_proxy_to(owner)) as client:
                result = await client.call_tool("scrape", raise_on_error=False)

        # Repaired, because the next call should succeed, but not replayed.
        repair.assert_awaited_once_with("stale", None)
        assert len(calls) == 1
        assert result.is_error is True

    async def test_no_login_starts_while_the_profile_may_still_be_held(self):
        # A login takes the profile exclusively. Starting one while the owner's
        # Chromium may still be on it fails on a lease it cannot get, and the user
        # sees a login that refuses to open for no visible reason.
        owner, calls = _owner_that_fails_with(
            AuthMissingOnOwnerError("no session", nothing_ran_yet=True)
        )

        with (
            patch(
                "linkedin_mcp_server.daemon_auth._browser_still_holds_the_profile",
                return_value=True,
            ),
            self._repair() as repair,
        ):
            async with Client(_proxy_to(owner)) as client:
                result = await client.call_tool("scrape", raise_on_error=False)

        repair.assert_not_awaited()
        assert len(calls) == 1
        assert result.is_error is True

    async def test_a_marker_from_a_newer_build_is_ignored(self):
        # An older frontend attaches to a newer owner on purpose, so it will meet
        # markers whose fields it cannot be sure of. Guessing at them would act on
        # a shape that may mean something else.
        owner = FastMCP("owner", mask_error_details=True)

        @owner.tool
        async def scrape() -> str:
            return "unreachable"

        class FromTheFuture(Middleware):
            async def on_call_tool(self, context, call_next):
                return ToolResult(
                    content=[mt.TextContent(type="text", text="please sign in")],
                    meta={
                        MARKER_KEY: {
                            "v": MARKER_VERSION + 1,
                            "reason": "missing",
                            "replayable": True,
                            "browser_open": False,
                            "generation": None,
                        }
                    },
                    is_error=True,
                )

        owner.add_middleware(FromTheFuture())

        with self._repair() as repair:
            async with Client(_proxy_to(owner)) as client:
                result = await client.call_tool("scrape", raise_on_error=False)

        repair.assert_not_awaited()
        assert result.is_error is True

    async def test_a_malformed_marker_is_ignored(self):
        # Every field is checked rather than assumed, because this runs on
        # whatever arrived over the hop.
        owner = FastMCP("owner", mask_error_details=True)

        @owner.tool
        async def scrape() -> str:
            return "unreachable"

        class Malformed(Middleware):
            async def on_call_tool(self, context, call_next):
                return ToolResult(
                    content=[mt.TextContent(type="text", text="please sign in")],
                    # replayable is a string, not a bool: acting on this would be
                    # a truthiness bug that replays a mutating call.
                    meta={
                        MARKER_KEY: {
                            "v": MARKER_VERSION,
                            "reason": "missing",
                            "replayable": "yes",
                            "browser_open": False,
                            "generation": None,
                        }
                    },
                    is_error=True,
                )

        owner.add_middleware(Malformed())

        with self._repair() as repair:
            async with Client(_proxy_to(owner)) as client:
                await client.call_tool("scrape", raise_on_error=False)

        repair.assert_not_awaited()

    async def test_a_failed_login_leaves_the_owners_answer_standing(self):
        owner, calls = _owner_that_fails_with(
            AuthMissingOnOwnerError("no session", nothing_ran_yet=True)
        )

        with (
            self._profile_is_free(),
            patch(
                "linkedin_mcp_server.daemon_auth._repair_auth_locally",
                new_callable=AsyncMock,
                side_effect=RuntimeError("the user closed the window"),
            ),
        ):
            async with Client(_proxy_to(owner)) as client:
                result = await client.call_tool("scrape", raise_on_error=False)

        # No replay against an owner that still cannot serve the call, and the
        # failure the owner reported is still the honest answer.
        assert len(calls) == 1
        assert result.is_error is True

    async def test_an_ordinary_success_is_untouched(self):
        owner = FastMCP("owner", mask_error_details=True)

        @owner.tool
        async def scrape() -> str:
            return "scraped ok"

        with self._repair() as repair:
            async with Client(_proxy_to(owner)) as client:
                result = await client.call_tool("scrape")

        repair.assert_not_awaited()
        assert result.content[0].text == "scraped ok"
        # And no control value rides along on a success, which a second proxy
        # layer would otherwise read and act on.
        assert MARKER_KEY not in (result.meta or {})

    async def test_a_login_that_does_not_help_stops_after_one_retry(self):
        """The bound has to hold when the repair changes nothing.

        The happy path cannot show this: there the replay succeeds, so a recursive
        or looping implementation would stop anyway and look correct. An owner that
        keeps refusing is what separates "runs the call again" from "runs the call
        again until something gives", and the second would hammer LinkedIn through
        a shared browser on every forwarded call.
        """
        owner = FastMCP("owner", mask_error_details=True)
        calls: list[int] = []

        @owner.tool(annotations={"readOnlyHint": True})
        async def scrape() -> str:
            calls.append(1)
            raise_tool_error(
                AuthMissingOnOwnerError("still no session", nothing_ran_yet=True),
                "scrape",
            )

        owner.add_middleware(OwnerAuthSignalMiddleware())

        with self._profile_is_free(), self._repair() as repair:
            async with Client(_proxy_to(owner)) as client:
                result = await client.call_tool("scrape", raise_on_error=False)

        assert len(calls) == 2
        # And the login is attempted once, not once per attempt.
        repair.assert_awaited_once()
        assert result.is_error is True

    async def test_a_peer_win_still_replays_the_call(self):
        """Somebody else signing in is a repair, not a failed one.

        The session the call needs exists either way. Treating the peer win as a
        failure refused the replay and sent the user back for a retry that was
        not needed, while a usable session was already on disk.
        """
        from linkedin_mcp_server.session_state import PeerSessionInPlaceError

        owner, calls = _owner_that_fails_with(
            AuthMissingOnOwnerError("no session", nothing_ran_yet=True)
        )

        with (
            self._profile_is_free(),
            patch(
                "linkedin_mcp_server.daemon_auth._repair_auth_locally",
                new_callable=AsyncMock,
                side_effect=PeerSessionInPlaceError("another client signed in"),
            ),
        ):
            async with Client(_proxy_to(owner)) as client:
                result = await client.call_tool("scrape", raise_on_error=False)

        # Replayed, and the owner served it the second time.
        assert len(calls) == 2
        assert result.is_error is False
        assert result.content[0].text == "scraped ok"


class TestTheRepairRunsForReal:
    """The same replay, with the production repair instead of a stand-in.

    Every test above replaces ``_repair_auth_locally`` with a mock that returns
    or raises on command. That is the right shape for asking what the middleware
    does with an outcome, and the wrong one for asking whether the outcome ever
    occurs: both functions behind it report a *started* login by raising, while
    the window is still open. Mocked away, the middleware was measured to abandon
    every repair one step before it worked, on both paths, and the tests above
    stayed green throughout.

    So these stub only the login itself -- the one thing a test cannot do -- and
    let the real bootstrap code run in between.
    """

    def _profile_is_free(self):
        return patch(
            "linkedin_mcp_server.daemon_auth._browser_still_holds_the_profile",
            return_value=False,
        )

    def _a_login_that(self, *, takes: float, succeeds: bool = True):
        """Stub the login flow, and with it what readiness reports afterwards.

        *takes* is what separates the two cases that matter: a login finishing
        inside the inline budget returns through the normal path, while a slower
        one leaves ``_start_login_if_needed`` raising "in progress" -- which is
        every login a person actually performs.
        """
        done: list[bool] = []

        async def login() -> None:
            await asyncio.sleep(takes)
            if succeeds:
                done.append(True)

        return patch.multiple(
            "linkedin_mcp_server.bootstrap",
            _run_login_flow=AsyncMock(side_effect=login),
            _force_move_auth_state_aside=lambda *a, **k: None,
            _move_invalid_auth_state_aside=lambda *a, **k: None,
            _auto_import_allowed=lambda *a, **k: False,
            # A login that succeeded has written a session; one that failed has
            # not. This is what the wait reads to decide whether to replay.
            _auth_ready=lambda *a, **k: bool(done),
        )

    @pytest.fixture(autouse=True)
    def _short_budget(self, monkeypatch):
        """Scale the inline wait down, keeping the shape of the real timings.

        Built rather than read: ``get_config`` parses ``sys.argv``, which under
        pytest is pytest's own command line.
        """
        from linkedin_mcp_server.config import set_config
        from linkedin_mcp_server.config.schema import AppConfig

        config = AppConfig()
        config.browser.login_inline_wait_seconds = 0.2
        # The repair wait is a fraction of this, so setting the real budget is
        # what scales it, and the test exercises that derivation rather than
        # patching over it.
        config.server.tool_timeout_seconds = 6.0
        set_config(config)

    async def test_a_stale_session_is_replayed_once_the_login_succeeds(self):
        """The path that never replayed: this one raises immediately, always."""
        owner, calls = _owner_that_fails_with(
            AuthStaleOnOwnerError("dead", nothing_ran_yet=True, generation="g-old")
        )

        with self._profile_is_free(), self._a_login_that(takes=0.05):
            async with Client(_proxy_to(owner)) as client:
                result = await client.call_tool("scrape", raise_on_error=False)

        assert len(calls) == 2, "the call was never run again"
        assert result.is_error is False
        assert result.content[0].text == "scraped ok"

    async def test_a_slow_sign_in_is_still_replayed(self):
        """A login outlasting the inline budget, which is every human one.

        The missing path used to pass only because the stub finished at once.
        Give it a login somebody has to type into and it raised "in progress",
        which the frontend read as a failed repair.
        """
        owner, calls = _owner_that_fails_with(
            AuthMissingOnOwnerError("no session", nothing_ran_yet=True)
        )

        with self._profile_is_free(), self._a_login_that(takes=0.6):
            async with Client(_proxy_to(owner)) as client:
                result = await client.call_tool("scrape", raise_on_error=False)

        assert len(calls) == 2, "a login slower than the budget was not waited out"
        assert result.is_error is False

    async def test_a_login_that_fails_is_not_replayed(self):
        """Waiting is not the same as assuming success."""
        owner, calls = _owner_that_fails_with(
            AuthMissingOnOwnerError("no session", nothing_ran_yet=True)
        )

        with self._profile_is_free(), self._a_login_that(takes=0.05, succeeds=False):
            async with Client(_proxy_to(owner)) as client:
                result = await client.call_tool("scrape", raise_on_error=False)

        assert len(calls) == 1, "the call was replayed with no session to serve it"
        assert result.is_error is True

    async def test_a_sign_in_slower_than_the_wait_gives_up_without_replaying(self):
        """The client is blocked on this call, so the wait cannot be unbounded.

        Giving up costs only this attempt: the login keeps running, and the next
        call finds the session it writes.
        """
        from linkedin_mcp_server.config import get_config

        owner, calls = _owner_that_fails_with(
            AuthMissingOnOwnerError("no session", nothing_ran_yet=True)
        )
        get_config().server.tool_timeout_seconds = 0.4

        with self._profile_is_free(), self._a_login_that(takes=5.0):
            async with Client(_proxy_to(owner, tool_timeout=0.4)) as client:
                result = await client.call_tool("scrape", raise_on_error=False)

        assert len(calls) == 1
        assert result.is_error is True

    async def test_the_wait_stays_inside_a_shortened_tool_budget(self):
        """A user who lowers TOOL_TIMEOUT must still get the readable answer.

        The wait was a fixed 150 seconds once. Measured against
        ``TOOL_TIMEOUT=60``: the client's call was cut short while the middleware
        was still waiting, so the user got a bare timeout instead of "sign-in
        still in progress". Nothing was corrupted -- the login survived and the
        next call worked -- but the explanation is the part a person acts on.

        The budget asked for is asserted rather than the wall clock it produces.
        A first version timed the whole call and compared against the timeout: it
        passed alone and failed inside the full suite at 1.22s against 1.2s,
        because everything else in the call is also on that clock and a loaded
        machine stretches all of it. That measures the runner, not the code.

        Both halves of the arithmetic are checked. Asserting only "less than the
        timeout" left the subtraction untested: dropping ``- already_spent``
        yields five sixths of the budget, which is still under it, and the test
        stayed green while the overrun it exists for came back.
        """
        budget = 1.2
        owner, calls = _owner_that_fails_with(
            AuthMissingOnOwnerError("no session", nothing_ran_yet=True)
        )
        asked_for: list[float] = []

        real_wait = daemon_auth._wait_for_the_sign_in

        async def record(timeout: float) -> bool:
            asked_for.append(timeout)
            return await real_wait(timeout)

        with (
            self._profile_is_free(),
            self._a_login_that(takes=30.0),
            patch.object(daemon_auth, "_wait_for_the_sign_in", record),
        ):
            async with Client(_proxy_to(owner, tool_timeout=budget)) as client:
                result = await client.call_tool("scrape", raise_on_error=False)

        assert asked_for, "the sign-in was never waited for"
        share = budget * 5 / 6
        # Strictly under the share, because time had already gone before this was
        # reached. Comparing against the share rather than against a second clock
        # reading: an earlier version subtracted a duration measured from the
        # start of the *test*, which includes client setup the middleware's own
        # clock never saw, and it failed at 1.22s against 1.2s under full-suite
        # load while passing alone. That measured the runner.
        assert asked_for[0] < share, "the spent time was not subtracted"
        assert result.is_error is True
        assert len(calls) == 1

        # And exactly, away from the clock entirely, so the subtraction is pinned
        # rather than merely implied by an inequality.
        assert daemon_auth._how_long_to_wait_for_the_sign_in(1.2, 0.5) == pytest.approx(
            0.5
        )

    async def test_a_replay_that_runs_long_does_not_hang_the_client(self):
        """Waiting bounded and replaying unbounded still overruns the deadline.

        Nothing under this middleware bounds the whole path: the forwarded call
        gets its own deadline per hop, so the first call, the wait and the replay
        each stay inside one while their sum stays inside nothing. A sign-in
        finishing near the end of the wait, followed by a slow replay, put the
        client past its deadline with the answer already in hand.

        The owner's readable failure is the right answer then: the session now
        exists, so the next call succeeds, and a bare transport error would say
        none of that.

        Read-only, which is what makes giving up safe here: an abandoned scrape
        costs a wasted page load. The mutating case below must not do this.
        """
        owner = FastMCP("owner", mask_error_details=True)
        calls: list[int] = []

        @owner.tool(annotations={"readOnlyHint": True})
        async def scrape() -> str:
            calls.append(1)
            if len(calls) == 1:
                raise_tool_error(
                    AuthMissingOnOwnerError("no session", nothing_ran_yet=True),
                    "scrape",
                )
            await asyncio.sleep(30)  # a replay that outlives the call
            return "scraped ok"

        owner.add_middleware(OwnerAuthSignalMiddleware())
        with (
            self._profile_is_free(),
            self._a_login_that(takes=0.05),
            patch("linkedin_mcp_server.daemon_auth._MINIMUM_REPLAY_SECONDS", 0.2),
        ):
            async with Client(_proxy_to(owner, tool_timeout=1.0)) as client:
                result = await client.call_tool("scrape", raise_on_error=False)

        assert len(calls) == 2, "the replay never ran"
        # The owner's own words, not a timeout: the client is told what happened
        # and that retrying now works.
        assert result.is_error is True
        assert "no session" in result.content[0].text

    async def test_a_tool_that_changes_something_is_never_replayed(self):
        """Repaired, and left for the caller to run again.

        No bound on the replay can make this safe, and two attempts showed why.
        Bounding it locally left the owner finishing the mutation after the
        frontend had reported failure, because cancellation is not forwarded
        across the hop: measured over a real loopback owner, the client was
        answered at 0.66s with an error and the effect landed 0.7s later.
        Removing the bound only moved the deadline: an MCP client cancelling its
        own call produced the same orphan, and the retry sent the message twice.

        So the decision goes to the user, who knows whether it happened. Asking
        costs one retry; guessing wrong sends it twice.
        """
        owner = FastMCP("owner", mask_error_details=True)
        calls: list[int] = []
        sent: list[str] = []

        @owner.tool(annotations={"destructiveHint": True})
        async def send() -> str:
            calls.append(1)
            if len(calls) == 1:
                raise_tool_error(
                    AuthMissingOnOwnerError("no session", nothing_ran_yet=True),
                    "send",
                )
            sent.append("message")
            return "sent"

        owner.add_middleware(OwnerAuthSignalMiddleware())
        with self._profile_is_free(), self._a_login_that(takes=0.05):
            async with Client(_proxy_to(owner, tool_timeout=5.0)) as client:
                result = await client.call_tool("send", raise_on_error=False)
            # Repaired all the same, which is what makes the retry the message
            # asks for work. Read inside the patch, where readiness reflects the
            # stubbed login rather than the real filesystem.
            from linkedin_mcp_server.bootstrap import _auth_ready

            repaired = _auth_ready()

        assert calls == [1], "a mutating call was run again on its own"
        assert sent == [], "the message was sent without the user asking twice"
        assert repaired, "the auth was not repaired either"
        assert result.is_error is True
        assert "no session" in result.content[0].text

    async def test_an_unannotated_tool_counts_as_mutating(self):
        """A tool that promised nothing is not treated as safe.

        Only ``readOnlyHint`` earns a replay. Defaulting the other way would make
        every future tool replayable until someone remembered to annotate it, and
        the failure would be silent and irreversible.
        """
        owner = FastMCP("owner", mask_error_details=True)
        calls: list[int] = []

        @owner.tool  # no annotations at all
        async def act() -> str:
            calls.append(1)
            if len(calls) == 1:
                raise_tool_error(
                    AuthMissingOnOwnerError("no session", nothing_ran_yet=True),
                    "act",
                )
            return "done"

        owner.add_middleware(OwnerAuthSignalMiddleware())
        with self._profile_is_free(), self._a_login_that(takes=0.05):
            async with Client(_proxy_to(owner, tool_timeout=5.0)) as client:
                result = await client.call_tool("act", raise_on_error=False)

        assert calls == [1], "an unannotated tool was replayed"
        assert result.is_error is True

    async def test_the_replay_floor_stays_inside_the_budget(self):
        """The floor must not outlive the call it is a floor for.

        Measured at ``tool_timeout_seconds=1.0`` with 0.9s spent: a flat floor
        allowed a 5-second replay inside a 1-second call, which is the overrun
        the budget arithmetic exists to prevent.
        """
        from linkedin_mcp_server import daemon_auth as da

        assert da._what_is_left_of_this_call(1.0, 0.9) <= 1.0
        assert da._what_is_left_of_this_call(60.0, 59.9) == da._MINIMUM_REPLAY_SECONDS
        # The ordinary case is untouched: plenty left, so the floor never binds.
        assert da._what_is_left_of_this_call(180.0, 30.0) == 150.0


class TestWhichRolesGetWhichHalf:
    """Each half belongs to exactly one role, and the wrong pairing is silent."""

    def _built(self, role):
        """A server of *role*, built as a freshly spawned process would.

        Reuses the helper in ``test_server.py`` rather than growing a second one:
        it already knows the arguments each role cannot be built without, and the
        role is process state, so each build has to start from an unset one.
        """
        from test_server import _server_for  # tests/ is on the path, not a package

        return _server_for(role)

    def test_only_the_owner_signals(self):
        from linkedin_mcp_server.server_role import ServerRole

        for role in ServerRole:
            has_it = any(
                isinstance(m, OwnerAuthSignalMiddleware)
                for m in self._built(role).middleware
            )
            assert has_it == (role is ServerRole.OWNER), role

    def test_only_the_proxy_repairs(self):
        from linkedin_mcp_server.server_role import ServerRole

        # A DIRECT server must not get it either: it already repairs its own auth
        # inline, and a second path would run a login on top of that one.
        for role in ServerRole:
            has_it = any(
                isinstance(m, FrontendAuthRepairMiddleware)
                for m in self._built(role).middleware
            )
            assert has_it == (role is ServerRole.PROXY), role

    def test_the_owners_signal_sits_outside_the_serializing_middleware(self):
        # Not a safety requirement, and an earlier version of this claimed it was:
        # close_browser does not consult the in-flight count, so quiescence works
        # from the inner position too. It is asserted because the marker reports
        # whether the profile is still held, and from inside that answer describes
        # the layer below rather than the process.
        from linkedin_mcp_server.sequential_tool_middleware import (
            SequentialToolExecutionMiddleware,
        )
        from linkedin_mcp_server.server_role import ServerRole

        kinds = [type(m) for m in self._built(ServerRole.OWNER).middleware]

        assert kinds.index(OwnerAuthSignalMiddleware) < kinds.index(
            SequentialToolExecutionMiddleware
        )
