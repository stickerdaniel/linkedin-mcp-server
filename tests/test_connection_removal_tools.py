"""The connection tools as an MCP client sees them: gates before any browser."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

from linkedin_mcp_server.linkedin.connection_removal import _slug
from linkedin_mcp_server.tools.connection_removal import (
    register_connection_removal_tools,
)

READY = "linkedin_mcp_server.tools.connection_removal.get_ready_connection_remover"


class FakeRemover:
    def __init__(self, connected: set[str]):
        self.connected = set(connected)
        self.removed: list[str] = []
        self._session = AsyncMock()

    async def list_connections(self) -> dict:
        return {"connections": [{"username": u} for u in sorted(self.connected)]}

    async def remove(self, username: str, *, dry_run: bool) -> dict:
        slug = _slug(username)
        if slug not in self.connected:
            return {"username": slug, "status": "NOT_CONNECTED", "verified": True}
        if dry_run:
            return {"username": slug, "status": "WOULD_REMOVE", "verified": True}
        self.connected.discard(slug)
        self.removed.append(slug)
        return {"username": slug, "status": "REMOVED", "verified": True}


@pytest.fixture
def mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    monkeypatch.setenv("LINKEDIN_PROFILE_EDITS_DIR", str(tmp_path))
    monkeypatch.delenv("MCP_LINKEDIN_WRITE_ENABLED", raising=False)
    server = FastMCP("test")
    register_connection_removal_tools(server)
    return server


async def call(mcp: FastMCP, name: str, args: dict) -> dict:
    result = await mcp.call_tool(name, args)
    assert result.structured_content is not None
    return result.structured_content


async def test_removal_without_confirm_is_refused_before_any_browser(mcp, monkeypatch):
    monkeypatch.setenv("MCP_LINKEDIN_WRITE_ENABLED", "true")
    ready = AsyncMock()
    with patch(READY, ready):
        out = await call(mcp, "remove_connections", {"usernames": ["a"]})
    assert out["error"] == "CONFIRMATION_REQUIRED"
    ready.assert_not_called()


async def test_removal_with_writes_disabled_is_refused_before_any_browser(mcp):
    ready = AsyncMock()
    with patch(READY, ready):
        out = await call(
            mcp, "remove_connections", {"usernames": ["a"], "confirm": True}
        )
    assert out["error"] == "WRITES_DISABLED"
    ready.assert_not_called()


async def test_a_dry_run_removes_nobody_and_needs_no_write_switch(mcp):
    fake = FakeRemover({"a", "b"})
    with patch(READY, AsyncMock(return_value=fake)):
        out = await call(
            mcp, "remove_connections", {"usernames": ["a", "b"], "dryRun": True}
        )
    assert [r["status"] for r in out["results"]] == ["WOULD_REMOVE"] * 2
    assert fake.removed == [] and fake.connected == {"a", "b"}


async def test_removal_removes_exactly_the_named_people(mcp, monkeypatch):
    monkeypatch.setenv("MCP_LINKEDIN_WRITE_ENABLED", "true")
    fake = FakeRemover({"a", "b", "keep"})
    with patch(READY, AsyncMock(return_value=fake)):
        out = await call(
            mcp,
            "remove_connections",
            {
                "usernames": ["a", "https://www.linkedin.com/in/b/"],
                "confirm": True,
            },
        )
    assert out["allDone"] is True
    assert fake.removed == ["a", "b"] and fake.connected == {"keep"}


async def test_more_than_five_per_call_is_rejected(mcp, monkeypatch):
    monkeypatch.setenv("MCP_LINKEDIN_WRITE_ENABLED", "true")
    fake = FakeRemover(set("abcdef"))
    with patch(READY, AsyncMock(return_value=fake)), pytest.raises(Exception):
        await mcp.call_tool(
            "remove_connections", {"usernames": list("abcdef"), "confirm": True}
        )
    assert fake.removed == []


def test_slugs_are_decoded_and_case_folded():
    assert _slug("https://www.linkedin.com/in/Jane-Doe/") == "jane-doe"
    assert _slug("%D0%B4%D0%B0%D1%80") == "дар"
