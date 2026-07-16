"""Tests for core utility functions (rate-limit detection, scrolling, modals)."""

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import (
    BlockError,
    ChallengeError,
    RateLimitError,
)
from linkedin_mcp_server.core.utils import detect_rate_limit, scroll_to_bottom


@pytest.fixture
def mock_page():
    """Create a mock Patchright page for rate-limit tests."""
    page = MagicMock()
    page.url = "https://www.linkedin.com/in/testuser/details/experience/"
    page.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "value": "session"}]
    )

    mock_locator = MagicMock()
    mock_locator.count = AsyncMock(return_value=0)
    mock_locator.inner_text = AsyncMock(return_value="")
    page.locator = MagicMock(return_value=mock_locator)
    return page


class TestDetectRateLimit:
    async def test_checkpoint_url_raises(self, mock_page):
        # /checkpoint is classified CHALLENGE (recoverable) by
        # core.auth's AuthBarrierKind -- detect_rate_limit now delegates
        # its URL-based check there instead of raising a flat RateLimitError,
        # so the two disconnected detectors agree on exactly one taxonomy.
        mock_page.url = "https://www.linkedin.com/checkpoint/challenge/123"
        with pytest.raises(ChallengeError, match="challenge"):
            await detect_rate_limit(mock_page)

    async def test_authwall_url_raises(self, mock_page):
        # /authwall is classified BLOCK (hard login wall) by the same
        # detector -- a real re-login is needed, not just a retry.
        mock_page.url = "https://www.linkedin.com/authwall?trk=login"
        with pytest.raises(BlockError, match="block"):
            await detect_rate_limit(mock_page)

    async def test_normal_page_with_main_skips_body_heuristic(self, mock_page):
        """A normal page with <main> should NOT trigger body text checks."""
        main_locator = MagicMock()
        main_locator.count = AsyncMock(return_value=1)

        body_locator = MagicMock()
        # Body contains a phrase that would false-positive
        body_locator.inner_text = AsyncMock(
            return_value="Helping SaaS teams slow down churn with data-driven retention"
        )

        def locator_side_effect(selector):
            if selector == "main":
                return main_locator
            if selector == "body":
                return body_locator
            return MagicMock(count=AsyncMock(return_value=0))

        mock_page.locator = MagicMock(side_effect=locator_side_effect)
        # Should NOT raise — the page has <main>, so body heuristic is skipped
        await detect_rate_limit(mock_page)

    async def test_error_page_without_main_triggers_heuristic(self, mock_page):
        """A short error page without <main> with rate-limit text should raise."""
        main_locator = MagicMock()
        main_locator.count = AsyncMock(return_value=0)

        body_locator = MagicMock()
        body_locator.inner_text = AsyncMock(
            return_value="Too many requests. Slow down."
        )

        def locator_side_effect(selector):
            if selector == "main":
                return main_locator
            if selector == "body":
                return body_locator
            return MagicMock(count=AsyncMock(return_value=0))

        mock_page.locator = MagicMock(side_effect=locator_side_effect)
        with pytest.raises(RateLimitError, match="Rate limit message"):
            await detect_rate_limit(mock_page)

    async def test_long_body_without_main_does_not_trigger(self, mock_page):
        """A page without <main> but with long body text (>2000 chars) is not an error page."""
        main_locator = MagicMock()
        main_locator.count = AsyncMock(return_value=0)

        body_locator = MagicMock()
        # Long body with a matching phrase buried in content
        body_locator.inner_text = AsyncMock(
            return_value="x" * 2000 + " try again later"
        )

        def locator_side_effect(selector):
            if selector == "main":
                return main_locator
            if selector == "body":
                return body_locator
            return MagicMock(count=AsyncMock(return_value=0))

        mock_page.locator = MagicMock(side_effect=locator_side_effect)
        # Should NOT raise — body is too long to be an error page
        await detect_rate_limit(mock_page)

    async def test_normal_url_no_error_passes(self, mock_page):
        """A clean normal page passes all checks without raising."""
        main_locator = MagicMock()
        main_locator.count = AsyncMock(return_value=1)

        def locator_side_effect(selector):
            if selector == "main":
                return main_locator
            return MagicMock(count=AsyncMock(return_value=0))

        mock_page.locator = MagicMock(side_effect=locator_side_effect)
        await detect_rate_limit(mock_page)


@dataclass
class _ScrollPageState:
    height_reads: int = 0
    scroll_to_calls: list = field(default_factory=list)


def _make_scroll_page(height_sequence, *, already_at_bottom=False):
    """A page whose document.body.scrollHeight reads walk through
    *height_sequence* in order (repeating the last value once exhausted),
    and whose scrollTo calls are recorded separately -- scrollTo doesn't
    consume a height value, only an explicit height read does."""
    page = MagicMock()
    heights = list(height_sequence)
    state = _ScrollPageState()

    async def fake_evaluate(script):
        if "scrollY" in script:
            return already_at_bottom
        if "scrollTo" in script:
            state.scroll_to_calls.append(script)
            return None
        idx = min(state.height_reads, len(heights) - 1)
        state.height_reads += 1
        return heights[idx]

    page.evaluate = AsyncMock(side_effect=fake_evaluate)
    page._state = state
    return page


class TestScrollToBottom:
    """scroll_to_bottom's smarter internals -- see the function docstring
    for the four behaviors these map to. Every scroll_to_bottom call site
    in extractor.py mocks the function out entirely (call-arg checks only,
    see test_scraping.py), so this is the only place its real body runs."""

    async def test_skips_entirely_when_already_at_bottom(self, monkeypatch):
        monkeypatch.setattr("linkedin_mcp_server.core.utils.asyncio.sleep", AsyncMock())
        page = _make_scroll_page([1000], already_at_bottom=True)

        await scroll_to_bottom(page)

        assert page._state.scroll_to_calls == []

    async def test_scrolls_through_four_fractional_steps_per_pass(self, monkeypatch):
        monkeypatch.setattr("linkedin_mcp_server.core.utils.asyncio.sleep", AsyncMock())
        # Stable height throughout -- exactly one pass, then stop.
        page = _make_scroll_page([1000] * 10, already_at_bottom=False)

        await scroll_to_bottom(page, max_scrolls=5)

        fractions_seen = [call for call in page._state.scroll_to_calls]
        assert len(fractions_seen) == 4
        assert "* 0.25" in fractions_seen[0]
        assert "* 0.5" in fractions_seen[1]
        assert "* 0.75" in fractions_seen[2]
        assert "* 1.0" in fractions_seen[3]

    async def test_stops_after_one_pass_when_height_stable(self, monkeypatch):
        monkeypatch.setattr("linkedin_mcp_server.core.utils.asyncio.sleep", AsyncMock())
        page = _make_scroll_page([1000] * 10, already_at_bottom=False)

        await scroll_to_bottom(page, max_scrolls=5)

        # 1 baseline read + 4 step reads + 1 end-of-pass read = 6 height
        # reads for one pass, not 6 + 5*5 (5 passes worth) -- confirms it
        # stopped after pass 1.
        assert page._state.height_reads == 6

    async def test_continues_when_height_keeps_growing(self, monkeypatch):
        monkeypatch.setattr("linkedin_mcp_server.core.utils.asyncio.sleep", AsyncMock())
        # Baseline 1000, then grows every read across 2 full passes, then
        # stabilizes at 3000 for the 3rd pass.
        heights = (
            [1000]
            + [1500, 1600, 1700, 1800, 2000]
            + [
                2200,
                2400,
                2600,
                2800,
                3000,
            ]
            + [3000] * 5
        )
        page = _make_scroll_page(heights, already_at_bottom=False)

        await scroll_to_bottom(page, max_scrolls=5)

        # 3 passes: two that kept growing, one stable pass that stopped it.
        assert len(page._state.scroll_to_calls) == 12  # 4 steps * 3 passes

    async def test_backoff_grows_and_caps_at_max_scroll_delay(self, monkeypatch):
        sleep_mock = AsyncMock()
        monkeypatch.setattr("linkedin_mcp_server.core.utils.asyncio.sleep", sleep_mock)
        # Keep growing forever so every pass up to max_scrolls actually runs.
        page = _make_scroll_page(
            [i * 100 for i in range(1, 200)], already_at_bottom=False
        )

        await scroll_to_bottom(page, pause_time=0.5, max_scrolls=5)

        delays = [call.args[0] for call in sleep_mock.await_args_list]
        assert delays == [0.5, 0.75, 1.125, 1.6875, 2.0]  # 1.5x backoff, capped at 2.0

    async def test_respects_max_wait_seconds_over_max_scrolls(self, monkeypatch):
        monkeypatch.setattr("linkedin_mcp_server.core.utils.asyncio.sleep", AsyncMock())
        page = _make_scroll_page(
            [i * 100 for i in range(1, 200)], already_at_bottom=False
        )

        class _FakeLoop:
            def __init__(self):
                # First call establishes the deadline; then jump straight
                # past it so only the first pass's deadline-check passes.
                self._times = iter([0.0, 0.0, 9999.0, 9999.0, 9999.0, 9999.0])

            def time(self):
                try:
                    return next(self._times)
                except StopIteration:
                    return 9999.0

        monkeypatch.setattr(
            "linkedin_mcp_server.core.utils.asyncio.get_running_loop",
            lambda: _FakeLoop(),
        )

        await scroll_to_bottom(page, max_scrolls=5, max_wait_seconds=10.0)

        # Only the first pass's 4 scrollTo calls ran before the deadline
        # check stopped the loop, well short of max_scrolls=5 passes.
        assert len(page._state.scroll_to_calls) == 4
