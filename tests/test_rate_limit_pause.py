"""RATE_LIMIT_PAUSE: no LinkedIn call for a while after LinkedIn pushes back.

The tools run through FastMCP and the real ``tool_errors`` and
``raise_tool_error``. Only the browser is a stand-in, and it counts every time a
call asks for it, which is the step that launches Chromium and navigates.
"""

import json
import os
import stat
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.config import reset_config
from linkedin_mcp_server.core.exceptions import AccountRestrictedError, RateLimitError
from linkedin_mcp_server.rate_limit_pause import PAUSE_FILE
from linkedin_mcp_server.session_state import write_source_state
from linkedin_mcp_server.tools.person import register_person_tools

DAY = 86400


class Server:
    """Person tools on a stand-in browser that records every request for it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        browser = MagicMock()
        browser.page.is_closed.return_value = False
        browser.context.browser.is_connected.return_value = True
        self.readiness = AsyncMock(return_value=None)
        self.launch = AsyncMock(return_value=browser)
        self.extractor = MagicMock()
        self.extractor.read_person = AsyncMock(
            return_value={"url": "https://www.linkedin.com/in/someone/", "sections": {}}
        )
        target = "linkedin_mcp_server.dependencies"
        monkeypatch.setattr(f"{target}.ensure_tool_ready_or_raise", self.readiness)
        monkeypatch.setattr(f"{target}.get_or_create_browser", self.launch)
        monkeypatch.setattr(f"{target}.ensure_authenticated", AsyncMock())
        monkeypatch.setattr(f"{target}.LinkedInExtractor", lambda page: self.extractor)
        self.mcp = FastMCP("test", mask_error_details=True)
        register_person_tools(self.mcp)

    def pushes_back(self, error: Exception) -> None:
        self.extractor.read_person.side_effect = error

    async def call(self) -> None:
        await self.mcp.call_tool("get_person_profile", {"linkedin_username": "someone"})


@pytest.fixture
def auth_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    return tmp_path


def _pause_for(monkeypatch: pytest.MonkeyPatch, seconds: int) -> None:
    monkeypatch.setenv("RATE_LIMIT_PAUSE", str(seconds))
    reset_config()


def _rate_limit() -> RateLimitError:
    return RateLimitError("Rate limit message detected on page.", 30)


async def test_a_rate_limit_page_pauses_every_later_call(monkeypatch, auth_root):
    _pause_for(monkeypatch, DAY)
    server = Server(monkeypatch)
    server.pushes_back(_rate_limit())

    with pytest.raises(ToolError, match=r"Rate limit detected\. .* paused until"):
        await server.call()
    for _ in range(2):
        with pytest.raises(ToolError, match="showed a rate-limit page") as refused:
            await server.call()

    assert "Nothing was sent to LinkedIn" in str(refused.value)
    assert server.launch.await_count == 1
    assert server.extractor.read_person.await_count == 1


async def test_a_refused_call_never_asks_for_a_browser_even_after_a_restart(
    monkeypatch, auth_root
):
    _pause_for(monkeypatch, DAY)
    first = Server(monkeypatch)
    first.pushes_back(_rate_limit())
    with pytest.raises(ToolError):
        await first.call()

    # A new process: nothing in memory, only what is on disk.
    reset_config()
    restarted = Server(monkeypatch)
    with pytest.raises(ToolError, match="paused until"):
        await restarted.call()

    restarted.readiness.assert_not_awaited()
    restarted.launch.assert_not_awaited()
    restarted.extractor.read_person.assert_not_awaited()


async def test_a_restricted_account_pauses_too(monkeypatch, auth_root):
    _pause_for(monkeypatch, DAY)
    server = Server(monkeypatch)
    server.pushes_back(AccountRestrictedError())

    with pytest.raises(ToolError) as first:
        await server.call()
    with pytest.raises(ToolError, match="restricted the account"):
        await server.call()

    # LinkedIn's own instructions stay first; the pause is appended.
    assert str(first.value).startswith(str(AccountRestrictedError()))
    assert "paused until" in str(first.value)
    assert server.launch.await_count == 1


async def test_off_by_default(monkeypatch, auth_root):
    server = Server(monkeypatch)
    server.pushes_back(_rate_limit())

    for _ in range(2):
        with pytest.raises(ToolError, match="Wait 30 seconds before trying again"):
            await server.call()

    assert server.launch.await_count == 2
    assert not (auth_root / PAUSE_FILE).exists()


async def test_a_new_sign_in_ends_the_pause(monkeypatch, auth_root):
    _pause_for(monkeypatch, DAY)
    write_source_state()
    server = Server(monkeypatch)
    server.pushes_back(_rate_limit())
    with pytest.raises(ToolError):
        await server.call()
    with pytest.raises(ToolError, match="paused until"):
        await server.call()

    write_source_state()  # what a successful --login writes
    server.extractor.read_person.side_effect = None
    await server.call()

    assert server.launch.await_count == 2


@pytest.mark.parametrize(("age", "paused"), [(DAY - 60, True), (DAY, False)])
async def test_the_pause_lasts_its_configured_length(
    monkeypatch, auth_root, age: int, paused: bool
):
    _pause_for(monkeypatch, DAY)
    record = {"since": time.time() - age, "restricted": False, "login_generation": None}
    (auth_root / PAUSE_FILE).write_text(json.dumps(record))
    server = Server(monkeypatch)

    if paused:
        with pytest.raises(ToolError, match="paused until"):
            await server.call()
    else:
        await server.call()

    assert server.launch.await_count == (0 if paused else 1)


@pytest.mark.parametrize(
    "content", ["not json", "[]", '{"since": "yesterday"}', '{"since": NaN}']
)
async def test_a_record_that_cannot_be_read_pauses_nothing(
    monkeypatch, auth_root, content: str
):
    _pause_for(monkeypatch, DAY)
    (auth_root / PAUSE_FILE).write_text(content)
    server = Server(monkeypatch)

    await server.call()

    assert server.launch.await_count == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
async def test_the_record_is_owner_only(monkeypatch, auth_root):
    _pause_for(monkeypatch, DAY)
    server = Server(monkeypatch)
    server.pushes_back(_rate_limit())
    with pytest.raises(ToolError):
        await server.call()

    mode = stat.S_IMODE((auth_root / PAUSE_FILE).stat().st_mode)
    assert mode == 0o600
