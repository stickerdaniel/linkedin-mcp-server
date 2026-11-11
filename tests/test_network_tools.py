"""The network tools end to end through FastMCP, against a temporary store."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.linkedin.my_connections import PageConnection, WalkResult
from linkedin_mcp_server.tools.network import register_network_tools
from linkedin_mcp_server.tools.person import register_person_tools

EXPORT = """Notes:
"Some email addresses are missing."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Ada,Lovelace,https://www.linkedin.com/in/ada-lovelace,ada@example.org,Analytical Engines,Head of Policy,05 Mar 2024
"""


@pytest.fixture
def mcp(monkeypatch, tmp_path) -> FastMCP:
    monkeypatch.setenv("LINKEDIN_NETWORK_DB", str(tmp_path / "network.db"))
    server = FastMCP("test")
    register_network_tools(server)
    register_person_tools(server)
    return server


async def call(mcp: FastMCP, name: str, args: dict) -> dict:
    result = await mcp.call_tool(name, args)
    assert result.structured_content is not None
    return result.structured_content


async def test_import_then_search_and_overview(mcp, tmp_path):
    csv = tmp_path / "Connections.csv"
    csv.write_text(EXPORT, encoding="utf-8")

    imported = await call(mcp, "import_connections_csv", {"path": str(csv)})
    assert imported["rows"] == 1 and imported["added"] == 1

    found = await call(mcp, "search_my_network", {"query": "policy"})
    assert found["total"] == 1
    assert found["results"][0]["slug"] == "ada-lovelace"

    overview = await call(mcp, "get_network_overview", {})
    assert overview["total"] == 1
    assert overview["top_companies"] == [{"company": "Analytical Engines", "count": 1}]


async def test_import_refuses_a_missing_file(mcp, tmp_path):
    with pytest.raises(ToolError, match="No such file"):
        await mcp.call_tool("import_connections_csv", {"path": str(tmp_path / "x.csv")})


async def test_search_refuses_a_malformed_date(mcp):
    with pytest.raises(Exception):
        await mcp.call_tool("search_my_network", {"connected_after": "March 2024"})


async def test_sync_saves_every_batch_and_reports_the_walk(mcp):
    async def walk(on_batch, **kwargs):
        on_batch(
            [PageConnection("ada-lovelace", "Ada Lovelace", "Policy lead", None, None)]
        )
        on_batch([PageConnection("alan-turing", "Alan Turing", "Reader", None, None)])
        # No walk has reached the end yet, so this one may not stop early.
        assert kwargs["stop_after_known"] is None
        return WalkResult(seen=2, rounds=3, stop_reason="end_of_list", complete=True)

    extractor = MagicMock()
    extractor.walk_my_connections = AsyncMock(side_effect=walk)
    with patch(
        "linkedin_mcp_server.tools.person.get_ready_extractor",
        AsyncMock(return_value=extractor),
    ):
        result = await call(mcp, "sync_my_connections", {})

    assert result["seen"] == 2 and result["added"] == 2
    assert result["total_in_store"] == 2
    assert result["stop_reason"] == "end_of_list" and result["complete"]

    overview = await call(mcp, "get_network_overview", {})
    assert overview["from_page"] == 2
    assert overview["last_runs"]["page_full"]["complete"] == 1


async def test_full_sync_does_not_stop_at_known_connections(mcp):
    extractor = MagicMock()
    extractor.walk_my_connections = AsyncMock(return_value=WalkResult())
    with patch(
        "linkedin_mcp_server.tools.person.get_ready_extractor",
        AsyncMock(return_value=extractor),
    ):
        await call(mcp, "sync_my_connections", {"full": True})
    awaited = extractor.walk_my_connections.await_args
    assert awaited is not None
    assert awaited.kwargs["stop_after_known"] is None


async def test_an_unfinished_walk_is_continued_not_cut_short_by_known_connections(mcp):
    """The scenario from review: a first walk saves a prefix, then times out."""
    walks: list[int | None] = []

    def walk_ending(stop_reason: str, complete: bool):
        async def walk(on_batch, **kwargs):
            walks.append(kwargs["stop_after_known"])
            on_batch([PageConnection(f"p{len(walks)}", "P", None, None, None)])
            return WalkResult(seen=1, stop_reason=stop_reason, complete=complete)

        return walk

    extractor = MagicMock()
    with patch(
        "linkedin_mcp_server.tools.person.get_ready_extractor",
        AsyncMock(return_value=extractor),
    ):
        extractor.walk_my_connections = AsyncMock(
            side_effect=walk_ending("time_budget", False)
        )
        first = await call(mcp, "sync_my_connections", {})
        second = await call(mcp, "sync_my_connections", {})
        extractor.walk_my_connections = AsyncMock(
            side_effect=walk_ending("end_of_list", True)
        )
        third = await call(mcp, "sync_my_connections", {})
        fourth = await call(mcp, "sync_my_connections", {})

    # Both walks after the cut-short one keep going past known connections;
    # only once one reached the end may the next stop at them.
    assert walks == [None, None, None, 25]
    assert [r["incremental"] for r in (first, second, third, fourth)] == [
        False,
        False,
        False,
        True,
    ]
