"""Tests for the post content-search owner."""

from __future__ import annotations

from typing import Any
from unittest.mock import ANY, AsyncMock, call, patch

import pytest

from linkedin_mcp_server.scraping import posts as posts_module
from linkedin_mcp_server.scraping.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
    rate_limited_section_error,
)
from linkedin_mcp_server.scraping.link_metadata import Reference
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.posts import PostSearch
from linkedin_mcp_server.scraping.session import ScrapingSession


def _search(page) -> PostSearch:
    """Wire the post-search owner the way the facade does."""
    session = ScrapingSession(page)
    navigator = PageNavigator(session)
    return PostSearch(SectionCapture(session, navigator, PageContentReader(session)))


def extracted(
    text: str,
    references: list[Reference] | None = None,
    error: dict | None = None,
) -> ExtractedSection:
    """Create an ExtractedSection for tests."""
    return ExtractedSection(text=text, references=references or [], error=error)


class TestSearchPosts:
    async def test_the_results_page_is_returned_under_the_content_search_url(
        self, mock_page
    ):
        search = _search(mock_page)
        with patch.object(
            search._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("We're hiring a Unity dev"),
        ) as mock_extract:
            result = await search.search_posts("Buscamos Unity")

        assert "/search/results/content/" in result["url"]
        assert "origin=FACETED_SEARCH" in result["url"]
        assert result["sections"]["search_results"] == "We're hiring a Unity dev"
        # ``CONTENT_SEARCH`` is what makes ``max_posts`` mean anything: the
        # plan ignores it under every other mode, so a default that forwards
        # the count without the mode scrolls to no depth at all.
        assert mock_extract.await_args_list == [
            call(
                result["url"],
                section_name="search_results",
                plan=CapturePlan(
                    CaptureMode.SEARCH_RESULTS | CaptureMode.CONTENT_SEARCH,
                    max_posts=10,
                    apply_cap=False,
                ),
            )
        ]

    async def test_the_recency_filter_reaches_the_url(self, mock_page):
        search = _search(mock_page)
        with patch.object(
            search._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("post"),
        ) as mock_extract:
            result = await search.search_posts("Buscamos Unity", "past-week")

        assert "datePosted=%5B%22past-week%22%5D" in result["url"]
        assert mock_extract.call_args.args[0] == result["url"]

    async def test_max_posts_reaches_the_capture_plan(self, mock_page):
        """The count goes through untouched, with no depth alongside it.

        Content search is an infinite scroll with no per-page URL, so the
        capture counts loaded cards instead of scrolling a fixed depth. A
        ``max_scrolls`` next to the count would re-introduce the depth budget
        this replaced; asserted at a value that is not the default so a
        dropped argument cannot pass as one that was forwarded.
        """
        search = _search(mock_page)
        with patch.object(
            search._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("post"),
        ) as mock_extract:
            await search.search_posts("python", max_posts=25)

        assert mock_extract.await_args_list == [
            call(
                ANY,
                section_name="search_results",
                plan=CapturePlan(
                    CaptureMode.SEARCH_RESULTS | CaptureMode.CONTENT_SEARCH,
                    max_posts=25,
                    apply_cap=False,
                ),
            )
        ]

    async def test_an_invalid_recency_filter_is_refused_before_the_page_is_read(
        self, mock_page
    ):
        """The URL is built first, so LinkedIn never sees the query at all.

        LinkedIn ignores a filter it does not recognise and answers with
        unfiltered results, which look filtered to whoever asked. Building
        after the capture would still raise, so the assertions that nothing
        was read are what hold the ordering.
        """
        search = _search(mock_page)
        with patch.object(
            search._capture, "capture", new_callable=AsyncMock
        ) as mock_extract:
            with pytest.raises(ValueError, match="Invalid date_posted"):
                await search.search_posts("python", date_posted="last-year")

        mock_extract.assert_not_awaited()
        mock_page.goto.assert_not_awaited()

    async def test_an_empty_result_omits_the_optional_keys(self, mock_page):
        search = _search(mock_page)
        with patch.object(
            search._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted(""),
        ) as mock_extract:
            result = await search.search_posts("nothing matches this query")

        assert result == {
            "url": mock_extract.call_args.args[0],
            "sections": {},
        }

    async def test_the_reference_cap_follows_max_posts(self, mock_page):
        """The section cap is 15, ``max_posts`` allows 50: with the cap applied
        inside the capture a 30-post search handed back 15 authors and the
        prospect list stopped halfway. The capture returns every anchor and
        the owner caps at ``max(max_posts, 15)``, as the paged searches do."""
        refs: list[Reference] = [
            {"kind": "person", "url": f"/in/author{i}/"} for i in range(40)
        ]
        search = _search(mock_page)
        with patch.object(
            search._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("posts", refs),
        ) as mock_extract:
            result = await search.search_posts("python", max_posts=30)

        mock_extract.assert_awaited_once_with(
            ANY,
            section_name="search_results",
            plan=CapturePlan(
                CaptureMode.SEARCH_RESULTS | CaptureMode.CONTENT_SEARCH,
                max_posts=30,
                apply_cap=False,
            ),
        )
        assert result["references"]["search_results"] == refs[:30]

    async def test_references_are_reported_under_the_section_name(self, mock_page):
        reference: Reference = {"kind": "person", "url": "/in/someone/"}
        search = _search(mock_page)
        with patch.object(
            search._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("post text", [reference]),
        ):
            result = await search.search_posts("python")

        assert result["references"] == {"search_results": [reference]}
        assert "section_errors" not in result

    async def test_a_rate_limited_page_is_an_error_rather_than_content(self, mock_page):
        search = _search(mock_page)
        with patch.object(
            search._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted(RATE_LIMITED_SECTION_TEXT),
        ):
            result = await search.search_posts("python")

        # The sentinel is checked before the text is accepted, or the banner
        # itself would be handed back as the results page.
        assert result["sections"] == {}
        assert "references" not in result
        assert result["section_errors"] == {
            "search_results": {
                "error_type": "rate_limit",
                "error_message": RATE_LIMITED_SECTION_TEXT,
            }
        }

    async def test_the_rate_limit_entry_echoes_the_text_it_classified(self, mock_page):
        """Built inline from ``extracted.text``, not by the shared helper.

        Every other workflow calls ``rate_limited_section_error()`` here, and
        under the real constant the two are indistinguishable: the helper
        returns the same two keys with the same sentinel. A substituted
        sentinel is the only thing that separates them, and it separates them
        the way the asymmetry matters — this entry reports the text that was
        actually classified, while the helper reports the constant whatever
        was read.
        """
        search = _search(mock_page)
        with (
            patch.object(posts_module, "RATE_LIMITED_SECTION_TEXT", "[Blocked]"),
            patch.object(
                search._capture,
                "capture",
                new_callable=AsyncMock,
                return_value=extracted("[Blocked]"),
            ),
        ):
            result = await search.search_posts("python")

        assert result["section_errors"]["search_results"] == {
            "error_type": "rate_limit",
            "error_message": "[Blocked]",
        }
        assert (
            result["section_errors"]["search_results"] != rate_limited_section_error()
        )

    async def test_a_navigation_error_surfaces_as_a_section_error(self, mock_page):
        error: dict[str, Any] = {
            "error_type": "navigation_error",
            "error_message": "timeout",
        }
        search = _search(mock_page)
        with patch.object(
            search._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("", error=error),
        ):
            result = await search.search_posts("python")

        assert result["sections"] == {}
        assert result["section_errors"] == {"search_results": error}
