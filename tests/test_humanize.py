"""Tests for the human-like timing/interaction jitter.

The point of the module is that no two pauses are equal and the cursor is not
frozen. These assert the jitter stays in a sane band, varies, and that the
mouse helper never raises.
"""

from unittest.mock import AsyncMock, MagicMock


from linkedin_mcp_server.core.humanize import human_pause, humanize_after_nav, jitter


class TestJitter:
    def test_stays_within_the_spread_band(self):
        for _ in range(200):
            v = jitter(2.0, spread=0.5)
            assert 1.0 <= v <= 3.0

    def test_actually_varies(self):
        values = {round(jitter(2.0), 4) for _ in range(50)}
        assert len(values) > 1, "a constant delay is the tell we are removing"

    def test_never_negative_even_with_huge_spread(self):
        assert jitter(1.0, spread=5.0) >= 0.0

    def test_zero_base_is_zero(self):
        assert jitter(0.0) == 0.0


async def test_human_pause_sleeps_a_jittered_amount(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr("linkedin_mcp_server.core.humanize.asyncio.sleep", fake_sleep)
    await human_pause(2.0, spread=0.5)
    assert len(slept) == 1
    assert 1.0 <= slept[0] <= 3.0


async def test_humanize_after_nav_moves_the_mouse_and_never_raises(monkeypatch):
    monkeypatch.setattr(
        "linkedin_mcp_server.core.humanize.asyncio.sleep", AsyncMock()
    )
    page = MagicMock()
    page.viewport_size = {"width": 1200, "height": 800}
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()

    await humanize_after_nav(page)

    assert page.mouse.move.await_count >= 1
    # moves land inside the viewport interior
    for call in page.mouse.move.await_args_list:
        x, y = call.args[0], call.args[1]
        assert 0 < x < 1200 and 0 < y < 800


async def test_humanize_after_nav_swallows_mouse_errors():
    page = MagicMock()
    page.viewport_size = {"width": 1200, "height": 800}
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock(side_effect=RuntimeError("detached"))
    # Must not raise -- a mouse failure can never break a scrape.
    await humanize_after_nav(page)


async def test_humanize_after_nav_tolerates_missing_viewport(monkeypatch):
    monkeypatch.setattr(
        "linkedin_mcp_server.core.humanize.asyncio.sleep", AsyncMock()
    )
    page = MagicMock()
    page.viewport_size = None
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    await humanize_after_nav(page)  # falls back to default 1280x720
    assert page.mouse.move.await_count >= 1
