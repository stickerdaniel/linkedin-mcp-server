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
    content = PageContentReader(session)
    return PostSearch(
        session, navigator, content, SectionCapture(session, navigator, content)
    )


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
        # max_pages default (3) -> 15 scrolls
        assert mock_extract.await_args_list == [
            call(
                result["url"],
                section_name="search_results",
                plan=CapturePlan(CaptureMode.SEARCH_RESULTS, max_scrolls=15),
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
                plan=CapturePlan(CaptureMode.SEARCH_RESULTS, max_scrolls=10),
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
                    CaptureMode.SEARCH_RESULTS,
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


def test_saved_posts_scroll_budget_is_explicit():
    """The two numbers that decide how far the saved-items list is walked.

    Same reason as the content-search constant: the behavioural tests above
    and below would pass under any pair of constants, so a changed budget
    belongs in a diff that says so.
    """
    assert posts_module._MAX_SAVED_POSTS_SCROLLS == 12
    assert posts_module._MAX_SAVED_POSTS_STALE == 3


def _script_saved_posts_page(
    page, *, counts, raw_text, raw_references=None
) -> list[str]:
    """Script the evaluate programs the saved-posts scroll loop alternates.

    The anchor-count program answers with the scripted counts in order (the
    last one held, like a list that stopped growing). ``window.scrollBy``
    has no return value by definition. The root-content program answers
    with the scripted innerText and raw anchors. The recorded program
    markers are returned so a test can count scrolls without re-describing
    the dispatch.
    """
    remaining = list(counts)
    programs: list[str] = []

    async def dispatch(script, arg=None):
        if '"/feed/update/"' in script:
            programs.append("count")
            if len(remaining) > 1:
                return remaining.pop(0)
            return remaining[0]
        if "window.scrollBy" in script:
            programs.append("scroll")
            return None
        if arg is not None and "selectors" in arg:
            return {"text": raw_text, "references": raw_references or []}
        raise AssertionError(f"unexpected evaluate: {script[:80]!r}")

    page.evaluate = AsyncMock(side_effect=dispatch)
    return programs


def _suppress_delay():
    """Cut the one-second boundary the way the fixture page cuts the browser.

    The session is a frozen slots dataclass, so the class carries the patch —
    an instance assignment is refused before the test starts.
    """
    return patch.object(ScrapingSession, "delay", AsyncMock())


class TestGetSavedPosts:
    async def test_scrolling_stops_once_enough_item_anchors_are_counted(
        self, mock_page
    ):
        """The count is the only locale-independent signal on this page.

        Two scrolls for a list that reaches the request on the third count:
        each round re-counts after the scroll, and the round that finally
        reaches ``num_posts`` scrolls no further. Deleting the
        ``count >= num_posts`` break would run on to the stale budget here,
        so the asserted scroll total catches it.
        """
        search = _search(mock_page)
        with _suppress_delay():
            programs = _script_saved_posts_page(
                mock_page, counts=[1, 2, 2, 5], raw_text="saved item"
            )
            result = await search.get_saved_posts(5)

        assert programs.count("scroll") == 2
        assert result["url"].endswith("my-items/saved-posts/")
        assert result["sections"]["saved_posts"] == "saved item"

    async def test_stale_scrolls_stop_below_the_request(self, mock_page):
        """A list that never grows ends after the stale budget, not at the ceiling.

        ``_MAX_SAVED_POSTS_STALE`` identical counts in a row mean the list
        has ended; keeping going to ``_MAX_SAVED_POSTS_SCROLLS`` would read
        the last page of a finished list nine times.
        """
        search = _search(mock_page)
        with _suppress_delay():
            programs = _script_saved_posts_page(
                mock_page, counts=[2, 2, 2, 2], raw_text="saved item"
            )
            await search.get_saved_posts(5)

        scrolls = programs.count("scroll")
        assert scrolls == posts_module._MAX_SAVED_POSTS_STALE

    async def test_references_keep_only_post_and_article_permalinks(self, mock_page):
        """Author and company anchors ride along in the DOM and are filtered.

        The page offers every anchor the cards carry; what callers want is
        the saved items' permalinks. Authors stay reachable through the
        section text, exactly like the home feed's reference filter.
        """
        raw_references = [
            {
                "href": "https://www.linkedin.com/feed/update/urn:li:activity:123?updateEntityUrn=x",
                "text": "A saved post",
                "heading": "",
            },
            {
                "href": "https://www.linkedin.com/pulse/some-article/",
                "text": "Some article",
                "heading": "",
            },
            {
                "href": "https://www.linkedin.com/in/somebody/",
                "text": "Somebody",
                "heading": "",
            },
        ]
        search = _search(mock_page)
        with _suppress_delay():
            _script_saved_posts_page(
                mock_page,
                counts=[3],
                raw_text="saved items",
                raw_references=raw_references,
            )
            result = await search.get_saved_posts(1)

        assert result["references"] == {
            "saved_posts": [
                {
                    "kind": "feed_post",
                    "url": "/feed/update/urn:li:activity:123/",
                    "text": "A saved post",
                    "context": "saved posts",
                },
                {
                    "kind": "article",
                    "url": "/pulse/some-article/",
                    "text": "Some article",
                    "context": "saved posts",
                },
            ]
        }

    async def test_a_page_of_chrome_only_is_a_rate_limit(self, mock_page):
        """The truncation branch decides the rate-limit entry, not the text.

        A page that is nothing but LinkedIn chrome truncates to empty, and
        the section must be an error rather than an empty list that reads
        as "nothing saved".
        """
        search = _search(mock_page)
        with (
            _suppress_delay(),
            patch.object(posts_module, "truncate_linkedin_noise", lambda *_: ""),
        ):
            _script_saved_posts_page(mock_page, counts=[1], raw_text="chrome")
            result = await search.get_saved_posts(1)

        assert result["sections"] == {}
        assert result["section_errors"] == {
            "saved_posts": {
                "error_type": "rate_limit",
                "error_message": RATE_LIMITED_SECTION_TEXT,
            }
        }

    async def test_a_browser_failure_is_diagnosed_into_the_section_error(
        self, mock_page
    ):
        """Browsers fail; the report goes to the section, not the exception channel.

        The tool shape promised `section_errors` for precisely this, and an
        exception instead would close over the one case the shape exists for.
        """
        search = _search(mock_page)
        with (
            _suppress_delay(),
            patch.object(
                posts_module,
                "build_issue_diagnostics",
                return_value={"error_type": "diagnosed"},
            ),
        ):
            mock_page.evaluate = AsyncMock(side_effect=RuntimeError("boom"))
            result = await search.get_saved_posts(1)

        assert result["sections"] == {}
        assert result["section_errors"] == {"saved_posts": {"error_type": "diagnosed"}}
