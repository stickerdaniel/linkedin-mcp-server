import asyncio
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, call

import mcp.types as mt
import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.server.providers.proxy import ProxyClient

import linkedin_mcp_server.server as server_module
from linkedin_mcp_server import __version__
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.server import ServerRole, create_mcp_server
from linkedin_mcp_server.server_role import (
    RoleAlreadyClaimedError,
    process_role,
)
from linkedin_mcp_server.update_check import UpdateNoticeMiddleware


def _has_middleware(mcp: FastMCP, kind: type) -> bool:
    return any(isinstance(middleware, kind) for middleware in mcp.middleware)


def _owner_serving(*tool_names: str) -> FastMCP:
    """A stand-in owner that serves the named tools and nothing else."""
    owner = FastMCP("owner")
    for name in tool_names:

        async def tool() -> dict[str, str]:
            return {"served": "by the owner"}

        tool.__name__ = name
        owner.tool(tool)
    return owner


def _an_attachment() -> MagicMock:
    """Stands in for an elected owner.

    The real one carries a loopback address and a bearer token and needs a
    published descriptor to build. These tests are about which parts of a server
    a role gets, so the endpoint is stood in for; the real URL, token, timeout
    and proxy-environment wiring are covered in ``tests/test_daemon_proxy.py``.
    """
    attachment = MagicMock(name="attachment")
    attachment.descriptor.url = "http://127.0.0.1:1/mcp"
    attachment.token = "a-token"
    return attachment


def _proxy_to(monkeypatch: pytest.MonkeyPatch, owner: FastMCP, **kwargs) -> FastMCP:
    """Build a PROXY whose provider reaches *owner* in memory rather than over HTTP."""
    import linkedin_mcp_server.daemon_proxy as daemon_proxy

    # ProxyClient, matching production: a plain Client would drop the progress
    # every browser-backed tool reports.
    monkeypatch.setattr(
        daemon_proxy,
        "_client_factory",
        lambda _a, *, timeout: lambda: ProxyClient(owner),
    )
    return create_mcp_server(
        role=ServerRole.PROXY, proxy_attachment=_an_attachment(), **kwargs
    )


def _extras_for(role: ServerRole) -> dict[str, MagicMock]:
    """The arguments a role cannot be built without."""
    if role is ServerRole.PROXY:
        return {"proxy_attachment": _an_attachment()}
    return {}


def _server_for(role: ServerRole) -> FastMCP:
    """Build a server of *role*, as a fresh process would.

    The role is process state as well as an argument, and ``create_mcp_server``
    refuses a second, different one: in production that is a process trying to be
    both a proxy and an owner, which cannot be right about whether it may open a
    login window. A test comparing every role in one interpreter is the one
    legitimate exception, so it starts each build from an unset role the way a
    newly spawned process does.
    """
    from linkedin_mcp_server.server_role import reset_process_role_for_testing

    reset_process_role_for_testing()
    return create_mcp_server(role=role, **_extras_for(role))


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
            mcp = _server_for(role)
            serializes = _has_middleware(mcp, SequentialToolExecutionMiddleware)

            # Stated as the conditional it is. Asserting that every role drives
            # a browser would pass today and fail the moment the forwarding
            # role arrives, for the entirely legitimate reason that one does
            # not, which is a test failing at the wrong thing.
            assert serializes == role.drives_browser, role

    def test_the_update_notice_goes_only_where_a_user_reads_it(self):
        # Appended to one tool result per process, so it belongs wherever a user
        # reads results and nowhere else. On the shared owner it would reach
        # whichever client happened to call first and nobody after that, however
        # many clients attach over its life. A proxy is the opposite case: it is
        # the process the client spawned, so the notice has to survive there.
        for role in ServerRole:
            mcp = _server_for(role)

            assert (
                _has_middleware(mcp, UpdateNoticeMiddleware) == role.faces_a_client
            ), role

    def test_every_role_that_drives_a_browser_serves_the_same_tools(self):
        # A tool present in one role and missing from another would disappear
        # from a client's list depending on how its server happened to start.
        #
        # A proxy is deliberately not in here: it registers none of these and
        # serves the owner's instead, which is a different claim and has its own
        # test below.
        async def tool_names(role: ServerRole) -> set[str]:
            tools = await _server_for(role).list_tools()
            return {tool.name for tool in tools}

        served = {
            role: asyncio.run(tool_names(role))
            for role in ServerRole
            if role.drives_browser
        }

        assert len(served) > 1
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


class TestTheRoleAsProcessState:
    """The role has to be readable where no server can be passed in.

    ``create_mcp_server`` is handed a role, and that settles everything it
    assembles. The auth gates are elsewhere: whether a login window may open is
    decided in ``bootstrap``, reached from a tool body with nothing to ask. A
    detached owner has no terminal, so it has to be able to find out what it is.
    """

    def test_a_fresh_process_is_direct(self):
        # The default carries every embedder and every test that never mentions a
        # role, so it has to be the historical behaviour.
        #
        # Asserted in a subprocess rather than here. This suite resets the role per
        # test, so an in-process assertion passes even against a module default
        # mutated to OWNER: it would be testing the fixture, not the module.
        probe = (
            "from linkedin_mcp_server.server_role import process_role, ServerRole;"
            "print(process_role() is ServerRole.DIRECT)"
        )
        answered = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        assert answered == "True"

    def test_a_direct_server_followed_by_an_owner_is_refused(self):
        # The gap a separate unclaimed state exists to close. With DIRECT doubling
        # as "nothing said yet", this sequence was accepted, and the DIRECT
        # server's own later tool calls would read OWNER and refuse the logins it
        # is supposed to perform.
        create_mcp_server()

        with pytest.raises(RoleAlreadyClaimedError, match="already serves as direct"):
            create_mcp_server(role=ServerRole.OWNER, auth_token="a-token")

    def test_building_a_server_records_what_this_process_is(self):
        # Recorded by the assembly rather than only by the entry points, because
        # anything may call it directly. A caller that built an OWNER while the
        # process still said DIRECT would get an owner with every auth gate off,
        # which is a detached process opening a window nobody can see.
        create_mcp_server(role=ServerRole.OWNER, auth_token="a-token")

        assert process_role() is ServerRole.OWNER

    def test_a_second_role_in_one_process_is_refused(self):
        create_mcp_server(role=ServerRole.OWNER, auth_token="a-token")

        # Not a resolvable disagreement: the two would have to disagree about
        # whether this process may open a login window.
        with pytest.raises(RoleAlreadyClaimedError, match="already serves as owner"):
            create_mcp_server(role=ServerRole.PROXY, proxy_attachment=_an_attachment())

    def test_restating_the_same_role_is_allowed(self):
        # A process that builds two servers of one kind is doing nothing
        # contradictory, and refusing it would break the owner, which records the
        # role at its entry point and again when it assembles.
        create_mcp_server(role=ServerRole.OWNER, auth_token="a-token")
        create_mcp_server(role=ServerRole.OWNER, auth_token="another-token")

        assert process_role() is ServerRole.OWNER

    def test_the_owner_entry_point_says_so_before_it_can_fail(self, monkeypatch):
        """`main` claims OWNER before anything downstream could need to know.

        `create_owner_server` claims it too, but that runs several steps into
        `_serve`. A failure between the two would be handled by a process that
        still believed it had a terminal.

        Driven rather than read: an earlier version of this asserted on the source
        text of `main`, which stayed green when the call was made unreachable.
        `configure_logging` is the first thing after the claim, so it serves as a
        checkpoint to observe the role at and abort from.
        """
        from linkedin_mcp_server import daemon_owner
        from linkedin_mcp_server.config.schema import AppConfig

        class Checkpoint(Exception):
            pass

        seen: list[ServerRole] = []

        def at_checkpoint(**_kwargs):
            seen.append(process_role())
            raise Checkpoint

        monkeypatch.setattr(daemon_owner, "_read_config", lambda: AppConfig())
        monkeypatch.setattr(daemon_owner, "set_headless", lambda _headless: None)
        monkeypatch.setattr(daemon_owner, "configure_logging", at_checkpoint)
        monkeypatch.setattr(
            daemon_owner, "_claim_handshake_stream", lambda: MagicMock()
        )

        with pytest.raises(Checkpoint):
            daemon_owner.main([])

        assert seen == [ServerRole.OWNER]

    def test_the_state_is_readable_without_importing_the_server(self):
        # Same reason the enum lives here: `bootstrap` reads this, and `server`
        # reaches `bootstrap` through the tool modules, so importing back would
        # close the cycle.
        probe = (
            "from linkedin_mcp_server.server_role import process_role, ServerRole;"
            "import sys;"
            "print(process_role() is ServerRole.DIRECT"
            " and 'linkedin_mcp_server.server' not in sys.modules)"
        )
        answered = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        assert answered == "True"


class TestProxyRole:
    """A proxy serves the owner's tools and drives nothing itself.

    The failures pinned here are all quiet ones: a browser installed in a process
    that never opens one, a local tool shadowing the forwarded tool of the same
    name, or a server that lost its tools and cannot say why.
    """

    async def test_a_proxy_serves_the_owners_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        owner = _owner_serving("get_person_profile", "search_jobs")
        proxy = _proxy_to(monkeypatch, owner)

        async with Client(proxy) as client:
            served = {tool.name for tool in await client.list_tools()}

        assert served == {"get_person_profile", "search_jobs"}

    async def test_a_proxy_registers_none_of_its_own_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Only what the owner offers, exactly. An owner serving one tool proves
        # nothing local slipped through, which a comparison against the full
        # local list could not: that list would match by coincidence.
        owner = _owner_serving("the_only_tool")
        proxy = _proxy_to(monkeypatch, owner)

        async with Client(proxy) as client:
            served = {tool.name for tool in await client.list_tools()}

        assert served == {"the_only_tool"}

    async def test_close_session_is_not_kept_locally_by_a_proxy(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # The registration that is easiest to miss, because it is the one tool
        # defined inline rather than in a `register_*` call. Left behind it would
        # shadow the forwarded tool of the same name and close a browser this
        # process does not have.
        proxy = _proxy_to(monkeypatch, _owner_serving("get_person_profile"))

        async with Client(proxy) as client:
            served = {tool.name for tool in await client.list_tools()}

        assert "close_session" not in served

    async def test_a_proxy_never_runs_the_browser_lifespan(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # A proxy opens no browser, so entering this lifespan would install
        # Chromium in a process that never uses it, poll to hand over a profile
        # it does not hold, and then claim to close a browser it never had.
        #
        # Asserted through the collaborators rather than `proxy.lifespan is None`:
        # FastMCP substitutes a default lifespan of its own when given none, so
        # the identity check passed for a server that still had ours.
        watcher = AsyncMock()
        bootstrap = MagicMock()
        browser_setup = AsyncMock()
        close = AsyncMock()
        monkeypatch.setattr(server_module, "initialize_bootstrap", bootstrap)
        monkeypatch.setattr(server_module, "get_runtime_policy", MagicMock())
        monkeypatch.setattr(
            server_module, "start_background_browser_setup_if_needed", browser_setup
        )
        monkeypatch.setattr(server_module, "watch_for_handoff_requests", watcher)
        monkeypatch.setattr(server_module, "close_browser", close)

        proxy = _proxy_to(monkeypatch, _owner_serving("get_person_profile"))
        async with Client(proxy) as client:
            await client.list_tools()

        bootstrap.assert_not_called()
        browser_setup.assert_not_awaited()
        watcher.assert_not_called()
        close.assert_not_awaited()

    async def test_a_browser_driving_role_does_run_it(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # The other half of the assertion above: without this, deleting the
        # lifespan outright would satisfy that test.
        bootstrap = MagicMock()
        close = AsyncMock()
        monkeypatch.setattr(server_module, "initialize_bootstrap", bootstrap)
        monkeypatch.setattr(server_module, "get_runtime_policy", MagicMock())
        monkeypatch.setattr(
            server_module, "start_background_browser_setup_if_needed", AsyncMock()
        )
        monkeypatch.setattr(server_module, "watch_for_handoff_requests", AsyncMock())
        monkeypatch.setattr(server_module, "close_browser", close)

        async with Client(create_mcp_server()) as client:
            await client.list_tools()

        bootstrap.assert_called_once()
        close.assert_awaited_once()

    async def test_a_dead_owner_is_an_error_rather_than_an_empty_tool_list(self):
        # FastMCP logs a failing provider and carries on by default. For a proxy
        # the provider *is* the server, so that default turns an unreachable
        # owner into a client that sees no tools and no reason why. Measured on
        # 3.4.4: `tools/list` returned `[]`.
        proxy = create_mcp_server(
            role=ServerRole.PROXY, proxy_attachment=_an_attachment()
        )

        async with Client(proxy) as client:
            with pytest.raises(Exception, match="connect"):
                await client.list_tools()

    async def test_the_update_notice_still_reaches_a_forwarded_result(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # The notice is appended by middleware around a call whose work happens in
        # another process, so it has to survive the hop rather than be dropped
        # with the local tool it used to ride on.
        import linkedin_mcp_server.update_check as update_check

        monkeypatch.setattr(update_check, "prime_from_cache", lambda: None)
        monkeypatch.setattr(update_check, "refresh_latest_version", AsyncMock())
        monkeypatch.setattr(
            update_check, "pending_update_notice", lambda: "Update available: 9.9.9"
        )

        proxy = _proxy_to(monkeypatch, _owner_serving("get_person_profile"))

        async with Client(proxy) as client:
            result = await client.call_tool("get_person_profile", {})

        texts = [getattr(block, "text", "") for block in result.content]
        assert any("Update available: 9.9.9" in text for text in texts)
        # And the owner's own result is still there, not replaced by the notice.
        assert any("by the owner" in text for text in texts)

    def test_a_proxy_without_an_owner_is_refused(self):
        # It would serve an empty tool list and look like a server whose tools
        # disappeared, which is the confusing half of the same bug.
        with pytest.raises(ValueError, match="needs an owner"):
            create_mcp_server(role=ServerRole.PROXY)

    def test_only_a_proxy_may_be_given_an_owner(self):
        # A role that does not forward would be handed a bearer token for a
        # shared browser and quietly ignore it.
        for role in (ServerRole.DIRECT, ServerRole.OWNER):
            with pytest.raises(ValueError, match="Only a proxy forwards"):
                create_mcp_server(role=role, proxy_attachment=_an_attachment())

    def test_the_owners_inbound_token_is_not_the_proxys_outbound_one(self):
        # Two credentials pointing opposite ways. Conflating them would either
        # authenticate a server that has no port, or send an owner's own token
        # back to itself.
        with pytest.raises(ValueError, match="daemon owner"):
            create_mcp_server(role=ServerRole.PROXY, auth_token="a-token")


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
        #
        # Two roles in one test, so each starts from an unclaimed process the way
        # a freshly spawned one does.
        assert _server_for(ServerRole.DIRECT).auth is None
        assert _server_for(ServerRole.OWNER).auth is None

    def test_a_token_on_a_role_that_cannot_use_one_is_refused(self):
        # Passing a token to a stdio server is a caller that believes it built
        # the owner. Ignoring it quietly would leave an endpoint that was meant
        # to be authenticated serving anyone who finds it.
        #
        # A proxy is the case worth spelling out, because it is the one role that
        # legitimately handles a token — the owner's, outbound. Accepting one
        # here would be a caller confusing the credential it *presents* with the
        # one an owner *demands*, and narrowing this guard to DIRECT alone would
        # let that through.
        for role in (ServerRole.DIRECT, ServerRole.PROXY):
            with pytest.raises(ValueError, match="daemon owner"):
                create_mcp_server(role=role, auth_token="a-token")

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


class TestWhatTheToolsPromise:
    """``readOnlyHint`` is a claim the daemon acts on, not documentation.

    A tool that declares it is replayed automatically after the frontend repairs
    auth, with no second confirmation from anyone. So the annotation has to mean
    what it says: anything that can change LinkedIn's state, however slightly,
    must not carry it.
    """

    async def test_no_tool_claims_read_only_while_changing_state(self):
        """The two messaging tools that read by clicking are the reason for this.

        Resolving a conversation by username enumerates the inbox by visiting
        rows, and LinkedIn marks a visited row as read. Both tools' own
        docstrings say so, and both carried ``readOnlyHint`` anyway. An unread
        message the user has not seen is state, and a replay that silently clears
        it is not something a reader should do.
        """
        mcp = create_mcp_server()
        marks_things_read = {"get_conversation", "search_conversations"}
        claiming = set()
        for name in marks_things_read:
            tool = await mcp.get_tool(name)
            # A missing tool means this test is guarding a name that no longer
            # exists, which is worse than a wrong annotation: it would pass
            # forever while covering nothing.
            assert tool is not None, f"{name} is not registered any more"
            if tool.annotations and tool.annotations.readOnlyHint:
                claiming.add(name)

        assert not (marks_things_read & claiming), (
            "these tools mark conversations as read, so they cannot be replayed "
            f"unattended: {sorted(marks_things_read & claiming)}"
        )
