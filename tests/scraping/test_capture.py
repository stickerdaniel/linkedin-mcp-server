"""Tests for the generic page and overlay capture owner."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.scraping.capture import (
    RATE_LIMIT_RETRY_DELAY,
    SectionCapture,
)
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import RATE_LIMITED_SECTION_TEXT
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession


def _capture(page) -> SectionCapture:
    """Wire the capture owner the way the facade does."""
    session = ScrapingSession(page)
    return SectionCapture(session, PageNavigator(session), PageContentReader(session))


class TestExtractPage:
    async def test_extract_page_returns_text(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Sample profile text",
                "references": [],
            }
        )
        capture = _capture(mock_page)
        # Patch scroll_to_bottom and detect_rate_limit to avoid complex mock chains
        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await capture.extract_page(
                "https://www.linkedin.com/in/testuser/",
                section_name="main_profile",
            )

        assert result.text == "Sample profile text"
        assert result.references == []
        mock_page.goto.assert_awaited_once()

    async def test_extract_page_returns_empty_on_failure(self, mock_page):
        mock_page.goto = AsyncMock(side_effect=Exception("Network error"))
        capture = _capture(mock_page)

        with patch(
            "linkedin_mcp_server.scraping.capture.build_issue_diagnostics",
            return_value={"issue_template_path": "/tmp/issue.md"},
        ):
            result = await capture.extract_page(
                "https://www.linkedin.com/in/bad/",
                section_name="main_profile",
            )
        assert result.text == ""
        assert result.references == []
        assert result.error == {"issue_template_path": "/tmp/issue.md"}

    async def test_extract_page_raises_auth_error_for_account_picker(self, mock_page):
        mock_page.goto = AsyncMock(side_effect=Exception("net::ERR_TOO_MANY_REDIRECTS"))
        capture = _capture(mock_page)

        with (
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value="auth barrier text: welcome back + sign in using another account",
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await capture.extract_page(
                "https://www.linkedin.com/in/testuser/",
                section_name="main_profile",
            )

    async def test_rate_limit_detected(self, mock_page):
        from linkedin_mcp_server.core.exceptions import RateLimitError

        capture = _capture(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
                side_effect=RateLimitError("Rate limited", suggested_wait_time=3600),
            ),
            pytest.raises(RateLimitError),
        ):
            await capture.extract_page(
                "https://www.linkedin.com/in/testuser/",
                section_name="main_profile",
            )

    async def test_returns_rate_limited_msg_after_retry(self, mock_page):
        """When both attempts return only noise, surface rate limit message."""
        noise_only = (
            "More profiles for you\n\n"
            "You've approached your profile search limit\n\n"
            "About\nAccessibility\nTalent Solutions"
        )
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": noise_only, "references": []}
        )
        capture = _capture(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await capture.extract_page(
                "https://www.linkedin.com/in/testuser/details/experience/",
                section_name="experience",
            )

        assert result.text == RATE_LIMITED_SECTION_TEXT
        # goto called twice (initial + retry)
        assert mock_page.goto.await_count == 2

    async def test_retry_succeeds_after_rate_limit(self, mock_page):
        """When first attempt is rate-limited but retry succeeds, return content."""
        noise_only = "More profiles for you\n\nAbout\nAccessibility\nTalent Solutions"
        call_count = 0

        async def evaluate_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return noise_only
            return "Education\nHarvard University\n1973 – 1975"

        async def root_content_side_effect(*args, **kwargs):
            return {
                "source": "root",
                "text": await evaluate_side_effect(),
                "references": [],
            }

        mock_page.evaluate = AsyncMock(side_effect=root_content_side_effect)
        capture = _capture(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await capture.extract_page(
                "https://www.linkedin.com/in/testuser/details/education/",
                section_name="education",
            )

        assert result.text == "Education\nHarvard University\n1973 – 1975"

    async def test_media_only_controls_are_not_misclassified_as_rate_limited(
        self, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Play\nLoaded: 100.00%\nRemaining time 0:07\nShow captions",
                "references": [],
            }
        )
        capture = _capture(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await capture._extract_page_once(
                "https://www.linkedin.com/in/testuser/recent-activity/all/",
                section_name="posts",
            )

        assert result.text == ""
        assert result.references == []


class TestActivityFeedExtraction:
    """Tests for activity page detection and wait behavior in _extract_page_once."""

    async def test_activity_page_waits_for_content_and_uses_slow_scroll(
        self, mock_page
    ):
        """Activity URLs should call wait_for_function and use slower scroll params."""
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Post content " * 50,
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock()
        capture = _capture(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await capture._extract_page_once(
                "https://www.linkedin.com/in/billgates/recent-activity/all/",
                section_name="posts",
            )

        mock_page.wait_for_function.assert_awaited_once()
        mock_scroll.assert_awaited_once()
        _, kwargs = mock_scroll.call_args
        assert kwargs["pause_time"] == 1.0
        assert kwargs["max_scrolls"] == 10
        assert len(result.text) > 200

    async def test_company_posts_page_waits_for_content_and_uses_slow_scroll(
        self, mock_page
    ):
        """Company posts URLs get the same lazy-load wait and scroll budget
        as person activity pages, even though they lack /recent-activity/."""
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Post content " * 50,
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock()
        capture = _capture(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await capture._extract_page_once(
                "https://www.linkedin.com/company/microsoft/posts/",
                section_name="posts",
            )

        mock_page.wait_for_function.assert_awaited_once()
        mock_scroll.assert_awaited_once()
        _, kwargs = mock_scroll.call_args
        assert kwargs["pause_time"] == 1.0
        assert kwargs["max_scrolls"] == 10
        assert len(result.text) > 200

    async def test_company_posts_page_with_query_string_still_waits(self, mock_page):
        """The lazy-load branch keys off the parsed path, so a company posts
        url carrying a query string is not mistaken for a static page."""
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Post content " * 50,
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock()
        capture = _capture(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            await capture._extract_page_once(
                "https://www.linkedin.com/company/microsoft/posts/?viewAsMember=true",
                section_name="posts",
            )

        mock_page.wait_for_function.assert_awaited_once()
        _, kwargs = mock_scroll.call_args
        assert kwargs["max_scrolls"] == 10

    async def test_non_activity_non_details_page_skips_wait_and_uses_fast_scroll(
        self, mock_page
    ):
        """Plain profile URLs (not activity, search, or details) skip wait_for_function."""
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": "Profile text", "references": []}
        )
        mock_page.wait_for_function = AsyncMock()
        capture = _capture(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            await capture._extract_page_once(
                "https://www.linkedin.com/in/billgates/",
                section_name="main_profile",
            )

        mock_page.wait_for_function.assert_not_awaited()
        mock_scroll.assert_awaited_once()
        _, kwargs = mock_scroll.call_args
        assert kwargs["pause_time"] == 0.5
        assert kwargs["max_scrolls"] == 5

    async def test_details_page_waits_for_panel_content(self, mock_page):
        """Detail pages (/details/experience/ etc.) call wait_for_function to wait for the panel."""
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Experience\nSoftware Engineer",
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock()
        capture = _capture(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            await capture._extract_page_once(
                "https://www.linkedin.com/in/billgates/details/experience/",
                section_name="experience",
            )

        mock_page.wait_for_function.assert_awaited_once()
        mock_scroll.assert_awaited_once()
        _, kwargs = mock_scroll.call_args
        assert kwargs["pause_time"] == 0.5
        assert kwargs["max_scrolls"] == 5

    async def test_max_scrolls_override_passed_to_scroll_to_bottom(self, mock_page):
        """Custom max_scrolls on a detail page overrides the default of 5."""
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Experience\nSoftware Engineer",
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock()
        capture = _capture(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            await capture._extract_page_once(
                "https://www.linkedin.com/in/billgates/details/certifications/",
                section_name="certifications",
                max_scrolls=20,
            )

        mock_scroll.assert_awaited_once()
        _, kwargs = mock_scroll.call_args
        assert kwargs["max_scrolls"] == 20

    async def test_default_scrolls_without_max_scrolls_override(self, mock_page):
        """Without max_scrolls, detail pages use the default of 5."""
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Experience\nSoftware Engineer",
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock()
        capture = _capture(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            await capture._extract_page_once(
                "https://www.linkedin.com/in/billgates/details/certifications/",
                section_name="certifications",
            )

        mock_scroll.assert_awaited_once()
        _, kwargs = mock_scroll.call_args
        assert kwargs["max_scrolls"] == 5

    async def test_details_page_clicks_show_more_until_gone(self, mock_page):
        """Detail pages click 'Show more' in a loop until the button disappears."""
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": "text", "references": []}
        )
        mock_page.wait_for_function = AsyncMock()

        show_more = MagicMock()
        # count() returns 1, 1, 0 across iterations — button disappears on 3rd check
        show_more.count = AsyncMock(side_effect=[1, 1, 0])
        show_more.is_visible = AsyncMock(return_value=True)
        show_more.scroll_into_view_if_needed = AsyncMock()
        show_more.click = AsyncMock()
        show_more.first = show_more
        show_more.filter = MagicMock(return_value=show_more)

        def locator_side_effect(selector):
            if selector == "main button":
                return show_more
            return MagicMock(count=AsyncMock(return_value=0))

        mock_page.locator = MagicMock(side_effect=locator_side_effect)
        capture = _capture(mock_page)

        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await capture._extract_page_once(
                "https://www.linkedin.com/in/billgates/details/certifications/",
                section_name="certifications",
            )

        assert show_more.click.await_count == 2

    async def test_details_page_show_more_respects_max_scrolls_budget(self, mock_page):
        """When 'Show more' never disappears, loop exits after max_scrolls clicks."""
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": "text", "references": []}
        )
        mock_page.wait_for_function = AsyncMock()

        show_more = MagicMock()
        show_more.count = AsyncMock(return_value=1)  # always present
        show_more.is_visible = AsyncMock(return_value=True)
        show_more.scroll_into_view_if_needed = AsyncMock()
        show_more.click = AsyncMock()
        show_more.first = show_more
        show_more.filter = MagicMock(return_value=show_more)

        def locator_side_effect(selector):
            if selector == "main button":
                return show_more
            return MagicMock(count=AsyncMock(return_value=0))

        mock_page.locator = MagicMock(side_effect=locator_side_effect)
        capture = _capture(mock_page)

        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await capture._extract_page_once(
                "https://www.linkedin.com/in/billgates/details/experience/",
                section_name="experience",
                max_scrolls=3,
            )

        assert show_more.click.await_count == 3

    async def test_non_details_page_does_not_click_show_more(self, mock_page):
        """Non-details URLs (main profile, activity) skip the Show more loop."""
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": "text", "references": []}
        )
        mock_page.wait_for_function = AsyncMock()

        show_more = MagicMock()
        show_more.count = AsyncMock(return_value=1)
        show_more.click = AsyncMock()
        show_more.first = show_more
        show_more.filter = MagicMock(return_value=show_more)

        def locator_side_effect(selector):
            if selector == "main button":
                return show_more
            return MagicMock(count=AsyncMock(return_value=0))

        mock_page.locator = MagicMock(side_effect=locator_side_effect)
        capture = _capture(mock_page)

        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            await capture._extract_page_once(
                "https://www.linkedin.com/in/billgates/",
                section_name="main_profile",
            )

        show_more.click.assert_not_awaited()

    async def test_activity_page_timeout_proceeds_gracefully(self, mock_page):
        """When activity feed content never loads, extraction proceeds with available text."""
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        tab_headers = "All activity\nPosts\nComments\nVideos\nImages"
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": tab_headers, "references": []}
        )
        mock_page.wait_for_function = AsyncMock(
            side_effect=PlaywrightTimeoutError("Timeout")
        )
        capture = _capture(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await capture._extract_page_once(
                "https://www.linkedin.com/in/billgates/recent-activity/all/",
                section_name="posts",
            )

        # Should return whatever text is available, not crash
        assert result.text == tab_headers


class TestCompanyPeopleExtraction:
    """Tests for /company/<slug>/people/ hydration wait in _extract_page_once."""

    async def test_waits_for_listing_with_5s_timeout(self, mock_page):
        """Company /people/ pages call wait_for_function so the employee
        listing has hydrated before scroll/extract. Empty/restricted listings
        are common, so the timeout is 5s rather than the 10s pattern shared
        with is_search/is_details."""
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Anthropic\nFollowing\nHome\nAbout\nPeople",
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock()
        capture = _capture(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            await capture._extract_page_once(
                "https://www.linkedin.com/company/anthropicresearch/people/",
                section_name="employees",
            )

        mock_page.wait_for_function.assert_awaited_once()
        wait_predicate = mock_page.wait_for_function.call_args[0][0]
        wait_kwargs = mock_page.wait_for_function.call_args.kwargs
        assert "/in/" in wait_predicate
        assert "querySelectorAll" in wait_predicate
        assert wait_kwargs["timeout"] == 5000
        mock_scroll.assert_awaited_once()

    async def test_continues_extraction_on_wait_timeout(self, mock_page):
        """When the hydration wait times out (genuinely empty listing), the
        extractor swallows PlaywrightTimeoutError and still scrolls + extracts
        rather than propagating the error to the caller."""
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Empty company page",
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock(
            side_effect=PlaywrightTimeoutError("Timeout")
        )
        capture = _capture(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await capture._extract_page_once(
                "https://www.linkedin.com/company/anthropicresearch/people/",
                section_name="employees",
            )

        mock_scroll.assert_awaited_once()
        assert result.text  # non-empty placeholder text from the mock


class TestSearchResultsExtraction:
    """Tests for search results page detection and wait behavior in _extract_page_once."""

    async def test_search_results_page_waits_for_content(self, mock_page):
        """Search results URLs should call wait_for_function to wait for content."""
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Search results for John Doe. " * 10,
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock()
        capture = _capture(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await capture._extract_page_once(
                "https://www.linkedin.com/search/results/people/?keywords=John+Doe",
                section_name="search_results",
            )

        mock_page.wait_for_function.assert_awaited_once()
        assert len(result.text) > 100

    async def test_non_search_page_does_not_wait_for_search_content(self, mock_page):
        """Non-search URLs should not trigger the search results wait."""
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": "Profile text", "references": []}
        )
        mock_page.wait_for_function = AsyncMock()
        capture = _capture(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            await capture._extract_page_once(
                "https://www.linkedin.com/in/billgates/",
                section_name="main_profile",
            )

        mock_page.wait_for_function.assert_not_awaited()

    async def test_search_results_timeout_proceeds_gracefully(self, mock_page):
        """When search results never load, extraction proceeds with available text."""
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        placeholder = "Search results for John Doe. No results found"
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": placeholder, "references": []}
        )
        mock_page.wait_for_function = AsyncMock(
            side_effect=PlaywrightTimeoutError("Timeout")
        )
        capture = _capture(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.session.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await capture._extract_page_once(
                "https://www.linkedin.com/search/results/people/?keywords=John+Doe",
                section_name="search_results",
            )

        assert result.text == placeholder


class TestExtractOverlay:
    """Tests for the dialog read behind /overlay/contact-info/."""

    OVERLAY_URL = "https://www.linkedin.com/in/testuser/overlay/contact-info/"
    NOISE_ONLY = (
        "More profiles for you\n\n"
        "You've approached your profile search limit\n\n"
        "About\nAccessibility\nTalent Solutions"
    )

    async def test_the_dialog_is_read_before_main_and_never_dismissed(self, mock_page):
        """The contact-info overlay *is* the modal.

        Dismissing it would destroy the content before the read, and the
        fallback to ``main`` only applies once no dialog matched.
        """
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Email\nada@example.com",
                "references": [],
            }
        )
        capture = _capture(mock_page)

        with (
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ) as modal,
        ):
            result = await capture._extract_overlay(
                self.OVERLAY_URL, section_name="contact_info"
            )

        assert result.text == "Email\nada@example.com"
        modal.assert_not_awaited()
        mock_page.wait_for_selector.assert_awaited_once_with(
            "dialog[open], .artdeco-modal__content"
        )
        await_args = mock_page.evaluate.await_args
        assert await_args is not None
        assert await_args.args[1] == {
            "selectors": ["dialog[open]", ".artdeco-modal__content", "main"]
        }

    async def test_a_noise_only_overlay_is_read_again_after_the_backoff(
        self, mock_page
    ):
        reads = [
            {"source": "root", "text": self.NOISE_ONLY, "references": []},
            {"source": "root", "text": "Email\nada@example.com", "references": []},
        ]

        async def read_or_pass_through(script, *args, **kwargs):
            # Keyed on the program rather than on the call count: the
            # navigation lifecycle evaluates scripts of its own, and a bare
            # sequence would hand one of those the overlay's second read.
            if "MAX_REFERENCE_ANCHORS" not in script:
                return None
            return reads.pop(0)

        mock_page.evaluate = AsyncMock(side_effect=read_or_pass_through)
        capture = _capture(mock_page)

        with (
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ) as sleep,
        ):
            result = await capture._extract_overlay(
                self.OVERLAY_URL, section_name="contact_info"
            )

        assert result.text == "Email\nada@example.com"
        assert mock_page.goto.await_count == 2
        sleep.assert_awaited_once_with(RATE_LIMIT_RETRY_DELAY)

    async def test_a_noise_only_retry_reports_the_rate_limit_sentinel(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": self.NOISE_ONLY, "references": []}
        )
        capture = _capture(mock_page)

        with (
            patch(
                "linkedin_mcp_server.scraping.session.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await capture._extract_overlay(
                self.OVERLAY_URL, section_name="contact_info"
            )

        assert result.text == RATE_LIMITED_SECTION_TEXT
        assert mock_page.goto.await_count == 2

    async def test_an_overlay_failure_is_isolated_into_a_section_error(self, mock_page):
        mock_page.goto = AsyncMock(side_effect=Exception("Network error"))
        capture = _capture(mock_page)

        with patch(
            "linkedin_mcp_server.scraping.capture.build_issue_diagnostics",
            return_value={"issue_template_path": "/tmp/issue.md"},
        ) as diagnostics:
            result = await capture._extract_overlay(
                self.OVERLAY_URL, section_name="contact_info"
            )

        assert result.text == ""
        assert result.error == {"issue_template_path": "/tmp/issue.md"}
        assert diagnostics.call_args.kwargs["context"] == "extract_overlay"
