"""Tests for the LinkedInExtractor scraping engine."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.scraping.extractor import (
    ExtractedSection,
    LinkedInExtractor,
    _RATE_LIMITED_MSG,
    _truncate_linkedin_noise,
    _url_matches_profile,
    strip_linkedin_noise,
)
from linkedin_mcp_server.scraping.link_metadata import Reference


class TestUrlMatchesProfile:
    def test_exact_match(self):
        assert _url_matches_profile("https://www.linkedin.com/in/testuser", "/in/testuser")

    def test_trailing_slash(self):
        assert _url_matches_profile("https://www.linkedin.com/in/testuser/", "/in/testuser")

    def test_sub_path(self):
        assert _url_matches_profile("https://www.linkedin.com/in/testuser/details/", "/in/testuser")

    def test_prefix_mismatch_no_false_positive(self):
        # "/in/dan" must NOT match "/in/daniel"
        assert not _url_matches_profile("https://www.linkedin.com/in/daniel/", "/in/dan")

    def test_redirect_url(self):
        assert not _url_matches_profile("https://www.linkedin.com/login", "/in/testuser")

    def test_authwall_redirect(self):
        assert not _url_matches_profile("https://www.linkedin.com/authwall", "/in/testuser")


def extracted(
    text: str,
    references: list[Reference] | None = None,
    error: dict | None = None,
) -> ExtractedSection:
    """Create an ExtractedSection for tests."""
    return ExtractedSection(text=text, references=references or [], error=error)


class TestBuildJobSearchUrl:
    """Tests for _build_job_search_url URL construction."""

    def test_keywords_only(self):
        url = LinkedInExtractor._build_job_search_url("python developer")
        assert url == "https://www.linkedin.com/jobs/search/?keywords=python+developer"

    def test_with_location(self):
        url = LinkedInExtractor._build_job_search_url("python", location="Remote")
        assert "keywords=python" in url
        assert "location=Remote" in url

    def test_date_posted_normalization(self):
        url = LinkedInExtractor._build_job_search_url("python", date_posted="past_week")
        assert "f_TPR=r604800" in url

    def test_date_posted_passthrough(self):
        url = LinkedInExtractor._build_job_search_url("python", date_posted="r3600")
        assert "f_TPR=r3600" in url

    def test_experience_level_normalization(self):
        url = LinkedInExtractor._build_job_search_url(
            "python", experience_level="entry"
        )
        assert "f_E=2" in url

    def test_experience_level_csv(self):
        url = LinkedInExtractor._build_job_search_url(
            "python", experience_level="entry,director"
        )
        assert "f_E=2,5" in url

    def test_work_type_normalization(self):
        url = LinkedInExtractor._build_job_search_url("python", work_type="remote")
        assert "f_WT=2" in url

    def test_work_type_csv(self):
        url = LinkedInExtractor._build_job_search_url(
            "python", work_type="on_site,hybrid"
        )
        assert "f_WT=1,3" in url

    def test_easy_apply(self):
        url = LinkedInExtractor._build_job_search_url("python", easy_apply=True)
        assert "f_EA=true" in url

    def test_easy_apply_false_omitted(self):
        url = LinkedInExtractor._build_job_search_url("python", easy_apply=False)
        assert "f_EA" not in url

    def test_sort_by_normalization(self):
        url = LinkedInExtractor._build_job_search_url("python", sort_by="date")
        assert "sortBy=DD" in url

    def test_job_type_normalization(self):
        url = LinkedInExtractor._build_job_search_url("python", job_type="full_time")
        assert "f_JT=F" in url

    def test_job_type_csv(self):
        url = LinkedInExtractor._build_job_search_url(
            "python", job_type="full_time,contract"
        )
        assert "f_JT=F,C" in url

    def test_job_type_passthrough(self):
        url = LinkedInExtractor._build_job_search_url("python", job_type="F")
        assert "f_JT=F" in url

    def test_all_filters_combined(self):
        url = LinkedInExtractor._build_job_search_url(
            "python",
            location="Berlin",
            date_posted="past_week",
            experience_level="entry,mid_senior",
            work_type="remote",
            easy_apply=True,
            sort_by="date",
        )
        assert "keywords=python" in url
        assert "location=Berlin" in url
        assert "f_TPR=r604800" in url
        assert "f_E=2,4" in url
        assert "f_WT=2" in url
        assert "f_EA=true" in url
        assert "sortBy=DD" in url


@pytest.fixture
def mock_page():
    """Create a mock Patchright page."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.title = AsyncMock(return_value="LinkedIn")
    page.wait_for_selector = AsyncMock()
    page.wait_for_function = AsyncMock()
    page.evaluate = AsyncMock(
        return_value={"source": "root", "text": "Sample page text", "references": []}
    )
    page.url = "https://www.linkedin.com/in/testuser/"
    page.locator = MagicMock()
    # Default: no modals, no CAPTCHA
    mock_locator = MagicMock()
    mock_locator.count = AsyncMock(return_value=0)
    mock_locator.is_visible = AsyncMock(return_value=False)
    mock_locator.first = mock_locator
    mock_locator.inner_text = AsyncMock(return_value="normal page content")
    page.locator.return_value = mock_locator
    page.main_frame = object()
    page.on = MagicMock()
    page.remove_listener = MagicMock()
    return page


class TestExtractPage:
    async def test_extract_page_returns_text(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Sample profile text",
                "references": [],
            }
        )
        extractor = LinkedInExtractor(mock_page)
        # Patch scroll_to_bottom and detect_rate_limit to avoid complex mock chains
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await extractor.extract_page(
                "https://www.linkedin.com/in/testuser/",
                section_name="main_profile",
            )

        assert result.text == "Sample profile text"
        assert result.references == []
        mock_page.goto.assert_awaited_once()

    async def test_root_content_filters_empty_href_before_resolution(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Sample profile text",
                "references": [],
            }
        )
        extractor = LinkedInExtractor(mock_page)

        await extractor._extract_root_content(["main"])

        await_args = mock_page.evaluate.await_args
        assert await_args is not None
        script = await_args.args[0]
        assert "MAX_HEADING_CONTAINERS = 300" in script
        assert "MAX_REFERENCE_ANCHORS = 500" in script
        assert "const getPreviousHeading = node =>" in script
        assert "index < 3" in script
        assert "if (!rawHref || rawHref === '#')" in script
        assert ".slice(0, MAX_REFERENCE_ANCHORS)" in script
        assert "in_list" not in script
        assert ".filter(Boolean);" in script

    async def test_extract_page_returns_empty_on_failure(self, mock_page):
        mock_page.goto = AsyncMock(side_effect=Exception("Network error"))
        extractor = LinkedInExtractor(mock_page)

        with patch(
            "linkedin_mcp_server.scraping.extractor.build_issue_diagnostics",
            return_value={"issue_template_path": "/tmp/issue.md"},
        ):
            result = await extractor.extract_page(
                "https://www.linkedin.com/in/bad/",
                section_name="main_profile",
            )
        assert result.text == ""
        assert result.references == []
        assert result.error == {"issue_template_path": "/tmp/issue.md"}

    async def test_extract_page_returns_error_on_empty_page_without_main(
        self, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": "", "references": []}
        )
        extractor = LinkedInExtractor(mock_page)

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await extractor.extract_page(
                "https://www.linkedin.com/in/testuser/",
                section_name="main_profile",
            )

        assert result.text == ""
        assert result.references == []
        assert result.error is not None
        assert result.error["error_type"] == "ScrapingError"

    async def test_extract_page_raises_auth_error_for_account_picker(self, mock_page):
        mock_page.goto = AsyncMock(side_effect=Exception("net::ERR_TOO_MANY_REDIRECTS"))
        extractor = LinkedInExtractor(mock_page)

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value="auth barrier text: welcome back + sign in using another account",
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor.extract_page(
                "https://www.linkedin.com/in/testuser/",
                section_name="main_profile",
            )

    async def test_rate_limit_detected(self, mock_page):
        from linkedin_mcp_server.core.exceptions import RateLimitError

        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
                side_effect=RateLimitError("Rate limited", suggested_wait_time=3600),
            ),
            pytest.raises(RateLimitError),
        ):
            await extractor.extract_page(
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
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.extract_page(
                "https://www.linkedin.com/in/testuser/details/experience/",
                section_name="experience",
            )

        assert result.text == _RATE_LIMITED_MSG
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
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.extract_page(
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
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await extractor._extract_page_once(
                "https://www.linkedin.com/in/testuser/recent-activity/all/",
                section_name="posts",
            )

        assert result.text == ""
        assert result.references == []

    async def test_extract_search_page_raises_auth_error_for_login_barrier(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_navigate_to_page",
                new_callable=AsyncMock,
                side_effect=AuthenticationError("Run with --login"),
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor._extract_search_page_once(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )


class TestNavigationDiagnostics:
    async def test_goto_with_auth_checks_clicks_remember_me_and_retries(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)

        async def goto_side_effect(*args, **kwargs):
            if mock_page.goto.await_count == 1:
                raise Exception("net::ERR_TOO_MANY_REDIRECTS")
            return None

        mock_page.goto = AsyncMock(side_effect=goto_side_effect)

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                side_effect=[True],
            ) as mock_resolve,
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier_quick",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await extractor._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        assert mock_page.goto.await_count == 2
        mock_resolve.assert_awaited_once()

    async def test_goto_with_auth_checks_unhooks_outer_listener_before_retry(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        listener_events: list[str] = []

        def record_on(event_name, callback):
            listener_events.append(f"on:{event_name}")

        def record_remove(event_name, callback):
            listener_events.append(f"off:{event_name}")

        mock_page.on.side_effect = record_on
        mock_page.remove_listener.side_effect = record_remove

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier_quick",
                new_callable=AsyncMock,
                side_effect=["account picker", None],
            ),
        ):
            await extractor._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        assert listener_events == [
            "on:framenavigated",
            "off:framenavigated",
            "on:framenavigated",
            "off:framenavigated",
        ]

    async def test_goto_with_auth_checks_records_original_failure_before_retry(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        mock_page.goto = AsyncMock(
            side_effect=[
                Exception("net::ERR_TOO_MANY_REDIRECTS"),
                Exception("retry failed"),
            ]
        )

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                side_effect=[True, False],
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.record_page_trace",
                new_callable=AsyncMock,
            ) as mock_trace,
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(Exception, match="retry failed"),
        ):
            await extractor._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        trace_steps = [call.args[1] for call in mock_trace.await_args_list]
        assert "extractor-navigation-error-before-remember-me-retry" in trace_steps

        trace_call = next(
            call
            for call in mock_trace.await_args_list
            if call.args[1] == "extractor-navigation-error-before-remember-me-retry"
        )
        assert (
            trace_call.kwargs["extra"]["error"]
            == "Exception: net::ERR_TOO_MANY_REDIRECTS"
        )

    async def test_goto_with_auth_checks_logs_failure_context(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        mock_page.goto = AsyncMock(side_effect=Exception("net::ERR_TOO_MANY_REDIRECTS"))

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                extractor,
                "_log_navigation_failure",
                new_callable=AsyncMock,
            ) as mock_log_failure,
            pytest.raises(Exception, match="ERR_TOO_MANY_REDIRECTS"),
        ):
            await extractor._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        mock_log_failure.assert_awaited_once()
        mock_page.on.assert_called_once()
        mock_page.remove_listener.assert_called_once()


class TestScrapePersonUrls:
    """Test that scrape_person visits the correct URLs per section set."""

    async def test_baseline_always_included(self, mock_page):
        """Passing only experience still visits main profile."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"experience"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert "main_profile" in result["sections"]
        assert any(u.endswith("/in/testuser/") for u in urls)
        assert any("/details/experience/" in u for u in urls)

    async def test_basic_info_only_visits_main_profile(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("profile text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"main_profile"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert len(urls) == 1
        assert urls[0].endswith("/in/testuser/")
        assert set(result["sections"]) == {"main_profile"}

    async def test_scrape_person_returns_section_errors(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                side_effect=[
                    extracted("profile text"),
                    extracted("", error={"issue_template_path": "/tmp/issue.md"}),
                ],
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"posts"})

        assert result["sections"]["main_profile"] == "profile text"
        assert (
            result["section_errors"]["posts"]["issue_template_path"] == "/tmp/issue.md"
        )

    async def test_experience_education_visits_correct_urls(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person(
                "testuser", {"main_profile", "experience", "education"}
            )

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert len(urls) == 3
        assert any(u.endswith("/in/testuser/") for u in urls)
        assert any("/details/experience/" in u for u in urls)
        assert any("/details/education/" in u for u in urls)
        assert set(result["sections"]) == {"main_profile", "experience", "education"}

    async def test_all_sections_visit_all_urls(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        all_sections = {
            "main_profile",
            "experience",
            "education",
            "interests",
            "honors",
            "languages",
            "contact_info",
            "posts",
        }
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted("contact text"),
            ) as mock_overlay,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", all_sections)

        page_urls = [call.args[0] for call in mock_extract.call_args_list]
        overlay_urls = [call.args[0] for call in mock_overlay.call_args_list]
        all_urls = page_urls + overlay_urls
        # 7 full-page sections + 1 overlay (contact_info)
        assert len(page_urls) == 7
        assert len(overlay_urls) == 1
        # Verify each expected suffix was navigated
        assert any(u.endswith("/in/testuser/") for u in all_urls)
        assert any("/details/experience/" in u for u in all_urls)
        assert any("/details/education/" in u for u in all_urls)
        assert any("/details/interests/" in u for u in all_urls)
        assert any("/details/honors/" in u for u in all_urls)
        assert any("/details/languages/" in u for u in all_urls)
        assert any("/overlay/contact-info/" in u for u in overlay_urls)
        assert any("/recent-activity/all/" in u for u in all_urls)
        assert set(result["sections"]) == all_sections

    async def test_posts_visits_recent_activity(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("Post 1\nPost 2"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("test-user", {"posts"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert any("/recent-activity/all/" in url for url in urls)
        assert "posts" in result["sections"]

    async def test_references_are_grouped_by_section(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                side_effect=[
                    extracted(
                        "profile text",
                        [
                            {
                                "kind": "person",
                                "url": "/in/testuser/",
                                "text": "Test User",
                            }
                        ],
                    ),
                    extracted(
                        "post text",
                        [
                            {
                                "kind": "article",
                                "url": "/pulse/test-post/",
                                "text": "Test post",
                            }
                        ],
                    ),
                ],
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"posts"})

        assert result["references"] == {
            "main_profile": [
                {"kind": "person", "url": "/in/testuser/", "text": "Test User"}
            ],
            "posts": [
                {"kind": "article", "url": "/pulse/test-post/", "text": "Test post"}
            ],
        }

    async def test_error_isolation(self, mock_page):
        """One section failing doesn't block others."""

        async def extract_with_failure(url, *args, **kwargs):
            if "experience" in url:
                raise Exception("Simulated failure")
            return extracted(f"text for {url}")

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                side_effect=extract_with_failure,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_issue_diagnostics",
                return_value={"issue_template_path": "/tmp/issue.md"},
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person(
                "testuser", {"main_profile", "experience", "education"}
            )

        # main_profile and education should have sections, experience should not
        assert "main_profile" in result["sections"]
        assert "education" in result["sections"]
        assert "experience" not in result["sections"]
        assert result["section_errors"]["experience"]["issue_template_path"] == (
            "/tmp/issue.md"
        )

    async def test_rate_limited_sections_are_omitted(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                side_effect=[
                    extracted(_RATE_LIMITED_MSG),
                    extracted("Post text"),
                ],
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"posts"})

        assert "main_profile" not in result["sections"]
        assert result["sections"]["posts"] == "Post text"


class TestScrapeCompany:
    async def test_company_baseline_always_included(self, mock_page):
        """Passing only posts still visits about page."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_company("testcorp", {"posts"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert any("/about/" in u for u in urls)
        assert any("/posts/" in u for u in urls)
        assert "about" in result["sections"]
        assert "posts" in result["sections"]

    async def test_about_only_visits_about(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("about text"),
            ) as mock_extract,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_company("testcorp", {"about"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert len(urls) == 1
        assert "/about/" in urls[0]
        assert set(result["sections"]) == {"about"}

    async def test_all_sections_visit_correct_urls(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_company(
                "testcorp", {"about", "posts", "jobs"}
            )

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert len(urls) == 3
        assert any("/about/" in u for u in urls)
        assert any("/posts/" in u for u in urls)
        assert any("/jobs/" in u for u in urls)
        assert set(result["sections"]) == {"about", "posts", "jobs"}

    async def test_rate_limited_company_sections_are_omitted(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                side_effect=[
                    extracted(_RATE_LIMITED_MSG),
                    extracted("Posts text"),
                ],
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_company("testcorp", {"posts"})

        assert "about" not in result["sections"]
        assert result["sections"]["posts"] == "Posts text"


class TestScrapeJob:
    async def test_scrape_job(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted("Job: Software Engineer"),
        ):
            result = await extractor.scrape_job("12345")

        assert result["url"] == "https://www.linkedin.com/jobs/view/12345/"
        assert "job_posting" in result["sections"]
        assert "pages_visited" not in result
        assert "sections_requested" not in result

    async def test_scrape_job_omits_rate_limited_sentinel(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted(_RATE_LIMITED_MSG),
        ):
            result = await extractor.scrape_job("12345")

        assert result["sections"] == {}

    async def test_scrape_job_omits_orphaned_references_when_text_empty(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted(
                "",
                [{"kind": "job", "url": "/jobs/view/12345/", "text": "Engineer"}],
            ),
        ):
            result = await extractor.scrape_job("12345")

        assert result["sections"] == {}
        assert "references" not in result


class TestSearchJobs:
    """Tests for search_jobs with job ID extraction and pagination."""

    @pytest.fixture(autouse=True)
    def _set_search_url(self, mock_page):
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=python"

    async def test_returns_job_ids(self, mock_page):
        """search_jobs should return a job_ids list extracted from hrefs."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted("Job 1\nJob 2\nJob 3"),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111", "222", "333"],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=1)

        assert result["job_ids"] == ["111", "222", "333"]
        assert "search_results" in result["sections"]

    async def test_returns_references(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted(
                    "Job 1",
                    [{"kind": "job", "url": "/jobs/view/111/", "text": "Job 1"}],
                ),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111"],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=1)

        assert result["references"] == {
            "search_results": [
                {"kind": "job", "url": "/jobs/view/111/", "text": "Job 1"}
            ]
        }

    async def test_pagination_uses_fixed_page_size(self, mock_page):
        """Pages use &start= with fixed 25-per-page offset."""
        extractor = LinkedInExtractor(mock_page)
        page1_ids = ["100", "200", "300"]
        page2_ids = ["400", "500"]
        id_pages = iter([page1_ids, page2_ids])
        text_pages = iter(["Page 1 text", "Page 2 text"])
        urls_visited: list[str] = []

        async def mock_extract(url, *args, **kwargs):
            urls_visited.append(url)
            return extracted(next(text_pages))

        with (
            patch.object(extractor, "_extract_search_page", side_effect=mock_extract),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda: next(id_pages),
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=2)

        assert result["job_ids"] == ["100", "200", "300", "400", "500"]
        assert len(urls_visited) == 2
        assert "&start=25" in urls_visited[1]

    async def test_deduplication_across_pages(self, mock_page):
        """Duplicate job IDs across pages should be deduplicated."""
        extractor = LinkedInExtractor(mock_page)
        id_pages = iter([["100", "200"], ["200", "300"]])
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda: next(id_pages),
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=2)

        assert result["job_ids"] == ["100", "200", "300"]
        assert mock_extract.await_count == 2

    async def test_early_stop_no_new_ids(self, mock_page):
        """Should stop early when a page yields no new job IDs."""
        extractor = LinkedInExtractor(mock_page)
        # Page 2 returns same IDs as page 1
        id_pages = iter([["100", "200"], ["100", "200"]])
        extract_call_count = 0

        async def mock_extract(url, *args, **kwargs):
            nonlocal extract_call_count
            extract_call_count += 1
            if extract_call_count == 1:
                return extracted(
                    "text",
                    [{"kind": "job", "url": "/jobs/view/100/", "text": "Job 100"}],
                )
            return extracted(
                "text",
                [{"kind": "job", "url": "/jobs/view/200/", "text": "Job 200"}],
            )

        with (
            patch.object(extractor, "_extract_search_page", side_effect=mock_extract),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda: next(id_pages),
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=5)

        assert result["job_ids"] == ["100", "200"]
        assert extract_call_count == 2
        assert result["references"] == {
            "search_results": [
                {"kind": "job", "url": "/jobs/view/100/", "text": "Job 100"},
                {"kind": "job", "url": "/jobs/view/200/", "text": "Job 200"},
            ]
        }

    async def test_stops_at_total_pages(self, mock_page):
        """Should stop when total_pages from pagination state is reached."""
        extractor = LinkedInExtractor(mock_page)
        # Distinct IDs per page so the no-new-IDs guard never fires
        id_pages = iter([["100"], ["200"]])
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda: next(id_pages),
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=2,
            ) as mock_total_pages,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=10)

        # Should only visit 2 pages despite max_pages=10
        assert mock_extract.await_count == 2
        assert mock_total_pages.await_count == 1
        assert result["job_ids"] == ["100", "200"]

    async def test_zero_max_pages_fetches_nothing(self, mock_page):
        """max_pages=0 should fetch zero pages (validation at tool boundary)."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=0)

        assert result["job_ids"] == []
        assert mock_extract.await_count == 0

    async def test_single_page(self, mock_page):
        """max_pages=1 should only visit one page; filters appear in URL."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted("Job posting text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["42"],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs(
                "python",
                "Remote",
                max_pages=1,
                date_posted="past_week",
                work_type="remote",
                easy_apply=True,
            )

        assert result["job_ids"] == ["42"]
        assert "keywords=python" in result["url"]
        assert "location=Remote" in result["url"]
        assert "f_TPR=r604800" in result["url"]
        assert "f_WT=2" in result["url"]
        assert "f_EA=true" in result["url"]
        assert mock_extract.await_count == 1

    async def test_page_texts_joined_with_separator(self, mock_page):
        """Multiple pages should join text with --- separator."""
        extractor = LinkedInExtractor(mock_page)
        text_pages = iter(["Page 1 content", "Page 2 content"])
        id_pages = iter([["100"], ["200"]])
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                side_effect=lambda url, *args, **kwargs: extracted(next(text_pages)),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda: next(id_pages),
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=2)

        assert "\n---\n" in result["sections"]["search_results"]
        assert "Page 1 content" in result["sections"]["search_results"]
        assert "Page 2 content" in result["sections"]["search_results"]
        assert mock_extract.await_count == 2

    async def test_empty_results(self, mock_page):
        """Should handle empty results gracefully and skip ID extraction."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_ids,
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("nonexistent_xyz")

        assert result["job_ids"] == []
        assert result["sections"] == {}
        # Empty text should skip ID extraction to avoid stale DOM
        mock_ids.assert_not_awaited()

    async def test_no_ids_on_first_page_captures_text(self, mock_page):
        """Non-empty text with zero job IDs should be returned in sections."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted("No matching jobs found"),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("xyzzy123", max_pages=1)

        assert result["job_ids"] == []
        assert result["sections"]["search_results"] == "No matching jobs found"

    async def test_url_redirect_skips_id_extraction(self, mock_page):
        """Unexpected page URL should skip ID extraction but capture text."""
        extractor = LinkedInExtractor(mock_page)
        mock_page.url = "https://www.linkedin.com/uas/login"
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted(
                    "Login page content",
                    [{"kind": "person", "url": "/in/testuser/", "text": "Test User"}],
                ),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_ids,
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=2)

        mock_ids.assert_not_awaited()
        assert result["job_ids"] == []
        assert result["sections"]["search_results"] == "Login page content"
        assert result["references"] == {
            "search_results": [
                {"kind": "person", "url": "/in/testuser/", "text": "Test User"}
            ]
        }

    async def test_rate_limited_skips_ids_and_text(self, mock_page):
        """Rate-limited pages should yield no IDs or text."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted(_RATE_LIMITED_MSG),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["100"],
            ) as mock_ids,
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=1)

        assert result["job_ids"] == []
        assert result["sections"] == {}
        mock_ids.assert_not_awaited()

    async def test_search_people_omits_orphaned_references(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted(
                "",
                [
                    {
                        "kind": "person",
                        "url": "/in/testuser/",
                        "text": "Test User",
                    }
                ],
            ),
        ):
            result = await extractor.search_people("python")

        assert result["sections"] == {}
        assert "references" not in result

    async def test_rate_limited_search_results_surfaced_in_section_errors(
        self, mock_page
    ):
        """_RATE_LIMITED_MSG on search_results populates section_errors as SessionBlockedError."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted(_RATE_LIMITED_MSG),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=1)

        assert result["sections"] == {}
        assert (
            result["section_errors"]["search_results"]["error_type"]
            == "SessionBlockedError"
        )


class TestStripLinkedInNoise:
    def test_strips_footer(self):
        text = "Bill Gates\nChair, Gates Foundation\n\nAbout\nAccessibility\nTalent Solutions\nCareers"
        assert strip_linkedin_noise(text) == "Bill Gates\nChair, Gates Foundation"

    def test_strips_footer_with_talent_solutions_variant(self):
        text = "Profile content here\n\nAbout\nTalent Solutions\nMore footer"
        assert strip_linkedin_noise(text) == "Profile content here"

    def test_strips_sidebar_recommendations(self):
        text = "Experience\nCo-chair\nGates Foundation\n\nMore profiles for you\nSundar Pichai\nCEO at Google"
        assert strip_linkedin_noise(text) == "Experience\nCo-chair\nGates Foundation"

    def test_strips_premium_upsell(self):
        text = "Education\nHarvard University\n\nExplore premium profiles\nRandom Person\nSoftware Engineer"
        assert strip_linkedin_noise(text) == "Education\nHarvard University"

    def test_picks_earliest_marker(self):
        text = "Content\n\nExplore premium profiles\nStuff\n\nMore profiles for you\nMore stuff\n\nAbout\nAccessibility"
        assert strip_linkedin_noise(text) == "Content"

    def test_no_noise_returns_unchanged(self):
        text = "Clean content with no LinkedIn chrome"
        assert strip_linkedin_noise(text) == "Clean content with no LinkedIn chrome"

    def test_empty_string(self):
        assert strip_linkedin_noise("") == ""

    def test_truncate_noise_preserves_media_controls_for_rate_limit_detection(self):
        text = "Play\nLoaded: 100.00%\nRemaining time 0:07\nShow captions"
        assert _truncate_linkedin_noise(text) == text
        assert strip_linkedin_noise(text) == ""

    def test_about_in_profile_content_not_stripped(self):
        """'About' followed by actual content (not 'Accessibility') should be preserved."""
        text = "About\nChair of the Gates Foundation.\n\nFeatured\nPost"
        assert (
            strip_linkedin_noise(text)
            == "About\nChair of the Gates Foundation.\n\nFeatured\nPost"
        )

    def test_real_footer_with_languages(self):
        text = (
            "Company info\n\n"
            "About\nAccessibility\nTalent Solutions\nCareers\n"
            "Select language\nEnglish (English)\nDeutsch (German)"
        )
        assert strip_linkedin_noise(text) == "Company info"

    def test_preserves_real_careers_content(self):
        text = "Careers\nWe're hiring globally.\nOpen roles in engineering and design."
        assert strip_linkedin_noise(text) == text

    def test_preserves_real_questions_content(self):
        text = "Questions?\nReach out to our recruiting team for details."
        assert strip_linkedin_noise(text) == text

    def test_strips_media_controls_lines(self):
        text = (
            "Feed post number 1\n"
            "Play\n"
            "Loaded: 100.00%\n"
            "Remaining time 0:07\n"
            "Playback speed\n"
            "Actual post content\n"
            "Show captions\n"
            "Close modal window"
        )
        assert strip_linkedin_noise(text) == "Feed post number 1\nActual post content"


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
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await extractor._extract_page_once(
                "https://www.linkedin.com/in/billgates/recent-activity/all/",
                section_name="posts",
            )

        mock_page.wait_for_function.assert_awaited_once()
        mock_scroll.assert_awaited_once()
        _, kwargs = mock_scroll.call_args
        assert kwargs["pause_time"] == 1.0
        assert kwargs["max_scrolls"] == 10
        assert len(result.text) > 200

    async def test_non_activity_page_skips_wait_and_uses_fast_scroll(self, mock_page):
        """Non-activity URLs should not call wait_for_function and use fast scroll."""
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": "Profile text", "references": []}
        )
        mock_page.wait_for_function = AsyncMock()
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            await extractor._extract_page_once(
                "https://www.linkedin.com/in/billgates/details/experience/",
                section_name="experience",
            )

        mock_page.wait_for_function.assert_not_awaited()
        mock_scroll.assert_awaited_once()
        _, kwargs = mock_scroll.call_args
        assert kwargs["pause_time"] == 0.5
        assert kwargs["max_scrolls"] == 5

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
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await extractor._extract_page_once(
                "https://www.linkedin.com/in/billgates/recent-activity/all/",
                section_name="posts",
            )

        # Should return whatever text is available, not crash
        assert result.text == tab_headers


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
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await extractor._extract_page_once(
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
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            await extractor._extract_page_once(
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
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await extractor._extract_page_once(
                "https://www.linkedin.com/search/results/people/?keywords=John+Doe",
                section_name="search_results",
            )

        assert result.text == placeholder


class TestScrapePersonSessionStatus:
    """Tests for session_status field and _SESSION_BLOCKED_ERROR surfacing in scrape_person."""

    async def test_rate_limited_main_profile_surfaced_in_section_errors(
        self, mock_page
    ):
        """_RATE_LIMITED_MSG on main_profile appears in section_errors as SessionBlockedError."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted(_RATE_LIMITED_MSG),
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"main_profile"})

        assert "main_profile" not in result["sections"]
        assert (
            result["section_errors"]["main_profile"]["error_type"]
            == "SessionBlockedError"
        )

    async def test_session_blocked_status_on_rate_limited_main_profile(
        self, mock_page
    ):
        """session_status='session_blocked' when main_profile returns _RATE_LIMITED_MSG."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted(_RATE_LIMITED_MSG),
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"main_profile"})

        assert result.get("session_status") == "session_blocked"
        assert result["sections"] == {}

    async def test_profile_not_found_status_on_url_mismatch(self, mock_page):
        """session_status='profile_not_found' when final URL doesn't contain /in/username."""
        mock_page.url = "https://www.linkedin.com/login"
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted(""),  # empty text, no error
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"main_profile"})

        assert result.get("session_status") == "profile_not_found"

    async def test_no_session_status_on_success(self, mock_page):
        """session_status is absent when main_profile is extracted successfully."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("John Doe\nSoftware Engineer"),
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"main_profile"})

        assert "session_status" not in result
        assert "main_profile" in result["sections"]

    async def test_no_session_status_on_navigation_error(self, mock_page):
        """Navigation/timeout errors set section_errors but do NOT set session_status."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted(
                    "",
                    error={
                        "error_type": "NavigationError",
                        "error_message": "timeout",
                    },
                ),
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"main_profile"})

        assert "session_status" not in result
        assert result["section_errors"]["main_profile"]["error_type"] == "NavigationError"

    async def test_no_session_status_on_navigation_error_with_mismatched_url(
        self, mock_page
    ):
        """Navigation errors must not be reclassified as profile_not_found."""
        mock_page.url = "chrome-error://chromewebdata/"
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted(
                    "",
                    error={
                        "error_type": "NavigationError",
                        "error_message": "ERR_TOO_MANY_REDIRECTS",
                    },
                ),
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"main_profile"})

        assert "session_status" not in result
        assert result["section_errors"]["main_profile"]["error_type"] == "NavigationError"


class TestScrapeCompanySessionStatus:
    """Tests for session_status field and _SESSION_BLOCKED_ERROR surfacing in scrape_company."""

    async def test_rate_limited_about_surfaced_in_section_errors(self, mock_page):
        """_RATE_LIMITED_MSG on about section appears in section_errors as SessionBlockedError."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted(_RATE_LIMITED_MSG),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_company("testcorp", {"about"})

        assert "about" not in result["sections"]
        assert (
            result["section_errors"]["about"]["error_type"] == "SessionBlockedError"
        )

    async def test_session_blocked_status_on_rate_limited_about(self, mock_page):
        """session_status='session_blocked' when about section returns _RATE_LIMITED_MSG."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted(_RATE_LIMITED_MSG),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_company("testcorp", {"about"})

        assert result.get("session_status") == "session_blocked"
        assert result["sections"] == {}
