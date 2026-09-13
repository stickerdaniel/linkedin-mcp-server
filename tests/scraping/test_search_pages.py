"""Tests for the paged search walk shared by the search workflows."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server.scraping.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
)
from linkedin_mcp_server.scraping.link_metadata import Reference
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.search_pages import SearchPages, paginate_search
from linkedin_mcp_server.scraping.session import NAV_DELAY, ScrapingSession

PEOPLE = "https://www.linkedin.com/search/results/people/?keywords=engineer"


def extracted(
    text: str,
    references: list[Reference] | None = None,
    error: dict | None = None,
) -> ExtractedSection:
    """Create an ExtractedSection for tests."""
    return ExtractedSection(text=text, references=references or [], error=error)


def _capture(page) -> SectionCapture:
    """Wire the capture owner the way the facade does."""
    session = ScrapingSession(page)
    return SectionCapture(session, PageNavigator(session), PageContentReader(session))


def _page(n: int) -> ExtractedSection:
    """One results page holding a single, page-unique person."""
    return extracted(
        f"Person {n}",
        [{"kind": "person", "url": f"/in/person{n}/", "text": f"Person {n}"}],
    )


class TestPaginateSearch:
    """``max_pages`` walks LinkedIn's ``&page=N`` facet (issue #526)."""

    @staticmethod
    def _walk(
        capture: SectionCapture,
        *,
        kind: str = "person",
        max_pages: int = 1,
    ):
        return paginate_search(
            capture,
            capture._session,
            PEOPLE,
            kind=kind,
            max_pages=max_pages,
        )

    async def test_search_pages_starts_empty(self):
        gathered = SearchPages()

        assert gathered == SearchPages([], [], {})
        # Fresh containers per instance, not one shared default.
        gathered.page_texts.append("x")
        assert SearchPages().page_texts == []

    async def test_one_page_by_default_with_the_search_plan(self, mock_page):
        capture = _capture(mock_page)
        with (
            patch.object(
                capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[_page(1), _page(2)],
            ) as fetch,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ) as sleep,
        ):
            gathered = await self._walk(capture)

        assert fetch.await_count == 1
        assert fetch.await_args_list[0].args == (
            PEOPLE,
            "search_results",
            CapturePlan(CaptureMode.SEARCH_RESULTS),
        )
        assert gathered.page_texts == ["Person 1"]
        assert gathered.section_errors == {}
        sleep.assert_not_awaited()

    async def test_pages_are_joined_in_order_and_paced_between(self, mock_page):
        capture = _capture(mock_page)
        with (
            patch.object(
                capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[_page(1), _page(2), _page(3)],
            ) as fetch,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ) as sleep,
            patch(
                "linkedin_mcp_server.scraping.session.jitter",
                side_effect=lambda base, spread=0.5: base,
            ),
        ):
            gathered = await self._walk(capture, max_pages=3)

        urls = [c.args[0] for c in fetch.await_args_list]
        assert urls == [PEOPLE, f"{PEOPLE}&page=2", f"{PEOPLE}&page=3"]
        # One pause per page after the first, before its navigation, at the
        # navigation delay.
        assert [c.args for c in sleep.await_args_list] == [
            (NAV_DELAY,),
            (NAV_DELAY,),
        ]
        assert gathered.page_texts == ["Person 1", "Person 2", "Person 3"]
        assert [r["url"] for r in gathered.page_references] == [
            "/in/person1/",
            "/in/person2/",
            "/in/person3/",
        ]

    async def test_stops_when_a_page_repeats_people(self, mock_page):
        """Running past the last page re-serves it; stop instead of looping."""
        capture = _capture(mock_page)
        with (
            patch.object(
                capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[_page(1), _page(1), _page(3)],
            ) as fetch,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            gathered = await self._walk(capture, max_pages=10)

        assert fetch.await_count == 2
        # The repeated page is kept -- it is real text, just not new people.
        assert gathered.page_texts == ["Person 1", "Person 1"]
        assert [r["url"] for r in gathered.page_references] == [
            "/in/person1/",
            "/in/person1/",
        ]
        assert gathered.section_errors == {}

    async def test_only_references_of_the_walked_kind_count_as_new(self, mock_page):
        """A page of nothing but company/job anchors is the end of the
        people results."""
        capture = _capture(mock_page)
        filler = extracted(
            "Sidebar",
            [{"kind": "company", "url": "/company/acme/", "text": "Acme"}],
        )
        with (
            patch.object(
                capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[_page(1), filler, _page(3)],
            ) as fetch,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            gathered = await self._walk(capture, max_pages=10)

        assert fetch.await_count == 2
        assert gathered.page_texts == ["Person 1", "Sidebar"]

    async def test_kind_selects_which_anchors_keep_the_walk_going(self, mock_page):
        capture = _capture(mock_page)
        company = extracted(
            "Acme", [{"kind": "company", "url": "/company/acme/", "text": "Acme"}]
        )
        with (
            patch.object(
                capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[company, _page(2), _page(3)],
            ) as fetch,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await self._walk(capture, kind="company", max_pages=3)

        # Page 2 carries people only, which is not a new company.
        assert fetch.await_count == 2

    async def test_rate_limit_midway_keeps_earlier_pages(self, mock_page):
        capture = _capture(mock_page)
        with (
            patch.object(
                capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[_page(1), extracted(RATE_LIMITED_SECTION_TEXT), _page(3)],
            ) as fetch,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            gathered = await self._walk(capture, max_pages=5)

        assert fetch.await_count == 2
        assert gathered.page_texts == ["Person 1"]
        assert gathered.section_errors["search_results"]["error_type"] == "rate_limit"

    async def test_a_throttled_page_reports_the_rate_limit_over_its_error(
        self, mock_page
    ):
        # The more specific diagnosis wins when a page carries both.
        capture = _capture(mock_page)
        throttled = extracted(
            RATE_LIMITED_SECTION_TEXT, error={"error_type": "NetworkError"}
        )
        with patch.object(
            capture, "capture", new_callable=AsyncMock, return_value=throttled
        ):
            gathered = await self._walk(capture)

        assert gathered.section_errors["search_results"]["error_type"] == "rate_limit"

    async def test_an_errored_page_surfaces_its_diagnostics(self, mock_page):
        capture = _capture(mock_page)
        failed = extracted("", error={"error_type": "NetworkError"})
        with patch.object(
            capture, "capture", new_callable=AsyncMock, return_value=failed
        ):
            gathered = await self._walk(capture)

        assert gathered.page_texts == []
        assert gathered.section_errors == {
            "search_results": {"error_type": "NetworkError"}
        }

    async def test_an_empty_page_without_an_error_is_not_reported(self, mock_page):
        capture = _capture(mock_page)
        with patch.object(
            capture, "capture", new_callable=AsyncMock, return_value=extracted("")
        ):
            gathered = await self._walk(capture)

        assert gathered == SearchPages()


@pytest.mark.parametrize("max_pages", [0, -1])
async def test_no_pages_requested_means_no_navigation(mock_page, max_pages):
    capture = _capture(mock_page)
    with patch.object(capture, "capture", new_callable=AsyncMock) as fetch:
        gathered = await paginate_search(
            capture,
            capture._session,
            PEOPLE,
            kind="person",
            max_pages=max_pages,
        )

    fetch.assert_not_awaited()
    assert gathered == SearchPages()
