"""Signing in inside the chat, as an MCP client and the sign-in page see it.

Every case talks to the production Direct server through a real in-memory
FastMCP client that declares the MCP Apps extension, the way Claude Desktop
does. The login task is the real one from ``bootstrap``; only what lies behind
it is stood in: the browser install, and ``interactive_login``, whose stand-in
publishes a recorded page through ``login_view.published`` exactly where the
real login would.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import mcp.types as mt
import pytest
from fastmcp import Client, FastMCP
from fastmcp.apps.config import UI_EXTENSION_ID
from mcp.client import advertise
from test_bootstrap import _make_auth_ready
from test_login_view import FakeContext, FakePage, jpeg
from test_server import _stand_in_the_lifespan

import linkedin_mcp_server.bootstrap as bootstrap
import linkedin_mcp_server.login_app as login_app
import linkedin_mcp_server.server as server_module
from linkedin_mcp_server import login_view
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.exceptions import BrowserBusyError, BrowserSetupFailedError
from linkedin_mcp_server.login_viewer import LoginViewerError
from linkedin_mcp_server.server import create_mcp_server
from linkedin_mcp_server.session_state import source_state_path

_VIEW = "linkedin.dev/login-view"
_DOCKER_TEXT = "Create one with the explicit --login --login-viewer Docker command"


class StandInLogin:
    """``interactive_login`` as the login task calls it, publishing *page*."""

    def __init__(self, profile: Path, page: FakePage | None = None) -> None:
        self.profile = profile
        self.page = page or FakePage([jpeg(800, 600, b"sign-in")])
        self.calls: list[object] = []
        self.finish = asyncio.Event()
        self.cancelled = False
        self.publish = True
        self.failure: BaseException | None = None

    async def __call__(self, _profile: Path, *, superseded_by: object) -> bool:
        self.calls.append(superseded_by)
        if self.failure is not None:
            raise self.failure
        if not self.publish:
            _make_auth_ready(self.profile)
            return True
        browser = SimpleNamespace(page=self.page, context=FakeContext())
        try:
            async with login_view.published(cast(Any, browser)):
                await self.finish.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        _make_auth_ready(self.profile)
        return True


@pytest.fixture
def login(isolate_profile_dir: Path, monkeypatch: pytest.MonkeyPatch) -> StandInLogin:
    """A Docker server's login, with a persistent mount and an installed browser."""
    stand_in = StandInLogin(isolate_profile_dir)
    _stand_in_the_lifespan(monkeypatch)
    bootstrap.initialize_bootstrap("docker")
    monkeypatch.setattr(bootstrap, "interactive_login", stand_in)
    monkeypatch.setattr(bootstrap, "_ensure_browser_installed", AsyncMock())
    monkeypatch.setattr(login_app, "require_persistent_profile_mount", lambda _p: None)
    return stand_in


def _ui_client(server: FastMCP, *, mode: str = "legacy") -> Client:
    return Client(
        server,
        mode=mode,
        extensions=[
            advertise(UI_EXTENSION_ID, {"mimeTypes": ["text/html;profile=mcp-app"]})
        ],
    )


def _text(result: mt.CallToolResult) -> str:
    return " ".join(c.text for c in result.content if isinstance(c, mt.TextContent))


def _reply(result: mt.CallToolResult) -> dict[str, Any]:
    return json.loads(_text(result))


def _images(result: mt.CallToolResult) -> list[mt.ImageContent]:
    return [c for c in result.content if isinstance(c, mt.ImageContent)]


async def _frame(
    client: Client, view: dict[str, str], after: str = "", budget: float = 1
) -> mt.CallToolResult:
    return await client.call_tool_mcp(
        "login_view_frame",
        {
            "grant": view["grant"],
            "attempt_id": view["attempt_id"],
            "after": after,
            "budget_s": budget,
        },
    )


async def _until(
    client: Client, view: dict[str, str], wanted: str, **fields: str
) -> mt.CallToolResult:
    """Poll as the page does until a frame reply says *wanted* for *field*."""
    for _ in range(30):
        result = await _frame(client, view)
        reply = _reply(result)
        if all(reply.get(k) == v for k, v in fields.items()) and wanted in (
            reply.get("frame"),
            reply.get("state"),
        ):
            return result
    raise AssertionError(f"the view never said {wanted}: {reply}")


async def _open(client: Client) -> tuple[mt.CallToolResult, dict[str, str] | None]:
    result = await client.call_tool_mcp("open_login", {})
    meta = result.meta or {}
    return result, meta.get(_VIEW)


@pytest.fixture
async def client(login: StandInLogin) -> AsyncIterator[Client]:
    async with _ui_client(create_mcp_server()) as connected:
        yield connected
    login.finish.set()


class TestWhoIsOfferedTheView:
    async def test_a_client_without_the_apps_extension_gets_the_commands_and_nothing_starts(
        self, login: StandInLogin
    ):
        async with Client(create_mcp_server()) as plain:
            result, view = await _open(plain)

        assert result.is_error and view is None
        assert _DOCKER_TEXT in _text(result)
        assert login.calls == []
        assert bootstrap.current_login_attempt() is None

    async def test_an_http_server_never_offers_the_view(
        self, login: StandInLogin, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(get_config().server, "transport", "streamable-http")
        async with _ui_client(create_mcp_server()) as ui:
            result, view = await _open(ui)

        assert result.is_error and view is None
        assert login.calls == []

    async def test_a_docker_profile_without_a_persistent_mount_gets_its_remedy(
        self, login: StandInLogin, monkeypatch: pytest.MonkeyPatch
    ):
        def refuse(_profile: Path) -> None:
            raise LoginViewerError(
                "Use -v ~/.linkedin-mcp:/home/pwuser/.linkedin-mcp and try again."
            )

        monkeypatch.setattr(login_app, "require_persistent_profile_mount", refuse)
        async with _ui_client(create_mcp_server()) as ui:
            result, view = await _open(ui)

        assert result.is_error and view is None
        assert "-v ~/.linkedin-mcp:/home/pwuser/.linkedin-mcp" in _text(result)
        assert login.calls == []


class TestWhatOpenLoginStarts:
    async def test_the_grant_travels_only_in_meta(
        self, client: Client, login: StandInLogin
    ):
        result, view = await _open(client)

        assert view is not None and view["grant"]
        assert not result.is_error
        assert view["grant"] not in _text(result)
        assert result.structured_content is None
        assert "Call the original tool again" in _text(result)

    async def test_two_callers_start_one_login_and_a_later_one_joins_it(
        self, client: Client, login: StandInLogin
    ):
        (_, first), (_, second) = await asyncio.gather(_open(client), _open(client))
        await asyncio.sleep(0.05)
        _, third = await _open(client)

        assert first and second and third
        assert first["attempt_id"] == second["attempt_id"] == third["attempt_id"]
        assert len(login.calls) == 1

    async def test_session_files_nobody_found_dead_are_left_alone(
        self, client: Client, login: StandInLogin, isolate_profile_dir: Path
    ):
        # `_raise_if_docker_auth_missing` only words the error; it records
        # nothing, so files that appear later count as a session to retry.
        with pytest.raises(Exception, match="open_login"):
            await bootstrap.ensure_tool_ready_or_raise("get_feed", _ui_ctx())
        _make_auth_ready(isolate_profile_dir)

        result, view = await _open(client)

        assert view is None and not result.is_error
        assert "session files exist; retry the tool" in _text(result)
        assert login.calls == []

    async def test_a_session_a_tool_found_dead_is_retired_and_signed_in_again(
        self, client: Client, login: StandInLogin, isolate_profile_dir: Path
    ):
        _make_auth_ready(isolate_profile_dir)
        await _a_tool_finds_the_session_dead()

        result, view = await _open(client)

        assert view is not None, _text(result)
        assert login.calls == ["gen-1"]

    async def test_a_dead_session_already_replaced_is_not_retired(
        self, client: Client, login: StandInLogin, isolate_profile_dir: Path
    ):
        _make_auth_ready(isolate_profile_dir)
        await _a_tool_finds_the_session_dead()
        state = source_state_path(isolate_profile_dir)
        state.write_text(state.read_text().replace("gen-1", "gen-2"))

        result, view = await _open(client)

        assert view is None
        assert "session files exist" in _text(result)
        assert login.calls == []

    async def test_a_running_login_wins_over_files_and_a_dead_session(
        self, client: Client, login: StandInLogin, isolate_profile_dir: Path
    ):
        _make_auth_ready(isolate_profile_dir)
        await _a_tool_finds_the_session_dead()
        _, first = await _open(client)
        # The rotation moved the dead session aside; put it back, as a
        # restore would, while the login keeps running.
        _make_auth_ready(isolate_profile_dir)

        _, again = await _open(client)

        assert first and again and again["attempt_id"] == first["attempt_id"]
        assert len(login.calls) == 1


class TestHowTheAttemptEnds:
    @pytest.mark.parametrize(
        ("failure", "reason"),
        [
            (
                BrowserSetupFailedError("the browser download failed"),
                "the browser download failed",
            ),
            (
                BrowserBusyError("Another LinkedIn MCP client is using the browser"),
                "Another LinkedIn MCP client",
            ),
        ],
        ids=["install failure", "profile busy"],
    )
    async def test_a_login_that_never_opens_a_browser_tells_the_view_why(
        self,
        client: Client,
        login: StandInLogin,
        monkeypatch: pytest.MonkeyPatch,
        failure: Exception,
        reason: str,
    ):
        if isinstance(failure, BrowserSetupFailedError):
            monkeypatch.setattr(
                bootstrap, "_ensure_browser_installed", AsyncMock(side_effect=failure)
            )
        else:
            login.failure = failure

        _, view = await _open(client)
        assert view is not None
        ended = _reply(await _until(client, view, "failed"))

        assert ended["access"] == "status"
        assert reason in ended["reason"]

    async def test_a_peer_that_signed_in_first_ends_the_view_as_signed_in(
        self, client: Client, login: StandInLogin
    ):
        login.publish = False

        _, view = await _open(client)
        assert view is not None

        assert (
            _reply(await _until(client, view, "repaired_by_peer"))["access"] == "status"
        )

    async def test_a_successful_import_needs_no_view(
        self, isolate_profile_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _stand_in_the_lifespan(monkeypatch)
        bootstrap.initialize_bootstrap("managed")
        monkeypatch.setattr(bootstrap, "browser_setup_ready", lambda: True)
        monkeypatch.setattr(bootstrap, "_auto_import_allowed", lambda: True)
        login = StandInLogin(isolate_profile_dir)
        monkeypatch.setattr(bootstrap, "interactive_login", login)

        async def imported(_ctx: object = None) -> bool:
            _make_auth_ready(isolate_profile_dir)
            return True

        monkeypatch.setattr(bootstrap, "_try_auto_import_session", imported)
        async with _ui_client(create_mcp_server()) as ui:
            result, view = await _open(ui)

        assert view is None and not result.is_error
        assert "session is in place now. Retry the tool" in _text(result)
        assert login.calls == []

    async def test_after_sign_in_the_former_grant_reads_only_the_outcome(
        self, client: Client, login: StandInLogin
    ):
        _, view = await _open(client)
        assert view is not None
        frame = await _until(client, view, "image")
        reply = _reply(frame)

        login.finish.set()
        ended = await _until(client, view, "signed_in")
        typed = await client.call_tool_mcp(
            "login_view_input",
            {
                **view,
                "page_id": reply["page_id"],
                "geometry": reply["geometry"],
                "events": [{"type": "type", "text": "x"}],
            },
        )

        assert _reply(ended)["access"] == "status" and _images(ended) == []
        assert _reply(typed)["input"] == "refused"
        assert ("type", "x") not in login.page.received


class TestTheGrant:
    async def test_a_replaced_grant_gets_no_image_and_no_input_through(
        self, client: Client, login: StandInLogin
    ):
        _, old = await _open(client)
        assert old is not None
        reply = _reply(await _until(client, old, "image"))

        # The old view waits on a capture that stalls, and a new view opens.
        login.page.gate = asyncio.Event()
        waiting = asyncio.create_task(
            _frame(client, old, after=reply["hash"], budget=3)
        )
        await asyncio.sleep(0.2)
        _, new = await _open(client)
        assert new is not None and new["attempt_id"] == old["attempt_id"]
        login.page.gate.set()

        stalled = await waiting
        typed = await client.call_tool_mcp(
            "login_view_input",
            {
                **old,
                "page_id": reply["page_id"],
                "geometry": reply["geometry"],
                "events": [{"type": "paste", "text": "hunter2"}],
            },
        )
        fresh = await _frame(client, new)

        assert _reply(stalled) == {"access": "replaced"} and _images(stalled) == []
        assert _reply(typed)["access"] == "replaced" and login.page.received == []
        # The page did not change, and the new view still gets it.
        assert (
            _reply(fresh)["frame"] == "image" and _reply(fresh)["hash"] == reply["hash"]
        )
        assert len(_images(fresh)) == 1

    async def test_a_made_up_grant_reads_nothing(
        self, client: Client, login: StandInLogin
    ):
        _, view = await _open(client)
        assert view is not None

        result = await _frame(client, {**view, "grant": "not-the-grant"})

        assert _reply(result) == {"access": "denied"}
        assert _images(result) == []

    async def test_input_from_the_view_reaches_the_page(
        self, client: Client, login: StandInLogin
    ):
        _, view = await _open(client)
        assert view is not None
        reply = _reply(await _until(client, view, "image"))

        typed = await client.call_tool_mcp(
            "login_view_input",
            {
                **view,
                "page_id": reply["page_id"],
                "geometry": reply["geometry"],
                "events": [{"type": "paste", "text": "pässwörd ✓"}],
            },
        )

        assert _reply(typed) == {"access": "full", "input": "applied", "applied": 1}
        assert login.page.received == [("insert_text", "pässwörd ✓")]


class TestNothingLeaks:
    async def test_invalid_view_arguments_and_the_debug_log_name_no_values(
        self, client: Client, login: StandInLogin
    ):
        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        handler.setLevel(logging.DEBUG)
        loggers = [logging.getLogger(), logging.getLogger("fastmcp")]
        previous = [(lg, lg.level) for lg in loggers]
        for lg in loggers:
            lg.addHandler(handler)
            lg.setLevel(logging.DEBUG)
        try:
            _, view = await _open(client)
            assert view is not None
            reply = _reply(await _until(client, view, "image"))
            accepted = await client.call_tool_mcp(
                "login_view_input",
                {
                    **view,
                    "page_id": reply["page_id"],
                    "geometry": reply["geometry"],
                    "events": [{"type": "paste", "text": "SYNTHETIC-PASSWORD"}],
                },
            )
            rejected = await client.call_tool_mcp(
                "login_view_input",
                {
                    "grant": "SYNTHETIC-GRANT",
                    "attempt_id": view["attempt_id"],
                    "page_id": "p1",
                    "geometry": "800x600",
                    "events": [{"type": "paste", "text": "SYNTHETIC-SECRET" * 300}],
                },
            )
        finally:
            for lg, level in previous:
                lg.removeHandler(handler)
                lg.setLevel(level)

        assert _reply(accepted)["input"] == "applied"
        assert rejected.is_error
        assert "string_too_long" in _text(rejected)
        logs = captured.getvalue()
        assert "login_view_input" in logs  # the call was logged, its arguments were not
        for secret in (
            "SYNTHETIC-PASSWORD",
            "SYNTHETIC-GRANT",
            "SYNTHETIC-SECRET",
            view["grant"],
        ):
            assert secret not in _text(rejected)
            assert secret not in logs


async def test_the_server_ending_cancels_a_running_login(
    login: StandInLogin, monkeypatch: pytest.MonkeyPatch
):
    # The real `stop_login`, every other lifespan step stood in.
    monkeypatch.setattr(server_module, "stop_login", bootstrap.stop_login)
    async with _ui_client(create_mcp_server()) as ui:
        _, view = await _open(ui)
        assert view is not None
        await _until(ui, view, "image")

    attempt = bootstrap.current_login_attempt()
    assert login.cancelled
    assert attempt is not None and attempt.state == "cancelled"


async def _a_tool_finds_the_session_dead() -> None:
    from linkedin_mcp_server.dependencies import handle_auth_error

    with pytest.raises(Exception, match="open_login"):
        await handle_auth_error(AuthenticationError("session expired"), _ui_ctx())


def _ui_ctx() -> Any:
    """A tool context whose client declared the MCP Apps extension."""
    return SimpleNamespace(
        client_supports_extension=lambda extension: extension == UI_EXTENSION_ID
    )
