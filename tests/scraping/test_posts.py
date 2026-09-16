"""Tests for the post content-search owner."""

from __future__ import annotations

from typing import Any
from unittest.mock import ANY, AsyncMock, call, patch

import pytest

from linkedin_mcp_server.callbacks import ProgressCallback
from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    RateLimitError,
)
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
    FilterValidationError,
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


def _raw_item(
    activity: str = "123",
    *,
    text: str = "Saved item",
    author: str = "Ada Lovelace",
    preview: str = "",
    truncated: bool = False,
    href: str | None = None,
) -> dict[str, Any]:
    """One card as the saved-items program reports it."""
    return {
        "href": href
        or (
            f"https://www.linkedin.com/feed/update/urn:li:activity:{activity}"
            "?updateEntityUrn=x"
        ),
        "text": text,
        "author": author,
        "preview": preview,
        "truncated": truncated,
    }


def _script_saved_posts_page(
    page, *, counts, items=None, page_text="saved items", detail=None
) -> list[str]:
    """Script the evaluate programs the saved-posts workflows alternate.

    The anchor-count program answers with the scripted counts in order (the
    last one held, like a list that stopped growing). ``window.scrollBy``
    has no return value by definition. The saved-items program answers with
    the scripted cards and page innerText, and the post-detail program with
    one scripted detail payload per call (the last one held), or raises the
    scripted exception. The recorded program markers are returned so a test
    can count scrolls and detail reads without re-describing the dispatch.
    """
    remaining = list(counts)
    remaining_detail = list(detail or [])
    programs: list[str] = []

    async def dispatch(script, arg=None):
        if "const seen = new Set()" in script:
            programs.append("items")
            return {"text": page_text, "items": list(items or [])}
        if "img[src]" in script:
            programs.append("detail")
            if not remaining_detail:
                raise AssertionError("unscripted post-detail read")
            payload = (
                remaining_detail.pop(0)
                if len(remaining_detail) > 1
                else remaining_detail[0]
            )
            if isinstance(payload, Exception):
                raise payload
            return payload
        if '"/feed/update/"' in script:
            programs.append("count")
            if len(remaining) > 1:
                return remaining.pop(0)
            return remaining[0]
        if "window.scrollBy" in script:
            programs.append("scroll")
            return None
        raise AssertionError(f"unexpected evaluate: {script[:80]!r}")

    page.evaluate = AsyncMock(side_effect=dispatch)
    return programs


def _detail(
    text="Full body",
    images=None,
    links=None,
    *,
    scoped=True,
    page_text=None,
) -> dict[str, Any]:
    """One post-detail read as the browser program reports it.

    ``scoped`` says whether the page carried the post element the program
    addresses by URN; without it the reader falls back to ``page_text``.
    """
    return {
        "text": text if scoped else "",
        "page_text": page_text if page_text is not None else f"Chrome\n{text}",
        "scoped": scoped,
        "images": images if images is not None else [],
        "links": links if links is not None else [],
    }


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
                mock_page, counts=[1, 2, 2, 5], items=[_raw_item()]
            )
            result = await search.get_saved_posts(5)

        assert programs.count("scroll") == 2
        assert result["url"].endswith("my-items/saved-posts/")
        assert result["saved_posts"][0]["text"] == "Saved item"

    async def test_stale_scrolls_stop_below_the_request(self, mock_page):
        """A list that never grows ends after the stale budget, not at the ceiling.

        ``_MAX_SAVED_POSTS_STALE`` identical counts in a row mean the list
        has ended; keeping going to ``_MAX_SAVED_POSTS_SCROLLS`` would read
        the last page of a finished list nine times.
        """
        search = _search(mock_page)
        with _suppress_delay():
            programs = _script_saved_posts_page(
                mock_page, counts=[2, 2, 2, 2], items=[_raw_item()]
            )
            await search.get_saved_posts(5)

        scrolls = programs.count("scroll")
        assert scrolls == posts_module._MAX_SAVED_POSTS_STALE

    async def test_each_card_becomes_one_addressable_item(self, mock_page):
        """The permalink is the item's identity, and the URN comes off it.

        A consumer reaches the full post through ``read_post``, which takes
        exactly these two fields; an item without them is unreadable.
        """
        search = _search(mock_page)
        with _suppress_delay():
            _script_saved_posts_page(
                mock_page,
                counts=[2],
                items=[
                    _raw_item("111", text="A post", truncated=True),
                    _raw_item(href="https://www.linkedin.com/pulse/some-article/"),
                ],
            )
            result = await search.get_saved_posts(2)

        assert result["saved_posts"] == [
            {
                "kind": "feed_post",
                "permalink": "/feed/update/urn:li:activity:111/",
                "urn": "urn:li:activity:111",
                "author": "Ada Lovelace",
                "text": "A post",
                "truncated": True,
            },
            {
                "kind": "article",
                "permalink": "/pulse/some-article/",
                "author": "Ada Lovelace",
                "text": "Saved item",
                "truncated": False,
            },
        ]

    async def test_a_card_that_is_not_a_saved_item_is_dropped(self, mock_page):
        """Author and company anchors ride along in the DOM of this list.

        They are not saved items, and an item keyed by an author's profile
        would send ``read_post`` to a profile page.
        """
        search = _search(mock_page)
        with _suppress_delay():
            _script_saved_posts_page(
                mock_page,
                counts=[2],
                items=[
                    _raw_item("111"),
                    _raw_item(href="https://www.linkedin.com/in/somebody/"),
                ],
            )
            result = await search.get_saved_posts(2)

        assert [item["permalink"] for item in result["saved_posts"]] == [
            "/feed/update/urn:li:activity:111/"
        ]

    async def test_a_preview_reports_its_domain_only_when_it_has_one(self, mock_page):
        """``preview.domain`` is the "content lives elsewhere" signal.

        A link-preview card ends in its source domain, but LinkedIn's own
        article shares end in a localized sentence instead. Reading that
        sentence as a domain would send a consumer chasing a URL that does
        not exist, so only a hostname-shaped last line counts.
        """
        search = _search(mock_page)
        with _suppress_delay():
            _script_saved_posts_page(
                mock_page,
                counts=[2],
                items=[
                    _raw_item("1", preview="Brownfield Agentic\naddyo.substack.com"),
                    _raw_item(
                        "2", preview="AI is an amplifier\nAda auf LinkedIn • 4 Min."
                    ),
                ],
            )
            result = await search.get_saved_posts(2)

        assert [item["preview"] for item in result["saved_posts"]] == [
            {"domain": "addyo.substack.com", "title": "Brownfield Agentic"},
            {"title": "AI is an amplifier Ada auf LinkedIn • 4 Min."},
        ]

    async def test_a_page_of_chrome_only_is_a_rate_limit(self, mock_page):
        """An empty list and a blocked page must not read the same.

        No items plus a page that truncates to nothing is LinkedIn refusing
        the read; reporting it as ``saved_posts: []`` would read as "nothing
        saved" and send the caller away satisfied.
        """
        search = _search(mock_page)
        with (
            _suppress_delay(),
            patch.object(posts_module, "truncate_linkedin_noise", lambda *_: ""),
        ):
            _script_saved_posts_page(
                mock_page, counts=[1], items=[], page_text="chrome"
            )
            result = await search.get_saved_posts(1)

        assert result["saved_posts"] == []
        assert result["section_errors"] == {
            "saved_posts": {
                "error_type": "rate_limit",
                "error_message": RATE_LIMITED_SECTION_TEXT,
            }
        }

    async def test_an_empty_list_is_not_an_error(self, mock_page):
        """Nothing saved is an answer, not a failure."""
        search = _search(mock_page)
        with _suppress_delay():
            _script_saved_posts_page(
                mock_page, counts=[0, 0, 0, 0], items=[], page_text="Saved items"
            )
            result = await search.get_saved_posts(1)

        assert result["saved_posts"] == []
        assert "section_errors" not in result

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

        assert result["saved_posts"] == []
        assert result["section_errors"] == {"saved_posts": {"error_type": "diagnosed"}}

    async def test_an_unknown_enrich_level_is_refused(self, mock_page):
        """Silently reading the listing only would read as "nothing was cut"."""
        search = _search(mock_page)
        with pytest.raises(FilterValidationError, match="enrich"):
            await search.get_saved_posts(1, enrich="everything")


class TestSavedPostEnrichment:
    async def test_truncated_level_re_reads_only_the_cut_items(self, mock_page):
        """One navigation per cut item, and none for the rest.

        The level exists to bound the cost: re-reading an item whose text
        was already whole buys nothing but seconds. A loop ignoring the flag
        would read both cards here, so the detail count catches it.
        """
        search = _search(mock_page)
        with _suppress_delay():
            programs = _script_saved_posts_page(
                mock_page,
                counts=[2],
                items=[
                    _raw_item("1", text="Cut …", truncated=True),
                    _raw_item("2", text="Whole", truncated=False),
                ],
                detail=[_detail("The whole body")],
            )
            result = await search.get_saved_posts(2, enrich="truncated")

        assert programs.count("detail") == 1
        first, second = result["saved_posts"]
        assert (first["text"], first["truncated"]) == ("The whole body", False)
        assert (second["text"], second["truncated"]) == ("Whole", False)
        assert "images" not in second

    async def test_all_level_re_reads_every_item(self, mock_page):
        """The only level that reports images for an uncut item."""
        search = _search(mock_page)
        with _suppress_delay():
            programs = _script_saved_posts_page(
                mock_page,
                counts=[2],
                items=[_raw_item("1"), _raw_item("2")],
                detail=[_detail("The whole body")],
            )
            result = await search.get_saved_posts(2, enrich="all")

        assert programs.count("detail") == 2
        assert all(item["text"] == "The whole body" for item in result["saved_posts"])

    async def test_media_and_external_links_are_separated_from_page_chrome(
        self, mock_page
    ):
        """A post's own media, and the URLs it points at — nothing else.

        Avatars sit on the same CDN as the post's images and every comment
        adds one, so an unfiltered list is mostly faces; LinkedIn's own
        anchors are navigation, not something the post links to.
        """
        search = _search(mock_page)
        with _suppress_delay():
            _script_saved_posts_page(
                mock_page,
                counts=[1],
                items=[_raw_item("1", truncated=True)],
                detail=[
                    _detail(
                        images=[
                            "https://media.licdn.com/dms/image/v2/feedshare-shrink_800/x",
                            "https://media.licdn.com/dms/image/v2/profile-displayphoto-scale_100_100/y",
                            "https://media.licdn.com/dms/image/v2/comment-image-shrink_8192_480/z",
                            "https://static.licdn.com/aero-v1/sc/h/icon",
                            "data:image/gif;base64,R0lGOD",
                        ],
                        links=[
                            "https://lnkd.in/abc",
                            "https://www.linkedin.com/in/ada-lovelace/",
                            "https://lnkd.in/abc",
                        ],
                    )
                ],
            )
            result = await search.get_saved_posts(1, enrich="truncated")

        item = result["saved_posts"][0]
        assert item["images"] == [
            "https://media.licdn.com/dms/image/v2/feedshare-shrink_800/x"
        ]
        assert item["links"] == ["https://lnkd.in/abc"]

    async def test_one_unreadable_item_does_not_end_the_batch(self, mock_page):
        """A dead permalink is that item's problem and nothing else's."""
        search = _search(mock_page)
        with (
            _suppress_delay(),
            patch.object(
                posts_module,
                "build_issue_diagnostics",
                return_value={"error_type": "diagnosed"},
            ),
        ):
            programs = _script_saved_posts_page(
                mock_page,
                counts=[2],
                items=[_raw_item("1"), _raw_item("2")],
                detail=[RuntimeError("gone"), _detail("The whole body")],
            )
            result = await search.get_saved_posts(2, enrich="all")

        assert programs.count("detail") == 2
        first, second = result["saved_posts"]
        assert first["error"] == {"error_type": "diagnosed"}
        assert first["text"] == "Saved item"
        assert second["text"] == "The whole body"

    async def test_a_rate_limit_stops_the_loop_and_keeps_what_was_read(self, mock_page):
        """Walking the rest of the list into the same wall helps nobody.

        The items already enriched are still worth returning, so the limit
        is reported beside them rather than thrown over them.
        """
        search = _search(mock_page)
        with _suppress_delay():
            programs = _script_saved_posts_page(
                mock_page,
                counts=[3],
                items=[_raw_item("1"), _raw_item("2"), _raw_item("3")],
                detail=[
                    _detail("The whole body"),
                    RateLimitError(RATE_LIMITED_SECTION_TEXT),
                ],
            )
            result = await search.get_saved_posts(3, enrich="all")

        assert programs.count("detail") == 2
        assert result["saved_posts"][0]["text"] == "The whole body"
        assert result["saved_posts"][1]["text"] == "Saved item"
        assert result["section_errors"]["saved_posts"]["error_type"] == "rate_limit"

    async def test_progress_is_reported_once_per_enriched_item(self, mock_page):
        """Enrichment is minutes of navigations; a silent tool looks hung."""
        reported: list[tuple[str, int]] = []

        class Recording(ProgressCallback):
            async def on_progress(self, message: str, percent: int) -> None:
                reported.append((message, percent))

        search = _search(mock_page)
        with _suppress_delay():
            _script_saved_posts_page(
                mock_page,
                counts=[2],
                items=[_raw_item("1"), _raw_item("2")],
                detail=[_detail("The whole body")],
            )
            await search.get_saved_posts(2, enrich="all", callbacks=Recording())

        assert reported == [
            ("Reading saved post 1/2", 50),
            ("Reading saved post 2/2", 100),
        ]


class TestReadPost:
    @pytest.mark.parametrize(
        "reference",
        [
            "urn:li:activity:111",
            "/feed/update/urn:li:activity:111/",
            "https://www.linkedin.com/feed/update/urn:li:activity:111?updateEntityUrn=x",
        ],
    )
    async def test_every_accepted_reference_reaches_one_permalink(
        self, mock_page, reference
    ):
        """A URN, a path and a full URL name the same post.

        ``get_saved_posts`` hands back both a ``urn`` and a ``permalink``,
        and ``get_feed`` hands back URLs; refusing any of them would make
        the caller rebuild an address the server already knows.
        """
        search = _search(mock_page)
        with _suppress_delay():
            _script_saved_posts_page(
                mock_page, counts=[1], detail=[_detail("The whole body")]
            )
            result = await search.read_post(reference)

        assert result["url"] == (
            "https://www.linkedin.com/feed/update/urn:li:activity:111/"
        )
        assert result["text"] == "The whole body"
        mock_page.goto.assert_awaited()

    async def test_the_post_element_is_asked_for_by_urn(self, mock_page):
        """The program can only scope to the post the caller named.

        A detail page renders several updates' worth of markup (the post,
        its reshare source, every comment), so the URN travels with the URL
        rather than being rediscovered on the page.
        """
        search = _search(mock_page)
        with _suppress_delay():
            _script_saved_posts_page(
                mock_page, counts=[1], detail=[_detail("The whole body")]
            )
            await search.read_post("/feed/update/urn%3Ali%3Aactivity%3A111/")

        detail_call = next(
            call
            for call in mock_page.evaluate.await_args_list
            if "img[src]" in call.args[0]
        )
        assert detail_call.args[1] == {"urn": "urn:li:activity:111"}

    async def test_a_scoped_body_is_taken_as_is(self, mock_page):
        """The post element holds prose only, so nothing is trimmed off it.

        Running the page-level noise filters over it would be the bug this
        catches: they cut at chrome markers, and a body that happens to
        mention one would lose everything after it.
        """
        search = _search(mock_page)
        with _suppress_delay():
            _script_saved_posts_page(
                mock_page,
                counts=[1],
                detail=[
                    _detail(
                        "Body line\nMehr dazu",
                        page_text="Feedbeitrag\nAuthor\nBody line\nKommentare",
                    )
                ],
            )
            result = await search.read_post("urn:li:activity:111")

        assert result["text"] == "Body line\nMehr dazu"

    async def test_a_page_without_the_post_element_falls_back_to_page_text(
        self, mock_page
    ):
        """An article page carries no activity URN, and still has to answer.

        Returning nothing when the scope is missing would turn every
        ``/pulse/`` item into an empty read, so the fallback keeps the whole
        page and pays for it with the usual chrome filtering.
        """
        search = _search(mock_page)
        with _suppress_delay():
            _script_saved_posts_page(
                mock_page,
                counts=[1],
                detail=[_detail(scoped=False, page_text="Article body")],
            )
            result = await search.read_post("/pulse/some-article/")

        assert result["text"] == "Article body"

    async def test_a_screen_reader_label_is_dropped_from_the_body(self, mock_page):
        """innerText reports hidden labels, and every one of them is localized.

        LinkedIn puts one in front of each hashtag, so a body ending in tags
        ends in alternating noise; the page reports which strings they are,
        and only whole lines matching them go.
        """
        search = _search(mock_page)
        with _suppress_delay():
            _script_saved_posts_page(
                mock_page,
                counts=[1],
                detail=[
                    {
                        **_detail("Body about a Hashtag rule\nHashtag\n#patterns"),
                        "hidden_labels": ["Hashtag"],
                    }
                ],
            )
            result = await search.read_post("urn:li:activity:111")

        assert result["text"] == "Body about a Hashtag rule\n#patterns"

    @pytest.mark.parametrize(
        "reference", ["", "ada-lovelace", "/in/ada-lovelace/", "https://example.com/x"]
    )
    async def test_an_unusable_reference_is_refused_before_navigating(
        self, mock_page, reference
    ):
        """Nothing is broken; the argument is wrong and the message says so."""
        search = _search(mock_page)
        with pytest.raises(InvalidReferenceError):
            await search.read_post(reference)

        mock_page.goto.assert_not_awaited()

    async def test_a_detail_page_of_chrome_only_is_a_rate_limit(self, mock_page):
        """The caller asked for one post; an empty body is not that post."""
        search = _search(mock_page)
        with (
            _suppress_delay(),
            patch.object(posts_module, "truncate_linkedin_noise", lambda *_: ""),
        ):
            _script_saved_posts_page(mock_page, counts=[1], detail=[_detail("chrome")])
            with pytest.raises(RateLimitError):
                await search.read_post("urn:li:activity:111")
