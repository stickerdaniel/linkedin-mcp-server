"""Tests for page-level interaction simulation (core/interaction_simulation.py)."""

from unittest.mock import AsyncMock, MagicMock

from linkedin_mcp_server.core.interaction_simulation import (
    simulate_mouse_jitter,
    simulate_page_interaction,
    simulate_paced_scroll,
)
from linkedin_mcp_server.core.stealth_profile import (
    DelayConfig,
    NavigationMode,
    SimulationLevel,
    StealthProfile,
)


def _make_page():
    page = MagicMock()
    page.evaluate = AsyncMock()
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    page.viewport_size = {"width": 1280, "height": 720}
    return page


def _profile(simulation: SimulationLevel) -> StealthProfile:
    return StealthProfile(
        name="test",
        navigation=NavigationMode.DIRECT,
        delays=DelayConfig(scroll=(0.0, 0.0), reading=(0.0, 0.0)),
        simulation=simulation,
    )


class TestSimulateMouseJitter:
    async def test_moves_mouse_two_to_four_times_by_default(self, monkeypatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.core.interaction_simulation.asyncio.sleep",
            AsyncMock(),
        )
        page = _make_page()

        await simulate_mouse_jitter(page)

        assert 2 <= page.mouse.move.await_count <= 4

    async def test_exact_point_count_when_specified(self, monkeypatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.core.interaction_simulation.asyncio.sleep",
            AsyncMock(),
        )
        page = _make_page()

        await simulate_mouse_jitter(page, points=3)

        assert page.mouse.move.await_count == 3

    async def test_stays_within_viewport_margin(self, monkeypatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.core.interaction_simulation.asyncio.sleep",
            AsyncMock(),
        )
        page = _make_page()
        page.viewport_size = {"width": 400, "height": 300}

        await simulate_mouse_jitter(page, points=10)

        for call in page.mouse.move.await_args_list:
            x, y = call.args
            assert 100 <= x <= 300
            assert 100 <= y <= 200

    async def test_falls_back_to_default_viewport_when_unset(self, monkeypatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.core.interaction_simulation.asyncio.sleep",
            AsyncMock(),
        )
        page = _make_page()
        page.viewport_size = None

        await simulate_mouse_jitter(page, points=2)  # must not raise

        assert page.mouse.move.await_count == 2


class TestSimulatePacedScroll:
    async def test_scrolls_to_every_position_in_order(self, monkeypatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.core.interaction_simulation.asyncio.sleep",
            AsyncMock(),
        )
        page = _make_page()

        await simulate_paced_scroll(
            page, positions=(0.25, 0.5, 1.0), delay_range=(0.0, 0.0)
        )

        scripts = [call.args[0] for call in page.evaluate.await_args_list]
        assert len(scripts) == 3
        assert "0.25" in scripts[0]
        assert "0.5" in scripts[1]
        assert "1.0" in scripts[2]


class TestSimulatePageInteraction:
    async def test_none_level_is_a_complete_no_op(self, monkeypatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.core.interaction_simulation.asyncio.sleep",
            AsyncMock(),
        )
        page = _make_page()

        await simulate_page_interaction(page, _profile(SimulationLevel.NONE))

        page.evaluate.assert_not_awaited()
        page.mouse.move.assert_not_awaited()

    async def test_basic_scrolls_down_then_returns_to_top(self, monkeypatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.core.interaction_simulation.asyncio.sleep",
            AsyncMock(),
        )
        page = _make_page()

        await simulate_page_interaction(page, _profile(SimulationLevel.BASIC))

        scripts = [call.args[0] for call in page.evaluate.await_args_list]
        assert len(scripts) == 4  # 3 paced-scroll positions + return-to-top
        assert "top: 0" in scripts[-1]

    async def test_moderate_scrolls_five_positions_with_possible_jitter(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.core.interaction_simulation.asyncio.sleep",
            AsyncMock(),
        )
        # Force the 30%-chance jitter branch to always fire, deterministically.
        monkeypatch.setattr(
            "linkedin_mcp_server.core.interaction_simulation.random.random",
            lambda: 0.0,
        )
        page = _make_page()

        await simulate_page_interaction(page, _profile(SimulationLevel.MODERATE))

        assert page.evaluate.await_count == 5  # 5 scroll positions
        assert page.mouse.move.await_count > 0  # jitter fired at least once

    async def test_moderate_never_jitters_when_chance_always_misses(self, monkeypatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.core.interaction_simulation.asyncio.sleep",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.core.interaction_simulation.random.random",
            lambda: 0.99,
        )
        page = _make_page()

        await simulate_page_interaction(page, _profile(SimulationLevel.MODERATE))

        page.mouse.move.assert_not_awaited()

    async def test_comprehensive_runs_full_down_jitter_pause_and_partial_up(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.core.interaction_simulation.asyncio.sleep",
            AsyncMock(),
        )
        page = _make_page()

        await simulate_page_interaction(page, _profile(SimulationLevel.COMPREHENSIVE))

        # 7 positions down + 3 positions back up = 10 scroll evaluates.
        assert page.evaluate.await_count == 10
        assert page.mouse.move.await_count >= 2  # jitter always runs once


class TestSimulationLevelCoversAllEnumMembers:
    async def test_every_simulation_level_runs_without_raising(self, monkeypatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.core.interaction_simulation.asyncio.sleep",
            AsyncMock(),
        )
        for level in SimulationLevel:
            page = _make_page()
            await simulate_page_interaction(page, _profile(level))  # must not raise
