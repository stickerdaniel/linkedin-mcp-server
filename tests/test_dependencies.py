"""Tests for dependencies.py — bootstrap gating and auto-relogin."""

from unittest.mock import AsyncMock, MagicMock, patch

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
            mock_relogin.assert_awaited_once_with(None)

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

    async def test_a_direct_server_neither_reads_nor_latches(self):
        # The historical path has no owner state to keep, and touching it would
        # make a single-process server refuse its own logins.
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

        # Both halves, as the name says. An earlier version asserted only the
        # latch, so reading the generation unconditionally went unnoticed, and a
        # DIRECT server would touch owner state it has no use for.
        read.assert_not_called()
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
            patch("linkedin_mcp_server.dependencies.go_auth_quiescent", latched),
        ):
            with pytest.raises(BrowserShutdownUnconfirmedError, match="Restart"):
                await handle_auth_error(AuthenticationError("expired"), ctx=None)

        # Not latched: a latch here is the wedge, because nothing can clear it.
        latched.assert_not_called()
