"""Tests for the LinkedInExtractor scraping engine."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.scraping.extractor import LinkedInExtractor
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
        with patch(
            "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
            new_callable=AsyncMock,
            side_effect=RateLimitError("Rate limited", suggested_wait_time=3600),
        ):
            # extract_page catches all exceptions and returns ""
            result = await extractor.extract_page(
                "https://www.linkedin.com/in/testuser/"
            )
            assert result == ""


class TestScrapePersonUrls:
    """Test that scrape_person visits the correct URLs per field combination."""

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
            | PersonScrapingFields.ACCOMPLISHMENTS
            | PersonScrapingFields.CONTACTS
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
        # main_profile, experience, education, interests, honors, languages, contacts
        assert len(urls) == 7
        assert result["sections_requested"] == [
            "main_profile",
            "experience",
            "education",
            "interests",
            "accomplishments",
            "contacts",
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
