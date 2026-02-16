"""Tests for the LinkedInExtractor scraping engine."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.scraping.extractor import (
    LinkedInExtractor,
    _RATE_LIMITED_MSG,
    strip_linkedin_noise,
)
from linkedin_mcp_server.scraping.fields import (
    CompanyScrapingFields,
    PersonScrapingFields,
)


@pytest.fixture
def mock_page():
    """Create a mock Patchright page."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock(return_value="Sample page text")
    page.url = "https://www.linkedin.com/in/testuser/"
    page.locator = MagicMock()
    # Default: no modals, no CAPTCHA
    mock_locator = MagicMock()
    mock_locator.count = AsyncMock(return_value=0)
    mock_locator.is_visible = AsyncMock(return_value=False)
    mock_locator.first = mock_locator
    mock_locator.inner_text = AsyncMock(return_value="normal page content")
    page.locator.return_value = mock_locator
    return page


class TestExtractPage:
    async def test_extract_page_returns_text(self, mock_page):
        mock_page.evaluate = AsyncMock(
            side_effect=[
                "Sample profile text",  # main.innerText
                100,  # scrollHeight (first check)
                None,  # scrollTo
                100,  # scrollHeight (unchanged = stop)
            ]
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
                "https://www.linkedin.com/in/testuser/"
            )

        assert result == "Sample profile text"
        mock_page.goto.assert_awaited_once()

    async def test_extract_page_returns_empty_on_failure(self, mock_page):
        mock_page.goto = AsyncMock(side_effect=Exception("Network error"))
        extractor = LinkedInExtractor(mock_page)

        result = await extractor.extract_page("https://www.linkedin.com/in/bad/")
        assert result == ""

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
            await extractor.extract_page("https://www.linkedin.com/in/testuser/")

    async def test_returns_rate_limited_msg_after_retry(self, mock_page):
        """When both attempts return only noise, surface rate limit message."""
        noise_only = (
            "More profiles for you\n\n"
            "You've approached your profile search limit\n\n"
            "About\nAccessibility\nTalent Solutions"
        )
        mock_page.evaluate = AsyncMock(return_value=noise_only)
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
                "https://www.linkedin.com/in/testuser/details/experience/"
            )

        assert result == _RATE_LIMITED_MSG
        # goto called twice (initial + retry)
        assert mock_page.goto.await_count == 2

    async def test_retry_succeeds_after_rate_limit(self, mock_page):
        """When first attempt is rate-limited but retry succeeds, return content."""
        noise_only = "More profiles for you\n\nAbout\nAccessibility\nTalent Solutions"
        call_count = 0

        async def evaluate_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # First two calls are from first attempt (goto triggers evaluate via
            # _extract_page_once), return noise. Third+ calls return real content.
            if call_count <= 1:
                return noise_only
            return "Education\nHarvard University\n1973 – 1975"

        mock_page.evaluate = AsyncMock(side_effect=evaluate_side_effect)
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
                "https://www.linkedin.com/in/testuser/details/education/"
            )

        assert result == "Education\nHarvard University\n1973 – 1975"


class TestScrapePersonUrls:
    """Test that scrape_person visits the correct URLs per field combination."""

    async def test_baseline_always_included(self, mock_page):
        """Passing EXPERIENCE without BASIC_INFO still visits main profile."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value="text",
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value="",
            ),
        ):
            result = await extractor.scrape_person(
                "testuser", PersonScrapingFields.EXPERIENCE
            )

        urls = result["pages_visited"]
        assert any("/in/testuser/" in u for u in urls), "main profile should be visited"
        assert any("/details/experience/" in u for u in urls)

    async def test_basic_info_only_visits_main_profile(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value="profile text",
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value="",
            ),
        ):
            result = await extractor.scrape_person(
                "testuser", PersonScrapingFields.BASIC_INFO
            )

        assert len(result["pages_visited"]) == 1
        assert "https://www.linkedin.com/in/testuser/" in result["pages_visited"]
        assert result["sections_requested"] == ["main_profile"]

    async def test_experience_education_visits_three_pages(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        fields = (
            PersonScrapingFields.BASIC_INFO
            | PersonScrapingFields.EXPERIENCE
            | PersonScrapingFields.EDUCATION
        )
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value="text",
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value="",
            ),
        ):
            result = await extractor.scrape_person("testuser", fields)

        urls = result["pages_visited"]
        assert len(urls) == 3
        assert any("/in/testuser/" in u for u in urls)
        assert any("/details/experience/" in u for u in urls)
        assert any("/details/education/" in u for u in urls)
        assert result["sections_requested"] == [
            "main_profile",
            "experience",
            "education",
        ]

    async def test_all_flags_visit_all_pages(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        fields = (
            PersonScrapingFields.BASIC_INFO
            | PersonScrapingFields.EXPERIENCE
            | PersonScrapingFields.EDUCATION
            | PersonScrapingFields.INTERESTS
            | PersonScrapingFields.HONORS
            | PersonScrapingFields.LANGUAGES
            | PersonScrapingFields.CONTACT_INFO
        )
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value="text",
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value="contact text",
            ),
        ):
            result = await extractor.scrape_person("testuser", fields)

        urls = result["pages_visited"]
        # main_profile, experience, education, interests, honors, languages, contact_info
        assert len(urls) == 7
        assert result["sections_requested"] == [
            "main_profile",
            "experience",
            "education",
            "interests",
            "honors",
            "languages",
            "contact_info",
        ]

    async def test_error_isolation(self, mock_page):
        """One section failing doesn't block others."""
        call_count = 0

        async def extract_with_failure(url):
            nonlocal call_count
            call_count += 1
            if "experience" in url:
                raise Exception("Simulated failure")
            return f"text for {url}"

        extractor = LinkedInExtractor(mock_page)
        fields = (
            PersonScrapingFields.BASIC_INFO
            | PersonScrapingFields.EXPERIENCE
            | PersonScrapingFields.EDUCATION
        )
        with (
            patch.object(
                extractor,
                "extract_page",
                side_effect=extract_with_failure,
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value="",
            ),
        ):
            result = await extractor.scrape_person("testuser", fields)

        # All 3 pages should be visited even though experience failed
        assert len(result["pages_visited"]) == 3
        # main_profile and education should have sections, experience should not
        assert "main_profile" in result["sections"]
        assert "education" in result["sections"]


class TestScrapeCompany:
    async def test_company_baseline_always_included(self, mock_page):
        """Passing POSTS without ABOUT still visits about page."""
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value="text",
        ):
            result = await extractor.scrape_company(
                "testcorp", CompanyScrapingFields.POSTS
            )

        urls = result["pages_visited"]
        assert any("/about/" in u for u in urls), "about page should be visited"
        assert any("/posts/" in u for u in urls)

    async def test_about_only_visits_about(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value="about text",
        ):
            result = await extractor.scrape_company(
                "testcorp", CompanyScrapingFields.ABOUT
            )

        assert len(result["pages_visited"]) == 1
        assert any("/about/" in u for u in result["pages_visited"])
        assert result["sections_requested"] == ["about"]

    async def test_all_flags_visit_about_posts_jobs(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        fields = (
            CompanyScrapingFields.ABOUT
            | CompanyScrapingFields.POSTS
            | CompanyScrapingFields.JOBS
        )
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value="text",
        ):
            result = await extractor.scrape_company("testcorp", fields)

        assert len(result["pages_visited"]) == 3
        assert result["sections_requested"] == ["about", "posts", "jobs"]


class TestScrapeJob:
    async def test_scrape_job(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value="Job: Software Engineer",
        ):
            result = await extractor.scrape_job("12345")

        assert result["url"] == "https://www.linkedin.com/jobs/view/12345/"
        assert "job_posting" in result["sections"]
        assert result["sections_requested"] == ["job_posting"]

    async def test_search_jobs(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value="Job 1\nJob 2",
        ):
            result = await extractor.search_jobs("python", "Remote")

        assert "keywords=python" in result["url"]
        assert "location=Remote" in result["url"]
        assert "search_results" in result["sections"]
        assert result["sections_requested"] == ["search_results"]


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
