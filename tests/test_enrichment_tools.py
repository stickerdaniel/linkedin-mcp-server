"""Tests for the paced bulk-enrichment tools.

The pacing arithmetic is covered in test_pacing.py; these cover the browser
loop around it -- that it stops for the right reasons, persists as it goes,
and never silently drops a queued profile.
"""

import asyncio
import logging
import time
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from patchright._impl._errors import TargetClosedError
from patchright.async_api import Error as PatchrightError

from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    RateLimitError,
)
from linkedin_mcp_server.exceptions import BrowserBusyError
from linkedin_mcp_server.pacing import (
    ACCOUNT_BUDGET_JOB,
    Job,
    JobStore,
    Ledger,
    Schedule,
    request_arrived_at,
)
from linkedin_mcp_server.tools.enrichment import (
    RETRY_AFTER_QUEUED_OUT,
    _normalize,
    register_enrichment_tools,
)

from test_tools import get_tool_fn

# Always open: no weekend, no lunch, so bunch tests never depend on the wall
# clock (a default 9-18 schedule with a noon lunch made these flaky).
OPEN_ALL = Schedule(work_start=0, work_end=24, days_off=())

WED_10AM = datetime(2026, 8, 5, 10, 0)


def _seed_budget(store, *, cap=100, ledger=None, schedule=None, warmup=False):
    """Seed the shared account budget the tools gate on.

    The budget now owns the schedule/cap/ledger (not the per-job queue), so
    tests configure account-wide pacing here.
    """
    store.save(
        Job(
            name=ACCOUNT_BUDGET_JOB,
            started_on=date(2020, 1, 1),
            warmup=warmup,
            daily_cap=cap,
            schedule=schedule or OPEN_ALL,
            ledger=ledger or Ledger(),
        )
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the tools at a temporary job directory."""
    import linkedin_mcp_server.tools.enrichment as enrichment

    real_store = JobStore(tmp_path / "jobs")
    monkeypatch.setattr(enrichment, "JobStore", lambda *a, **k: real_store)
    # An always-open account budget by default, so bunch tests don't depend on
    # the wall clock; individual tests overwrite it to exercise limits.
    _seed_budget(real_store)
    return real_store


@pytest.fixture
def mcp(store):
    from linkedin_mcp_server.tools.enrichment import register_enrichment_tools

    server = FastMCP("test")
    register_enrichment_tools(server)
    return server


CLOSED_TARGET = "Target page, context or browser has been closed"

# The incident shape: scrape_person swallowed a dead browser into
# section_errors for every section and returned with nothing loaded.
NOTHING_LOADED = {
    "url": "x",
    "sections": {},
    "section_errors": {
        "main_profile": {"error_type": "scraping", "error_message": CLOSED_TARGET}
    },
}


def _dead_extractor(failure):
    """An extractor whose every scrape says the browser is gone, in the
    given shape: the swallowed result or a raised error."""
    if isinstance(failure, Exception):
        return _extractor(error=failure)
    return _extractor(result=failure)


@pytest.fixture(
    params=[
        NOTHING_LOADED,
        TargetClosedError(CLOSED_TARGET),
        PatchrightError(CLOSED_TARGET),
    ],
    ids=["empty-result", "TargetClosedError", "Error-with-closed-message"],
)
def dead(request):
    return _dead_extractor(request.param)


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

    async def test_reserved_budget_name_is_refused(self, mcp):
        # A user job named like the internal budget record would overwrite it.
        fn = await get_tool_fn(mcp, "start_enrichment_job")
        with pytest.raises(ToolError, match="reserved"):
            await fn(ACCOUNT_BUDGET_JOB, ["a"])


class TestRunBunch:
    async def _seed(self, mcp, store, usernames):
        """Seed just the per-job queue; pacing lives on the account budget."""
        job = Job(name="j", started_on=date(2020, 1, 1), pending=list(usernames))
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
        # A budget schedule that is shut at the frozen 20:00 test instant.
        await self._seed(mcp, store, ["a"])
        _seed_budget(store, schedule=Schedule(work_start=3, work_end=4))
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
        await self._seed(mcp, store, ["a"])
        _seed_budget(store, schedule=Schedule(work_start=3, work_end=4))
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
        await self._seed(mcp, store, ["a"])
        # The shared account budget is fully spent, so no queue can run.
        _seed_budget(store, cap=5, ledger=Ledger(actions=[now.timestamp()] * 5))

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn("j", mock_context, extractor=_extractor())

        assert out["stopped_because"] == "daily_budget_spent"
        assert out["next_run_after_seconds"] > 0
        assert store.load("j").pending == ["a"]

    async def test_a_rate_limit_keeps_the_profile_queued(
        self, mcp, store, mock_context
    ):
        """The page was never read, so the queue entry must survive."""
        await self._seed(mcp, store, ["a", "b"])

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
        # Unread, but requested: the throttled load counts against the cap.
        assert out["account_spent_last_24h"] == 1

    async def test_a_session_expiry_keeps_the_profile_queued(
        self, mcp, store, mock_context
    ):
        """An expired session must not drain the queue into `failed`.

        AuthenticationError is a sibling of RateLimitError, not a subclass, so
        without explicit handling it would fall through to the generic handler
        and permanently fail every unread profile in the bunch.
        """
        await self._seed(mcp, store, ["a", "b"])

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn(
            "j",
            mock_context,
            extractor=_extractor(error=AuthenticationError("session expired")),
        )

        assert out["stopped_because"] == "session_expired"
        assert store.load("j").pending == ["a", "b"]  # nothing consumed
        assert store.load("j").failed == {}  # nothing wrongly failed

    async def test_extra_sections_do_not_overshoot_the_budget(
        self, mcp, store, mock_context, monkeypatch
    ):
        """Planning must be in page-loads, not profiles: a bunch of 5 with two
        extra sections (cost 3 each) and only 4 budget must run ONE profile,
        not five (which would spend 15)."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.step_delay", lambda **k: 0
        )
        now = datetime.now().astimezone()
        await self._seed(mcp, store, ["a", "b", "c", "d", "e"])
        _seed_budget(store, cap=4)  # 4 page-loads available
        assert now  # silence unused

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn(
            "j",
            mock_context,
            bunch_size=5,
            sections="experience,contact_info",  # cost = 3
            extractor=_extractor(),
        )

        assert out["done"] == 1
        assert out["account_spent_last_24h"] == 3  # not 15

    async def test_budget_below_one_profile_cost_stops_cleanly(
        self, mcp, store, mock_context
    ):
        """With less budget than a single profile costs, stop -- do not spin
        planning zero profiles forever."""
        now = datetime.now().astimezone()
        await self._seed(mcp, store, ["a"])
        _seed_budget(store, cap=5, ledger=Ledger(actions=[now.timestamp()] * 3))

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn(
            "j",
            mock_context,
            sections="experience,contact_info",  # cost 3 > remaining 2
            extractor=_extractor(),
        )

        assert out["stopped_because"] == "daily_budget_spent"
        assert out["done"] == 0
        assert store.load("j").pending == ["a"]

    async def test_a_scrape_failure_moves_on_without_blocking_the_queue(
        self, mcp, store, mock_context, monkeypatch
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.step_delay", lambda **k: 0
        )
        await self._seed(mcp, store, ["a", "b"])

        extractor = MagicMock()
        extractor.scrape_person = AsyncMock(
            side_effect=[
                ValueError("bad profile"),
                {"url": "y", "sections": {"main_profile": "Bob"}},
            ]
        )

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn("j", mock_context, bunch_size=2, extractor=extractor)

        saved = store.load("j")
        assert "a" in saved.failed
        assert "b" in saved.done
        assert saved.pending == []
        assert out["failed"] == 1

    async def test_a_dead_browser_is_relaunched_and_the_profile_retried_once(
        self, mcp, store, mock_context, monkeypatch, caplog, dead
    ):
        """Measured: the daemon's Chrome died mid-run, scrape_person filed
        every section as an error, and the loop marked 8 profiles done and
        charged 16 actions for zero LinkedIn traffic. Nothing loaded means
        nothing to charge; the browser is re-acquired (which relaunches it)
        and the same profile is retried once, at its normal cost."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.step_delay", lambda **k: 0
        )
        relaunched = _extractor()
        relaunch = AsyncMock(return_value=relaunched)
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.get_ready_extractor", relaunch
        )
        await self._seed(mcp, store, ["a", "b"])

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        with caplog.at_level(logging.WARNING):
            out = await fn("j", mock_context, bunch_size=2, extractor=dead)

        assert "browser gone under a; relaunching and retrying once" in caplog.text
        relaunch.assert_awaited_once()
        assert out["stopped_because"] == "queue_empty"
        assert out["account_spent_last_24h"] == 2  # a once, b once; no 2x
        assert set(store.load("j").done) == {"a", "b"}
        assert dead.scrape_person.await_count == 1
        # The retry of "a" and all of "b" went to the relaunched browser.
        assert [c.args[0] for c in relaunched.scrape_person.await_args_list] == [
            "a",
            "b",
        ]

    async def test_a_browser_still_dead_after_relaunch_stops_the_bunch(
        self, mcp, store, mock_context, monkeypatch, dead
    ):
        """The retry failing the same way is the stop: the profile stays
        pending (a dead browser says nothing about it, so not `failed`),
        nothing is charged, and the next profile is not attempted."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.step_delay", lambda **k: 0
        )
        relaunch = AsyncMock(return_value=dead)
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.get_ready_extractor", relaunch
        )
        await self._seed(mcp, store, ["a", "b"])

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn("j", mock_context, bunch_size=2, extractor=dead)

        assert out["stopped_because"] == "browser_unavailable"
        assert out["account_spent_last_24h"] == 0
        saved = store.load("j")
        assert saved.pending == ["a", "b"]
        assert saved.done == {}
        assert saved.failed == {}
        relaunch.assert_awaited_once()  # one relaunch, one retry, then stop
        assert [c.args[0] for c in dead.scrape_person.await_args_list] == ["a", "a"]
        if isinstance(dead.scrape_person.side_effect, Exception):
            assert "section_errors" not in out
        else:
            assert out["section_errors"] == NOTHING_LOADED["section_errors"]

    async def test_a_failed_relaunch_leaves_the_profile_pending_and_uncharged(
        self, mcp, store, mock_context, monkeypatch
    ):
        """get_ready_extractor turns a browser that will not start into the
        client-facing ToolError; that must not fall into the generic handler
        and file the profile as failed."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.get_ready_extractor",
            AsyncMock(side_effect=ToolError("browser would not start")),
        )
        await self._seed(mcp, store, ["a"])

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        with pytest.raises(ToolError, match="would not start"):
            await fn("j", mock_context, extractor=_extractor(result=NOTHING_LOADED))

        saved = store.load("j")
        assert saved.pending == ["a"]
        assert saved.failed == {}
        now = datetime.now().astimezone()
        assert store.load(ACCOUNT_BUDGET_JOB).ledger.spent(now) == 0

    @pytest.mark.parametrize(
        ("failure", "expected"),
        [
            (BrowserBusyError("profile held"), ToolError),
            (RuntimeError("profile held"), RuntimeError),
        ],
        ids=["LinkedInMCPError", "raw"],
    )
    async def test_a_relaunch_failure_of_any_kind_does_not_drain_the_queue(
        self, mcp, store, mock_context, monkeypatch, failure, expected
    ):
        """Reproduced: get_ready_extractor raised BrowserBusyError, which is
        not a ToolError, so it fell into the generic handler: the profile was
        filed as failed, the next one was scraped on the same dead extractor,
        the relaunch failed again, and a 3-profile queue drained into
        `failed`. Whatever the relaunch raises is the client-facing error
        (shaped by raise_tool_error, or re-raised raw for masking); the queue
        and the ledger are untouched and no second relaunch is attempted."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.step_delay", lambda **k: 0
        )
        relaunch = AsyncMock(side_effect=failure)
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.get_ready_extractor", relaunch
        )
        await self._seed(mcp, store, ["a", "b", "c"])
        dead = _extractor(result=NOTHING_LOADED)

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        with pytest.raises(expected, match="profile held"):
            await fn("j", mock_context, bunch_size=3, extractor=dead)

        relaunch.assert_awaited_once()
        assert dead.scrape_person.await_count == 1
        saved = store.load("j")
        assert saved.pending == ["a", "b", "c"]
        assert saved.failed == {}
        assert saved.done == {}
        now = datetime.now().astimezone()
        assert store.load(ACCOUNT_BUDGET_JOB).ledger.spent(now) == 0

    async def test_nothing_loaded_without_a_closed_target_is_done_and_charged(
        self, mcp, store, mock_context, monkeypatch
    ):
        """error_type is the exception's class name, so a navigation timeout
        files the same empty shape as a dead browser. It is not one: the
        page was asked for and the navigation happened, so the profile is
        done with its section_errors, charged, and the queue advances --
        rather than relaunching a live browser and pinning the profile at
        pending[0] for every later call to retry."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.step_delay", lambda **k: 0
        )
        relaunch = AsyncMock()
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.get_ready_extractor", relaunch
        )
        await self._seed(mcp, store, ["a", "b"])
        timed_out = {
            "url": "x",
            "sections": {},
            "section_errors": {
                "main_profile": {
                    "error_type": "TimeoutError",
                    "error_message": "Timeout 30000ms exceeded.",
                }
            },
        }
        extractor = _extractor(result=timed_out)

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn("j", mock_context, bunch_size=2, extractor=extractor)

        relaunch.assert_not_awaited()
        assert out["stopped_because"] == "queue_empty"
        assert out["account_spent_last_24h"] == 2
        saved = store.load("j")
        assert saved.pending == []
        assert saved.failed == {}
        assert saved.done["a"]["section_errors"] == timed_out["section_errors"]
        assert [c.args[0] for c in extractor.scrape_person.await_args_list] == [
            "a",
            "b",
        ]

    async def test_a_partial_result_is_done_and_charged_in_full(
        self, mcp, store, mock_context
    ):
        """One section loaded and one failed is a profile that was visited:
        every navigation happened, so the full cost is charged."""
        await self._seed(mcp, store, ["a"])
        extractor = _extractor(
            result={
                "url": "x",
                "sections": {"main_profile": "Jane"},
                "section_errors": {
                    "experience": {"error_type": "scraping", "error_message": "x"}
                },
            }
        )

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn("j", mock_context, sections="experience", extractor=extractor)

        assert out["stopped_because"] == "queue_empty"
        assert out["account_spent_last_24h"] == 2
        assert "a" in store.load("j").done

    async def test_a_soft_rate_limit_with_nothing_loaded_is_a_rate_limit(
        self, mcp, store, mock_context
    ):
        """scrape_person files a soft rate limit under section_errors rather
        than raising. With nothing else loaded that is the whole answer, and
        it must read as a rate limit, not as a dead browser."""
        await self._seed(mcp, store, ["a", "b"])
        extractor = _extractor(
            result={
                "url": "x",
                "sections": {},
                "section_errors": {
                    "main_profile": {
                        "error_type": "rate_limit",
                        "error_message": "throttled",
                    }
                },
            }
        )

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn("j", mock_context, extractor=extractor)

        assert out["stopped_because"] == "rate_limited"
        assert store.load("j").pending == ["a", "b"]

    async def test_a_profile_that_empties_twice_is_struck_out(
        self, mcp, store, mock_context, monkeypatch
    ):
        """A deleted or private URL comes back as the same empty shell a
        throttle does, on every call. The first is read as a rate limit and
        stays queued; the second strikes it out so the queue moves on."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.step_delay", lambda **k: 0
        )
        await self._seed(mcp, store, ["a", "b"])
        empty = {
            "url": "x",
            "sections": {},
            "section_errors": {
                "main_profile": {"error_type": "rate_limit", "error_message": "x"}
            },
        }
        loaded = {"url": "x", "sections": {"main_profile": "Jane"}}
        extractor = MagicMock()
        extractor.scrape_person = AsyncMock(
            side_effect=lambda username, *a, **k: empty if username == "a" else loaded
        )

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        first = await fn("j", mock_context, extractor=extractor)

        assert first["stopped_because"] == "rate_limited"
        assert store.load("j").pending == ["a", "b"]
        assert store.load("j").strikes == {"a": 1}

        second = await fn("j", mock_context, extractor=extractor)

        assert second["stopped_because"] == "queue_empty"
        assert store.load("j").pending == []
        assert "empty page on 2 consecutive visits" in store.load("j").failed["a"]
        assert store.load("j").strikes == {}
        assert "b" in store.load("j").done
        # The striking visit of "a" and the load of "b"; the first empty
        # visit of "a" was not charged.
        assert second["account_spent_last_24h"] == 2

    async def test_a_strike_out_followed_by_another_empty_page_is_undone(
        self, mcp, store, mock_context, monkeypatch
    ):
        """When the profile visited right after a strike-out empties too, the
        session is throttled, not the profile gone: the struck username goes
        back to the front of the queue and the call backs off."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.step_delay", lambda **k: 0
        )
        await self._seed(mcp, store, ["a", "b"])
        extractor = _extractor(
            result={
                "url": "x",
                "sections": {},
                "section_errors": {
                    "main_profile": {"error_type": "rate_limit", "error_message": "x"}
                },
            }
        )

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        first = await fn("j", mock_context, extractor=extractor)

        assert first["stopped_because"] == "rate_limited"
        assert store.load("j").strikes == {"a": 1}

        second = await fn("j", mock_context, extractor=extractor)

        assert second["stopped_because"] == "rate_limited"
        assert store.load("j").pending == ["a", "b"]
        assert store.load("j").failed == {}
        assert store.load("j").strikes == {"a": 2, "b": 1}
        # The strike-out charged a real page load; undoing it does not refund.
        assert second["account_spent_last_24h"] == 1

    async def test_a_strike_is_cleared_when_the_profile_fails_outright(
        self, mcp, store, mock_context
    ):
        await self._seed(mcp, store, ["a"])
        empty = {
            "url": "x",
            "sections": {},
            "section_errors": {
                "main_profile": {"error_type": "rate_limit", "error_message": "x"}
            },
        }
        extractor = MagicMock()
        extractor.scrape_person = AsyncMock(side_effect=[empty, ValueError("boom")])

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        await fn("j", mock_context, extractor=extractor)
        assert store.load("j").strikes == {"a": 1}

        await fn("j", mock_context, extractor=extractor)

        assert store.load("j").failed == {"a": "boom"}
        assert store.load("j").strikes == {}

    async def test_a_hard_rate_limit_is_never_struck_out(
        self, mcp, store, mock_context
    ):
        """An HTTP 429 or a checkpoint challenge the extractor raises is a
        real limit, not an ambiguous empty shell: under sustained pressure it
        must keep backing off, never drain the queue into `failed`."""
        await self._seed(mcp, store, ["a", "b"])
        extractor = _extractor(error=RateLimitError("HTTP 429"))

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        for _ in range(2):
            out = await fn("j", mock_context, extractor=extractor)

            assert out["stopped_because"] == "rate_limited"
            assert store.load("j").pending == ["a", "b"]
            assert store.load("j").failed == {}
            assert store.load("j").strikes == {}

    async def test_a_strike_is_cleared_once_the_profile_loads(
        self, mcp, store, mock_context
    ):
        await self._seed(mcp, store, ["a"])
        empty = {
            "url": "x",
            "sections": {},
            "section_errors": {
                "main_profile": {"error_type": "rate_limit", "error_message": "x"}
            },
        }
        loaded = {"url": "x", "sections": {"main_profile": "Jane"}}
        extractor = MagicMock()
        extractor.scrape_person = AsyncMock(side_effect=[empty, loaded])

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        await fn("j", mock_context, extractor=extractor)
        assert store.load("j").strikes == {"a": 1}

        out = await fn("j", mock_context, extractor=extractor)

        assert out["stopped_because"] == "queue_empty"
        assert "a" in store.load("j").done
        assert store.load("j").strikes == {}

    async def test_extra_sections_each_cost_budget(
        self, mcp, store, mock_context, monkeypatch
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.step_delay", lambda **k: 0
        )
        await self._seed(mcp, store, ["a"])

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn(
            "j",
            mock_context,
            sections="experience,contact_info",
            extractor=_extractor(),
        )

        # One main profile plus two extra section pages.
        assert out["account_spent_last_24h"] == 3

    async def test_empty_queue_reports_completion(self, mcp, store, mock_context):
        await self._seed(mcp, store, [])

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        out = await fn("j", mock_context, extractor=_extractor())

        assert out["stopped_because"] == "queue_empty"
        assert "next_run_after_seconds" not in out

    async def test_a_call_queued_past_its_deadline_loads_nothing(
        self, mcp, store, mock_context, monkeypatch
    ):
        """The incident: queued 200 s behind another session, past the 210 s
        the frontend proxy waits, then run to completion for nobody."""
        await self._seed(mcp, store, ["a", "b"])
        extractor = _extractor()
        arrival = request_arrived_at.set(time.monotonic() - 200)
        try:
            fn = await get_tool_fn(mcp, "run_enrichment_bunch")
            out = await fn("j", mock_context, extractor=extractor)
        finally:
            request_arrived_at.reset(arrival)

        extractor.scrape_person.assert_not_awaited()
        assert out["stopped_because"] == "tool_deadline"
        assert out["next_run_after_seconds"] == RETRY_AFTER_QUEUED_OUT
        assert store.load("j").pending == ["a", "b"]
        assert store.load(ACCOUNT_BUDGET_JOB).ledger.spent(datetime.now()) == 0

    async def test_time_spent_queued_shortens_the_deadline(
        self, mcp, store, mock_context, monkeypatch
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.step_delay", lambda **k: 0
        )
        await self._seed(mcp, store, ["a", "b"])
        extractor = _extractor()
        page = extractor.scrape_person.return_value

        async def slow_scrape(username, sections, callbacks=None):
            await asyncio.sleep(0.3)
            return page

        extractor.scrape_person = AsyncMock(side_effect=slow_scrape)
        server = FastMCP("test")
        # 75% of 13.6 s is 10.2 s from arrival; 10 s of that already went by
        # in the queue, so one 0.3 s profile is all there is time for.
        register_enrichment_tools(server, tool_timeout=13.6)

        arrival = request_arrived_at.set(time.monotonic() - 10)
        try:
            fn = await get_tool_fn(server, "run_enrichment_bunch")
            out = await fn("j", mock_context, extractor=extractor)
        finally:
            request_arrived_at.reset(arrival)

        assert out["stopped_because"] == "tool_deadline"
        assert out["done"] == 1
        assert store.load("j").pending == ["b"]


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

    async def test_the_budget_record_cannot_be_read_as_a_job(
        self, mcp, store, mock_context
    ):
        # Listing hides the budget record; naming it must not load it either,
        # or its action history and pacing settings leak out as "results".
        assert store.exists(ACCOUNT_BUDGET_JOB)

        status = await get_tool_fn(mcp, "get_enrichment_status")
        with pytest.raises(ToolError, match="reserved"):
            await status(ACCOUNT_BUDGET_JOB)

        run = await get_tool_fn(mcp, "run_enrichment_bunch")
        with pytest.raises(ToolError, match="reserved"):
            await run(ACCOUNT_BUDGET_JOB, mock_context, extractor=MagicMock())


class TestConfigurableLimits:
    """The tool's daily-cap and bunch-size bounds follow the environment.

    Pydantic ``Field(le=...)`` bounds are fixed at import, so the ceilings are
    applied at call time instead; these prove the environment reaches them.
    """

    async def test_daily_cap_above_the_default_ceiling_is_honoured_when_raised(
        self, mcp, store, monkeypatch
    ):
        monkeypatch.setenv(EnvironmentKeys.DAILY_ACTIONS_MAX, "200")
        monkeypatch.setenv(EnvironmentKeys.DAILY_CAP_JITTER, "0")
        fn = await get_tool_fn(mcp, "start_enrichment_job")
        out = await fn("j", ["a"], daily_cap=200, warmup=False)

        assert store.load(ACCOUNT_BUDGET_JOB).daily_cap == 200
        assert out["account_daily_cap_today"] == 200

    async def test_daily_cap_above_the_ceiling_is_clamped_not_rejected(
        self, mcp, store, caplog
    ):
        fn = await get_tool_fn(mcp, "start_enrichment_job")
        with caplog.at_level(logging.INFO):
            out = await fn("j", ["a"], daily_cap=200, warmup=False)

        assert store.load(ACCOUNT_BUDGET_JOB).daily_cap == 150
        assert out["account_daily_cap_today"] <= 150
        assert any("Clamping daily_cap=200" in r.getMessage() for r in caplog.records)

    async def test_daily_cap_garbage_ceiling_falls_back_with_a_warning(
        self, mcp, store, monkeypatch, caplog
    ):
        monkeypatch.setenv(EnvironmentKeys.DAILY_ACTIONS_MAX, "lots")
        fn = await get_tool_fn(mcp, "start_enrichment_job")
        with caplog.at_level(logging.WARNING):
            await fn("j", ["a"], daily_cap=200, warmup=False)

        assert store.load(ACCOUNT_BUDGET_JOB).daily_cap == 150
        assert any(
            EnvironmentKeys.DAILY_ACTIONS_MAX in r.getMessage() for r in caplog.records
        )

    async def test_omitted_daily_cap_takes_the_configured_default(
        self, mcp, store, monkeypatch
    ):
        monkeypatch.setenv(EnvironmentKeys.DAILY_ACTIONS_DEFAULT, "40")
        fn = await get_tool_fn(mcp, "start_enrichment_job")
        await fn("j", ["a"], warmup=False)

        assert store.load(ACCOUNT_BUDGET_JOB).daily_cap == 40

    async def test_omitted_daily_cap_garbage_default_falls_back_with_a_warning(
        self, mcp, store, monkeypatch, caplog
    ):
        monkeypatch.setenv(EnvironmentKeys.DAILY_ACTIONS_DEFAULT, "-3")
        fn = await get_tool_fn(mcp, "start_enrichment_job")
        with caplog.at_level(logging.WARNING):
            await fn("j", ["a"], warmup=False)

        assert store.load(ACCOUNT_BUDGET_JOB).daily_cap == 100
        assert any(
            EnvironmentKeys.DAILY_ACTIONS_DEFAULT in r.getMessage()
            for r in caplog.records
        )

    async def test_bunch_size_is_clamped_to_the_configured_ceiling(
        self, mcp, store, mock_context, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.step_delay", lambda **k: 0
        )
        monkeypatch.setenv(EnvironmentKeys.BUNCH_SIZE_MAX, "2")
        store.save(Job(name="j", started_on=date(2020, 1, 1), pending=["a", "b", "c"]))

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        with caplog.at_level(logging.INFO):
            out = await fn("j", mock_context, bunch_size=5, extractor=_extractor())

        assert out["done"] == 2
        assert any("Clamping bunch_size=5" in r.getMessage() for r in caplog.records)

    async def test_bunch_size_garbage_ceiling_falls_back_with_a_warning(
        self, mcp, store, mock_context, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.enrichment.step_delay", lambda **k: 0
        )
        monkeypatch.setenv(EnvironmentKeys.BUNCH_SIZE_MAX, "2.5")
        store.save(
            Job(
                name="j",
                started_on=date(2020, 1, 1),
                pending=list("abcdefghijklmnopqrstuvwxyz0"),
            )
        )

        fn = await get_tool_fn(mcp, "run_enrichment_bunch")
        with caplog.at_level(logging.WARNING):
            out = await fn("j", mock_context, bunch_size=27, extractor=_extractor())

        assert out["done"] == 25
        assert any(
            EnvironmentKeys.BUNCH_SIZE_MAX in r.getMessage() for r in caplog.records
        )


class _FrozenDatetime:
    """Stand-in for the datetime module attribute, pinned to one instant."""

    def __init__(self, when: datetime):
        self._when = when

    def now(self, tz=None):
        return self._when

    def __getattr__(self, name):
        return getattr(datetime, name)
