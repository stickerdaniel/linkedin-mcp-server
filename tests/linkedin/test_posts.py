"""Tests for the post content-search owner."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse
from unittest.mock import ANY, AsyncMock, call, patch

import pytest

from linkedin_mcp_server.linkedin import posts as posts_module
from linkedin_mcp_server.linkedin.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
    rate_limited_section_error,
)
from linkedin_mcp_server.linkedin.link_metadata import Reference
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.posts import PostSearch
from linkedin_mcp_server.linkedin.session import PageSession


def _search(page) -> PostSearch:
    """Wire the post-search owner the way the facade does."""
    session = PageSession(page)
    navigator = PageNavigator(session)
    return PostSearch(
        session, SectionCapture(session, navigator, PageContentReader(session))
    )


def extracted(
    text: str,
    references: list[Reference] | None = None,
    error: dict | None = None,
) -> ExtractedSection:
    """Create an ExtractedSection for tests."""
    return ExtractedSection(text=text, references=references or [], error=error)


class TestSearchPosts:
    @pytest.mark.parametrize(
        ("sort_by", "token"),
        [
            ("date", "date_posted"),
            ("date_posted", "date_posted"),
            ("relevance", "relevance"),
        ],
    )
    async def test_requested_sort_reaches_capture_and_survives_settled_url(
        self, mock_page, sort_by, token
    ):
        search = _search(mock_page)
        reference: Reference = {
            "kind": "feed_post",
            "url": "/feed/update/urn:li:activity:123/",
        }

        async def capture(url, **kwargs):
            mock_page.url = url
            return extracted("Need an SRE", [reference])

        with patch.object(search._capture, "capture", side_effect=capture) as read:
            result = await search.search_posts("SRE", sort_by=sort_by)

        assert parse_qs(urlparse(read.call_args.args[0]).query)["sortBy"] == [
            f'["{token}"]'
        ]
        assert result["sections"] == {"search_results": "Need an SRE"}
        assert result["references"] == {"search_results": [reference]}
        assert "section_errors" not in result

    async def test_semantically_equivalent_settled_sort_is_retained(self, mock_page):
        search = _search(mock_page)

        async def capture(url, **kwargs):
            mock_page.url = (
                "https://www.linkedin.com/search/results/content/"
                "?origin=FACETED_SEARCH&sortBy=%5B%20%22date_posted%22%20%5D&keywords=SRE"
            )
            return extracted("Need an SRE")

        with patch.object(search._capture, "capture", side_effect=capture):
            result = await search.search_posts("SRE", sort_by=" date ")

        assert result["sections"] == {"search_results": "Need an SRE"}
        assert "section_errors" not in result

    @pytest.mark.parametrize("retained", [True, False])
    async def test_an_empty_success_still_reports_whether_requested_sort_was_kept(
        self, mock_page, retained
    ):
        search = _search(mock_page)

        async def capture(url, **kwargs):
            mock_page.url = (
                url
                if retained
                else "https://www.linkedin.com/search/results/content/?keywords=SRE"
            )
            return extracted("")

        with patch.object(search._capture, "capture", side_effect=capture):
            result = await search.search_posts("SRE", sort_by="date")

        assert result["sections"] == {}
        assert "references" not in result
        if retained:
            assert "section_errors" not in result
        else:
            assert (
                result["section_errors"]["search_results"]["error_type"]
                == "filters_dropped"
            )

    @pytest.mark.parametrize(
        "settled_query",
        [
            "keywords=SRE",
            "sortBy=%5B%22relevance%22%5D",
            "sortBy=date_posted",
            "sortBy=%5B%22date_posted%22%2C%22relevance%22%5D",
            "sortBy=%5B%22date_posted%22%5D&sortBy=%5B%22relevance%22%5D",
            "sortBy=%5B%22date_posted%22%5D&sortBy=%5B%22date_posted%22%5D",
            "sortBy=",
            "sortBy=%5B",
        ],
        ids=[
            "missing",
            "changed",
            "not-json",
            "two-values",
            "repeated",
            "repeated-same",
            "empty",
            "malformed",
        ],
    )
    async def test_a_dropped_or_invalid_settled_sort_warns_without_losing_results(
        self, mock_page, settled_query
    ):
        search = _search(mock_page)
        reference: Reference = {"kind": "feed_post", "url": "/posts/sre-mission"}

        async def capture(url, **kwargs):
            mock_page.url = (
                f"https://www.linkedin.com/search/results/content/?{settled_query}"
            )
            return extracted("Need an SRE", [reference])

        with patch.object(search._capture, "capture", side_effect=capture):
            result = await search.search_posts("SRE", sort_by="date")

        assert result["sections"] == {"search_results": "Need an SRE"}
        assert result["references"] == {"search_results": [reference]}
        warning = result["section_errors"]["search_results"]
        assert warning["error_type"] == "filters_dropped"
        assert "sort" in warning["error_message"].lower()
        assert parse_qs(urlparse(result["url"]).query)["sortBy"] == ['["date_posted"]']

    @pytest.mark.parametrize("sort_by", [None, "", " \t\n"])
    async def test_an_unrequested_sort_does_not_warn_about_the_current_page(
        self, mock_page, sort_by
    ):
        search = _search(mock_page)
        with patch.object(
            search._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("post"),
        ):
            result = await search.search_posts("python", sort_by=sort_by)

        assert "sortBy" not in parse_qs(urlparse(result["url"]).query)
        assert "section_errors" not in result

    @pytest.mark.parametrize("sort_by", [None, "", " \t\n"])
    async def test_omitted_sort_keeps_the_legacy_partial_result_contract(
        self, mock_page, sort_by
    ):
        search = _search(mock_page)
        reference: Reference = {"kind": "person", "url": "/in/someone/"}
        with patch.object(
            search._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted(
                "Partial text",
                [reference],
                {"error_type": "navigation_error", "error_message": "Interrupted"},
            ),
        ):
            result = await search.search_posts("SRE", sort_by=sort_by)

        assert result["sections"] == {"search_results": "Partial text"}
        assert result["references"] == {"search_results": [reference]}
        assert "section_errors" not in result

    async def test_invalid_sort_is_refused_before_any_browser_read(self, mock_page):
        search = _search(mock_page)
        with patch.object(search._capture, "capture", new_callable=AsyncMock) as read:
            with pytest.raises(ValueError, match="Invalid sort_by"):
                await search.search_posts("python", sort_by="latest")

        read.assert_not_awaited()
        mock_page.goto.assert_not_awaited()

    @pytest.mark.parametrize(
        ("text", "error"),
        [
            ("", {"error_type": "authentication_required", "error_message": "Sign in"}),
            ("", {"error_type": "navigation_error", "error_message": "Timeout"}),
            ("", {"error_type": "rate_limit", "error_message": "Throttled"}),
            (
                "Partial results",
                {"error_type": "navigation_error", "error_message": "Interrupted"},
            ),
            (RATE_LIMITED_SECTION_TEXT, None),
        ],
    )
    async def test_sort_warning_never_masks_an_existing_capture_failure(
        self, mock_page, text, error
    ):
        search = _search(mock_page)
        with patch.object(
            search._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted(text, error=error),
        ):
            result = await search.search_posts("python", sort_by="date")

        expected_error = error or {
            "error_type": "rate_limit",
            "error_message": RATE_LIMITED_SECTION_TEXT,
        }
        assert result["section_errors"] == {"search_results": expected_error}
        assert result["sections"] == (
            {"search_results": text}
            if text and text != RATE_LIMITED_SECTION_TEXT
            else {}
        )

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
        # max_pages default (3) -> 15 scrolls
        assert mock_extract.await_args_list == [
            call(
                result["url"],
                section_name="search_results",
                plan=CapturePlan(
                    CaptureMode.SEARCH_RESULTS | CaptureMode.POST_PERMALINKS,
                    max_scrolls=15,
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

    async def test_the_content_type_filter_reaches_the_url(self, mock_page):
        search = _search(mock_page)
        with patch.object(
            search._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("post"),
        ) as mock_extract:
            result = await search.search_posts("robotics", content_type="documents")

        assert "contentType=%5B%22documents%22%5D" in result["url"]
        assert mock_extract.call_args.args[0] == result["url"]

    async def test_max_pages_buys_a_whole_page_of_scrolls_each(self, mock_page):
        """Two nominal pages are ten scrolls, not two.

        The multiplication is the whole of what ``max_pages`` means on an
        infinite scroll: dropping it leaves a caller asking for three pages
        with three scrolls, which reads as a page that simply had little on
        it. Asserted at a value that is neither the argument nor the default
        product, so neither half of the arithmetic can go missing quietly.
        """
        search = _search(mock_page)
        with patch.object(
            search._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("post"),
        ) as mock_extract:
            await search.search_posts("python", max_pages=2)

        assert mock_extract.await_args_list == [
            call(
                ANY,
                section_name="search_results",
                plan=CapturePlan(
                    CaptureMode.SEARCH_RESULTS | CaptureMode.POST_PERMALINKS,
                    max_scrolls=10,
                ),
            )
        ]

    @pytest.mark.parametrize("max_pages", [0, -3])
    async def test_a_nonpositive_max_pages_still_scrolls_one_page_worth(
        self, mock_page, max_pages
    ):
        """The ``max(1, ...)`` floor, which is what keeps a zero readable.

        Without it the tool answers a ``max_pages`` of 0 with no scrolling at
        all and a negative one with a negative budget, and both come back as
        an empty results page rather than as a refused argument.
        """
        search = _search(mock_page)
        with patch.object(
            search._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("post"),
        ) as mock_extract:
            await search.search_posts("python", max_pages=max_pages)

        assert mock_extract.await_args_list == [
            call(
                ANY,
                section_name="search_results",
                plan=CapturePlan(
                    CaptureMode.SEARCH_RESULTS | CaptureMode.POST_PERMALINKS,
                    max_scrolls=posts_module._CONTENT_SCROLLS_PER_REQUESTED_PAGE,
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


def test_one_requested_page_is_five_scrolls():
    """The policy constant itself, which every scroll-depth test multiplies.

    Asserted here rather than inferred from a product: the tests above would
    all still pass with a different constant and a matching expectation, so a
    changed scroll budget belongs in a diff that says so.
    """
    assert posts_module._CONTENT_SCROLLS_PER_REQUESTED_PAGE == 5
