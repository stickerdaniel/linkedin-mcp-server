"""The minimum gap the middleware leaves between two MCP tool calls, and what
it records about a call before it queues it.

Nothing here mocks the sleep or the lock wait away: the point of the change is
elapsed time on the wire, and a test that only watched a code path would still
pass with the spacing removed or the arrival stamped after the wait. The
configured gaps are therefore small, and the assertions are on measured
seconds.
"""

import asyncio
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.pacing import (
    JobStore,
    load_account_budget,
    request_arrived_at,
)
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)

# Small enough that the suite does not crawl, large enough to sit well clear of
# scheduling noise. The jitter is +/-20%, so the floor for a call is 0.8x this.
GAP = 0.5
# How long the arrival tests hold the lock: long enough to sit well clear of
# scheduling noise, short enough that the suite does not crawl.
HOLD = 0.3


def _call_context(tool_name: str = "get_inbox") -> MagicMock:
    context = MagicMock()
    context.message.name = tool_name
    context.fastmcp_context = None
    return context


async def _timed_call(
    middleware: SequentialToolExecutionMiddleware, tool_name: str = "get_inbox"
) -> float:
    started = time.monotonic()
    await middleware.on_call_tool(_call_context(tool_name), AsyncMock())
    return time.monotonic() - started


@pytest.fixture
def paced(monkeypatch):
    monkeypatch.setenv(EnvironmentKeys.TOOL_CALL_GAP_SECONDS, str(GAP))
    return SequentialToolExecutionMiddleware()


@pytest.fixture
def unpaced(monkeypatch):
    """A middleware with the gap off, for tests about the lock rather than it.

    The default gap is 5s; left on, the queued call below would sit it out
    after the hold and the measurement would no longer be about the lock.
    """
    monkeypatch.setenv(EnvironmentKeys.TOOL_CALL_GAP_SECONDS, "0")
    return SequentialToolExecutionMiddleware()


class TestCallsAreSpacedApart:
    """`get_inbox` then `get_conversation` back to back is what drew a 429."""

    async def test_the_second_call_waits_out_the_gap(self, paced):
        first = await _timed_call(paced, "get_inbox")
        second = await _timed_call(paced, "get_conversation")

        assert second >= GAP * 0.8, (
            f"the second call started {second:.3f}s in, so nothing spaced it "
            "from the first"
        )
        assert first < GAP * 0.8, (
            "the first call of the process was delayed; the gap belongs "
            "between calls, not in front of each one"
        )

    async def test_the_gap_is_the_configured_one(self, monkeypatch):
        monkeypatch.setenv(EnvironmentKeys.TOOL_CALL_GAP_SECONDS, "0.1")
        middleware = SequentialToolExecutionMiddleware()

        await _timed_call(middleware)
        second = await _timed_call(middleware)

        # Would be ~4s on the 5s default, so this fails if the environment is
        # ignored, and ~0s if the spacing is gone.
        assert 0.08 <= second < GAP * 0.8

    async def test_zero_turns_the_spacing_off(self, monkeypatch):
        monkeypatch.setenv(EnvironmentKeys.TOOL_CALL_GAP_SECONDS, "0")
        middleware = SequentialToolExecutionMiddleware()

        await _timed_call(middleware)
        second = await _timed_call(middleware)

        assert second < 0.1


class TestEveryCallDrawsOnTheAccountBudget:
    """A direct tool call costs LinkedIn activity like an enrichment one does.

    Without this the shared ledger only ever saw the bulk jobs, so a day of
    interactive scraping left the budget reading as untouched.
    """

    async def test_each_call_records_one_action(self, paced, tmp_path):
        # The same jobs root the autouse ledger-isolation fixture hands the
        # middleware.
        store = JobStore(tmp_path / "jobs")
        now = datetime.now()

        await _timed_call(paced)
        assert load_account_budget(store, now).ledger.spent(now) == 1

        await _timed_call(paced)
        assert load_account_budget(store, now).ledger.spent(now) == 2


class TestLocalOnlyToolsAreNotPaced:
    """A tool answered from disk never reached LinkedIn, so it owes nothing.

    Both halves matter and both are silent when wrong: a status poll made to
    wait out a page-load gap merely feels broken, while one charged to the
    account budget makes the ledger overstate real activity and stop the next
    bulk job early.
    """

    async def test_a_local_tool_does_not_wait(self, paced):
        await _timed_call(paced)
        second = await _timed_call(paced, "get_enrichment_status")

        assert second < 0.1

    async def test_a_local_tool_does_not_arm_the_gap_for_the_next_call(self, paced):
        await _timed_call(paced, "get_enrichment_status")
        second = await _timed_call(paced)

        assert second < 0.1

    @pytest.mark.parametrize("tool", ["start_enrichment_job", "close_session"])
    async def test_queue_and_session_tools_do_not_wait(self, paced, tool):
        """``start_enrichment_job`` only writes the queue; ``close_session``
        only closes the browser. Neither loads a page, so neither waits."""
        await _timed_call(paced)
        second = await _timed_call(paced, tool)

        assert second < 0.1

    async def test_a_local_tool_spends_no_budget(self, paced, tmp_path):
        store = JobStore(tmp_path / "jobs")
        now = datetime.now()

        await _timed_call(paced, "get_enrichment_status")
        await _timed_call(paced, "get_company_cache")
        # Writing a queue to disk and closing the browser are local too.
        await _timed_call(paced, "start_enrichment_job")
        await _timed_call(paced, "close_session")
        await _timed_call(paced, "query_company_cache")
        assert load_account_budget(store, now).ledger.spent(now) == 0

        await _timed_call(paced)
        assert load_account_budget(store, now).ledger.spent(now) == 1


class TestSelfRecordingToolsAreNotCountedTwice:
    """An enrichment tool writes its own units, one per profile it scrapes.

    Recording here as well added one more per call. The ledger drives a
    rolling daily cap, so an overstated count makes the next bunch stop before
    the budget it was actually given. The gap still applies: these tools do
    reach LinkedIn, and skipping the record is not the same as skipping the
    pacing.
    """

    async def test_a_self_recording_tool_spends_no_extra_unit(self, paced, tmp_path):
        store = JobStore(tmp_path / "jobs")
        now = datetime.now()

        await _timed_call(paced, "run_enrichment_bunch")
        await _timed_call(paced, "enrich_companies")
        await _timed_call(paced, "enrich_company_deep")

        assert load_account_budget(store, now).ledger.spent(now) == 0

    async def test_a_self_recording_tool_is_still_paced(self, paced):
        await _timed_call(paced, "run_enrichment_bunch")
        second = await _timed_call(paced, "run_enrichment_bunch")

        # Unlike a local-only tool, this one waits: it did reach LinkedIn.
        assert second >= GAP * 0.8


class TestArrivalIsRecordedBeforeTheQueue:
    """A bunch's deadline has to see the time its call spent queued.

    The tool timeout starts only once the middleware lets a call through, so a
    call queued behind another session's can already be past the frontend
    proxy's deadline when it starts. The middleware records the arrival for the
    tool to start its deadline from.
    """

    async def test_the_tool_sees_the_arrival_from_before_the_wait(self, unpaced):
        middleware = unpaced
        seen: list[tuple[float | None, float]] = []

        async def hold_the_lock(context):
            await asyncio.sleep(HOLD)
            return None

        async def record(context):
            seen.append((request_arrived_at.get(), time.monotonic()))
            return None

        await asyncio.gather(
            middleware.on_call_tool(_call_context(), hold_the_lock),
            middleware.on_call_tool(_call_context(), record),
        )

        arrived, ran = seen[0]
        assert arrived is not None
        # The second call sat out the first one's hold between arrival and
        # running, so an arrival stamped after the wait would read as (almost)
        # now.
        assert ran - arrived >= HOLD * 0.8

    async def test_the_arrival_is_reset_after_the_call(self, unpaced):
        middleware = unpaced

        await middleware.on_call_tool(_call_context(), AsyncMock())

        assert request_arrived_at.get() is None
