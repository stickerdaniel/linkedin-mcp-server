"""Tests for dependencies.py — bootstrap gating and auto-relogin."""

from unittest.mock import AsyncMock, MagicMock, patch

import asyncio

import pytest
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    NetworkError,
    RateLimitError,
)
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.exceptions import (
    AuthenticationStartedError,
    AuthStaleOnOwnerError,
    DockerHostLoginRequiredError,
)


class TestHandleAuthError:
    async def test_managed_triggers_relogin(self):
        """On managed runtime, close browser + trigger relogin."""
        with (
            patch(
                "linkedin_mcp_server.dependencies.get_runtime_policy",
                return_value="managed",
            ),
            patch(
                "linkedin_mcp_server.dependencies.close_browser",
                new_callable=AsyncMock,
            ) as mock_close,
            patch(
                "linkedin_mcp_server.dependencies.current_login_generation",
                return_value="the-dead-one",
            ),
            patch(
                "linkedin_mcp_server.dependencies.invalidate_auth_and_trigger_relogin",
                new_callable=AsyncMock,
                side_effect=AuthenticationStartedError("login opened"),
            ) as mock_relogin,
        ):
            with pytest.raises(AuthenticationStartedError):
                await handle_auth_error(
                    AuthenticationError("Session expired"), ctx=None
                )

            mock_close.assert_awaited_once()
            # The generation it observed travels with it, so the rotation
            # downstream can tell the dead session from a peer's repair.
            mock_relogin.assert_awaited_once()
            assert mock_relogin.await_args.args == (None,)
            # The value, not merely the keyword. Asserting only that the argument
            # exists left a mutation passing a hardcoded None green, which is the
            # regression this whole path was fixed for.
            assert mock_relogin.await_args.kwargs["stale_generation"] == "the-dead-one"

    async def test_docker_raises_host_error(self):
        """On Docker runtime, raise DockerHostLoginRequiredError."""
        with patch(
            "linkedin_mcp_server.dependencies.get_runtime_policy",
            return_value="docker",
        ):
            with pytest.raises(DockerHostLoginRequiredError, match="host machine"):
                await handle_auth_error(
                    AuthenticationError("Session expired"), ctx=None
                )


class TestGetReadyExtractor:
    async def test_ready_resumes_to_scrape_path(self):
        """When gating returns (login resolved in-budget), control falls through
        to get_or_create_browser + ensure_authenticated and returns an extractor.
        """
        browser = MagicMock()
        browser.page = MagicMock()
        with (
            patch(
                "linkedin_mcp_server.dependencies.ensure_tool_ready_or_raise",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
                return_value=browser,
            ) as mock_get_browser,
            patch(
                "linkedin_mcp_server.dependencies.ensure_authenticated",
                new_callable=AsyncMock,
            ) as mock_ensure_auth,
        ):
            from linkedin_mcp_server.scraping import LinkedInExtractor

            extractor = await get_ready_extractor(ctx=None, tool_name="test_tool")

            assert isinstance(extractor, LinkedInExtractor)
            mock_get_browser.assert_awaited_once()
            mock_ensure_auth.assert_awaited_once()

    async def test_auth_error_triggers_relogin(self):
        """AuthenticationError from ensure_authenticated triggers relogin."""
        with (
            patch(
                "linkedin_mcp_server.dependencies.ensure_tool_ready_or_raise",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.ensure_authenticated",
                new_callable=AsyncMock,
                side_effect=AuthenticationError("Session expired or invalid."),
            ),
            patch(
                "linkedin_mcp_server.dependencies.handle_auth_error",
                new_callable=AsyncMock,
                side_effect=AuthenticationStartedError("login opened"),
            ) as mock_handle,
        ):
            with pytest.raises(AuthenticationStartedError):
                await get_ready_extractor(ctx=None, tool_name="test_tool")

            mock_handle.assert_awaited_once()

    async def test_non_auth_error_uses_standard_handler(self):
        """RateLimitError goes through raise_tool_error, not relogin."""
        with (
            patch(
                "linkedin_mcp_server.dependencies.ensure_tool_ready_or_raise",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
                side_effect=RateLimitError("Too many requests"),
            ),
            patch(
                "linkedin_mcp_server.dependencies.handle_auth_error",
                new_callable=AsyncMock,
            ) as mock_handle,
        ):
            with pytest.raises(ToolError, match="Rate limit"):
                await get_ready_extractor(ctx=None, tool_name="test_tool")

            mock_handle.assert_not_awaited()

    async def test_browser_binary_missing_invalidates_and_raises_actionable(self):
        """Patchright "Executable doesn't exist" surfaces as actionable BrowserBinaryMissingError, and metadata is dropped."""
        err = NetworkError(
            "Failed to start browser: BrowserType.launch_persistent_context: "
            "Executable doesn't exist at /tmp/foo/chrome-headless-shell. "
            "Looks like Playwright was just installed or updated. "
            "Please run the following command to download new browsers: patchright install"
        )
        with (
            patch(
                "linkedin_mcp_server.dependencies.ensure_tool_ready_or_raise",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
                side_effect=err,
            ),
            patch(
                "linkedin_mcp_server.dependencies.invalidate_browser_setup"
            ) as mock_invalidate,
        ):
            with pytest.raises(
                ToolError, match="Patchright Chromium browser is missing"
            ):
                await get_ready_extractor(ctx=None, tool_name="test_tool")

            mock_invalidate.assert_called_once()

    async def test_unrelated_network_error_is_not_treated_as_binary_missing(self):
        """A generic connection error must not call invalidate_browser_setup or surface the binary-missing copy."""
        err = NetworkError("Failed to start browser: connection reset by peer")
        with (
            patch(
                "linkedin_mcp_server.dependencies.ensure_tool_ready_or_raise",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
                side_effect=err,
            ),
            patch(
                "linkedin_mcp_server.dependencies.invalidate_browser_setup"
            ) as mock_invalidate,
        ):
            with pytest.raises(ToolError, match="Network error"):
                await get_ready_extractor(ctx=None, tool_name="test_tool")

            mock_invalidate.assert_not_called()

    async def test_mid_scrape_auth_error_triggers_relogin(self):
        """AuthenticationError caught in tool wrapper invokes handle_auth_error."""
        from linkedin_mcp_server.tools.person import register_person_tools

        mock_mcp = MagicMock()
        tools = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

        mock_mcp.tool = capture_tool
        register_person_tools(mock_mcp)

        mock_extractor = AsyncMock()
        mock_extractor.scrape_person = AsyncMock(
            side_effect=AuthenticationError("Auth barrier detected")
        )

        mock_ctx = MagicMock()
        mock_ctx.report_progress = AsyncMock()

        with patch(
            "linkedin_mcp_server.tools.person.handle_auth_error",
            new_callable=AsyncMock,
            side_effect=AuthenticationStartedError("login opened"),
        ) as mock_handle:
            with pytest.raises(ToolError, match="login opened"):
                await tools["get_person_profile"](
                    linkedin_username="testuser",
                    ctx=mock_ctx,
                    extractor=mock_extractor,
                )

            mock_handle.assert_awaited_once()
            # First arg should be the AuthenticationError
            assert isinstance(mock_handle.call_args[0][0], AuthenticationError)


class TestAnOwnerGoesQuiescentInsteadOfLoggingIn:
    """The owner closes the browser, records what it found, and reports."""

    async def test_the_broken_generation_is_read_before_the_browser_closes(self):
        """The ordering is the whole point, so it is asserted rather than assumed.

        A confirmed close releases the profile lease, which is exactly what lets
        another process log in. If the generation were read afterwards, a login
        that landed in that gap would be recorded as the broken one, and the owner
        would then hold its latch against the very session it asked for.
        """
        from linkedin_mcp_server.server_role import ServerRole, set_process_role

        set_process_role(ServerRole.OWNER)
        order: list[str] = []

        async def closing() -> None:
            order.append("close")

        with (
            patch(
                "linkedin_mcp_server.dependencies.get_runtime_policy",
                return_value="managed",
            ),
            patch(
                "linkedin_mcp_server.dependencies.current_login_generation",
                side_effect=lambda: (order.append("read"), "gen-1")[1],
            ),
            patch("linkedin_mcp_server.dependencies.close_browser", closing),
            patch(
                "linkedin_mcp_server.dependencies.go_auth_quiescent",
                side_effect=lambda gen: order.append(f"latch:{gen}"),
            ),
            patch(
                "linkedin_mcp_server.dependencies.invalidate_auth_and_trigger_relogin",
                new_callable=AsyncMock,
                side_effect=AuthStaleOnOwnerError("owner cannot sign in"),
            ),
        ):
            with pytest.raises(AuthStaleOnOwnerError):
                await handle_auth_error(AuthenticationError("expired"), ctx=None)

        assert order == ["read", "close", "latch:gen-1"]

    async def test_a_direct_server_reads_the_generation_but_does_not_latch(self):
        """DIRECT reads it too, and that is a correction rather than a widening.

        An earlier version asserted that DIRECT read nothing, on the reasoning
        that the generation was owner state. It is not: a DIRECT server rotates
        through the same function a frontend does, so without the generation it
        reaches the rotation with nothing to compare against. Measured with the
        old behaviour and a momentary profile holder: the login reported that a
        window had opened, opened none, kept the dead session, and left readiness
        saying the session was fine.

        The latch stays owner-only. That is process state a single-process server
        has no use for, and setting it would make it refuse its own logins.
        """
        latched = MagicMock()
        read = MagicMock(return_value="gen-1")

        with (
            patch(
                "linkedin_mcp_server.dependencies.get_runtime_policy",
                return_value="managed",
            ),
            patch("linkedin_mcp_server.dependencies.current_login_generation", read),
            patch("linkedin_mcp_server.dependencies.close_browser", AsyncMock()),
            patch("linkedin_mcp_server.dependencies.go_auth_quiescent", latched),
            patch(
                "linkedin_mcp_server.dependencies.invalidate_auth_and_trigger_relogin",
                new_callable=AsyncMock,
                side_effect=AuthenticationStartedError("login opened"),
            ),
        ):
            with pytest.raises(AuthenticationStartedError):
                await handle_auth_error(AuthenticationError("expired"), ctx=None)

        read.assert_called_once()
        latched.assert_not_called()

    async def test_an_unconfirmed_close_is_reported_instead_of_latching(self):
        """A teardown that could not be confirmed must not invite a login.

        ``close_browser`` keeps the profile lease when Chromium's shutdown times
        out, deliberately, because it may still be running
        (``drivers/browser.py:786-798``). Telling a client to sign in then sends it
        after a lease it can never take, and latching would answer every later call
        from a state only an owner restart clears.

        Reproduced before this guard existed: the owner said "retry, the client
        will open a login window" while holding the lease with ``browser_open``
        still set.
        """
        from linkedin_mcp_server.exceptions import BrowserShutdownUnconfirmedError
        from linkedin_mcp_server.server_role import ServerRole, set_process_role

        set_process_role(ServerRole.OWNER)
        latched = MagicMock()
        lease = MagicMock()
        lease.browser_open = True  # what an unconfirmed close leaves behind

        with (
            patch(
                "linkedin_mcp_server.dependencies.get_runtime_policy",
                return_value="managed",
            ),
            patch(
                "linkedin_mcp_server.dependencies.current_login_generation",
                return_value="gen-1",
            ),
            patch("linkedin_mcp_server.dependencies.close_browser", AsyncMock()),
            patch(
                "linkedin_mcp_server.dependencies.get_profile_lease",
                return_value=lease,
            ),
            # Also at the source: the stand-down helper lives in `server_role`
            # and reads the lease through `profile_lease`, not through the
            # module under test.
            patch(
                "linkedin_mcp_server.profile_lease.get_profile_lease",
                return_value=lease,
            ),
            patch("linkedin_mcp_server.dependencies.go_auth_quiescent", latched),
        ):
            with pytest.raises(BrowserShutdownUnconfirmedError, match="Restart"):
                await handle_auth_error(AuthenticationError("expired"), ctx=None)

        # Not latched: a latch here is the wedge, because nothing can clear it.
        latched.assert_not_called()

    async def test_an_unconfirmed_close_stops_a_single_server_too(self):
        """Not only the shared owner: nothing that follows can work for either.

        An unconfirmed teardown keeps the profile lease until the process exits,
        so a single-process server that carried on would start a login whose own
        rotation is refused for exactly that reason, after having told the user a
        window had opened. Measured with the check owner-only: the server reported
        "a login browser window has been opened", opened none, kept the dead
        session, and left a state only a restart clears.
        """
        from linkedin_mcp_server.exceptions import BrowserShutdownUnconfirmedError

        relogin = AsyncMock()
        lease = MagicMock()
        lease.browser_open = True

        with (
            patch(
                "linkedin_mcp_server.dependencies.get_runtime_policy",
                return_value="managed",
            ),
            patch(
                "linkedin_mcp_server.dependencies.current_login_generation",
                return_value="gen-1",
            ),
            patch("linkedin_mcp_server.dependencies.close_browser", AsyncMock()),
            patch(
                "linkedin_mcp_server.dependencies.get_profile_lease",
                return_value=lease,
            ),
            # Also at the source: the stand-down helper lives in `server_role`
            # and reads the lease through `profile_lease`, not through the
            # module under test.
            patch(
                "linkedin_mcp_server.profile_lease.get_profile_lease",
                return_value=lease,
            ),
            patch(
                "linkedin_mcp_server.dependencies.invalidate_auth_and_trigger_relogin",
                relogin,
            ),
        ):
            # A DIRECT server: the role is never set, so it is the default.
            with pytest.raises(BrowserShutdownUnconfirmedError, match="Restart"):
                await handle_auth_error(AuthenticationError("expired"), ctx=None)

        relogin.assert_not_awaited()

    async def test_a_wedged_owner_asks_to_be_replaced(self):
        """Reporting the wedge is not enough for an owner: it has to go.

        The two roles need different answers to the same condition. "Restart the
        server" is actionable for a single-process server, whose client owns it.
        A detached owner outlives the client that started it, so restarting the
        client elects nothing: the next frontend attaches to this same owner and
        meets the same held profile. Only exiting frees the lock.

        Asserted against the request rather than against a real exit, because the
        exit itself belongs to the serve loop; ``test_daemon_owner`` covers that
        the loop acts on this.
        """
        from linkedin_mcp_server.exceptions import BrowserShutdownUnconfirmedError
        from linkedin_mcp_server.server_role import (
            ServerRole,
            set_process_role,
            stand_down_reason,
        )

        set_process_role(ServerRole.OWNER)
        lease = MagicMock()
        lease.browser_open = True

        with (
            patch(
                "linkedin_mcp_server.dependencies.get_runtime_policy",
                return_value="managed",
            ),
            patch(
                "linkedin_mcp_server.dependencies.current_login_generation",
                return_value="gen-1",
            ),
            patch("linkedin_mcp_server.dependencies.close_browser", AsyncMock()),
            patch(
                "linkedin_mcp_server.dependencies.get_profile_lease",
                return_value=lease,
            ),
            # Also at the source: the stand-down helper lives in `server_role`
            # and reads the lease through `profile_lease`, not through the
            # module under test.
            patch(
                "linkedin_mcp_server.profile_lease.get_profile_lease",
                return_value=lease,
            ),
        ):
            with pytest.raises(BrowserShutdownUnconfirmedError):
                await handle_auth_error(AuthenticationError("expired"), ctx=None)

        assert stand_down_reason() is not None, "the wedged owner keeps serving"

    async def test_a_timeout_during_the_close_still_frees_the_owner(self):
        """The wedge must be noticed even when the call it runs in is cut short.

        ``close_browser`` defers cancellation until teardown finishes and then
        re-raises ``CancelledError`` (``drivers/browser.py``), which is a
        ``BaseException`` and passes through ``except Exception`` untouched. A
        tool timeout landing during the close therefore skipped the whole
        stand-down decision, and the owner stayed wedged exactly as before the
        fix -- recoverable only by killing it.

        Measured with the decision outside the ``finally``: the call ended in
        ``TimeoutError`` with ``stand_down_reason()`` still None and the lease
        still held.
        """
        from linkedin_mcp_server.server_role import (
            ServerRole,
            set_process_role,
            stand_down_reason,
        )

        set_process_role(ServerRole.OWNER)
        lease = MagicMock()
        lease.browser_open = True

        async def close_that_defers_its_cancel():
            """What the production close does, in miniature."""
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                pass  # teardown runs to the end regardless
            raise asyncio.CancelledError

        with (
            patch(
                "linkedin_mcp_server.dependencies.get_runtime_policy",
                return_value="managed",
            ),
            patch(
                "linkedin_mcp_server.dependencies.current_login_generation",
                return_value="gen-1",
            ),
            patch(
                "linkedin_mcp_server.dependencies.close_browser",
                close_that_defers_its_cancel,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_profile_lease",
                return_value=lease,
            ),
            # Also at the source: the stand-down helper lives in `server_role`
            # and reads the lease through `profile_lease`, not through the
            # module under test.
            patch(
                "linkedin_mcp_server.profile_lease.get_profile_lease",
                return_value=lease,
            ),
        ):
            with pytest.raises((TimeoutError, asyncio.CancelledError)):
                await asyncio.wait_for(
                    handle_auth_error(AuthenticationError("expired"), ctx=None),
                    timeout=0.02,
                )

        assert stand_down_reason() is not None, (
            "a timeout during the close left the owner wedged"
        )

    async def test_a_single_server_is_not_asked_to_stand_down(self):
        """The same condition, and deliberately not the same answer.

        A DIRECT server has no successor to elect. Asking it to stand down would
        end the only server the client has, over something a restart fixes.
        """
        from linkedin_mcp_server.exceptions import BrowserShutdownUnconfirmedError
        from linkedin_mcp_server.server_role import stand_down_reason

        lease = MagicMock()
        lease.browser_open = True

        with (
            patch(
                "linkedin_mcp_server.dependencies.get_runtime_policy",
                return_value="managed",
            ),
            patch(
                "linkedin_mcp_server.dependencies.current_login_generation",
                return_value="gen-1",
            ),
            patch("linkedin_mcp_server.dependencies.close_browser", AsyncMock()),
            patch(
                "linkedin_mcp_server.dependencies.get_profile_lease",
                return_value=lease,
            ),
            # Also at the source: the stand-down helper lives in `server_role`
            # and reads the lease through `profile_lease`, not through the
            # module under test.
            patch(
                "linkedin_mcp_server.profile_lease.get_profile_lease",
                return_value=lease,
            ),
            patch(
                "linkedin_mcp_server.dependencies.invalidate_auth_and_trigger_relogin",
                AsyncMock(),
            ),
        ):
            with pytest.raises(BrowserShutdownUnconfirmedError):
                await handle_auth_error(AuthenticationError("expired"), ctx=None)

        assert stand_down_reason() is None


class TestAWedgeFromAnywhereFreesTheOwner:
    """The request belongs to the error, not to one of its raise sites.

    Found live rather than by review. A real detached owner failed to close
    Chromium inside 10 seconds during `_authenticate_existing_profile`, deep in
    the driver, which never reaches `handle_auth_error`. The owner kept the
    profile and three consecutive fresh clients each attached to it and got the
    same refusal, recoverable only by killing it.
    """

    def teardown_method(self):
        from linkedin_mcp_server.server_role import reset_process_role_for_testing

        reset_process_role_for_testing()

    def test_raising_it_asks_an_owner_to_give_way(self):
        from linkedin_mcp_server.exceptions import BrowserShutdownUnconfirmedError
        from linkedin_mcp_server.server_role import (
            ServerRole,
            set_process_role,
            stand_down_reason,
        )

        set_process_role(ServerRole.OWNER)
        # Constructed with the profile already held, which is the state the
        # request is about. This asserts the constructor's own contribution; the
        # startup path that produces that state in the real order is asserted
        # through `_create_browser` below, because a mocked flag there hid a live
        # wedge for a whole review round.
        lease = MagicMock()
        lease.browser_open = True
        with patch(
            "linkedin_mcp_server.profile_lease.get_profile_lease", return_value=lease
        ):
            BrowserShutdownUnconfirmedError("the startup teardown timed out")

        assert stand_down_reason() is not None, (
            "an owner wedged outside handle_auth_error keeps the profile forever"
        )

    async def test_the_startup_path_frees_the_owner_in_the_real_order(self):
        """The live wedge, driven through the function that produces it.

        `_create_browser_locked` raises while `browser_open` is still false, so
        the helper inside the exception's constructor reads "nothing is held" and
        returns. Only the catch afterwards marks the browser open and takes the
        extra reference -- holding the profile with nobody having asked for a
        replacement.

        Measured in production order: `browser_open=True`, `stand_down=None`.
        The previous test for this handed the constructor a lease that already
        said True, which is exactly the state production has not reached yet, and
        that mock is what hid it.
        """
        from linkedin_mcp_server.drivers import browser as drv
        from linkedin_mcp_server.exceptions import BrowserShutdownUnconfirmedError
        from linkedin_mcp_server.server_role import (
            ServerRole,
            set_process_role,
            stand_down_reason,
        )

        class Lease:
            """Real enough: the marker starts false, as production's does."""

            def __init__(self):
                self.browser_open = False
                self.refs = 1

            def try_acquire(self):
                self.refs += 1
                return True

            def mark_browser_open(self):
                self.browser_open = True

            def release(self):
                self.refs -= 1

        set_process_role(ServerRole.OWNER)
        lease = Lease()

        async def teardown_that_could_not_be_confirmed():
            raise BrowserShutdownUnconfirmedError("the startup teardown timed out")

        with (
            patch.object(drv, "_browser", None),
            patch.object(drv, "get_profile_lease", return_value=lease),
            patch(
                "linkedin_mcp_server.profile_lease.get_profile_lease",
                return_value=lease,
            ),
            patch.object(
                drv, "_create_browser_locked", teardown_that_could_not_be_confirmed
            ),
        ):
            with pytest.raises(BrowserShutdownUnconfirmedError):
                await drv._create_browser()

        assert lease.browser_open is True, "the profile was not actually held"
        assert stand_down_reason() is not None, (
            "the owner holds the profile and nobody asked for a replacement"
        )

    def test_a_single_server_is_left_alone(self):
        """Its client owns it, so "restart the server" is something to act on."""
        from linkedin_mcp_server.exceptions import BrowserShutdownUnconfirmedError
        from linkedin_mcp_server.server_role import stand_down_reason

        lease = MagicMock()
        lease.browser_open = True
        with patch(
            "linkedin_mcp_server.profile_lease.get_profile_lease", return_value=lease
        ):
            BrowserShutdownUnconfirmedError("the startup teardown timed out")

        assert stand_down_reason() is None

    async def test_a_quiet_close_that_keeps_the_profile_also_frees_the_owner(self):
        """The path that reports nothing at all, which is why it was missed twice.

        A handoff, the idle timeout and ``close_session`` all reach
        ``_close_browser_locked``, which returns *normally* when the teardown
        cannot be confirmed: it logs, keeps the lease, and raises nothing. So
        neither the exception constructor nor ``handle_auth_error`` runs, and the
        owner sits on the profile while every later launch is refused with
        ``BrowserBusyError``.

        Reproduced before this: close returned, the lease was kept, and
        ``stand_down_reason()`` was still None.
        """
        from linkedin_mcp_server.drivers import browser as drv
        from linkedin_mcp_server.server_role import (
            ServerRole,
            set_process_role,
            stand_down_reason,
        )

        set_process_role(ServerRole.OWNER)
        lease = MagicMock()
        lease.browser_open = True
        fake_browser = MagicMock()
        fake_browser.close = AsyncMock(return_value=False)  # unconfirmed teardown

        with (
            patch.object(drv, "_browser", fake_browser),
            patch.object(drv, "_browser_lease", lease),
            patch.object(drv, "get_profile_lease", return_value=lease),
            patch(
                "linkedin_mcp_server.profile_lease.get_profile_lease",
                return_value=lease,
            ),
        ):
            await drv._close_browser_locked()

        # The lease is kept deliberately; what must not also happen is silence.
        lease.release.assert_not_called()
        assert stand_down_reason() is not None, (
            "an owner stranded by a routine close keeps the profile forever"
        )

    def test_a_free_profile_leaves_the_owner_serving(self):
        """The guard is what keeps this from firing on every ordinary close.

        Asserted against the helper directly rather than through a confirmed
        close: that branch never reaches the helper at all, so a test driving it
        stays green with the guard deleted. Measured -- the first version of this
        did exactly that.

        Without the guard every call here would end the owner, healthy or not,
        and the daemon would churn a process per stale session.
        """
        from linkedin_mcp_server.server_role import (
            ServerRole,
            a_held_profile_means_this_owner_must_go,
            set_process_role,
            stand_down_reason,
        )

        set_process_role(ServerRole.OWNER)
        lease = MagicMock()
        lease.browser_open = False  # a confirmed teardown clears it

        with patch(
            "linkedin_mcp_server.profile_lease.get_profile_lease", return_value=lease
        ):
            a_held_profile_means_this_owner_must_go()

        assert stand_down_reason() is None, "a healthy owner was told to exit"

    async def test_a_confirmed_close_does_not_reach_the_request(self):
        """And the ordinary close path stays silent end to end."""
        from linkedin_mcp_server.drivers import browser as drv
        from linkedin_mcp_server.server_role import (
            ServerRole,
            set_process_role,
            stand_down_reason,
        )

        set_process_role(ServerRole.OWNER)
        lease = MagicMock()
        lease.browser_open = False  # a confirmed teardown clears it
        fake_browser = MagicMock()
        fake_browser.close = AsyncMock(return_value=True)

        with (
            patch.object(drv, "_browser", fake_browser),
            patch.object(drv, "_browser_lease", lease),
            patch.object(drv, "get_profile_lease", return_value=lease),
            patch(
                "linkedin_mcp_server.profile_lease.get_profile_lease",
                return_value=lease,
            ),
        ):
            await drv._close_browser_locked()

        assert stand_down_reason() is None, "a healthy owner was told to exit"

    def test_a_lease_it_cannot_read_counts_as_held(self):
        """Not being able to tell is not the same as being told it is free.

        The two outcomes are not symmetric: an unnecessary stand-down costs one
        election, a stranded owner costs every later call. Measured with the read
        raising ``PermissionError``: the owner kept serving.
        """
        from linkedin_mcp_server.server_role import (
            ServerRole,
            a_held_profile_means_this_owner_must_go,
            set_process_role,
            stand_down_reason,
        )

        set_process_role(ServerRole.OWNER)

        def unreadable():
            raise PermissionError("the profile path cannot be resolved")

        with patch(
            "linkedin_mcp_server.profile_lease.get_profile_lease",
            side_effect=unreadable,
        ):
            a_held_profile_means_this_owner_must_go()

        assert stand_down_reason() is not None

    async def test_a_close_settles_the_profile_without_any_lookup(self):
        """The close no longer asks where its lease is, so it cannot be told wrong.

        This replaces a pair of tests that injected ``get_profile_lease`` raising
        ``PermissionError`` inside the close and asserted the workaround fired.
        Kept as a scenario and inverted as an assertion: with the lookup raising
        for the whole call, an unconfirmed teardown still keeps the lease and
        still asks for a replacement. If a lookup ever returns to this path the
        ``PermissionError`` surfaces and this fails.

        Worth saying why the old pair had to go rather than be adjusted. Left as
        they were they would have passed *vacuously* — green while the injected
        failure had nothing to land on — which is the one outcome worse than a
        red test. And the failure they injected is hard to provoke for real:
        measured against the actual function, a removed auth root, a parent
        directory at mode 000 and a symlink loop all return normally, because
        ``Path.resolve()`` is non-strict and the registry is a dict. What they
        were really pinning is structural, and that is what is asserted here:
        whatever goes wrong between clearing ``_browser`` and settling the lease,
        the profile must not be left held in silence.
        """
        from linkedin_mcp_server.drivers import browser as drv
        from linkedin_mcp_server.server_role import (
            ServerRole,
            set_process_role,
            stand_down_reason,
        )

        set_process_role(ServerRole.OWNER)
        lease = MagicMock()
        lease.browser_open = True
        fake_browser = MagicMock()
        fake_browser.close = AsyncMock(return_value=False)  # unconfirmed

        def unreadable():
            raise PermissionError("the profile path cannot be resolved")

        with (
            patch.object(drv, "_browser", fake_browser),
            patch.object(drv, "_browser_lease", lease),
            patch.object(drv, "get_profile_lease", side_effect=unreadable),
            patch(
                "linkedin_mcp_server.profile_lease.get_profile_lease",
                side_effect=unreadable,
            ),
        ):
            # No PermissionError: nothing on this path resolves a path any more.
            await drv._close_browser_locked()

        lease.release.assert_not_called()
        assert stand_down_reason() is not None, (
            "an owner stranded by an unconfirmed close keeps the profile"
        )

    async def test_a_confirmed_close_releases_without_any_lookup_either(self):
        """The same for the ordinary ending, which used to strand a lease too.

        A proven-closed Chromium does not by itself free the profile: both
        ``mark_browser_closed()`` and ``release()`` need the lease object, and
        when that had to be looked up a failing lookup left it held with its
        marker set. Measured then with a real acquired lease: Chromium gone, and
        this owner's own next launch refused with ``BrowserBusyError`` while
        nobody had asked for a replacement.
        """
        from linkedin_mcp_server.drivers import browser as drv
        from linkedin_mcp_server.server_role import (
            ServerRole,
            set_process_role,
            stand_down_reason,
        )

        set_process_role(ServerRole.OWNER)
        lease = MagicMock()
        lease.browser_open = False  # a confirmed teardown clears it
        fake_browser = MagicMock()
        fake_browser.close = AsyncMock(return_value=True)  # confirmed

        def unreadable():
            raise PermissionError("the profile path cannot be resolved")

        with (
            patch.object(drv, "_browser", fake_browser),
            patch.object(drv, "_browser_lease", lease),
            patch.object(drv, "get_profile_lease", side_effect=unreadable),
            patch(
                "linkedin_mcp_server.profile_lease.get_profile_lease",
                side_effect=unreadable,
            ),
        ):
            await drv._close_browser_locked()

        lease.mark_browser_closed.assert_called_once()
        lease.release.assert_called_once()
        assert stand_down_reason() is None, "a healthy owner was told to exit"


class TestTheBrowserKeepsItsOwnLease:
    """Ownership as an object it holds, rather than a fact it remembers.

    Six review rounds each found one more way for an owner to end up sitting on
    the profile with nobody asked to replace it, and every one of them lived in
    the same gap: the close had cleared ``_browser``, still had work to do, and
    had to go and find its lease again before it could do any of it.
    """

    def teardown_method(self):
        from linkedin_mcp_server.server_role import reset_process_role_for_testing

        reset_process_role_for_testing()

    async def test_it_keeps_the_object_the_registry_handed_out(self, tmp_path):
        """By identity, because a fresh equivalent instance is the failure mode.

        ``profile_lease`` clears inherited leases after a fork by walking its
        registry, so a ``ProfileLease`` constructed outside it is invisible to
        that handler: a forked child would keep its parent's kernel lock alive
        with nothing in its own state to explain why. The two objects would
        behave identically everywhere else, which is exactly why this asserts
        identity rather than behaviour.
        """
        from linkedin_mcp_server.drivers import browser as drv
        from linkedin_mcp_server.profile_lease import get_profile_lease

        lease = get_profile_lease(tmp_path / "profile")

        async def built() -> object:
            return MagicMock()

        with (
            patch.object(drv, "_browser", None),
            patch.object(drv, "_browser_lease", None),
            patch.object(drv, "get_profile_lease", return_value=lease),
            patch.object(drv, "_create_browser_locked", built),
        ):
            await drv._create_browser()
            retained = drv._browser_lease

        try:
            assert retained is lease, "the browser kept a lease of its own making"
        finally:
            lease.release()

    def test_the_test_reset_drops_the_lease_without_releasing_it(self, tmp_path):
        """``conftest`` resets this module first, on purpose.

        A lease the browser still holds is settled by its own bookkeeping before
        ``reset_leases_for_testing`` runs. Releasing a reference here as well
        would drop one this module never took, and the next test would meet a
        lease reporting itself free while the kernel lock is still open.
        """
        from linkedin_mcp_server.drivers import browser as drv
        from linkedin_mcp_server.profile_lease import get_profile_lease

        lease = get_profile_lease(tmp_path / "profile")
        assert lease.try_acquire()

        with patch.object(drv, "_browser_lease", lease):
            drv.reset_browser_for_testing()

        try:
            assert lease.held, "the reset released a reference it never took"
        finally:
            lease.release()

    async def test_it_settles_the_lease_it_took_after_a_rotation(self, tmp_path):
        """The profile can move under a live browser, and the lease does not.

        ``rotate_source_profile`` takes the lease itself, and afterwards the
        registry's entry for the new auth root is a *different* object. A close
        that re-derived its lease from the current path would then mark and
        release the wrong one, leaving the browser's own profile held. Holding
        the object is what makes that unrepresentable.
        """
        from linkedin_mcp_server.drivers import browser as drv

        took = MagicMock()
        took.browser_open = True
        somewhere_else = MagicMock()
        somewhere_else.browser_open = True
        fake_browser = MagicMock()
        fake_browser.close = AsyncMock(return_value=True)

        with (
            patch.object(drv, "_browser", fake_browser),
            patch.object(drv, "_browser_lease", took),
            # What the path resolves to now, which is not what was acquired.
            patch.object(drv, "get_profile_lease", return_value=somewhere_else),
        ):
            await drv._close_browser_locked()

        took.release.assert_called_once()
        somewhere_else.release.assert_not_called()
        somewhere_else.mark_browser_closed.assert_not_called()

    def test_the_stand_down_helper_uses_the_lease_it_is_given(self):
        """And never reaches for the registry when it was handed one.

        The lookup is the thing being removed from these paths, so a helper that
        quietly consulted it anyway would leave the reconstruction alive in the
        one decision the six rounds converged on.

        Asserted against the *call*, not by making the lookup raise. That was the
        first version of this test and it passed against a helper that ignored
        its argument entirely: the helper catches broadly on purpose, so an
        injected failure is swallowed and read as "cannot tell, assume held",
        which is the same verdict the correct code reaches. The mutation was
        invisible until the assertion moved here.
        """
        from linkedin_mcp_server.server_role import (
            ServerRole,
            a_held_profile_means_this_owner_must_go,
            set_process_role,
            stand_down_reason,
        )

        set_process_role(ServerRole.OWNER)
        held = MagicMock()
        held.browser_open = True

        with patch("linkedin_mcp_server.profile_lease.get_profile_lease") as looked_up:
            a_held_profile_means_this_owner_must_go(held)

        looked_up.assert_not_called()
        assert stand_down_reason() is not None

    async def test_a_startup_that_could_not_tear_down_keeps_its_extra_reference(
        self, tmp_path
    ):
        """The one path that retains a lease with no browser behind it.

        ``_create_browser_locked`` can raise after Chromium is up, and then the
        teardown may not be confirmable: something may still be on the profile
        with nothing left to close it. So the lease is kept *and* an extra
        reference taken, so the caller's own release cannot drop the last one and
        let a second launch start on top of a live Chromium. The kernel frees it
        when the process exits, which is the whole recovery.
        """
        from linkedin_mcp_server.drivers import browser as drv
        from linkedin_mcp_server.exceptions import BrowserShutdownUnconfirmedError
        from linkedin_mcp_server.server_role import (
            ServerRole,
            set_process_role,
            stand_down_reason,
        )

        class Lease:
            """Real enough, and the marker starts false as production's does."""

            def __init__(self):
                self.browser_open = False
                self.refs = 0

            def try_acquire(self):
                self.refs += 1
                return True

            def mark_browser_open(self):
                self.browser_open = True

            def release(self):
                self.refs -= 1

        set_process_role(ServerRole.OWNER)
        lease = Lease()

        async def teardown_that_could_not_be_confirmed():
            raise BrowserShutdownUnconfirmedError("the startup teardown timed out")

        with (
            patch.object(drv, "_browser", None),
            patch.object(drv, "_browser_lease", None),
            patch.object(drv, "get_profile_lease", return_value=lease),
            patch(
                "linkedin_mcp_server.profile_lease.get_profile_lease",
                return_value=lease,
            ),
            patch.object(
                drv, "_create_browser_locked", teardown_that_could_not_be_confirmed
            ),
        ):
            with pytest.raises(BrowserShutdownUnconfirmedError):
                await drv._create_browser()
            retained = drv._browser_lease

        assert retained is lease, "nothing records who holds the profile"
        assert lease.refs >= 2, "the extra reference was not taken"
        assert lease.browser_open is True
        assert stand_down_reason() is not None, (
            "the owner holds the profile and nobody asked for a replacement"
        )
