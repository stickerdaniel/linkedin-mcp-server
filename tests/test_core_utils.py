"""Tests for core utility functions (rate-limit detection, scrolling, modals)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import RateLimitError
from linkedin_mcp_server.core.utils import detect_rate_limit, scroll_job_sidebar


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


class TestScrollDeadline:
    """A spent scroll budget must not turn into no deadline at all."""

    @staticmethod
    def _page() -> MagicMock:
        page = MagicMock()
        page.url = "https://www.linkedin.com/jobs/search/?keywords=python"
        page.wait_for_selector = AsyncMock()
        page.evaluate = AsyncMock(return_value={"status": "gone"})
        return page

    async def test_a_spent_budget_skips_the_scroll(self):
        """Patchright reads a zero timeout as no timeout.

        A search that has spent its budget would then wait on a page with no
        job card until the tool is cancelled, and cancellation throws away
        every page gathered before it.
        """
        page = self._page()

        assert await scroll_job_sidebar(page, deadline=0) is False
        page.wait_for_selector.assert_not_called()

    async def test_a_sliver_of_budget_is_still_a_timeout(self):
        """`int(0.0004 * 1000)` is zero, which the guard above does not catch."""
        page = self._page()

        await scroll_job_sidebar(page, deadline=0.0004)

        assert page.wait_for_selector.await_args.kwargs["timeout"] == 1
