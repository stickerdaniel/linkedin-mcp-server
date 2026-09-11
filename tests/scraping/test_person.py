"""Tests for the person-profile scraping owner."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import importlib.util

import pytest

from linkedin_mcp_server.callbacks import ProgressCallback
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    InvalidReferenceError,
    LinkedInScraperException,
    ProxyConnectionError,
)
from linkedin_mcp_server.scraping import person as person_module
from linkedin_mcp_server.scraping import text as text_module
from linkedin_mcp_server.scraping.capture import SectionCapture
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
)
from linkedin_mcp_server.scraping.link_metadata import Reference
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.person import PersonScraper
from linkedin_mcp_server.scraping.profile_page import ProfilePageReader
from linkedin_mcp_server.scraping.session import ScrapingSession


def _scraper(page, *, message_target: Any = None) -> PersonScraper:
    """Wire the person owner the way the facade does.

    The top-card read the profile URN comes from belongs to the facade until
    the message sender owns it, so the default here is what a page with no
    resolvable action answers: no target, and therefore no URN.
    """
    session = ScrapingSession(page)
    navigator = PageNavigator(session)
    capture = SectionCapture(session, navigator, PageContentReader(session))

    async def read_message_target() -> Any:
        return SimpleNamespace(target=message_target)

    return PersonScraper(
        session,
        navigator,
        capture,
        ProfilePageReader(session, read_message_target),
    )


def extracted(
    text: str,
    references: list[Reference] | None = None,
    error: dict | None = None,
) -> ExtractedSection:
    """Create an ExtractedSection for tests."""
    return ExtractedSection(text=text, references=references or [], error=error)


class TestScrapePersonUrls:
    """Test that scrape_person visits the correct URLs per section set."""

    async def test_baseline_always_included(self, mock_page):
        """Passing only experience still visits main profile."""
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch.object(
                scraper._capture,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person("testuser", {"experience"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert "main_profile" in result["sections"]
        assert any(u.endswith("/in/testuser/") for u in urls)
        assert any("/details/experience/" in u for u in urls)

    async def test_basic_info_only_visits_main_profile(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("profile text"),
            ) as mock_extract,
            patch.object(
                scraper._capture,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person("testuser", {"main_profile"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert len(urls) == 1
        assert urls[0].endswith("/in/testuser/")
        assert set(result["sections"]) == {"main_profile"}

    async def test_a_pasted_profile_link_reaches_the_canonical_profile_url(
        self, mock_page
    ):
        """A URL argument must be reduced before it becomes a path segment.

        Without this the navigation target is
        https://www.linkedin.com/in/https://de.linkedin.com/in/testuser, which
        LinkedIn does not serve, and the tool reports that page as a profile.
        """
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("profile text"),
            ) as mock_extract,
            patch.object(
                scraper._capture,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person(
                "https://de.linkedin.com/in/testuser", {"main_profile"}
            )

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert urls == ["https://www.linkedin.com/in/testuser/"]
        assert result["url"] == "https://www.linkedin.com/in/testuser/"

    async def test_a_dot_segment_value_never_reaches_a_navigation(self, mock_page):
        # A browser resolves ../ away before the request, so this would open the
        # feed and return it as a profile.
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture, "extract_page", new_callable=AsyncMock
        ) as mock_extract:
            with pytest.raises(LinkedInScraperException):
                await scraper.scrape_person("testuser/../../feed", {"main_profile"})
        mock_extract.assert_not_called()

    async def test_an_already_encoded_username_is_not_encoded_twice(self, mock_page):
        """get_my_profile hands over the username exactly this way.

        It reads the segment out of page.url after the /in/me/ redirect, and a
        browser reports that path percent-encoded. Escaping it again turns %D0
        into %25D0, which is a different profile path, so the own-profile scrape
        of any member with a non-ASCII vanity would navigate somewhere else.
        """
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("profile text"),
            ) as mock_extract,
            patch.object(
                scraper._capture,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await scraper.scrape_person(
                "%D0%B0%D0%BD%D0%B4%D1%80%D0%B5%D0%B9", {"main_profile"}
            )

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert urls == [
            "https://www.linkedin.com/in/%D0%B0%D0%BD%D0%B4%D1%80%D0%B5%D0%B9/"
        ]

    async def test_scrape_person_returns_section_errors(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                side_effect=[
                    extracted("profile text"),
                    extracted("", error={"issue_template_path": "/tmp/issue.md"}),
                ],
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person("testuser", {"posts"})

        assert result["sections"]["main_profile"] == "profile text"
        assert (
            result["section_errors"]["posts"]["issue_template_path"] == "/tmp/issue.md"
        )

    async def test_experience_education_visits_correct_urls(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch.object(
                scraper._capture,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person(
                "testuser", {"main_profile", "experience", "education"}
            )

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert len(urls) == 3
        assert any(u.endswith("/in/testuser/") for u in urls)
        assert any("/details/experience/" in u for u in urls)
        assert any("/details/education/" in u for u in urls)
        assert set(result["sections"]) == {"main_profile", "experience", "education"}

    async def test_all_sections_visit_all_urls(self, mock_page):
        scraper = _scraper(mock_page)
        all_sections = {
            "main_profile",
            "experience",
            "education",
            "interests",
            "honors",
            "languages",
            "certifications",
            "skills",
            "projects",
            "contact_info",
            "posts",
        }
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch.object(
                scraper._capture,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted("contact text"),
            ) as mock_overlay,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person("testuser", all_sections)

        page_urls = [call.args[0] for call in mock_extract.call_args_list]
        overlay_urls = [call.args[0] for call in mock_overlay.call_args_list]
        all_urls = page_urls + overlay_urls
        # 10 full-page sections + 1 overlay (contact_info)
        assert len(page_urls) == 10
        assert len(overlay_urls) == 1
        # Verify each expected suffix was navigated
        assert any(u.endswith("/in/testuser/") for u in all_urls)
        assert any("/details/experience/" in u for u in all_urls)
        assert any("/details/education/" in u for u in all_urls)
        assert any("/details/interests/" in u for u in all_urls)
        assert any("/details/honors/" in u for u in all_urls)
        assert any("/details/languages/" in u for u in all_urls)
        assert any("/details/certifications/" in u for u in all_urls)
        assert any("/details/skills/" in u for u in all_urls)
        assert any("/details/projects/" in u for u in all_urls)
        assert any("/overlay/contact-info/" in u for u in overlay_urls)
        assert any("/recent-activity/all/" in u for u in all_urls)
        assert set(result["sections"]) == all_sections

    async def test_posts_visits_recent_activity(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("Post 1\nPost 2"),
            ) as mock_extract,
            patch.object(
                scraper._capture,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person("test-user", {"posts"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert any("/recent-activity/all/" in url for url in urls)
        assert "posts" in result["sections"]

    async def test_certifications_visits_details_page(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("Python for Data Science\nIBM"),
            ) as mock_extract,
            patch.object(
                scraper._capture,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person("test-user", {"certifications"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert any("/details/certifications/" in url for url in urls)
        assert "certifications" in result["sections"]

    async def test_skills_visits_details_page(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("Python\nData Analysis"),
            ) as mock_extract,
            patch.object(
                scraper._capture,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person("test-user", {"skills"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert any("/details/skills/" in url for url in urls)
        assert "skills" in result["sections"]

    async def test_projects_visits_details_page(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("Portfolio Website\nBuilt with React"),
            ) as mock_extract,
            patch.object(
                scraper._capture,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person("test-user", {"projects"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert any("/details/projects/" in url for url in urls)
        assert "projects" in result["sections"]

    async def test_scrape_person_passes_max_scrolls(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch.object(
                scraper._capture,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await scraper.scrape_person("test-user", {"certifications"}, max_scrolls=15)

        for call in mock_extract.call_args_list:
            assert call.kwargs.get("max_scrolls") == 15


class TestScrapePersonSectionOutcomes:
    """What one section's result does to the walk and to the response."""

    async def test_references_are_grouped_by_section(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
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
                scraper._capture,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person("testuser", {"posts"})

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

        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                side_effect=extract_with_failure,
            ),
            patch(
                "linkedin_mcp_server.scraping.person.build_issue_diagnostics",
                return_value={"issue_template_path": "/tmp/issue.md"},
            ),
            patch.object(
                scraper._capture,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person(
                "testuser", {"main_profile", "experience", "education"}
            )

        # main_profile and education should have sections, experience should not
        assert "main_profile" in result["sections"]
        assert "education" in result["sections"]
        assert "experience" not in result["sections"]
        assert result["section_errors"]["experience"]["issue_template_path"] == (
            "/tmp/issue.md"
        )

    async def test_a_rate_limited_section_is_reported_and_stops_the_rest(
        self, mock_page
    ):
        """A throttled section is named as an error, and the walk stops there.

        Both halves matter. Returning the section as merely absent reads as
        "nothing to find" and invites the caller to try again, which is the
        opposite of what LinkedIn just asked for. And continuing to the
        remaining sections would be another navigation each, immediately after
        being told to slow down.
        """
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                side_effect=[
                    extracted(RATE_LIMITED_SECTION_TEXT),
                    extracted("Post text"),
                ],
            ) as mock_extract,
            patch.object(
                scraper._capture,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person("testuser", {"posts"})

        assert "main_profile" not in result["sections"]
        assert result["section_errors"]["main_profile"]["error_type"] == "rate_limit"
        # The second section was never fetched, so its side effect is unused.
        assert mock_extract.await_count == 1
        assert "posts" not in result["sections"]

    async def test_a_failing_urn_read_cannot_bury_the_rate_limit(self, mock_page):
        """The URN read is skipped once throttled, so it cannot overwrite it.

        It runs after the section handling but inside the same try, so a
        failure there lands in the generic handler and replaces the entry with
        a diagnostic — losing the one thing this section had to report. There
        is nothing to read a URN from on a page with no content anyway.
        """
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted(RATE_LIMITED_SECTION_TEXT),
            ),
            patch.object(
                scraper._profile_page,
                "_extract_profile_urn",
                new_callable=AsyncMock,
                side_effect=RuntimeError("execution context destroyed"),
            ) as mock_urn,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person("testuser", set())

        mock_urn.assert_not_awaited()
        assert result["section_errors"]["main_profile"]["error_type"] == "rate_limit"

    async def test_earlier_sections_survive_a_later_rate_limit(self, mock_page):
        """Stopping early keeps what was already gathered."""
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                side_effect=[
                    extracted("Profile text"),
                    extracted(RATE_LIMITED_SECTION_TEXT),
                ],
            ),
            patch.object(
                scraper._capture,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person("testuser", {"posts"})

        assert result["sections"]["main_profile"] == "Profile text"
        assert result["section_errors"]["posts"]["error_type"] == "rate_limit"


class TestScrapePersonCallbacks:
    """Test that scrape_person invokes callbacks at each stage."""

    async def test_scrape_person_calls_callbacks(self, mock_page):
        scraper = _scraper(mock_page)
        cb = MagicMock(spec=ProgressCallback)
        cb.on_start = AsyncMock()
        cb.on_progress = AsyncMock()
        cb.on_complete = AsyncMock()
        cb.on_error = AsyncMock()

        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ),
            patch.object(
                scraper._capture,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted("overlay text"),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await scraper.scrape_person(
                "testuser", {"experience", "education"}, callbacks=cb
            )

        cb.on_start.assert_awaited_once()
        assert cb.on_start.call_args[0][0] == "person profile"

        # 3 sections: main_profile (always) + experience + education
        assert cb.on_progress.await_count == 3
        messages = [c.args[0] for c in cb.on_progress.call_args_list]
        assert messages == [
            "Scraped main_profile (1/3)",
            "Scraped experience (2/3)",
            "Scraped education (3/3)",
        ]
        # Last section should be at 95%
        assert cb.on_progress.call_args_list[-1].args[1] == 95

        cb.on_complete.assert_awaited_once()
        assert cb.on_complete.call_args[0][0] == "person profile"
        cb.on_error.assert_not_awaited()

    async def test_scrape_person_no_callbacks_by_default(self, mock_page):
        """Without callbacks, scrape_person works identically to before."""
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person("testuser", {"main_profile"})

        assert "main_profile" in result["sections"]

    async def test_scrape_person_calls_on_error(self, mock_page):
        scraper = _scraper(mock_page)
        cb = MagicMock(spec=ProgressCallback)
        cb.on_start = AsyncMock()
        cb.on_progress = AsyncMock()
        cb.on_complete = AsyncMock()
        cb.on_error = AsyncMock()

        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                side_effect=LinkedInScraperException("boom"),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            with pytest.raises(LinkedInScraperException):
                await scraper.scrape_person("testuser", {"main_profile"}, callbacks=cb)

        cb.on_start.assert_awaited_once()
        cb.on_error.assert_awaited_once()
        error_arg = cb.on_error.call_args[0][0]
        assert isinstance(error_arg, LinkedInScraperException)
        assert "boom" in str(error_arg)
        cb.on_complete.assert_not_awaited()


class TestMainProfileAlreadyLoaded:
    """Reuse path for scrape_person when get_my_profile already loaded the page."""

    async def test_get_my_profile_passes_already_loaded_flag(self, mock_page):
        scraper = _scraper(mock_page)
        mock_page.url = "https://www.linkedin.com/in/realuser/"
        with (
            patch.object(
                PageNavigator, "_navigate_to_page", new_callable=AsyncMock
            ) as nav,
            patch.object(
                scraper,
                "scrape_person",
                new_callable=AsyncMock,
                return_value={"url": "...", "sections": {}},
            ) as scrape,
        ):
            await scraper.get_my_profile(sections={"main_profile"})

        nav.assert_awaited_once_with("https://www.linkedin.com/in/me/")
        assert scrape.await_count == 1
        assert scrape.call_args.kwargs["main_profile_already_loaded"] is True

    async def test_scrape_person_already_loaded_skips_navigation(self, mock_page):
        scraper = _scraper(mock_page)
        mock_page.url = "https://www.linkedin.com/in/foo/"
        with (
            patch.object(
                scraper._capture,
                "_extract_loaded_section",
                new_callable=AsyncMock,
                return_value=extracted("reused"),
            ) as loaded,
            patch.object(
                scraper._capture, "extract_page", new_callable=AsyncMock
            ) as extract_page,
            patch.object(
                PageNavigator, "_navigate_to_page", new_callable=AsyncMock
            ) as nav,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await scraper.scrape_person(
                "foo", {"main_profile"}, main_profile_already_loaded=True
            )

        loaded.assert_awaited_once()
        extract_page.assert_not_awaited()
        nav.assert_not_awaited()

    async def test_scrape_person_already_loaded_url_mismatch_falls_back(
        self, mock_page
    ):
        scraper = _scraper(mock_page)
        mock_page.url = "https://www.linkedin.com/feed/"
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("fallback"),
            ) as extract_page,
            patch.object(
                scraper._capture,
                "_extract_loaded_section",
                new_callable=AsyncMock,
            ) as loaded,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await scraper.scrape_person(
                "foo", {"main_profile"}, main_profile_already_loaded=True
            )

        extract_page.assert_awaited_once()
        loaded.assert_not_awaited()

    async def test_scrape_person_already_loaded_rate_limit_falls_back(self, mock_page):
        scraper = _scraper(mock_page)
        mock_page.url = "https://www.linkedin.com/in/foo/"

        with (
            patch.object(
                scraper._capture,
                "_extract_loaded_section",
                new_callable=AsyncMock,
                return_value=extracted(RATE_LIMITED_SECTION_TEXT),
            ) as loaded,
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("retry succeeded"),
            ) as extract_page,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person(
                "foo", {"main_profile"}, main_profile_already_loaded=True
            )

        loaded.assert_awaited_once()
        extract_page.assert_awaited_once()
        assert result["sections"]["main_profile"] == "retry succeeded"


class TestScrapePersonProfileUrn:
    async def test_includes_profile_urn_in_result_when_found(self, mock_page):
        """scrape_person includes profile_urn in result when _extract_profile_urn returns a value."""
        urn = "ACoAAB1IelEBLEkqTkNbZ-a1D8mq5R-6C1ihSEk"
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("profile text"),
            ),
            patch.object(
                scraper._profile_page,
                "_extract_profile_urn",
                new_callable=AsyncMock,
                return_value=urn,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person("testuser", {"main_profile"})

        assert result["profile_urn"] == urn

    async def test_omits_profile_urn_when_not_found(self, mock_page):
        """scrape_person omits profile_urn key when _extract_profile_urn returns None."""
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("profile text"),
            ),
            patch.object(
                scraper._profile_page,
                "_extract_profile_urn",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_person("testuser", {"main_profile"})

        assert "profile_urn" not in result


class TestGetMyProfileAlias:
    async def test_survives_a_redirect_that_never_resolves_the_alias(self, mock_page):
        """The one caller allowed to hold "me".

        get_my_profile navigates to /in/me/ and reads the identifier back out of
        the redirect. When the redirect has not happened it still holds the
        alias, and refusing there would answer the tool that owns the alias with
        an instruction to call itself.
        """
        mock_page.url = "https://www.linkedin.com/in/me/"
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("profile text"),
            ) as mock_extract,
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.get_my_profile()

        # The alias survives normalization, and because the page is already on
        # it, the scrape reuses the loaded document instead of navigating again.
        assert result["url"] == "https://www.linkedin.com/in/me/"
        assert "main_profile" in result["sections"]
        mock_extract.assert_not_called()

    async def test_refuses_the_alias_from_an_ordinary_caller(self, mock_page):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture, "extract_page", new_callable=AsyncMock
        ) as mock_extract:
            with pytest.raises(InvalidReferenceError):
                await scraper.scrape_person("me", {"main_profile"})
        mock_extract.assert_not_called()


class TestGetSidebarProfiles:
    async def test_returns_sidebar_profiles_from_all_sections(self, mock_page):
        """Happy path: extracts profiles from all sections, merges Show all results."""
        sidebar_js_result = {
            "sections": {
                "more_profiles_for_you": ["/in/alice/", "/in/bob/"],
                "explore_premium_profiles": ["/in/carol/"],
                "people_you_may_know": ["/in/dave/"],
            },
            "showAllUrls": {
                "more_profiles_for_you": "https://www.linkedin.com/search/results/people/?keywords=test",
            },
        }
        show_all_js_result = ["/in/alice/", "/in/eve/", "/in/frank/"]

        mock_page.evaluate = AsyncMock(
            side_effect=[sidebar_js_result, show_all_js_result]
        )
        mock_page.url = "https://www.linkedin.com/in/testuser/"

        scraper = _scraper(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
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
            result = await scraper.get_sidebar_profiles("testuser")

        assert result["url"] == "https://www.linkedin.com/in/testuser/"
        mpfy = result["sidebar_profiles"]["more_profiles_for_you"]
        # sidebar links first, then show_all expansion, deduped
        assert mpfy == ["/in/alice/", "/in/bob/", "/in/eve/", "/in/frank/"]
        assert result["sidebar_profiles"]["explore_premium_profiles"] == ["/in/carol/"]
        assert result["sidebar_profiles"]["people_you_may_know"] == ["/in/dave/"]

    @pytest.mark.parametrize(
        ("error_type", "message"),
        [
            pytest.param(
                AuthenticationError,
                "Run with --login",
                id="authentication-error",
            ),
            pytest.param(
                ProxyConnectionError,
                "Proxy unavailable",
                id="proxy-connection-error",
            ),
        ],
    )
    async def test_scraper_exception_from_show_all_propagates(
        self,
        mock_page,
        error_type: type[LinkedInScraperException],
        message: str,
    ):
        sidebar_js_result = {
            "sections": {"more_profiles_for_you": ["/in/alice/"]},
            "showAllUrls": {
                "more_profiles_for_you": "https://www.linkedin.com/search/results/people/?keywords=test"
            },
        }
        mock_page.evaluate = AsyncMock(return_value=sidebar_js_result)
        mock_page.url = "https://www.linkedin.com/in/testuser/"

        scraper = _scraper(mock_page)
        with (
            patch.object(
                PageNavigator,
                "_navigate_to_page",
                new_callable=AsyncMock,
                side_effect=[None, error_type(message)],
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
            pytest.raises(error_type, match=message),
        ):
            await scraper.get_sidebar_profiles("testuser")

    async def test_raw_exception_from_show_all_keeps_inline_profiles(self, mock_page):
        show_all_url = "https://www.linkedin.com/search/results/people/?keywords=test"
        sidebar_js_result = {
            "sections": {"more_profiles_for_you": ["/in/alice/"]},
            "showAllUrls": {"more_profiles_for_you": show_all_url},
        }
        mock_page.evaluate = AsyncMock(return_value=sidebar_js_result)
        mock_page.url = "https://www.linkedin.com/in/testuser/"

        scraper = _scraper(mock_page)
        with (
            patch.object(
                PageNavigator,
                "_navigate_to_page",
                new_callable=AsyncMock,
                side_effect=[None, RuntimeError("navigation failed")],
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
            patch.object(person_module.logger, "debug") as debug_mock,
        ):
            result = await scraper.get_sidebar_profiles("testuser")

        assert result == {
            "url": "https://www.linkedin.com/in/testuser/",
            "sidebar_profiles": {"more_profiles_for_you": ["/in/alice/"]},
        }
        debug_mock.assert_called_once_with(
            "Failed to navigate to Show all for section %s: %s",
            "more_profiles_for_you",
            show_all_url,
        )

    async def test_skips_show_all_when_url_contains_premium(self, mock_page):
        """Show all URL containing /premium is skipped without navigation."""
        sidebar_js_result = {
            "sections": {"explore_premium_profiles": ["/in/carol/"]},
            "showAllUrls": {
                "explore_premium_profiles": "https://www.linkedin.com/premium/products/"
            },
        }
        mock_page.evaluate = AsyncMock(return_value=sidebar_js_result)
        mock_page.url = "https://www.linkedin.com/in/testuser/"

        scraper = _scraper(mock_page)
        navigate_mock = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", navigate_mock),
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
            result = await scraper.get_sidebar_profiles("testuser")

        navigate_mock.assert_awaited_once()  # only the initial profile navigation
        mock_page.evaluate.assert_awaited_once()  # no show_all JS call
        assert result["sidebar_profiles"]["explore_premium_profiles"] == ["/in/carol/"]

    async def test_skips_show_all_when_page_redirects_to_premium(self, mock_page):
        """If navigating to Show all lands on a /premium URL, skip that section."""
        sidebar_js_result = {
            "sections": {"more_profiles_for_you": ["/in/alice/"]},
            "showAllUrls": {
                "more_profiles_for_you": "https://www.linkedin.com/search/results/people/?keywords=test"
            },
        }
        mock_page.evaluate = AsyncMock(return_value=sidebar_js_result)
        mock_page.url = "https://www.linkedin.com/in/testuser/"

        navigate_call_count = 0

        async def fake_navigate(url: str) -> None:
            nonlocal navigate_call_count
            navigate_call_count += 1
            if navigate_call_count >= 2:
                mock_page.url = "https://www.linkedin.com/premium/grow-your-network/"

        scraper = _scraper(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", side_effect=fake_navigate),
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
            result = await scraper.get_sidebar_profiles("testuser")

        mock_page.evaluate.assert_awaited_once()  # sidebar JS only, no show_all expansion
        assert result["sidebar_profiles"]["more_profiles_for_you"] == ["/in/alice/"]

    async def test_returns_empty_sidebar_profiles_when_no_sections_found(
        self, mock_page
    ):
        """No matching sidebar headings -> empty sidebar_profiles dict."""
        mock_page.evaluate = AsyncMock(return_value={"sections": {}, "showAllUrls": {}})
        mock_page.url = "https://www.linkedin.com/in/testuser/"

        scraper = _scraper(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
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
            result = await scraper.get_sidebar_profiles("testuser")

        assert result == {
            "url": "https://www.linkedin.com/in/testuser/",
            "sidebar_profiles": {},
        }


class TestSidebarProgramText:
    """The program `get_sidebar_profiles` evaluates, as text."""

    def test_every_substituted_heading_carries_the_template_indent(self):
        # `",\n".join(...)` indents the first heading and nothing after it,
        # because only the first one lands on the template's own indented
        # line. Whitespace alone, and `program_digest` strips per-line
        # whitespace before fingerprinting, so the traces hold the claim
        # `_js_literal` makes about byte identity open on exactly this.
        block = ",\n".join(
            f'{" " * 20}"{heading}"'
            for heading in text_module.SIDEBAR_CHROME_EN.section_headings
        )

        assert f"const SIDEBAR_SECTIONS = [\n{block}\n" in (
            person_module._SIDEBAR_PROFILES_JS
        )
        assert not [
            line
            for line in person_module._SIDEBAR_PROFILES_JS.splitlines()[1:]
            if line and not line.startswith(" ")
        ]

    @pytest.mark.parametrize(
        ("value", "quote"),
        [("voir l'ensemble", "'"), ('the "all" list', '"'), ("back\\slash", "'")],
    )
    def test_an_unquotable_locale_label_is_refused(self, value, quote):
        with pytest.raises(ValueError, match="cannot be quoted"):
            person_module._js_literal(value, quote)

    def test_an_unquotable_locale_label_stops_the_import(self, monkeypatch):
        # The only call sites are module-level, so a table entry carrying an
        # apostrophe has to fail here rather than as a JavaScript
        # `SyntaxError` out of the unguarded `page.evaluate` below — which
        # surfaces against live LinkedIn only, and only once this table grows
        # the locale it exists to accept. Loaded as a throwaway copy, so the
        # module every other test holds is left alone.
        monkeypatch.setattr(
            text_module,
            "SIDEBAR_CHROME_EN",
            replace(
                text_module.SIDEBAR_CHROME_EN, show_all_prefixes=("voir l'ensemble",)
            ),
        )
        spec = importlib.util.spec_from_file_location(
            "person_locale_probe", person_module.__file__
        )
        assert spec is not None and spec.loader is not None
        probe = importlib.util.module_from_spec(spec)

        with pytest.raises(ValueError, match="cannot be quoted"):
            spec.loader.exec_module(probe)


class TestSearchPeople:
    async def test_search_people_omits_orphaned_references(self, mock_page):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
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
            result = await scraper.search_people("python")

        assert result["sections"] == {}
        assert "references" not in result

    async def test_search_people_network_filter_first_degree(self, mock_page):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted("Jane Doe"),
        ):
            result = await scraper.search_people("engineer", network=["F"])

        assert "network=%5B%22F%22%5D" in result["url"]

    async def test_search_people_network_filter_multi_degree(self, mock_page):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted("Jane Doe"),
        ):
            result = await scraper.search_people("engineer", network=["F", "S"])

        assert "network=%5B%22F%22%2C%22S%22%5D" in result["url"]

    async def test_search_people_current_company_filter(self, mock_page):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted("Jane Doe"),
        ):
            result = await scraper.search_people("engineer", current_company="1115")

        assert "currentCompany=%5B%221115%22%5D" in result["url"]

    async def test_search_people_invalid_network_token_raises(self, mock_page):
        scraper = _scraper(mock_page)
        with pytest.raises(ValueError, match="Invalid network token"):
            await scraper.search_people("engineer", network=["X"])

        mock_page.goto.assert_not_awaited()

    async def test_search_people_rejects_plain_company_name(self, mock_page):
        scraper = _scraper(mock_page)
        with pytest.raises(ValueError, match="must be a numeric"):
            await scraper.search_people("engineer", current_company="SAP")

        mock_page.goto.assert_not_awaited()

    async def test_search_people_rejects_unicode_digit_company(self, mock_page):
        """LinkedIn URN ids are ASCII decimal; reject Unicode digits even
        though ``str.isdigit()`` would accept them."""
        scraper = _scraper(mock_page)
        with pytest.raises(ValueError, match="must be a numeric"):
            await scraper.search_people("engineer", current_company="١١١٥")

        mock_page.goto.assert_not_awaited()

    async def test_search_people_empty_current_company_is_noop(self, mock_page):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted("Jane Doe"),
        ):
            result = await scraper.search_people("engineer", current_company="")

        assert "currentCompany" not in result["url"]

    async def test_search_people_combines_all_filters(self, mock_page):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted("Jane Doe"),
        ):
            result = await scraper.search_people(
                "engineer",
                location="Seattle",
                network=["F"],
                current_company="1115",
            )

        assert "keywords=engineer" in result["url"]
        assert "location=Seattle" in result["url"]
        assert "network=%5B%22F%22%5D" in result["url"]
        assert "currentCompany=%5B%221115%22%5D" in result["url"]
