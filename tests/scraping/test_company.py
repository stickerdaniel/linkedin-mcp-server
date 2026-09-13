"""Tests for the company-page scraping owner."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import logging

import pytest
from patchright._impl._errors import TargetClosedError

from linkedin_mcp_server.callbacks import ProgressCallback
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    InvalidReferenceError,
)
from linkedin_mcp_server.scraping import company as company_module
from linkedin_mcp_server.scraping.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.scraping.company import CompanyScraper
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
    FilterValidationError,
)
from linkedin_mcp_server.scraping.facets import FacetResolver
from linkedin_mcp_server.scraping.fields import COMPANY_SECTIONS
from linkedin_mcp_server.scraping.link_metadata import Reference
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import NAV_DELAY, ScrapingSession


def _scraper(page) -> CompanyScraper:
    """Wire the company owner the way the facade does."""
    session = ScrapingSession(page)
    navigator = PageNavigator(session)
    capture = SectionCapture(session, navigator, PageContentReader(session))
    return CompanyScraper(session, capture, FacetResolver(session, navigator, capture))


def _no_jitter():
    """Pin the session's jitter to identity so a pace of N sleeps exactly N."""
    return patch(
        "linkedin_mcp_server.scraping.session.jitter",
        side_effect=lambda base, spread=0.5: base,
    )


def _company_card(name: str, industry: str, location: str, tagline: str) -> str:
    return (
        f"{name}\n\n{industry}\n\n{location}\n\nFollow\n\n{tagline}\n\n"
        f"Ann & 3 other connections follow this page · 20K followers"
    )


def _company_ref(slug: str, name: str) -> Reference:
    return {"kind": "company", "url": f"/company/{slug}/", "text": name}


def extracted(
    text: str,
    references: list[Reference] | None = None,
    error: dict | None = None,
) -> ExtractedSection:
    """Create an ExtractedSection for tests."""
    return ExtractedSection(text=text, references=references or [], error=error)


class TestScrapeCompany:
    async def test_a_pasted_company_link_reaches_the_canonical_company_url(
        self, mock_page
    ):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                return_value=extracted("company text"),
            ) as mock_extract,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_company(
                "https://de.linkedin.com/company/testco/posts/", {"about"}
            )

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert urls
        assert all(
            u.startswith("https://www.linkedin.com/company/testco") for u in urls
        )
        assert result["url"] == "https://www.linkedin.com/company/testco/"

    async def test_a_traversal_identifier_is_refused_before_navigating(self, mock_page):
        """The normalization runs before the first navigation, not after it.

        The pasted-link test above would still pass with the call moved below
        the loop, because the URL it builds is the same either way; this one
        only passes while the refusal happens first.
        """
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture, "capture", new_callable=AsyncMock
        ) as mock_extract:
            with pytest.raises(InvalidReferenceError):
                await scraper.scrape_company("../../feed", {"about"})

        mock_extract.assert_not_awaited()
        mock_page.goto.assert_not_awaited()

    async def test_company_baseline_always_included(self, mock_page):
        """Passing only posts still visits about page."""
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_company("testcorp", {"posts"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert any("/about/" in u for u in urls)
        assert any("/posts/" in u for u in urls)
        assert "about" in result["sections"]
        assert "posts" in result["sections"]

    async def test_the_baseline_is_added_even_when_nothing_is_requested(
        self, mock_page
    ):
        """An empty request is still one navigation, and it is the about page.

        The test above asks for a second section, so dropping the mandatory
        union there only loses one of two sections; here it loses the walk.
        """
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("about text"),
        ) as mock_extract:
            result = await scraper.scrape_company("testcorp", set())

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert len(urls) == 1
        assert urls[0].endswith("/company/testcorp/about/")
        assert set(result["sections"]) == {"about"}

    async def test_about_only_visits_about(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                return_value=extracted("about text"),
            ) as mock_extract,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_company("testcorp", {"about"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert len(urls) == 1
        assert "/about/" in urls[0]
        assert set(result["sections"]) == {"about"}

    async def test_all_sections_visit_correct_urls(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_company(
                "testcorp", {"about", "posts", "jobs"}
            )

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert len(urls) == 3
        assert any("/about/" in u for u in urls)
        assert any("/posts/" in u for u in urls)
        assert any("/jobs/" in u for u in urls)
        assert set(result["sections"]) == {"about", "posts", "jobs"}

    async def test_the_walk_follows_the_section_table_not_the_caller(self, mock_page):
        """Order comes from ``COMPANY_SECTIONS``, and the caller cannot move it.

        A synthetic table rather than the real three-entry one, which is a
        claim about the ordering rule alone: a plain ``set`` of three strings
        has only six iteration orders and two of them are the table's, so the
        real sections let a caller-ordered walk pass on a coincidence. Six
        entries leave one such coincidence in 720, and `requested | {"about"}`
        rebuilds a plain ``set`` whatever the caller passed, so there is no way
        to pin its order from the outside instead.
        """
        table = {
            "about": ("/about/", False),
            "zeta": ("/zeta/", False),
            "alpha": ("/alpha/", False),
            "posts": ("/posts/", False),
            "beta": ("/beta/", False),
            "jobs": ("/jobs/", False),
        }
        scraper = _scraper(mock_page)
        with (
            patch.object(company_module, "COMPANY_SECTIONS", table),
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_company("testcorp", set(table))

        assert [
            capture_call.args[1] for capture_call in mock_extract.call_args_list
        ] == list(table)
        assert [
            capture_call.args[2].mode for capture_call in mock_extract.call_args_list
        ] == [
            CaptureMode.STANDARD,
            CaptureMode.STANDARD,
            CaptureMode.STANDARD,
            CaptureMode.ACTIVITY,
            CaptureMode.STANDARD,
            CaptureMode.STANDARD,
        ]
        assert list(result["sections"]) == list(table)

    async def test_custom_overlay_table_routes_through_compatibility_seam(
        self, mock_page
    ):
        table = {
            "about": ("/custom-about-overlay/", True),
            "custom": ("/custom-standard/", False),
        }
        scraper = _scraper(mock_page)
        with (
            patch.object(company_module, "COMPANY_SECTIONS", table),
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                return_value=extracted("standard text"),
            ) as mock_capture,
            patch.object(
                scraper._capture,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted("overlay text"),
            ) as mock_overlay,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_company("testcorp", set(table))

        mock_overlay.assert_awaited_once()
        assert mock_overlay.call_args.args[:2] == (
            "https://www.linkedin.com/company/testcorp/custom-about-overlay/",
            "about",
        )
        assert mock_overlay.call_args.kwargs["plan"] == CapturePlan(CaptureMode.OVERLAY)
        mock_capture.assert_awaited_once_with(
            "https://www.linkedin.com/company/testcorp/custom-standard/",
            "custom",
            CapturePlan(CaptureMode.STANDARD),
        )
        assert result["sections"] == {
            "about": "overlay text",
            "custom": "standard text",
        }

    async def test_the_delay_is_taken_between_sections_and_not_before_the_first(
        self, mock_page
    ):
        """One pace per gap, at ``NAV_DELAY``, through the session boundary.

        The duration is asserted as well as the count: a delay of the wrong
        length paces the walk wrongly against LinkedIn while every
        count-only assertion stays green. Jitter is pinned to identity so
        the length is the configured one.
        """
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
            _no_jitter(),
        ):
            await scraper.scrape_company("testcorp", {"about", "posts", "jobs"})

        assert mock_sleep.await_args_list == [call(NAV_DELAY), call(NAV_DELAY)]

    async def test_a_single_section_walk_never_paces(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                return_value=extracted("about text"),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
        ):
            await scraper.scrape_company("testcorp", {"about"})

        mock_sleep.assert_not_awaited()

    async def test_a_rate_limited_company_section_is_reported_and_stops_the_rest(
        self, mock_page
    ):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[
                    extracted(RATE_LIMITED_SECTION_TEXT),
                    extracted("Posts text"),
                ],
            ) as mock_extract,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_company("testcorp", {"posts"})

        assert "about" not in result["sections"]
        assert result["section_errors"]["about"]["error_type"] == "rate_limit"
        assert mock_extract.await_count == 1
        assert "posts" not in result["sections"]

    async def test_the_rate_limited_section_is_still_reported_as_progress(
        self, mock_page
    ):
        """The stop happens after that section's callback, not instead of it.

        A caller watching progress otherwise sees the walk end one section
        before the one that failed, and the section carrying the only
        diagnostic is the one it never hears about.
        """
        scraper = _scraper(mock_page)
        cb = MagicMock(spec=ProgressCallback)
        cb.on_start = AsyncMock()
        cb.on_progress = AsyncMock()
        cb.on_complete = AsyncMock()
        cb.on_error = AsyncMock()

        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                return_value=extracted(RATE_LIMITED_SECTION_TEXT),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_company(
                "testcorp", {"about", "posts", "jobs"}, callbacks=cb
            )

        assert [c.args for c in cb.on_progress.call_args_list] == [
            ("Scraped about (1/3)", 32)
        ]
        cb.on_complete.assert_awaited_once_with("company profile", result)
        cb.on_error.assert_not_awaited()

    async def test_scrape_company_extracts_company_urn(self, mock_page):
        """End-to-end: a canned-search anchor on the company about page
        produces a ``company_urn`` reference with the parent-company id.

        Stubs ``_extract_root_content`` (rather than ``extract_page``) so
        the real ``build_references`` pipeline runs against raw anchor
        data, mirroring what the JS crawler emits live.
        """
        scraper = _scraper(mock_page)
        raw_root = {
            "source": "root",
            "text": "About SAP\nCompany overview",
            "references": [
                {
                    "href": "https://www.linkedin.com/search/results/people/"
                    "?currentCompany=%5B%221115%22%5D"
                    "&origin=COMPANY_PAGE_CANNED_SEARCH",
                    "text": "10K+ employees",
                    "aria_label": "",
                    "title": "",
                    "heading": "",
                    "in_article": False,
                    "in_nav": False,
                    "in_footer": False,
                }
            ],
        }
        with (
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=raw_root,
            ),
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
            result = await scraper.scrape_company("sap", {"about"})

        urns = [
            ref for ref in result["references"]["about"] if ref["kind"] == "company_urn"
        ]
        assert len(urns) == 1
        assert urns[0]["value"] == "1115"
        assert urns[0]["url"] == (
            "/search/results/people/?currentCompany=%5B%221115%22%5D"
        )
        assert "text" not in urns[0]

    async def test_a_clean_walk_omits_the_optional_keys(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                return_value=extracted("about text"),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_company("testcorp", {"about"})

        assert result == {
            "url": "https://www.linkedin.com/company/testcorp/",
            "sections": {"about": "about text"},
        }

    async def test_an_unclassified_section_failure_is_isolated_as_a_diagnostic(
        self, mock_page
    ):
        failure = RuntimeError("boom")
        diagnostics = MagicMock(return_value={"issue_template_path": "/tmp/issue.md"})
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[failure, extracted("Posts text")],
            ),
            patch.object(company_module, "build_issue_diagnostics", diagnostics),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.scrape_company("testcorp", {"posts"})

        # The walk continues past it, and the report names the workflow rather
        # than the collaborator the call happened to pass through.
        assert result["sections"] == {"posts": "Posts text"}
        assert result["section_errors"] == {
            "about": {"issue_template_path": "/tmp/issue.md"}
        }
        assert diagnostics.call_args_list == [
            call(
                failure,
                context="scrape_company",
                target_url="https://www.linkedin.com/company/testcorp/about/",
                section_name="about",
            )
        ]

    async def test_a_classified_section_failure_aborts_the_walk(self, mock_page):
        """A domain exception leaves the loop instead of becoming a diagnostic.

        Both halves matter: it re-raises to the caller, and the progress
        callback hears ``on_error`` rather than a completion. Swallowing it
        into ``section_errors`` would report a rate limit or an expired
        session as one section's bad luck and keep navigating.
        """
        scraper = _scraper(mock_page)
        cb = MagicMock(spec=ProgressCallback)
        cb.on_start = AsyncMock()
        cb.on_progress = AsyncMock()
        cb.on_complete = AsyncMock()
        cb.on_error = AsyncMock()
        failure = AuthenticationError("session expired")

        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=failure,
            ) as mock_extract,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            with pytest.raises(AuthenticationError):
                await scraper.scrape_company(
                    "testcorp", {"about", "posts", "jobs"}, callbacks=cb
                )

        assert mock_extract.await_count == 1
        cb.on_error.assert_awaited_once_with(failure)
        cb.on_progress.assert_not_awaited()
        cb.on_complete.assert_not_awaited()

    async def test_a_closed_target_is_reraised_rather_than_filed_as_a_section_error(
        self, mock_page
    ):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=TargetClosedError("closed"),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            pytest.raises(TargetClosedError),
        ):
            await scraper.scrape_company("testcorp", {"about"})


class TestScrapeCompanyCallbacks:
    """Test that scrape_company invokes callbacks at each stage."""

    async def test_scrape_company_calls_callbacks(self, mock_page):
        scraper = _scraper(mock_page)
        cb = MagicMock(spec=ProgressCallback)
        cb.on_start = AsyncMock()
        cb.on_progress = AsyncMock()
        cb.on_complete = AsyncMock()
        cb.on_error = AsyncMock()

        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await scraper.scrape_company(
                "testcorp", {"about", "posts", "jobs"}, callbacks=cb
            )

        cb.on_start.assert_awaited_once()
        assert cb.on_start.call_args[0][0] == "company profile"

        # 3 sections: about + posts + jobs
        assert cb.on_progress.await_count == 3
        messages = [c.args[0] for c in cb.on_progress.call_args_list]
        assert messages == [
            "Scraped about (1/3)",
            "Scraped posts (2/3)",
            "Scraped jobs (3/3)",
        ]
        # 95 rather than 100 at the end: the walk reports its own last section,
        # and the remaining 5 belong to whoever assembles the answer.
        assert [c.args[1] for c in cb.on_progress.call_args_list] == [32, 63, 95]

        cb.on_complete.assert_awaited_once()
        assert cb.on_complete.call_args[0][0] == "company profile"
        cb.on_error.assert_not_awaited()

    async def test_a_closed_target_reaches_on_error(self, mock_page):
        scraper = _scraper(mock_page)
        callbacks = AsyncMock()
        closed = TargetClosedError("closed")
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=closed,
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            pytest.raises(TargetClosedError),
        ):
            await scraper.scrape_company("testcorp", {"about"}, callbacks=callbacks)
        callbacks.on_error.assert_awaited_once_with(closed)


class TestGetCompanyEmployees:
    async def test_a_pasted_company_link_reaches_the_canonical_people_url(
        self, mock_page
    ):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("employees"),
        ) as mock_extract:
            result = await scraper.get_company_employees(
                "https://de.linkedin.com/company/testco/about/"
            )

        assert mock_extract.await_args_list == [
            call(
                "https://www.linkedin.com/company/testco/people/",
                "employees",
                CapturePlan(CaptureMode.COMPANY_PEOPLE),
            )
        ]
        assert result["url"] == "https://www.linkedin.com/company/testco/people/"

    async def test_no_keywords_leaves_the_url_unqueried(self, mock_page):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("employees"),
        ) as mock_extract:
            await scraper.get_company_employees("testcorp", None)

        assert mock_extract.call_args.args[0] == (
            "https://www.linkedin.com/company/testcorp/people/"
        )

    async def test_keywords_reach_the_url_percent_encoded(self, mock_page):
        """``quote_plus``, not the raw string.

        ``&`` is the value that separates a query parameter from the next, so
        an unencoded one turns the rest of the search term into a second
        parameter LinkedIn reads as a filter of its own.
        """
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("employees"),
        ) as mock_extract:
            result = await scraper.get_company_employees("testcorp", "R&D lead")

        expected = (
            "https://www.linkedin.com/company/testcorp/people/?keywords=R%26D+lead"
        )
        assert mock_extract.call_args.args[0] == expected
        assert result["url"] == expected

    async def test_references_and_errors_are_omitted_when_empty(self, mock_page):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("employee text"),
        ):
            result = await scraper.get_company_employees("testcorp")

        assert result == {
            "url": "https://www.linkedin.com/company/testcorp/people/",
            "sections": {"employees": "employee text"},
        }

    async def test_references_are_reported_under_the_section_name(self, mock_page):
        reference: Reference = {"kind": "person", "url": "/in/someone/"}
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("employee text", [reference]),
        ):
            result = await scraper.get_company_employees("testcorp")

        assert result["references"] == {"employees": [reference]}

    async def test_a_traversal_identifier_is_refused_before_navigating(self, mock_page):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture, "capture", new_callable=AsyncMock
        ) as mock_extract:
            with pytest.raises(InvalidReferenceError):
                await scraper.get_company_employees("../../feed")

        mock_extract.assert_not_awaited()
        mock_page.goto.assert_not_awaited()


class TestSearchCompanies:
    async def test_the_results_page_is_returned_under_the_search_url(self, mock_page):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("Fintech Inc"),
        ) as mock_extract:
            result = await scraper.search_companies("fintech")

        url = mock_extract.call_args.args[0]
        assert (
            url == "https://www.linkedin.com/search/results/companies/?keywords=fintech"
        )
        assert mock_extract.call_args.args[1] == "search_results"
        assert mock_extract.call_args.args[2].mode is CaptureMode.SEARCH_RESULTS
        assert result == {
            "url": url,
            "sections": {"search_results": "Fintech Inc"},
            "companies": [],
            "result_count": None,
        }

    async def test_an_empty_result_omits_the_optional_keys(self, mock_page):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted(""),
        ):
            result = await scraper.search_companies("nothing matches this")

        assert result["sections"] == {}
        assert result["companies"] == []
        assert result["result_count"] is None
        assert "references" not in result
        assert "section_errors" not in result

    async def test_a_navigation_error_surfaces_as_a_section_error(self, mock_page):
        error: dict[str, Any] = {
            "error_type": "navigation_error",
            "error_message": "timeout",
        }
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("", error=error),
        ):
            result = await scraper.search_companies("fintech")

        assert result["sections"] == {}
        assert result["section_errors"] == {"search_results": error}

    async def test_hq_location_resolves_to_company_hq_geo(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                return_value=extracted("Stripe"),
            ),
            patch.object(
                scraper._facets,
                "resolve_geo_urn",
                new_callable=AsyncMock,
                return_value="101282230",
            ) as resolve,
        ):
            result = await scraper.search_companies(hq_location="Germany")

        resolve.assert_awaited_once_with("Germany")
        assert "companyHqGeo=%5B%22101282230%22%5D" in result["url"]
        assert "geoUrn" not in result["url"]

    async def test_unresolvable_hq_location_raises_before_any_page_loads(
        self, mock_page
    ):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture, "capture", new_callable=AsyncMock
            ) as mock_extract,
            patch.object(
                scraper._facets,
                "resolve_geo_urn",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            with pytest.raises(FilterValidationError, match="Could not resolve"):
                await scraper.search_companies(hq_location="Nowhereland")

        mock_extract.assert_not_awaited()

    async def test_a_resolver_that_navigated_paces_the_first_page(self, mock_page):
        """The first results page follows a facet navigation, so it is spaced
        like every later hop rather than fired straight after the dropdown."""
        scraper = _scraper(mock_page)

        async def resolve(location: str) -> str:
            scraper._facets.navigated = True
            return "101282230"

        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                return_value=extracted("Stripe"),
            ),
            patch.object(scraper._facets, "resolve_geo_urn", side_effect=resolve),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
            _no_jitter(),
        ):
            await scraper.search_companies(hq_location="Germany")

        assert mock_sleep.await_args_list == [call(NAV_DELAY)]

    async def test_every_facet_reaches_the_url(self, mock_page):
        """The URL grammar is pinned in ``test_search_urls``; this pins that
        each keyword argument is handed to it rather than dropped."""
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                return_value=extracted("Stripe"),
            ),
            patch.object(
                scraper._facets,
                "resolve_geo_urn",
                new_callable=AsyncMock,
                return_value="101282230",
            ),
        ):
            result = await scraper.search_companies(
                "payments",
                industry=["Financial Services"],
                size=["C"],
                hq_location="Germany",
                has_jobs=True,
            )

        assert result["url"] == (
            "https://www.linkedin.com/search/results/companies/?keywords=payments"
            "&industryCompanyVertical=%5B%2243%22%5D&companySize=%5B%22C%22%5D"
            "&companyHqGeo=%5B%22101282230%22%5D&hasJobs=%22true%22"
        )

    @pytest.mark.parametrize(
        "kwargs",
        [{}, {"keywords": ""}, {"has_jobs": True}, {"industry": [], "size": []}],
    )
    async def test_no_narrowing_criteria_raises(self, mock_page, kwargs):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture, "capture", new_callable=AsyncMock
        ) as mock_extract:
            with pytest.raises(FilterValidationError, match="at least one of"):
                await scraper.search_companies(**kwargs)

        mock_extract.assert_not_awaited()

    async def test_facet_validation_runs_before_the_location_is_resolved(
        self, mock_page
    ):
        """A bad size or industry is refused without a browser round-trip."""
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._facets, "resolve_geo_urn", new_callable=AsyncMock
        ) as resolve:
            with pytest.raises(FilterValidationError, match="Unknown company size"):
                await scraper.search_companies(
                    "fintech", size=["huge"], hq_location="Germany"
                )

        resolve.assert_not_awaited()


class TestSearchCompaniesPagination:
    """``max_pages`` walks ``&page=N`` and stops once a page adds no company."""

    @staticmethod
    def _page(n: int) -> ExtractedSection:
        return extracted(
            f"Company {n}",
            [{"kind": "company", "url": f"/company/co{n}/", "text": f"Company {n}"}],
        )

    async def test_default_fetches_only_first_page(self, mock_page):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "capture",
            new_callable=AsyncMock,
            side_effect=[self._page(1), self._page(2)],
        ) as fetch:
            result = await scraper.search_companies("fintech")

        assert fetch.await_count == 1
        assert "&page=" not in fetch.await_args_list[0].args[0]
        assert result["sections"]["search_results"] == "Company 1"

    async def test_pages_are_joined_and_references_merged(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[self._page(1), self._page(2), self._page(3)],
            ) as fetch,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
            _no_jitter(),
        ):
            result = await scraper.search_companies("fintech", max_pages=3)

        assert fetch.await_count == 3
        urls = [call.args[0] for call in fetch.await_args_list]
        assert "&page=" not in urls[0]
        assert urls[1].endswith("&page=2")
        assert urls[2].endswith("&page=3")
        # One pause per page after the first, before its navigation.
        assert mock_sleep.await_args_list == [call(NAV_DELAY), call(NAV_DELAY)]
        assert (
            result["sections"]["search_results"]
            == "Company 1\n---\nCompany 2\n---\nCompany 3"
        )
        assert [r["url"] for r in result["references"]["search_results"]] == [
            "/company/co1/",
            "/company/co2/",
            "/company/co3/",
        ]
        assert "&page=" not in result["url"]

    async def test_stops_when_a_page_adds_no_new_company(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[self._page(1), self._page(1), self._page(3)],
            ) as fetch,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_companies("fintech", max_pages=10)

        assert fetch.await_count == 2
        assert result["sections"]["search_results"] == "Company 1\n---\nCompany 1"
        assert [r["url"] for r in result["references"]["search_results"]] == [
            "/company/co1/"
        ]

    async def test_only_company_refs_count_as_new(self, mock_page):
        """A page of nothing but people/job anchors is the end of the results."""
        scraper = _scraper(mock_page)
        filler = extracted(
            "Sidebar",
            [{"kind": "person", "url": "/in/someone/", "text": "Someone"}],
        )
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[self._page(1), filler, self._page(3)],
            ) as fetch,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await scraper.search_companies("fintech", max_pages=10)

        assert fetch.await_count == 2

    async def test_rate_limit_midway_keeps_earlier_pages(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[self._page(1), extracted(RATE_LIMITED_SECTION_TEXT)],
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_companies("fintech", max_pages=5)

        assert result["sections"]["search_results"] == "Company 1"
        assert result["section_errors"]["search_results"]["error_type"] == "rate_limit"


class TestSearchCompaniesRows:
    """``companies`` rows parsed per page, before the pages are joined."""

    async def test_anchors_without_rows_warn_of_an_unrecognised_layout(
        self, mock_page, caplog
    ):
        # Company cards are found by their followers line; a locale that
        # spells it differently yields no rows from a page full of anchors.
        page = "Acme\nSoftware\nOslo, Oslo\nFolgen\nTools\n1.200 Follower:innen"
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                return_value=extracted(page, [_company_ref("acme", "Acme")]),
            ),
            caplog.at_level(
                logging.WARNING, logger="linkedin_mcp_server.scraping.search_pages"
            ),
        ):
            result = await scraper.search_companies("tools")

        assert result["companies"] == []
        assert [r.message for r in caplog.records if r.levelno == logging.WARNING] == [
            "Page 1: 1 references but no result rows parsed (unrecognised card "
            "layout or locale)"
        ]

    async def test_rows_pair_before_the_reference_cap(self, mock_page):
        # Sixteen cards is one past the section cap; the page is extracted
        # uncapped so the last card still finds its anchor.
        cards = [
            _company_card(f"Company {i}", "Banking", "Bern, Bern", "Vaults")
            for i in range(1, 17)
        ]
        refs = [_company_ref(f"company-{i}", f"Company {i}") for i in range(1, 17)]
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("\n\n".join(cards), refs),
        ) as mock_extract:
            result = await scraper.search_companies("bank")

        assert mock_extract.await_args is not None
        assert mock_extract.await_args.args[2].apply_cap is False
        assert [r["url"] for r in result["companies"]] == [
            f"/company/company-{i}/" for i in range(1, 17)
        ]
        assert len(result["references"]["search_results"]) == 15

    # Beta is the repeat. Delta closes page 1: a company card is read
    # backwards from its followers line, and joined before parsing that line
    # would run into the separator and page 2's header and stop being one,
    # so Delta would be lost rather than shifted.
    PAGE_1 = "About 5,200 results\n\n" + "\n\n".join(
        [
            _company_card("Acme", "Software Development", "Austin, Texas", "Tools"),
            _company_card("Beta Ltd", "Financial Services", "London, England", "Pay"),
            _company_card("Delta", "Insurance", "Oslo, Oslo", "Cover"),
        ]
    )
    PAGE_2 = "About 5,100 results\n\n" + "\n\n".join(
        [
            _company_card("Beta Ltd", "Financial Services", "London, England", "Pay"),
            _company_card("Gamma", "Banking", "Zurich, Zurich", "Vault"),
        ]
    )

    @classmethod
    def _pages(cls) -> list[ExtractedSection]:
        return [
            extracted(
                cls.PAGE_1,
                [
                    _company_ref("acme", "Acme"),
                    _company_ref("beta", "Beta Ltd"),
                    _company_ref("delta", "Delta"),
                ],
            ),
            extracted(
                cls.PAGE_2,
                [_company_ref("beta", "Beta Ltd"), _company_ref("gamma", "Gamma")],
            ),
        ]

    async def test_rows_are_parsed_per_page_and_deduped_by_url(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=self._pages(),
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_companies("fintech", max_pages=2)

        assert [(row["name"], row["url"]) for row in result["companies"]] == [
            ("Acme", "/company/acme/"),
            ("Beta Ltd", "/company/beta/"),
            ("Delta", "/company/delta/"),
            ("Gamma", "/company/gamma/"),
        ]
        assert result["companies"][2] == {
            "name": "Delta",
            "industry": "Insurance",
            "location": "Oslo, Oslo",
            "tagline": "Cover",
            "followers": 20000,
            "url": "/company/delta/",
        }
        assert result["result_count"] == 5200
        assert result["sections"]["search_results"] == (
            self.PAGE_1 + "\n---\n" + self.PAGE_2
        )

    async def test_a_parser_failure_keeps_the_raw_text(self, mock_page, caplog):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=self._pages()[:1],
            ),
            patch.object(
                company_module,
                "parse_company_cards",
                side_effect=RuntimeError("parser bug"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = await scraper.search_companies("fintech")

        assert result["companies"] == []
        assert result["result_count"] == 5200
        assert result["sections"]["search_results"] == self.PAGE_1
        assert "Could not parse result cards on page 1" in caplog.text


def test_the_real_section_table_is_the_one_the_walk_orders_by():
    """The synthetic-table test above says nothing about the real sections.

    Iteration order is what `scrape_company` reads out of this mapping, so a
    reordered literal is a behavior change and belongs in a diff that says so.
    """
    assert list(COMPANY_SECTIONS) == ["about", "posts", "jobs"]
    assert list(company_module.COMPANY_SECTIONS) == list(COMPANY_SECTIONS)
