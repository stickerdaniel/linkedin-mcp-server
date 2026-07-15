"""Tests for human-like typing/mouse/scroll helpers (core.humanize)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.humanize import (
    _bezier_path,
    _char_delay_seconds,
    human_move_and_click,
    human_scroll,
    human_type,
)


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """Every test in this file mocks asyncio.sleep so timing logic runs
    instantly instead of actually waiting out sampled human-like delays."""
    monkeypatch.setattr("linkedin_mcp_server.core.humanize.asyncio.sleep", AsyncMock())


def _make_mock_page(*, viewport=None) -> MagicMock:
    page = MagicMock()
    page.viewport_size = viewport or {"width": 1280, "height": 720}
    page.keyboard = MagicMock()
    page.keyboard.type = AsyncMock()
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    page.mouse.click = AsyncMock()
    page.mouse.wheel = AsyncMock()
    return page


class TestCharDelay:
    def test_delay_stays_within_clamped_range(self):
        for _ in range(200):
            delay = _char_delay_seconds()
            assert 0.02 <= delay <= 0.4

    def test_delay_is_not_constant(self):
        samples = {_char_delay_seconds() for _ in range(20)}
        assert len(samples) > 1


class TestBezierPath:
    def test_path_starts_and_ends_at_endpoints(self):
        path = _bezier_path(0, 0, 100, 200, steps=10)
        assert path[0] == (0, 0)
        assert path[-1] == (100, 200)

    def test_path_has_requested_number_of_points(self):
        path = _bezier_path(0, 0, 50, 50, steps=25)
        assert len(path) == 26  # steps + 1


class TestHumanType:
    async def test_types_one_keyboard_call_per_character(self):
        page = _make_mock_page()
        await human_type(page, "hi!")
        assert page.keyboard.type.await_count == 3
        page.keyboard.type.assert_any_await("h")
        page.keyboard.type.assert_any_await("i")
        page.keyboard.type.assert_any_await("!")

    async def test_empty_text_makes_no_keyboard_calls(self):
        page = _make_mock_page()
        await human_type(page, "")
        page.keyboard.type.assert_not_called()


class TestHumanMoveAndClick:
    async def test_camoufox_is_a_direct_click_passthrough(self):
        page = _make_mock_page()
        locator = MagicMock()
        locator.click = AsyncMock()
        locator.bounding_box = AsyncMock()

        await human_move_and_click(page, locator, engine="camoufox")

        locator.click.assert_awaited_once_with()
        locator.bounding_box.assert_not_called()
        page.mouse.move.assert_not_called()

    async def test_patchright_moves_mouse_before_clicking(self):
        page = _make_mock_page()
        locator = MagicMock()
        locator.click = AsyncMock()
        locator.bounding_box = AsyncMock(
            return_value={"x": 100, "y": 100, "width": 50, "height": 20}
        )

        await human_move_and_click(page, locator, engine="patchright")

        assert page.mouse.move.await_count >= 20  # curved path, not a single jump
        page.mouse.click.assert_awaited_once()
        locator.click.assert_not_called()

    async def test_patchright_falls_back_to_direct_click_when_no_bounding_box(self):
        page = _make_mock_page()
        locator = MagicMock()
        locator.click = AsyncMock()
        locator.bounding_box = AsyncMock(return_value=None)

        await human_move_and_click(page, locator, engine="patchright")

        locator.click.assert_awaited_once_with()
        page.mouse.move.assert_not_called()

    async def test_patchright_falls_back_to_direct_click_when_bounding_box_errors(self):
        """A locator whose bounding_box() isn't a proper AsyncMock (e.g. an
        unconfigured test double) must degrade to a direct click, not raise
        -- a humanization nicety should never abandon a real write action."""
        page = _make_mock_page()
        locator = MagicMock()
        locator.click = AsyncMock()
        # Deliberately NOT an AsyncMock: awaiting its return value raises
        # TypeError, exactly like an unconfigured MagicMock attribute would.
        locator.bounding_box = MagicMock(side_effect=TypeError("not awaitable"))

        await human_move_and_click(page, locator, engine="patchright")

        locator.click.assert_awaited_once_with()
        page.mouse.move.assert_not_called()


class TestHumanScroll:
    async def test_camoufox_is_a_single_wheel_call(self):
        page = _make_mock_page()
        await human_scroll(page, 2000, engine="camoufox")
        page.mouse.wheel.assert_awaited_once_with(0, 2000)

    async def test_patchright_scrolls_in_multiple_increments(self):
        page = _make_mock_page()
        await human_scroll(page, 2000, engine="patchright")
        assert 3 <= page.mouse.wheel.await_count <= 6

    async def test_patchright_increments_sum_to_total_delta(self):
        page = _make_mock_page()
        await human_scroll(page, 2000, engine="patchright")
        total = sum(call.args[1] for call in page.mouse.wheel.await_args_list)
        assert total == 2000
