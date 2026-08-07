"""Tests for the paced bulk-enrichment tools.

The pacing arithmetic is covered in test_pacing.py; these cover the browser
loop around it -- that it stops for the right reasons, persists as it goes,
and never silently drops a queued profile.
"""

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.core.exceptions import RateLimitError
from linkedin_mcp_server.pacing import Job, JobStore, Ledger, Schedule
from linkedin_mcp_server.tools.enrichment import _normalize

from test_tools import get_tool_fn

# Always open: no weekend, no lunch, so bunch tests never depend on the wall
# clock (a default 9-18 schedule with a noon lunch made these flaky).
OPEN_ALL = Schedule(
    work_start=0, work_end=23, days_off=(), lunch_start=None, lunch_end=None
)

WED_10AM = datetime(2026, 8, 5, 10, 0)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the tools at a temporary job directory."""
    import linkedin_mcp_server.tools.enrichment as enrichment

    real_store = JobStore(tmp_path / "jobs")
    monkeypatch.setattr(enrichment, "JobStore", lambda *a, **k: real_store)
    return real_store


@pytest.fixture
def mcp(store):
    from linkedin_mcp_server.tools.enrichment import register_enrichment_tools

    server = FastMCP("test")
    register_enrichment_tools(server)
    return server


def _extractor(result=None, error=None):
    mock = MagicMock()
    if error is not None:
        mock.scrape_person = AsyncMock(side_effect=error)
    else:
        mock.scrape_person = AsyncMock(
            return_value=result or {"url": "x", "sections": {"main_profile": "Jane"}}
        )
    return mock


class TestNormalize:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("jane-doe", "jane-doe"),
            ("https://www.linkedin.com/in/jane-doe/", "jane-doe"),
            ("https://www.linkedin.com/in/jane-doe", "jane-doe"),
            ("linkedin.com/in/jane-doe/?trk=abc", "jane-doe"),
            ("  jane-doe  ", "jane-doe"),
        ],
    )
    def test_reduces_to_a_bare_username(self, raw, expected):
        assert _normalize(raw) == expected


class TestStartJob:
    async def test_creates_a_queue_without_visiting_anything(self, mcp, store):
        fn = await get_tool_fn(mcp, "start_enrichment_job")
        out = await fn("egypt-gulf", ["a", "https://linkedin.com/in/b/"])

        assert out["added"] == 2
        assert store.load("egypt-gulf").pending == ["a", "b"]

    async def test_deduplicates_within_a_single_call(self, mcp):
        fn = await get_tool_fn(mcp, "start_enrichment_job")
        out = await fn("j", ["a", "a", "https://linkedin.com/in/a/"])

        assert out["added"] == 1
        assert out["duplicates_dropped"] == 2

    async def test_resuming_appends_only_unseen_people(self, mcp, store):
        fn = await get_tool_fn(mcp, "start_enrichment_job")
        await fn("j", ["a", "b"])

        job = store.load("j")
        job.done["a"] = {"url": "x"}
        job.pending = ["b"]
        store.save(job)

        out = await fn("j", ["a", "b", "c"])
        # 'a' is already done and 'b' still queued, so only 'c' is new.
        assert out["added"] == 1
        assert store.load("j").pending == ["b", "c"]

    async def test_replace_existing_starts_over(self, mcp, store):
        fn = await get_tool_fn(mcp, "start_enrichment_job")
        await fn("j", ["a", "b"])
        await fn("j", ["c"], replace_existing=True)

        assert store.load("j").pending == ["c"]

    async def test_empty_input_is_rejected(self, mcp):
        fn = await get_tool_fn(mcp, "start_enrichment_job")
        with pytest.raises(ToolError, match="No usable usernames"):
            await fn("j", ["", "   ", "/"])


class TestRunBunch:
    async def _seed(self, mcp, store, usernames, **kw):
        kw.setdefault("schedule", OPEN_ALL)
        job = Job(
            name="j",
            started_on=kw.pop("started_on", date(2020, 1, 1)),
            pending=list(usernames),
            warmup=kw.pop("warmup", False),
            **kw,
        )
        store.save(job)
        return job

    async def test_missing_job_is_a_clear_error(self, mcp, mock_context):
        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        with pytest.raises(ToolError, match="No job named"):
            await fn("nope", mock_context)

    async def test_processes_a_bunch_and_persists_each_profile(
        self, mcp, store, mock_context, monkeypatch
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.step_delay", lambda **k: 0
        )
        await self._seed(mcp, store, ["a", "b", "c", "d"])

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn("j", mock_context, bunch_size=2, extractor=_extractor())

        assert out["done"] == 2
        assert out["pending"] == 2
        assert set(out["gathered"]) == {"a", "b"}
        # Persisted, not just returned.
        assert store.load("j").pending == ["c", "d"]

    async def test_reports_when_the_window_is_shut(
        self, mcp, store, mock_context, monkeypatch
    ):
        # Force a schedule that is never open on the current real date by
        # marking every weekday off except one that we then also exclude.
        await self._seed(mcp, store, ["a"], schedule=Schedule(work_start=3, work_end=4))
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.datetime",
            _FrozenDatetime(datetime(2026, 8, 5, 20, 0).astimezone()),
        )

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn("j", mock_context, extractor=_extractor())

        assert out["stopped_because"] == "outside_working_hours"
        assert out["next_run_after_seconds"] > 0
        # Nothing was visited.
        assert store.load("j").pending == ["a"]

    async def test_ignore_schedule_overrides_the_window(
        self, mcp, store, mock_context, monkeypatch
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.step_delay", lambda **k: 0
        )
        await self._seed(mcp, store, ["a"], schedule=Schedule(work_start=3, work_end=4))
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.datetime",
            _FrozenDatetime(datetime(2026, 8, 5, 20, 0).astimezone()),
        )

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn("j", mock_context, ignore_schedule=True, extractor=_extractor())

        assert out["done"] == 1

    async def test_stops_when_the_rolling_budget_is_spent(
        self, mcp, store, mock_context
    ):
        now = datetime.now().astimezone()
        job = Job(
            name="j",
            started_on=date(2020, 1, 1),
            pending=["a"],
            warmup=False,
            daily_cap=5,
            schedule=OPEN_ALL,
            ledger=Ledger(actions=[now.timestamp()] * 5),
        )
        store.save(job)

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn("j", mock_context, extractor=_extractor())

        assert out["stopped_because"] == "daily_budget_spent"
        assert out["next_run_after_seconds"] > 0
        assert store.load("j").pending == ["a"]

    async def test_a_rate_limit_keeps_the_profile_queued(
        self, mcp, store, mock_context
    ):
        """The page was never read, so the queue entry must survive."""
        await self._seed(
            mcp,
            store,
            ["a", "b"],
            schedule=OPEN_ALL,
        )

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn(
            "j",
            mock_context,
            extractor=_extractor(error=RateLimitError("throttled")),
        )

        assert out["stopped_because"] == "rate_limited"
        assert out["next_run_after_seconds"] >= 3600
        assert store.load("j").pending == ["a", "b"]
        assert store.load("j").failed == {}

    async def test_a_scrape_failure_moves_on_without_blocking_the_queue(
        self, mcp, store, mock_context, monkeypatch
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.step_delay", lambda **k: 0
        )
        await self._seed(
            mcp,
            store,
            ["a", "b"],
            schedule=OPEN_ALL,
        )

        extractor = MagicMock()
        extractor.scrape_person = AsyncMock(
            side_effect=[ValueError("bad profile"), {"url": "y", "sections": {}}]
        )

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn("j", mock_context, bunch_size=2, extractor=extractor)

        saved = store.load("j")
        assert "a" in saved.failed
        assert "b" in saved.done
        assert saved.pending == []
        assert out["failed"] == 1

    async def test_extra_sections_each_cost_budget(
        self, mcp, store, mock_context, monkeypatch
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.step_delay", lambda **k: 0
        )
        await self._seed(
            mcp,
            store,
            ["a"],
            schedule=OPEN_ALL,
        )

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn(
            "j",
            mock_context,
            sections="experience,contact_info",
            extractor=_extractor(),
        )

        # One main profile plus two extra section pages.
        assert out["spent_last_24h"] == 3

    async def test_empty_queue_reports_completion(self, mcp, store, mock_context):
        await self._seed(
            mcp,
            store,
            [],
            schedule=OPEN_ALL,
        )

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn("j", mock_context, extractor=_extractor())

        assert out["stopped_because"] == "queue_empty"
        assert "next_run_after_seconds" not in out


class TestStatus:
    async def test_lists_jobs_when_unnamed(self, mcp, store):
        store.save(Job(name="one", started_on=date(2026, 8, 5)))
        store.save(Job(name="two", started_on=date(2026, 8, 5)))

        fn = await get_tool_fn(mcp, "get_enrichment_status")
        assert (await fn())["jobs"] == ["one", "two"]

    async def test_returns_results_for_a_named_job(self, mcp, store):
        job = Job(name="j", started_on=date(2026, 8, 5), pending=["b"])
        job.done["a"] = {"url": "x"}
        job.failed["c"] = "boom"
        store.save(job)

        fn = await get_tool_fn(mcp, "get_enrichment_status")
        out = await fn("j")

        assert out["total"] == 3
        assert out["results"] == {"a": {"url": "x"}}
        assert out["failures"] == {"c": "boom"}

    async def test_unknown_job_is_a_clear_error(self, mcp):
        fn = await get_tool_fn(mcp, "get_enrichment_status")
        with pytest.raises(ToolError, match="No job named"):
            await fn("nope")


class _FrozenDatetime:
    """Stand-in for the datetime module attribute, pinned to one instant."""

    def __init__(self, when: datetime):
        self._when = when

    def now(self, tz=None):
        return self._when

    def __getattr__(self, name):
        return getattr(datetime, name)
