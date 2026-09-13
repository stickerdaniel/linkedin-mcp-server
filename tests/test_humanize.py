"""Tests for the human-like timing/interaction jitter.

The point of the module is that no two pauses are equal and the cursor is not
frozen. These assert the jitter stays in a sane band, varies, and that the
mouse helper never raises.
"""

from unittest.mock import AsyncMock, MagicMock


from linkedin_mcp_server.core.humanize import (
    human_pause,
    human_type,
    humanize_after_nav,
    jitter,
)


async def test_human_type_produces_exactly_the_text_correcting_typos(monkeypatch):
    """Even with typos injected, the net typed result must equal the input:
    a typo is always wrong-char + Backspace + right-char, nothing left behind."""
    monkeypatch.setattr("linkedin_mcp_server.core.humanize.asyncio.sleep", AsyncMock())
    # Force a typo on every alpha char to exercise the correction path hard.
    monkeypatch.setattr("linkedin_mcp_server.core.humanize._rng.random", lambda: 0.0)

    buf: list[str] = []

    async def type_(s):
        buf.append(s)

    async def press(key):
        if key == "Backspace":
            buf.pop()

    page = MagicMock()
    page.keyboard = MagicMock()
    page.keyboard.type = AsyncMock(side_effect=type_)
    page.keyboard.press = AsyncMock(side_effect=press)

    await human_type(page, "Hi there", typo_rate=1.0)
    assert "".join(buf) == "Hi there"


async def test_human_type_bounds_wall_clock_including_keyboard_io(monkeypatch):
    """A long message must not overrun the tool timeout. The bound is
    wall-clock: deliberate pauses stop once real elapsed reaches the budget,
    and because every keystroke advances that clock, the per-key browser I/O
    counts against the budget too -- not merely the summed sleeps. The exact
    text is still typed to the end."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(
        "linkedin_mcp_server.core.humanize.time.monotonic", lambda: clock["t"]
    )

    slept: list[float] = []

    async def fake_sleep(s):
        clock["t"] += s  # a deliberate pause advances the wall clock
        slept.append(s)

    monkeypatch.setattr("linkedin_mcp_server.core.humanize.asyncio.sleep", fake_sleep)

    buf: list[str] = []

    async def type_(s):
        clock["t"] += 0.01  # each keystroke is a browser round-trip: real time
        buf.append(s)

    page = MagicMock()
    page.keyboard = MagicMock()
    page.keyboard.type = AsyncMock(side_effect=type_)
    page.keyboard.press = AsyncMock()

    # 5000 keys of pure I/O alone is 50s here, dwarfing the sleep budget, so a
    # bound that ignored I/O (only summed sleeps) would let this run long.
    msg = "a" * 5000
    await human_type(page, msg, typo_rate=0.0, budget_seconds=30.0)

    assert "".join(buf) == msg
    # Deliberate pauses cut off at the budgeted share of wall-clock time; they
    # do not keep piling on for all 5000 characters.
    assert sum(slept) <= 30.0 * 0.8 + 1.0


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
    monkeypatch.setattr("linkedin_mcp_server.core.humanize.asyncio.sleep", AsyncMock())
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
    monkeypatch.setattr("linkedin_mcp_server.core.humanize.asyncio.sleep", AsyncMock())
    page = MagicMock()
    page.viewport_size = None
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    await humanize_after_nav(page)  # falls back to default 1280x720
    assert page.mouse.move.await_count >= 1
