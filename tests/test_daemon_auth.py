"""Carrying an auth failure from the owner to the process that can fix it.

The owner drives the browser and notices bad LinkedIn auth; it has no terminal, so
it cannot repair it. The frontend can. These tests cover the crossing between them
and the decisions made at each end, because every one of them is a way to either
lose the signal or act on it wrongly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import mcp.types as mt
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from fastmcp.server.providers.proxy import ProxyClient, ProxyProvider
from fastmcp.tools import ToolResult

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

    @owner.tool
    async def scrape() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise_tool_error(error, "scrape")
        return then

    owner.add_middleware(OwnerAuthSignalMiddleware())
    return owner, calls


def _proxy_to(owner: FastMCP, *, repairing: bool = True) -> FastMCP:
    """A frontend forwarding to *owner* in memory, wired as production does."""
    front = FastMCP("front", mask_error_details=True)
    front.add_provider(ProxyProvider(lambda: ProxyClient(owner)))
    if repairing:
        front.add_middleware(FrontendAuthRepairMiddleware())
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

        repair.assert_awaited_once_with("missing")
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
        repair.assert_awaited_once_with("stale")
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

        @owner.tool
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
