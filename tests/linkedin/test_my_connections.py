"""The connections walk: card parsing and every way a walk stops."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.linkedin import my_connections
from linkedin_mcp_server.linkedin.my_connections import (
    MyConnectionsReader,
    card_to_connection,
    parse_connected_on,
)


def raw(
    slug: str, *, headline: str = "Engineer", date: str = "Connected on May 2, 2022"
):
    return {"slug": slug, "name": slug.title(), "lines": [slug.title(), headline, date]}


class FakeSession:
    """Answers each collect call with the next batch; advance calls answer None."""

    def __init__(self, batches: list[list[dict[str, Any]]], *, step: float = 1.0):
        self._batches = list(batches)
        self._clock = 0.0
        self._step = step
        self.page = MagicMock()
        self.page.wait_for_function = AsyncMock()
        self.check_rate_limit = AsyncMock()
        self.dismiss_modal = AsyncMock(return_value=False)
        self.collect_args: list[list[str]] = []
        self.advances = 0

    def monotonic(self) -> float:
        return self._clock

    async def delay(self, seconds: float) -> None:
        self._clock += self._step

    async def run_on_linkedin(self, script: str, arg: Any = None) -> Any:
        if script is my_connections._ADVANCE_JS:
            self.advances += 1
            return {"anchors": 0, "scrolled": True}
        if script is my_connections._LISTED_TOTAL_JS:
            return 1473
        self.collect_args.append(list(arg))
        return self._batches.pop(0) if self._batches else []


def reader(session: FakeSession) -> MyConnectionsReader:
    navigator = MagicMock()
    navigator._navigate_to_page = AsyncMock()
    return MyConnectionsReader(cast(Any, session), navigator)


@pytest.fixture(autouse=True)
def _no_throttle():
    with patch.object(my_connections, "throttled_count", return_value=0):
        yield


def test_parse_connected_on_reads_english_and_refuses_the_rest():
    assert parse_connected_on("Connected on March 5, 2024") == "2024-03-05"
    assert parse_connected_on("Verbunden am 5. März 2024") is None
    assert parse_connected_on(None) is None


def test_card_without_a_date_line_keeps_the_headline():
    c = card_to_connection({"slug": "x", "name": "X", "lines": ["X", "CTO 2019-2024"]})
    assert c is not None
    assert c.headline == "CTO 2019-2024"
    assert c.connected_text is None


def test_card_in_another_locale_keeps_the_raw_date_text():
    c = card_to_connection(raw("x", date="Verbunden am 5. März 2024"))
    assert c is not None
    assert c.connected_text == "Verbunden am 5. März 2024"
    assert c.connected_on is None
    assert c.headline == "Engineer"


async def test_walk_ends_after_rounds_with_nothing_new():
    session = FakeSession([[raw("a"), raw("b")], [raw("c")]])
    batches: list[list[str]] = []
    result = await reader(session).walk(lambda b: batches.append([c.slug for c in b]))

    assert batches == [["a", "b"], ["c"]]
    assert result.seen == 3
    assert result.stop_reason == "end_of_list"
    assert result.complete
    assert result.listed_total == 1473
    # Each collect is told what was already returned.
    assert session.collect_args[:3] == [[], ["a", "b"], ["a", "b", "c"]]


async def test_incremental_walk_stops_at_a_run_of_known_connections():
    session = FakeSession([[raw("new1"), raw("k1"), raw("k2")], [raw("k3"), raw("k4")]])
    result = await reader(session).walk(
        lambda b: None,
        known={"k1", "k2", "k3", "k4"},
        stop_after_known=3,
    )
    assert result.stop_reason == "reached_known_connections"
    assert result.complete
    assert result.new_slugs == ["new1"]


async def test_an_unknown_connection_resets_the_known_run():
    session = FakeSession([[raw("k1"), raw("n"), raw("k2")], [raw("k3")]])
    result = await reader(session).walk(
        lambda b: None, known={"k1", "k2", "k3"}, stop_after_known=2
    )
    assert result.stop_reason == "reached_known_connections"
    assert result.rounds == 2


async def test_walk_stops_when_linkedin_throttles():
    session = FakeSession([[raw("a")], [raw("b")], [raw("c")]])
    counts = iter([0, 0, 1])
    with patch.object(
        my_connections, "throttled_count", side_effect=lambda: next(counts)
    ):
        result = await reader(session).walk(lambda b: None)
    assert result.throttled
    assert result.stop_reason == "throttled"
    assert not result.complete
    assert result.seen == 2


async def test_walk_respects_its_time_budget():
    session = FakeSession([[raw(f"p{i}")] for i in range(50)], step=10.0)
    result = await reader(session).walk(lambda b: None, time_budget=30.0)
    assert result.stop_reason == "time_budget"
    assert not result.complete
    assert result.seen == 4


async def test_async_batch_callbacks_are_awaited():
    session = FakeSession([[raw("a")]])
    got: list[str] = []

    async def on_batch(batch):
        got.extend(c.slug for c in batch)

    await reader(session).walk(on_batch)
    assert got == ["a"]
