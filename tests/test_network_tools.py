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


@pytest.fixture
def no_pause(monkeypatch):
    import linkedin_mcp_server.tools.person as person_tools

    pauses: list[float] = []

    async def pause(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr(person_tools.asyncio, "sleep", pause)
    return pauses


async def _store_people(mcp, *slugs: str) -> None:
    async def walk(on_batch, **kwargs):
        on_batch([PageConnection(s, s.title(), f"{s} role", None, None) for s in slugs])
        return WalkResult(seen=len(slugs), stop_reason="end_of_list", complete=True)

    extractor = MagicMock()
    extractor.walk_my_connections = AsyncMock(side_effect=walk)
    with patch(
        "linkedin_mcp_server.tools.person.get_ready_extractor",
        AsyncMock(return_value=extractor),
    ):
        await call(mcp, "sync_my_connections", {})


def _reader(errors_for: set[str] | None = None) -> MagicMock:
    async def read_person(slug, requested):
        assert requested == {"experience"}
        if errors_for and slug in errors_for:
            return {
                "sections": {"main_profile": f"{slug} profile"},
                "section_errors": {
                    "experience": {"error_type": "rate_limit", "error_message": "x"}
                },
            }
        return {
            "sections": {
                "main_profile": f"About {slug}: works on fiscal policy",
                "experience": f"{slug} was Economist at Ministry of Finance",
            }
        }

    extractor = MagicMock()
    extractor.read_person = AsyncMock(side_effect=read_person)
    return extractor


async def test_enrich_reads_paces_and_makes_history_searchable(mcp, no_pause):
    await _store_people(mcp, "ada", "alan", "grace")
    extractor = _reader()
    with patch(
        "linkedin_mcp_server.tools.person.get_ready_extractor",
        AsyncMock(return_value=extractor),
    ):
        result = await call(
            mcp,
            "enrich_my_network",
            {"usernames": ["ada", "alan", "nobody"], "limit": 5},
        )

    assert [e["slug"] for e in result["enriched"]] == ["ada", "alan"]
    assert result["skipped"] == [{"slug": "nobody", "reason": "not_in_network"}]
    assert result["stop_reason"] == "done"
    assert len(no_pause) == 1 and no_pause[0] >= 5.0  # a pause between two people

    found = await call(mcp, "search_my_network", {"query": "ministry finance"})
    assert sorted(r["slug"] for r in found["results"]) == ["ada", "alan"]
    person = await call(mcp, "get_network_person", {"username": "ada"})
    assert "Ministry of Finance" in person["details"]["experience_text"]


async def test_enrich_skips_people_already_read_unless_refreshing(mcp, no_pause):
    await _store_people(mcp, "ada", "alan")
    extractor = _reader()
    with patch(
        "linkedin_mcp_server.tools.person.get_ready_extractor",
        AsyncMock(return_value=extractor),
    ):
        await call(mcp, "enrich_my_network", {"usernames": ["ada"]})
        again = await call(mcp, "enrich_my_network", {"usernames": ["ada", "alan"]})
        forced = await call(
            mcp, "enrich_my_network", {"usernames": ["ada"], "refresh": True}
        )
    assert [e["slug"] for e in again["enriched"]] == ["alan"]
    assert again["skipped"] == [{"slug": "ada", "reason": "already_enriched"}]
    assert [e["slug"] for e in forced["enriched"]] == ["ada"]


async def test_enrich_by_query_stops_at_the_limit_and_reports_the_rest(mcp, no_pause):
    await _store_people(mcp, "ada", "alan", "grace")
    with patch(
        "linkedin_mcp_server.tools.person.get_ready_extractor",
        AsyncMock(return_value=_reader()),
    ):
        result = await call(mcp, "enrich_my_network", {"query": "role", "limit": 2})
    assert len(result["enriched"]) == 2
    assert result["stop_reason"] == "limit"
    assert len(result["remaining"]) == 1


async def test_enrich_stops_when_a_profile_is_rate_limited(mcp, no_pause):
    await _store_people(mcp, "ada", "alan", "grace")
    with patch(
        "linkedin_mcp_server.tools.person.get_ready_extractor",
        AsyncMock(return_value=_reader(errors_for={"alan"})),
    ):
        result = await call(
            mcp, "enrich_my_network", {"usernames": ["ada", "alan", "grace"]}
        )
    assert [e["slug"] for e in result["enriched"]] == ["ada", "alan"]
    assert result["stop_reason"] == "throttled"
    assert result["remaining"] == ["grace"]


async def test_enrich_needs_someone_to_read(mcp):
    with pytest.raises(ToolError, match="usernames or a query"):
        await mcp.call_tool("enrich_my_network", {})


async def test_introductions_rank_by_your_marks_and_name_the_path(mcp, no_pause):
    from linkedin_mcp_server.linkedin.introductions import SecondDegreeCard

    await _store_people(mcp, "nicky", "matthias", "ayhan")
    marked = await call(
        mcp, "mark_connections", {"usernames": ["matthias", "ghost"], "closeness": 3}
    )
    assert marked == {"marked": 1, "unknown": ["ghost"]}

    cards = [
        SecondDegreeCard("koen", "Koen", ["nicky"], ["Koen", "Consultant"]),
        SecondDegreeCard("raphael", "Raphaël", ["matthias", "ayhan"], ["Raphaël"]),
        SecondDegreeCard("koen", "Koen", ["nicky"], ["Koen"]),  # repeated on page 2
    ]
    extractor = MagicMock()
    extractor.read_second_degree = AsyncMock(side_effect=[cards[:2], cards[2:], []])
    with patch(
        "linkedin_mcp_server.tools.person.get_ready_extractor",
        AsyncMock(return_value=extractor),
    ):
        result = await call(
            mcp, "find_introductions", {"keywords": "ai governance", "pages": 3}
        )

    urls = [c.args[0] for c in extractor.read_second_degree.await_args_list]
    assert "network=%5B%22S%22%5D" in urls[0] and urls[1].endswith("&page=2")
    first_degree = extractor.read_second_degree.await_args_list[0].args[1]
    assert first_degree == {"nicky", "matthias", "ayhan"}

    assert [r["slug"] for r in result["results"]] == ["raphael", "koen"]
    top = result["results"][0]
    assert top["best_via"] == "matthias"
    assert "you marked closeness 3/3" in top["reasons"]
    assert [i["slug"] for i in top["introducers"]] == ["matthias", "ayhan"]
    assert result["by_introducer"][0]["connects_you_to"] in (["koen"], ["raphael"])


async def test_introductions_with_an_empty_store_say_why_nobody_is_named(mcp):
    from linkedin_mcp_server.linkedin.introductions import SecondDegreeCard

    extractor = MagicMock()
    extractor.read_second_degree = AsyncMock(
        return_value=[SecondDegreeCard("koen", "Koen", [], ["Koen"])]
    )
    with patch(
        "linkedin_mcp_server.tools.person.get_ready_extractor",
        AsyncMock(return_value=extractor),
    ):
        result = await call(mcp, "find_introductions", {"keywords": "ai"})
    assert [r["slug"] for r in result["results"]] == ["koen"]
    assert "network store is empty" in result["note"]


async def test_message_counts_import_without_their_text(mcp, tmp_path):
    await _store_people(mcp, "ada")
    csv = tmp_path / "messages.csv"
    csv.write_text(
        "CONVERSATION ID,FROM,SENDER PROFILE URL,TO,RECIPIENT PROFILE URLS,DATE,CONTENT\n"
        "1,Me,https://www.linkedin.com/in/member-x,Ada,https://www.linkedin.com/in/ada,"
        "2026-10-01 09:00:00 UTC,secret words\n"
        "2,Me,https://www.linkedin.com/in/member-x,Bo,https://www.linkedin.com/in/bo,"
        "2026-10-02 09:00:00 UTC,more secrets\n",
        encoding="utf-8",
    )
    imported = await call(mcp, "import_messages_csv", {"path": str(csv)})
    assert imported["people"] == 2 and imported["in_network"] == 1

    import sqlite3

    db = sqlite3.connect(imported["store"])
    dump = "\n".join(db.iterdump())
    db.close()
    assert "secret" not in dump
