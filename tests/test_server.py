import asyncio
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, call

import mcp.types as mt
import pytest
from fastmcp import FastMCP
from fastmcp.server.middleware import MiddlewareContext

import linkedin_mcp_server.server as server_module
from linkedin_mcp_server import __version__
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.server import ServerRole, create_mcp_server
from linkedin_mcp_server.update_check import UpdateNoticeMiddleware


def _has_middleware(mcp: FastMCP, kind: type) -> bool:
    return any(isinstance(middleware, kind) for middleware in mcp.middleware)


class TestServerRoles:
    """Which middleware each role gets, and why the split has to exist.

    These are not restatements of the implementation: each one pins a failure
    that is invisible in a single-process test run and only shows up once two
    processes share a profile.
    """

    def test_the_default_role_is_the_historical_server(self):
        # Every existing caller passes only tool_timeout, so the default has to
        # keep giving them exactly what they had before the split.
        default = create_mcp_server()
        direct = create_mcp_server(role=ServerRole.DIRECT)

        for mcp in (default, direct):
            assert _has_middleware(mcp, SequentialToolExecutionMiddleware)
            assert _has_middleware(mcp, UpdateNoticeMiddleware)

    def test_every_role_that_drives_a_browser_serializes_its_calls(self):
        # The invariant the shared profile depends on: any server that can
        # reach Chromium takes the lease first. A role added later that skips
        # this would corrupt the session rather than merely run slowly.
        for role in ServerRole:
            mcp = create_mcp_server(role=role)
            serializes = _has_middleware(mcp, SequentialToolExecutionMiddleware)

            # Stated as the conditional it is. Asserting that every role drives
            # a browser would pass today and fail the moment the forwarding
            # role arrives, for the entirely legitimate reason that one does
            # not, which is a test failing at the wrong thing.
            assert serializes == role.drives_browser, role

    def test_the_update_notice_goes_only_where_a_user_reads_it(self):
        # Appended to one tool result per process. On a server shared by many
        # clients that means the first caller sees it and nobody afterwards
        # does, however long that server lives.
        owner = create_mcp_server(role=ServerRole.OWNER)

        assert not _has_middleware(owner, UpdateNoticeMiddleware)

    def test_every_role_serves_the_same_tools(self):
        # A tool present in one role and missing from another would disappear
        # from a client's list depending on how its server happened to start.
        async def tool_names(role: ServerRole) -> set[str]:
            tools = await create_mcp_server(role=role).list_tools()
            return {tool.name for tool in tools}

        served = {role: asyncio.run(tool_names(role)) for role in ServerRole}

        assert len(set(map(frozenset, served.values()))) == 1
        assert "get_person_profile" in served[ServerRole.DIRECT]

    def test_the_role_is_readable_without_importing_the_server(self):
        # The reason the enum lives in its own module. Behaviour has to differ
        # by role well below the server — an owner must not open a login
        # window, and that decision is made in `dependencies`, which `server`
        # reaches only through the tool modules. Importing back the other way
        # would close the cycle, so the enum has to be reachable on its own.
        probe = (
            "import linkedin_mcp_server.server_role, sys;"
            "print(any(name.startswith('linkedin_mcp_server.tools') for name in sys.modules)"
            " or 'linkedin_mcp_server.server' in sys.modules)"
        )
        pulled_in_the_server_graph = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        assert pulled_in_the_server_graph == "False"

    def test_the_enum_stays_importable_from_the_server(self):
        # Moving it must not break an embedder that already imports it from
        # where it used to live.
        assert server_module.ServerRole is ServerRole


class TestOwnerAuthentication:
    """Only the shared owner authenticates, and it does so with one token.

    The owner listens on a loopback port, which every process on the machine can
    reach and which a browser reaches on behalf of any page the user visits. The
    token is what stands between that and a logged-in LinkedIn session.
    """

    def test_an_owner_with_a_token_requires_one(self):
        owner = create_mcp_server(role=ServerRole.OWNER, auth_token="a-token")

        assert owner.auth is not None

    def test_a_server_without_a_token_stays_unauthenticated(self):
        # The stdio server has no port to protect, and adding an auth provider
        # there would advertise metadata for a flow nothing performs.
        assert create_mcp_server().auth is None
        assert create_mcp_server(role=ServerRole.OWNER).auth is None

    def test_a_token_on_a_role_that_cannot_use_one_is_refused(self):
        # Passing a token to a stdio server is a caller that believes it built
        # the owner. Ignoring it quietly would leave an endpoint that was meant
        # to be authenticated serving anyone who finds it.
        with pytest.raises(ValueError, match="daemon owner"):
            create_mcp_server(role=ServerRole.DIRECT, auth_token="a-token")

    async def test_the_wrong_token_is_refused_and_the_right_one_accepted(self):
        owner = create_mcp_server(role=ServerRole.OWNER, auth_token="the-token")
        verifier = owner.auth
        assert verifier is not None

        assert await verifier.verify_token("the-token") is not None
        assert await verifier.verify_token("the-tokeN") is None
        assert await verifier.verify_token("") is None
        # A prefix must not pass. A comparison that stopped at the shorter
        # length would accept every prefix of the real token, which turns
        # guessing into a character-at-a-time search.
        assert await verifier.verify_token("the-toke") is None

    def test_the_token_does_not_survive_in_the_verifier(self):
        # The owner's own repr and any process dump would otherwise carry the
        # credential. Only its digest is kept.
        token = "a-secret-token"
        owner = create_mcp_server(role=ServerRole.OWNER, auth_token=token)

        assert token not in repr(owner.auth)
        assert token not in repr(vars(owner.auth))


class TestSequentialToolExecutionMiddleware:
    async def test_create_mcp_server_registers_sequential_tool_middleware(self):
        mcp = create_mcp_server()

        assert any(
            isinstance(middleware, SequentialToolExecutionMiddleware)
            for middleware in mcp.middleware
        )

    async def test_sequential_tool_middleware_serializes_parallel_tool_calls(self):
        mcp = FastMCP("test")
        mcp.add_middleware(SequentialToolExecutionMiddleware())

        active_calls = 0
        max_active_calls = 0

        @mcp.tool
        async def slow_tool(delay: float = 0.05) -> dict[str, float]:
            nonlocal active_calls, max_active_calls
            active_calls += 1
            max_active_calls = max(max_active_calls, active_calls)
            try:
                await asyncio.sleep(delay)
                return {"delay": delay}
            finally:
                active_calls -= 1

        result_one, result_two = await asyncio.gather(
            mcp.call_tool("slow_tool", {"delay": 0.05}),
            mcp.call_tool("slow_tool", {"delay": 0.05}),
        )

        assert max_active_calls == 1
        assert result_one.structured_content == {"delay": 0.05}
        assert result_two.structured_content == {"delay": 0.05}

    async def test_sequential_tool_middleware_preserves_tool_results(self):
        mcp = FastMCP("test")
        mcp.add_middleware(SequentialToolExecutionMiddleware())

        @mcp.tool
        async def simple_tool(value: int) -> dict[str, int]:
            return {"value": value}

        result = await mcp.call_tool("simple_tool", {"value": 7})

        assert result.structured_content == {"value": 7}

    async def test_sequential_tool_middleware_reports_queue_progress(self):
        middleware = SequentialToolExecutionMiddleware()
        fastmcp_context = MagicMock()
        fastmcp_context.request_context = object()
        fastmcp_context.report_progress = AsyncMock()
        call_next = AsyncMock(return_value=MagicMock())
        context = MiddlewareContext(
            message=mt.CallToolRequestParams(name="slow_tool", arguments={}),
            method="tools/call",
            fastmcp_context=fastmcp_context,
        )

        await middleware.on_call_tool(context, call_next)

        fastmcp_context.report_progress.assert_has_awaits(
            [
                call(
                    progress=0,
                    total=100,
                    message="Queued waiting for scraper lock",
                ),
                call(
                    progress=0,
                    total=100,
                    message="Scraper lock acquired, starting tool",
                ),
            ]
        )


class TestBrowserLifespan:
    """The lifespan owns the background handoff poller.

    Nothing else starts it, and nothing else stops it. Without the poller an
    owner that goes idle never notices a waiting process; without the cancel a
    stdio server would not exit.
    """

    @staticmethod
    def _patched(monkeypatch, watcher):
        """Replace the lifespan's collaborators, returning the close spy."""
        import linkedin_mcp_server.server as server_module

        monkeypatch.setattr(server_module, "initialize_bootstrap", MagicMock())
        monkeypatch.setattr(server_module, "get_runtime_policy", MagicMock())
        monkeypatch.setattr(
            server_module, "start_background_browser_setup_if_needed", AsyncMock()
        )
        monkeypatch.setattr(server_module, "watch_for_handoff_requests", watcher)
        close_browser = AsyncMock()
        monkeypatch.setattr(server_module, "close_browser", close_browser)
        return close_browser

    async def test_poller_runs_for_the_life_of_the_server(self, monkeypatch):
        from linkedin_mcp_server.server import browser_lifespan

        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def watcher():
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        close_browser = self._patched(monkeypatch, watcher)

        async with browser_lifespan(MagicMock()):
            await asyncio.wait_for(started.wait(), timeout=1)
            assert not cancelled.is_set()
            close_browser.assert_not_awaited()

        assert cancelled.is_set()
        close_browser.assert_awaited_once()

    async def test_a_dead_poller_still_lets_the_browser_close(self, monkeypatch):
        """A poller that raised must not take the browser down with it.

        Awaiting a task that already failed re-raises its exception. If that
        propagated out of the teardown, ``close_browser`` would be skipped and
        Chromium would stay on the shared profile past shutdown, which is the
        corruption the lease exists to prevent.
        """
        from linkedin_mcp_server.server import browser_lifespan

        async def watcher():
            raise RuntimeError("poller crashed")

        close_browser = self._patched(monkeypatch, watcher)

        async with browser_lifespan(MagicMock()):
            # Let the doomed task reach its exception before teardown.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        close_browser.assert_awaited_once()

    async def test_a_fatal_poller_error_still_closes_but_propagates(self, monkeypatch):
        """A BaseException is not ours to swallow, but the browser still closes.

        ``Exception`` is caught deliberately; ``KeyboardInterrupt`` and friends
        must keep travelling. What must not happen is that they take the profile
        with them by skipping the close.
        """
        from linkedin_mcp_server.server import browser_lifespan

        class Fatal(BaseException):
            """Stands in for KeyboardInterrupt, which would abort the run."""

        async def watcher():
            raise Fatal("fatal")

        close_browser = self._patched(monkeypatch, watcher)

        with pytest.raises(Fatal):
            async with browser_lifespan(MagicMock()):
                await asyncio.sleep(0)
                await asyncio.sleep(0)

        close_browser.assert_awaited_once()

    async def test_cancelling_the_lifespan_itself_is_not_swallowed(self, monkeypatch):
        """Shutdown must not report success after being told to stop.

        Cancelling the watcher and being cancelled ourselves surface as the
        same exception in the same handler, because a watcher takes a moment to
        wind down and a cancellation aimed at the teardown lands during exactly
        that window. Absorbing it would leave whoever asked us to stop waiting
        on a teardown that claimed to have finished normally.
        """
        from linkedin_mcp_server.server import browser_lifespan

        async def watcher():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # Stands in for a poller mid-probe: cancellation is requested,
                # but the task does not finish until it has unwound.
                await asyncio.sleep(0.5)
                raise

        close_browser = self._patched(monkeypatch, watcher)

        async def run_server() -> None:
            # Entered and exited by hand so the cancellation lands *during*
            # teardown. Cancelling an `async with` body is a different path: it
            # interrupts the yield and never reaches this handler.
            lifespan = browser_lifespan(MagicMock())
            await lifespan.__aenter__()
            await asyncio.sleep(0.05)  # let the watcher reach its sleep

            task = asyncio.current_task()
            assert task is not None

            async def cancel_during_teardown() -> None:
                await asyncio.sleep(0.1)
                task.cancel()

            asyncio.create_task(cancel_during_teardown())
            await lifespan.__aexit__(None, None, None)

        server = asyncio.create_task(run_server())

        with pytest.raises(asyncio.CancelledError):
            await server

        # Still closed: a cancelled shutdown is no reason to leave a browser
        # running on the shared profile.
        close_browser.assert_awaited_once()

    async def test_teardown_closes_the_browser_when_the_body_raises(self, monkeypatch):
        from linkedin_mcp_server.server import browser_lifespan

        async def watcher():
            await asyncio.sleep(3600)

        close_browser = self._patched(monkeypatch, watcher)

        with pytest.raises(RuntimeError, match="boom"):
            async with browser_lifespan(MagicMock()):
                raise RuntimeError("boom")

        close_browser.assert_awaited_once()


class TestServerVersion:
    def test_create_mcp_server_advertises_package_version(self):
        # Without an explicit version=, FastMCP advertises its own library
        # version in serverInfo instead of ours.
        mcp = create_mcp_server()

        assert mcp.version == __version__
