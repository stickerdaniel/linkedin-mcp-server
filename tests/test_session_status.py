"""`get_session_status`: what is stored, read without a browser or LinkedIn."""

import asyncio
import socket
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Client

import linkedin_mcp_server.server as server_module
from linkedin_mcp_server.core.browser import BrowserManager
from linkedin_mcp_server.core.exceptions import RateLimitError
from linkedin_mcp_server.drivers import browser as driver
from linkedin_mcp_server.profile_lease import ProfileLease
from linkedin_mcp_server.server import create_mcp_server
from linkedin_mcp_server.session_state import (
    get_runtime_id,
    get_source_profile_dir,
    portable_cookie_path,
    write_source_state,
)


@pytest.fixture
def server(monkeypatch):
    """The production server, with a lifespan that touches no browser."""
    for name in (
        "initialize_bootstrap",
        "get_runtime_policy",
        "report_retained_browser_revisions_if_ready",
    ):
        monkeypatch.setattr(server_module, name, MagicMock())
    for name in (
        "start_background_browser_setup_if_needed",
        "watch_for_handoff_requests",
        "stop_background_browser_setup",
        "close_browser",
    ):
        monkeypatch.setattr(server_module, name, AsyncMock())
    return create_mcp_server()


@pytest.fixture
def nothing_may_start(monkeypatch):
    """Fail the call if it launches Chromium, takes the profile, or reaches out."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("get_session_status reached for a browser or the network")

    for target, name in (
        (BrowserManager, "start"),
        (ProfileLease, "try_acquire"),
        (ProfileLease, "acquire"),
        (socket.socket, "connect"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
    ):
        monkeypatch.setattr(target, name, refuse)
    for name in (
        "ensure_tool_ready_or_raise",
        "start_background_browser_setup_if_needed",
        "start_login_if_needed",
    ):
        monkeypatch.setattr(f"linkedin_mcp_server.bootstrap.{name}", refuse)


async def _status(server) -> dict:
    async with Client(server) as client:
        result = await client.call_tool("get_session_status", {})
    assert result.structured_content is not None
    return result.structured_content


async def test_a_stored_session_is_reported_from_its_files(server, nothing_may_start):
    profile = get_source_profile_dir()
    profile.mkdir(parents=True)
    (profile / "Local State").write_text("{}", encoding="utf-8")
    portable_cookie_path(profile).write_text("[]", encoding="utf-8")
    source = write_source_state(profile)

    status = await _status(server)

    assert status["session"] == {
        "stored": True,
        "profile": True,
        "cookies": True,
        "login_generation": source.login_generation,
        "created_at": source.created_at,
        "created_on": source.source_runtime_id,
    }
    assert status["runtime"] == get_runtime_id()
    assert status["browser"]["open"] is False
    assert status["browser"]["idle_seconds"] is None
    assert str(profile) not in str(status)


async def test_no_session_is_reported_as_none_stored(server, nothing_may_start):
    status = await _status(server)

    assert status["session"]["stored"] is False
    assert status["session"]["login_generation"] is None
    assert (status["setup"], status["login"]) == ("not_started", "idle")
    assert status["account_restricted"] is False


async def test_a_cookie_export_alone_is_not_a_stored_session(server):
    profile = get_source_profile_dir()
    profile.mkdir(parents=True)
    portable_cookie_path(profile).write_text("[]", encoding="utf-8")

    session = (await _status(server))["session"]

    assert (session["stored"], session["profile"], session["cookies"]) == (
        False,
        False,
        True,
    )


async def test_it_answers_while_another_call_holds_the_browser(server):
    entered, release = asyncio.Event(), asyncio.Event()

    async def holding(*_args, **_kwargs):
        entered.set()
        await release.wait()
        raise RateLimitError("done")

    ready = AsyncMock(side_effect=holding)
    with patch("linkedin_mcp_server.tools.person.get_ready_extractor", ready):
        async with Client(server) as client:
            running = asyncio.create_task(
                client.call_tool(
                    "get_person_profile",
                    {"linkedin_username": "someone"},
                    raise_on_error=False,
                )
            )
            await asyncio.wait_for(entered.wait(), 5)
            try:
                status = await asyncio.wait_for(
                    client.call_tool("get_session_status", {}), 5
                )
            finally:
                release.set()
                await running

    assert status.structured_content["browser"]["calls_in_flight"] == 1


async def test_asking_does_not_reset_the_idle_clock(server, monkeypatch):
    ended = time.monotonic() - 42.0
    monkeypatch.setattr(driver, "_last_activity", ended)
    monkeypatch.setattr(driver, "_browser", MagicMock())

    status = await _status(server)

    assert driver._last_activity == ended
    assert driver._calls_in_flight == 0
    assert status["browser"]["open"] is True
    assert status["browser"]["idle_seconds"] >= 42.0


async def test_the_answer_leaves_the_update_notice_for_a_browser_tool(
    server, monkeypatch
):
    import linkedin_mcp_server.update_check as update_check

    monkeypatch.setattr(update_check, "__version__", "4.16.1")
    monkeypatch.setattr(update_check, "_latest_known", "4.18.0")
    prime = MagicMock()
    monkeypatch.setattr(update_check, "prime_from_cache", prime)
    refresh = AsyncMock()
    monkeypatch.setattr(update_check, "refresh_latest_version", refresh)
    notice = update_check.pending_update_notice()
    assert notice is not None

    async with Client(server) as client:
        status = await client.call_tool("get_session_status", {})

    assert notice not in [getattr(part, "text", None) for part in status.content]
    # Neither the cache on disk nor PyPI is asked.
    prime.assert_not_called()
    refresh.assert_not_called()
