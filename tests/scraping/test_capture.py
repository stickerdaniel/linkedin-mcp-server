"""Tests for the generic page and overlay capture owner."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import ast
import re

import pytest

from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.scraping.capture import (
    RATE_LIMIT_RETRY_DELAY,
    CaptureMode,
    CapturePlan,
    SectionCapture,
    capture_plan_for_url,
)
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession
from linkedin_mcp_server.scraping.text import DetailCaptureTextTable


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

    async def test_extract_page_adapts_the_url_to_an_explicit_plan(self, mock_page):
        capture = _capture(mock_page)
        url = "https://www.linkedin.com/in/testuser/recent-activity/all/"
        with patch.object(
            capture,
            "capture",
            new_callable=AsyncMock,
            return_value=ExtractedSection(text="posts", references=[]),
        ) as explicit_capture:
            result = await capture.extract_page(url, "posts", max_scrolls=7)

        assert result.text == "posts"
        explicit_capture.assert_awaited_once_with(
            url,
            "posts",
            CapturePlan(CaptureMode.ACTIVITY, max_scrolls=7),
        )

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
            result = await capture._capture_once(
                "https://www.linkedin.com/in/testuser/recent-activity/all/",
                section_name="posts",
                plan=CapturePlan(CaptureMode.ACTIVITY),
            )

        assert result.text == ""
        assert result.references == []


class TestActivityFeedExtraction:
    """Tests for activity capture plans and wait behavior."""

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
            result = await capture._capture_once(
                "https://www.linkedin.com/in/billgates/recent-activity/all/",
                section_name="posts",
                plan=CapturePlan(CaptureMode.ACTIVITY),
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
            result = await capture._capture_once(
                "https://www.linkedin.com/company/microsoft/posts/",
                section_name="posts",
                plan=CapturePlan(CaptureMode.ACTIVITY),
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
            await capture._capture_once(
                "https://www.linkedin.com/company/microsoft/posts/?viewAsMember=true",
                section_name="posts",
                plan=CapturePlan(CaptureMode.ACTIVITY),
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
            await capture._capture_once(
                "https://www.linkedin.com/in/billgates/",
                section_name="main_profile",
                plan=CapturePlan(CaptureMode.STANDARD),
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
            await capture._capture_once(
                "https://www.linkedin.com/in/billgates/details/experience/",
                section_name="experience",
                plan=CapturePlan(CaptureMode.DETAILS),
            )

        mock_page.wait_for_function.assert_awaited_once()
        mock_scroll.assert_awaited_once()
        _, kwargs = mock_scroll.call_args
        assert kwargs["pause_time"] == 0.5
        assert kwargs["max_scrolls"] == 5

    async def test_details_page_consumes_injected_text_policy(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": "Experience", "references": []}
        )
        mock_page.wait_for_function = AsyncMock()
        expansion = MagicMock()
        expansion.count = AsyncMock(return_value=0)
        expansion.filter = MagicMock(return_value=expansion)
        mock_page.locator = MagicMock(return_value=expansion)
        detail_text = DetailCaptureTextTable(
            readiness_blocking_prefixes=("Mutated placeholder",),
            expansion_button_pattern=re.compile(r"^Expand entries$"),
        )
        session = ScrapingSession(mock_page)
        capture = SectionCapture(
            session,
            PageNavigator(session),
            PageContentReader(session),
            detail_text,
        )

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
            await capture._capture_once(
                "https://www.linkedin.com/in/ada/details/experience/",
                section_name="experience",
                plan=CapturePlan(CaptureMode.DETAILS),
            )

        wait_args = mock_page.wait_for_function.await_args
        assert wait_args is not None
        assert "text.startsWith('Mutated placeholder')" in wait_args.args[0]
        expansion.filter.assert_called_once_with(
            has_text=detail_text.expansion_button_pattern
        )

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
            await capture._capture_once(
                "https://www.linkedin.com/in/billgates/details/certifications/",
                section_name="certifications",
                plan=CapturePlan(CaptureMode.DETAILS, max_scrolls=20),
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
            await capture._capture_once(
                "https://www.linkedin.com/in/billgates/details/certifications/",
                section_name="certifications",
                plan=CapturePlan(CaptureMode.DETAILS),
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
            await capture._capture_once(
                "https://www.linkedin.com/in/billgates/details/certifications/",
                section_name="certifications",
                plan=CapturePlan(CaptureMode.DETAILS),
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
            await capture._capture_once(
                "https://www.linkedin.com/in/billgates/details/experience/",
                section_name="experience",
                plan=CapturePlan(CaptureMode.DETAILS, max_scrolls=3),
            )

        assert show_more.click.await_count == 3

    async def test_details_page_show_more_default_ceiling_is_five_clicks(
        self, mock_page
    ):
        """Without a budget the loop stops at the default ceiling, not later.

        The two neighbouring tests both leave the default unmeasured: one
        scripts the button away after two clicks, the other passes its own
        budget. Lowering the literal has to fail somewhere.
        """
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
            await capture._capture_once(
                "https://www.linkedin.com/in/billgates/details/experience/",
                section_name="experience",
                plan=CapturePlan(CaptureMode.DETAILS),
            )

        assert show_more.click.await_count == 5

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
            await capture._capture_once(
                "https://www.linkedin.com/in/billgates/",
                section_name="main_profile",
                plan=CapturePlan(CaptureMode.STANDARD),
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
            result = await capture._capture_once(
                "https://www.linkedin.com/in/billgates/recent-activity/all/",
                section_name="posts",
                plan=CapturePlan(CaptureMode.ACTIVITY),
            )

        # Should return whatever text is available, not crash
        assert result.text == tab_headers


class TestCompanyPeopleExtraction:
    """Tests for company-people plan hydration waits."""

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
            await capture._capture_once(
                "https://www.linkedin.com/company/anthropicresearch/people/",
                section_name="employees",
                plan=CapturePlan(CaptureMode.COMPANY_PEOPLE),
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
            result = await capture._capture_once(
                "https://www.linkedin.com/company/anthropicresearch/people/",
                section_name="employees",
                plan=CapturePlan(CaptureMode.COMPANY_PEOPLE),
            )

        mock_scroll.assert_awaited_once()
        assert result.text  # non-empty placeholder text from the mock


class TestSearchResultsExtraction:
    """Tests for search-results plan wait behavior."""

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
            result = await capture._capture_once(
                "https://www.linkedin.com/search/results/people/?keywords=John+Doe",
                section_name="search_results",
                plan=CapturePlan(CaptureMode.SEARCH_RESULTS),
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
            await capture._capture_once(
                "https://www.linkedin.com/in/billgates/",
                section_name="main_profile",
                plan=CapturePlan(CaptureMode.STANDARD),
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
            result = await capture._capture_once(
                "https://www.linkedin.com/search/results/people/?keywords=John+Doe",
                section_name="search_results",
                plan=CapturePlan(CaptureMode.SEARCH_RESULTS),
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


class TestCapturePlans:
    @pytest.mark.parametrize(
        ("url", "mode"),
        [
            ("https://www.linkedin.com/in/ada/", CaptureMode.STANDARD),
            (
                "https://www.linkedin.com/in/ada/recent-activity/all/",
                CaptureMode.ACTIVITY,
            ),
            (
                "https://www.linkedin.com/company/acme/posts/?viewAsMember=true",
                CaptureMode.ACTIVITY,
            ),
            (
                "https://www.linkedin.com/search/results/people/?keywords=ada",
                CaptureMode.SEARCH_RESULTS,
            ),
            (
                "https://www.linkedin.com/company/acme/people/",
                CaptureMode.COMPANY_PEOPLE,
            ),
            (
                "https://www.linkedin.com/in/ada/details/experience/",
                CaptureMode.DETAILS,
            ),
        ],
    )
    def test_url_adapter_preserves_generic_mode_selection(self, url, mode):
        assert capture_plan_for_url(url, 17) == CapturePlan(mode, max_scrolls=17)

    def test_url_adapter_preserves_independent_mode_branches(self):
        url = "https://www.linkedin.com/company/acme/people/search/results/"
        assert capture_plan_for_url(url).mode == (
            CaptureMode.SEARCH_RESULTS | CaptureMode.COMPANY_PEOPLE
        )

    @pytest.mark.parametrize(
        ("url", "mode"),
        [
            (
                "https://www.linkedin.com/in/ada/?next=/details/experience/",
                CaptureMode.DETAILS,
            ),
            (
                "https://www.linkedin.com/in/ada/#/search/results/people/",
                CaptureMode.SEARCH_RESULTS,
            ),
            (
                "https://www.linkedin.com/in/ada/?next=/company/acme/people/",
                CaptureMode.COMPANY_PEOPLE,
            ),
        ],
    )
    def test_url_adapter_raw_url_markers_include_query_and_fragment(self, url, mode):
        assert capture_plan_for_url(url).mode is mode

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.linkedin.com/in/ada/?next=/recent-activity/all/",
            "https://www.linkedin.com/in/ada/#/company/acme/posts/",
        ],
    )
    def test_url_adapter_activity_markers_use_parsed_path_only(self, url):
        assert capture_plan_for_url(url).mode is CaptureMode.STANDARD

    def test_capture_plan_is_immutable(self):
        plan = CapturePlan(CaptureMode.DETAILS, max_scrolls=3)
        with pytest.raises(FrozenInstanceError):
            setattr(plan, "max_scrolls", 4)


_DETAIL_CAPTURE_POLICY_LITERALS = {
    "Load more",
    "More profiles for you",
    "Explore premium profiles",
    r"^Show (more|all)\b",
}


def _detail_capture_policy_literals(source: str) -> set[str]:
    return {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in _DETAIL_CAPTURE_POLICY_LITERALS
    }


def test_capture_module_has_no_en_us_detail_text_policy_literals():
    capture_source = Path("linkedin_mcp_server/scraping/capture.py").read_text(
        encoding="utf-8"
    )
    assert _detail_capture_policy_literals(capture_source) == set()


def test_detail_text_policy_ast_guard_rejects_reintroduced_literals():
    mutation = """
import re

READINESS = "Load more"
BUTTON = re.compile(r"^Show (more|all)\\b")
"""
    assert _detail_capture_policy_literals(mutation) == {
        "Load more",
        r"^Show (more|all)\b",
    }


def _generic_capture_domain_path_literals(source: str) -> set[str]:
    tree = ast.parse(source)
    domain_fragments = (
        "/recent-activity/",
        "/search/results/",
        "/company/",
        "/people/",
        "/details/",
    )
    root_names = {
        "capture",
        "_capture_once",
        "_extract_loaded_section",
        "_extract_overlay_content",
    }
    module_helpers = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    class_methods = {
        child.name: child
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SectionCapture"
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    module_constants: dict[str, ast.AST] = {}
    class_constants: dict[str, ast.AST] = {}

    def record_constants(nodes: list[ast.stmt], constants: dict[str, ast.AST]) -> None:
        for node in nodes:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                value = node.value
                if value is None:
                    continue
                for target in targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = value

    record_constants(tree.body, module_constants)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "SectionCapture":
            record_constants(node.body, class_constants)

    def referenced_constant_literals(
        name: str,
        constants: dict[str, ast.AST],
        seen: set[tuple[int, str]],
    ) -> set[str]:
        key = (id(constants), name)
        if key in seen or name not in constants:
            return set()
        seen.add(key)
        literals: set[str] = set()
        for child in ast.walk(constants[name]):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                if any(fragment in child.value for fragment in domain_fragments):
                    literals.add(child.value)
            elif isinstance(child, ast.Name):
                referenced = (
                    class_constants if constants is class_constants else constants
                )
                if child.id not in referenced:
                    referenced = module_constants
                literals.update(
                    referenced_constant_literals(child.id, referenced, seen)
                )
            elif (
                isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and child.value.id in {"self", "cls", "SectionCapture"}
            ):
                literals.update(
                    referenced_constant_literals(child.attr, class_constants, seen)
                )
        return literals

    pending = [method for name, method in class_methods.items() if name in root_names]
    visited: set[int] = set()
    literals: set[str] = set()
    while pending:
        function = pending.pop()
        if id(function) in visited:
            continue
        visited.add(id(function))
        for child in ast.walk(function):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                if any(fragment in child.value for fragment in domain_fragments):
                    literals.add(child.value)
            elif isinstance(child, ast.Name):
                literals.update(
                    referenced_constant_literals(child.id, module_constants, set())
                )
            elif (
                isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and child.value.id in {"self", "cls", "SectionCapture"}
            ):
                literals.update(
                    referenced_constant_literals(child.attr, class_constants, set())
                )
            elif isinstance(child, ast.Call):
                target: ast.AST | None = None
                if isinstance(child.func, ast.Name):
                    if child.func.id != "capture_plan_for_url":
                        target = module_helpers.get(child.func.id)
                elif (
                    isinstance(child.func, ast.Attribute)
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id in {"self", "cls", "SectionCapture"}
                ):
                    target = class_methods.get(child.func.attr)
                if target is not None:
                    pending.append(target)
    return literals


def test_generic_capture_has_no_domain_path_policy_branches():
    capture_source = Path("linkedin_mcp_server/scraping/capture.py").read_text(
        encoding="utf-8"
    )
    assert _generic_capture_domain_path_literals(capture_source) == set()


def test_generic_capture_ast_guard_rejects_a_reintroduced_domain_branch():
    mutation = """
class SectionCapture:
    async def _extract_loaded_section(self, url):
        if "/details/" in url:
            return "domain policy"
"""
    assert _generic_capture_domain_path_literals(mutation) == {"/details/"}


def test_generic_capture_ast_guard_follows_referenced_module_constants():
    mutation = """
DETAILS_PATH = "/details/"
class SectionCapture:
    async def capture(self, url):
        if DETAILS_PATH in url:
            return "domain policy"
"""
    assert _generic_capture_domain_path_literals(mutation) == {"/details/"}


def test_generic_capture_ast_guard_follows_same_module_helper_calls():
    mutation = """
def _is_company_people(url):
    return "/company/" in url and "/people/" in url
class SectionCapture:
    async def _capture_once(self, url):
        if _is_company_people(url):
            return "domain policy"
"""
    assert _generic_capture_domain_path_literals(mutation) == {
        "/company/",
        "/people/",
    }


@pytest.mark.parametrize("receiver", ["self", "cls"])
def test_generic_capture_ast_guard_follows_same_class_helper_calls(receiver):
    mutation = f"""
class SectionCapture:
    async def capture({receiver}, url):
        return {receiver}._is_details(url)

    def _is_details({receiver}, url):
        return "/details/" in url
"""
    assert _generic_capture_domain_path_literals(mutation) == {"/details/"}


def test_generic_capture_ast_guard_follows_class_qualified_helper_calls():
    mutation = """
class SectionCapture:
    async def capture(self, url):
        return SectionCapture._is_details(url)

    @staticmethod
    def _is_details(url):
        return "/details/" in url
"""
    assert _generic_capture_domain_path_literals(mutation) == {"/details/"}


@pytest.mark.parametrize("receiver", ["self", "cls", "SectionCapture"])
def test_generic_capture_ast_guard_follows_class_constant_access(receiver):
    mutation = f"""
class SectionCapture:
    DETAILS_PATH = "/details/"

    async def capture(self, url):
        return {receiver}.DETAILS_PATH in url
"""
    assert _generic_capture_domain_path_literals(mutation) == {"/details/"}


def test_generic_capture_ast_guard_follows_chained_class_constants():
    mutation = """
class SectionCapture:
    DETAILS_PATH = "/details/"
    DOMAIN_PATH = DETAILS_PATH
    CAPTURE_PATH = SectionCapture.DOMAIN_PATH

    async def capture(self, url):
        return self.CAPTURE_PATH in url
"""
    assert _generic_capture_domain_path_literals(mutation) == {"/details/"}


def test_generic_capture_ast_guard_excludes_url_compatibility_adapter():
    allowed = """
def capture_plan_for_url(url):
    return "/search/results/" in url
class SectionCapture:
    async def capture(self, url):
        return capture_plan_for_url(url)
"""
    assert _generic_capture_domain_path_literals(allowed) == set()
