"""Tests for randomized human-like pacing."""

from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server.scraping.pacing import (
    HumanPacing,
    SKIM_FRACTION,
    human_pause,
    skim_pause,
)


class TestHumanPacing:
    def test_disabled_factory_is_not_enabled(self):
        assert HumanPacing.disabled().enabled is False

    def test_full_delay_is_within_range(self):
        pacing = HumanPacing(enabled=True, min_seconds=1.0, max_seconds=5.0)
        for _ in range(200):
            assert 1.0 <= pacing.full_delay() <= 5.0

    def test_skim_delay_is_a_quarter_of_the_range(self):
        pacing = HumanPacing(enabled=True, min_seconds=1.0, max_seconds=5.0)
        for _ in range(200):
            assert 1.0 * SKIM_FRACTION <= pacing.skim_delay() <= 5.0 * SKIM_FRACTION

    def test_delays_vary(self):
        pacing = HumanPacing(enabled=True, min_seconds=1.0, max_seconds=5.0)
        assert len({round(pacing.full_delay(), 6) for _ in range(50)}) > 1


class TestPauseHelpers:
    async def test_human_pause_sleeps_when_enabled(self):
        pacing = HumanPacing(enabled=True, min_seconds=2.0, max_seconds=2.0)
        with patch(
            "linkedin_mcp_server.scraping.pacing.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep:
            await human_pause(pacing, "navigation")
        sleep.assert_awaited_once()
        await_args = sleep.await_args
        assert await_args is not None
        assert await_args.args[0] == pytest.approx(2.0)

    async def test_human_pause_does_nothing_when_disabled(self):
        with patch(
            "linkedin_mcp_server.scraping.pacing.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep:
            await human_pause(HumanPacing.disabled(), "navigation")
        sleep.assert_not_awaited()

    async def test_human_pause_does_nothing_when_none(self):
        with patch(
            "linkedin_mcp_server.scraping.pacing.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep:
            await human_pause(None, "navigation")
        sleep.assert_not_awaited()

    async def test_skim_pause_sleeps_a_quarter(self):
        pacing = HumanPacing(enabled=True, min_seconds=4.0, max_seconds=4.0)
        with patch(
            "linkedin_mcp_server.scraping.pacing.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep:
            await skim_pause(pacing, "row visit")
        await_args = sleep.await_args
        assert await_args is not None
        assert await_args.args[0] == pytest.approx(1.0)

    async def test_skim_pause_does_nothing_when_disabled(self):
        with patch(
            "linkedin_mcp_server.scraping.pacing.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep:
            await skim_pause(HumanPacing.disabled(), "row visit")
        sleep.assert_not_awaited()

    async def test_skim_pause_does_nothing_when_none(self):
        with patch(
            "linkedin_mcp_server.scraping.pacing.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep:
            await skim_pause(None, "row visit")
        sleep.assert_not_awaited()
