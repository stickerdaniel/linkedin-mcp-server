"""Tests for core utility functions (rate-limit detection, scrolling, modals)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.core.exceptions import RateLimitError
from linkedin_mcp_server.core.utils import (
    detect_rate_limit,
    scroll_job_sidebar,
    scroll_to_bottom,
)
from linkedin_mcp_server.pacing import HumanPacing


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


class TestScrollPacing:
    """Scrolling is a glance, so it is paced with a skim delay — or not at all."""

    @staticmethod
    def _lazy_loading_page() -> MagicMock:
        """A page that grows once and then stops: two scroll iterations."""
        page = MagicMock()
        heights = iter([100, 200, 200, 200])
        page.evaluate = AsyncMock(side_effect=lambda *a, **k: next(heights, 200))
        return page

    async def test_scroll_to_bottom_adds_skim_pause_when_paced(self):
        page = self._lazy_loading_page()
        # One patch for both waits, and it has to be one: ``core.utils.asyncio``
        # and ``pacing.asyncio`` name the same module object, so patching them
        # separately leaves only the second mock installed and the first
        # recording nothing whatever the code did. Their values tell them apart.
        with patch(
            "linkedin_mcp_server.core.utils.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            await scroll_to_bottom(
                page,
                pause_time=0.5,
                max_scrolls=2,
                pacing=HumanPacing(enabled=True, min_seconds=4.0, max_seconds=4.0),
            )
        delays = [call.args[0] for call in sleep.await_args_list]
        # The existing 0.5s settle survives; 4.0 * SKIM_FRACTION rides on top.
        assert delays == [pytest.approx(0.5), pytest.approx(1.0)] * 2

    # Two things mean "no pacing" — no ``HumanPacing`` at all, and one that is
    # disabled — and toggle-off has to leave upstream timing byte-identical
    # under both. A disabled instance is what the config path builds when the
    # operator leaves the feature off, so it is the reachable case, not None.
    UNPACED = pytest.mark.parametrize(
        "pacing", [None, HumanPacing.disabled()], ids=["none", "disabled"]
    )

    @UNPACED
    async def test_scroll_to_bottom_unchanged_without_pacing(self, pacing):
        page = self._lazy_loading_page()
        with patch(
            "linkedin_mcp_server.core.utils.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            await scroll_to_bottom(page, pause_time=0.5, max_scrolls=2, pacing=pacing)
        delays = [call.args[0] for call in sleep.await_args_list]
        assert delays == [pytest.approx(0.5)] * 2

    @staticmethod
    async def _sidebar_pause_time(pacing: HumanPacing | None) -> float:
        """Run the sidebar scroll and return the pause handed to the browser."""
        page = MagicMock()
        page.wait_for_selector = AsyncMock()
        page.evaluate = AsyncMock(return_value=1)

        await scroll_job_sidebar(page, pause_time=0.5, max_scrolls=3, pacing=pacing)
        call = page.evaluate.await_args
        assert call is not None
        return call.args[1]["pauseTime"]

    async def test_job_sidebar_pause_is_randomized_when_paced(self):
        pause = await self._sidebar_pause_time(
            HumanPacing(enabled=True, min_seconds=4.0, max_seconds=4.0)
        )
        # A skim, not a decision: 4.0 * SKIM_FRACTION rather than 4.0.
        assert pause == pytest.approx(1.0)

    @UNPACED
    async def test_job_sidebar_pause_unchanged_without_pacing(self, pacing):
        assert await self._sidebar_pause_time(pacing) == pytest.approx(0.5)

    async def test_job_sidebar_pause_never_undercuts_the_caller(self):
        """A shorter draw would scrape less, not merely faster.

        ``pauseTime`` gates the in-page lazy-load check: when it elapses before
        the new cards render, ``scrollHeight`` is unchanged, the loop breaks on
        its first iteration and the rest of the sidebar is never loaded. A
        0.2-0.8s range draws from [0.05, 0.2], which is below the caller's 0.5s
        on every single call — deterministic, not a tail case.
        """
        pause = await self._sidebar_pause_time(
            HumanPacing(enabled=True, min_seconds=0.2, max_seconds=0.8)
        )
        assert pause == pytest.approx(0.5)
