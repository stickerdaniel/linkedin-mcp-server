"""Tests for core utility functions (rate-limit detection, scrolling, modals)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import RateLimitError
from linkedin_mcp_server.core.utils import (
    RateLimitState,
    detect_rate_limit,
    humanized_delay,
    rate_limit_state,
)


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    """Reset global rate limit state between tests."""
    rate_limit_state.reset()
    yield
    rate_limit_state.reset()


@pytest.fixture
def mock_page():
    """Create a mock Patchright page for rate-limit tests."""
    page = MagicMock()
    page.url = "https://www.linkedin.com/in/testuser/details/experience/"

    mock_locator = MagicMock()
    mock_locator.count = AsyncMock(return_value=0)
    mock_locator.inner_text = AsyncMock(return_value="")
    page.locator = MagicMock(return_value=mock_locator)
    return page


class TestDetectRateLimit:
    async def test_checkpoint_url_raises(self, mock_page):
        mock_page.url = "https://www.linkedin.com/checkpoint/challenge/123"
        with pytest.raises(RateLimitError, match="security checkpoint"):
            await detect_rate_limit(mock_page)

    async def test_authwall_url_raises(self, mock_page):
        mock_page.url = "https://www.linkedin.com/authwall?trk=login"
        with pytest.raises(RateLimitError, match="security checkpoint"):
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


class TestHumanizedDelay:
    def test_returns_float_in_range(self):
        for _ in range(100):
            d = humanized_delay()
            assert 1.5 <= d <= 4.0

    def test_values_are_not_constant(self):
        values = {humanized_delay() for _ in range(20)}
        assert len(values) > 1


class TestRateLimitState:
    def test_initial_state_not_cooling_down(self):
        state = RateLimitState()
        assert not state.is_cooling_down
        assert state.cooldown_remaining == 0.0

    def test_record_rate_limit_sets_cooldown(self):
        state = RateLimitState()
        state.record_rate_limit()
        assert state.is_cooling_down
        assert state.cooldown_remaining > 0

    def test_exponential_backoff(self):
        state = RateLimitState()
        state.record_rate_limit()  # 30s
        first = state.cooldown_remaining
        state.record_rate_limit()  # 60s
        second = state.cooldown_remaining
        assert second > first

    def test_backoff_capped_at_300s(self):
        state = RateLimitState()
        for _ in range(20):
            state.record_rate_limit()
        assert state.cooldown_remaining <= 300.0

    def test_record_success_decrements(self):
        state = RateLimitState()
        state.record_rate_limit()
        state.record_rate_limit()
        state.record_success()
        state.record_success()
        state.record_success()
        # After 2 rate limits and 3 successes, counter should be at 0
        # (can't go below 0)

    def test_reset_clears_state(self):
        state = RateLimitState()
        state.record_rate_limit()
        state.reset()
        assert not state.is_cooling_down
        assert state.cooldown_remaining == 0.0

    async def test_detect_rate_limit_records_state(self, mock_page):
        """detect_rate_limit should update the global rate_limit_state."""
        mock_page.url = "https://www.linkedin.com/checkpoint/challenge/123"
        with pytest.raises(RateLimitError):
            await detect_rate_limit(mock_page)
        assert rate_limit_state.is_cooling_down
