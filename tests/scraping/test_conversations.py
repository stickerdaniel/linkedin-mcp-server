"""Tests for the messaging conversation owner.

The click-to-capture loop is the one thing in this module that changes state
on LinkedIn: selecting a row may mark the thread read. Every case that touches
the enumerator therefore asserts *which* rows were reached, not only what came
back. The JavaScript half of that ordering never executes under a mock and is
covered in ``tests/test_conversation_sidebar_dom.py`` instead.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, patch

import pytest

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.conversations import (
    ConversationReader,
    strip_select_conversation_prefix,
)
from linkedin_mcp_server.scraping.link_metadata import Reference
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.profile_page import ProfilePageReader
from linkedin_mcp_server.scraping.session import ScrapingSession


async def _no_message_target() -> SimpleNamespace:
    """The top-card read the profile page reader borrows, unused here.

    Nothing in this module resolves a message target; only the display name is
    read off a profile page. Handing the reader a callable that fails loudly
    keeps that true rather than assumed.
    """
    raise AssertionError("the conversation reader never reads a message target")


def _reader(page: Any) -> ConversationReader:
    """Wire the conversation owner the way the facade does."""
    session = ScrapingSession(page)
    return ConversationReader(
        session,
        PageNavigator(session),
        PageContentReader(session),
        ProfilePageReader(session, _no_message_target),
    )


@pytest.fixture(autouse=True)
def session_boundaries():
    """Replace the shared page boundaries for every case in this module.

    ``delay`` included: the scroll loops pace themselves through it, and a real
    half-second per attempt would make the scroll-budget assertions below the
    slowest thing in the suite.
    """
    with (
        patch.object(
            ScrapingSession, "check_rate_limit", new_callable=AsyncMock
        ) as rate_limit,
        patch.object(ScrapingSession, "dismiss_modal", new_callable=AsyncMock) as modal,
        patch.object(ScrapingSession, "delay", new_callable=AsyncMock) as delay,
    ):
        yield SimpleNamespace(
            check_rate_limit=rate_limit, dismiss_modal=modal, delay=delay
        )


def _root(text: str, references: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"source": "root", "text": text, "references": references or []}


def _ref(url: str, text: str, context: str) -> Reference:
    return {"kind": "conversation", "url": url, "text": text, "context": context}


class TestStripSelectConversationPrefix:
    """The one locale-dependent comparison in this module, and its bounds.

    The table is a single en-US verb because the browser locale is forced to
    en-US. The interesting half is the miss: an unrecognised locale has to fall
    through with the aria-label intact rather than guess at a participant name.
    """

    def test_strips_en_us_prefix(self):
        assert (
            strip_select_conversation_prefix("Select conversation with Jacki McMahan")
            == "Jacki McMahan"
        )

    def test_case_insensitive(self):
        assert (
            strip_select_conversation_prefix("select conversation with jacki mcmahan")
            == "jacki mcmahan"
        )

    def test_returns_full_aria_when_prefix_absent(self):
        """In a non-en-US locale the verb prefix won't match; return as-is so
        downstream matching can endsWith / endswith on the participant name."""
        assert (
            strip_select_conversation_prefix("Konversation auswählen mit Jacki McMahan")
            == "Konversation auswählen mit Jacki McMahan"
        )

    def test_empty_input(self):
        assert strip_select_conversation_prefix("") == ""


class TestExtractConversationThreadRefs:
    async def test_the_name_filter_reaches_the_browser_click_loop(self, mock_page):
        """The filter is applied in the browser, before any row is clicked.

        Forwarding it is the whole of the read-marking containment on the
        Python side: the loop skips a non-matching row without clicking it,
        and a filter that never arrives clicks every row in the sidebar.
        """
        reader = _reader(mock_page)
        captured: dict[str, object] = {}

        async def fake_evaluate(_js: str, arg: dict | None = None) -> list:
            captured["arg"] = arg
            return []

        mock_page.evaluate = fake_evaluate

        await reader._extract_conversation_thread_refs(
            limit=50, context="inbox", name_filter="Jacki McMahan"
        )

        assert captured["arg"] == {"limit": 50, "nameFilter": "Jacki McMahan"}

    async def test_rows_that_never_attach_return_nothing_and_click_nothing(
        self, mock_page
    ):
        """A sidebar that never hydrates is empty, not an error.

        The early return is what keeps it from being a click loop over zero
        rows *after* a ten-second wait, so the evaluate assertion is the load
        bearing half.
        """
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        reader = _reader(mock_page)
        mock_page.wait_for_selector = AsyncMock(
            side_effect=PlaywrightTimeoutError("no rows")
        )
        mock_page.evaluate = AsyncMock(return_value=[])

        refs = await reader._extract_conversation_thread_refs(
            limit=None, context="inbox"
        )

        assert refs == []
        mock_page.evaluate.assert_not_awaited()

    async def test_the_row_wait_is_structural_attached_and_bounded(self, mock_page):
        """Selector, state and timeout, none of which the refs themselves show.

        The selector is structural rather than a locale-dependent aria-label
        prefix; ``attached`` rather than ``visible`` because Ember-managed
        labels are reliably attached and not reliably visible.
        """
        reader = _reader(mock_page)
        mock_page.evaluate = AsyncMock(return_value=[])

        await reader._extract_conversation_thread_refs(limit=None, context="inbox")

        mock_page.wait_for_selector.assert_awaited_once_with(
            "main li label[aria-label]", state="attached", timeout=10000
        )

    async def test_every_ref_carries_the_callers_context_label(self, mock_page):
        """The label is how a consumer tells an inbox row from a search hit."""
        reader = _reader(mock_page)
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "ariaLabel": "Select conversation with Jacki McMahan",
                    "threadId": "2-aaa",
                },
            ]
        )

        refs = await reader._extract_conversation_thread_refs(
            limit=None, context="search_results"
        )

        assert refs == [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-aaa/",
                "context": "search_results",
                "text": "Jacki McMahan",
            }
        ]

    async def test_a_row_with_no_participant_name_omits_the_text_key(self, mock_page):
        """Omitted, not empty: a ref carrying ``text: ""`` claims a nameless
        participant, and the resolver's exact-equality match would accept it
        for a display name that stripped to nothing."""
        reader = _reader(mock_page)
        mock_page.evaluate = AsyncMock(
            return_value=[
                {"ariaLabel": "Select conversation with ", "threadId": "2-aaa"},
            ]
        )

        refs = await reader._extract_conversation_thread_refs(
            limit=None, context="inbox"
        )

        assert refs == [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-aaa/",
                "context": "inbox",
            }
        ]


class TestResolveConversationThreadUrls:
    async def test_inbox_enumeration_and_exact_aria_match(self, mock_page):
        """Enumerates the plain inbox and matches the participant by exact
        aria-label rather than substring."""
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        thread_refs = [
            _ref("/messaging/thread/2-aaa/", "Jacki McMahan", "search"),
            # Extra suffix, so not an exact match.
            _ref("/messaging/thread/2-bbb/", "Jacki McMahan-Group", "search"),
            # Second exact match (the multi-thread case).
            _ref("/messaging/thread/2-ccc/", "Jacki McMahan", "search"),
        ]
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=thread_refs,
            ),
        ):
            urls = await reader._resolve_conversation_thread_urls("Jacki McMahan")

        nav_mock.assert_awaited_once_with("https://www.linkedin.com/messaging/")
        assert urls == [
            "https://www.linkedin.com/messaging/thread/2-aaa/",
            "https://www.linkedin.com/messaging/thread/2-ccc/",
        ]

    async def test_matches_keep_the_order_the_sidebar_gave_them(self, mock_page):
        """LinkedIn renders newest activity first and nothing here reorders it.

        ``index`` in the caller is positional against exactly this list, so a
        reversal silently reassigns every index a caller ever recorded.
        """
        reader = _reader(mock_page)
        thread_refs = [
            _ref("/messaging/thread/2-newest/", "Jacki McMahan", "inbox"),
            _ref("/messaging/thread/2-middle/", "Jacki McMahan", "inbox"),
            _ref("/messaging/thread/2-oldest/", "Jacki McMahan", "inbox"),
        ]
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=thread_refs,
            ),
        ):
            urls = await reader._resolve_conversation_thread_urls("Jacki McMahan")

        assert urls == [
            "https://www.linkedin.com/messaging/thread/2-newest/",
            "https://www.linkedin.com/messaging/thread/2-middle/",
            "https://www.linkedin.com/messaging/thread/2-oldest/",
        ]

    async def test_the_resolver_passes_its_name_filter_to_the_enumerator(
        self, mock_page
    ):
        """Scopes the click side effect: only the participant's row is clicked."""
        reader = _reader(mock_page)
        refs_mock = AsyncMock(
            return_value=[_ref("/messaging/thread/2-aaa/", "Jacki McMahan", "inbox")]
        )
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(reader, "_extract_conversation_thread_refs", refs_mock),
        ):
            urls = await reader._resolve_conversation_thread_urls("Jacki McMahan")

        refs_mock.assert_awaited_once_with(
            limit=ANY, context="inbox", name_filter="Jacki McMahan"
        )
        assert urls == ["https://www.linkedin.com/messaging/thread/2-aaa/"]

    async def test_the_inbox_scan_scrolls_to_the_bottom_twice(self, mock_page):
        """Two attempts, at the bottom, before the rows are read.

        Neither half is visible in the returned URLs: a budget raised to cover
        a deeper inbox costs a click-and-mark pass over every row it uncovers,
        and scrolling to the top would leave the scan where it started.
        """
        reader = _reader(mock_page)
        scroll_mock = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_scroll_main_scrollable_region", scroll_mock),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=[_ref("/messaging/thread/2-a/", "Ada", "inbox")],
            ),
        ):
            await reader._resolve_conversation_thread_urls("Ada")

        scroll_mock.assert_awaited_once_with(
            position="bottom", attempts=2, pause_time=0.5
        )

    async def test_a_matching_inbox_never_reaches_the_search_fallback(self, mock_page):
        """The inbox comes first and the search only runs when it came up empty.

        LinkedIn's messaging search answers "We didn't find anything" for
        threads plainly present in the inbox (issue #434), so running it first
        would turn a resolvable participant into "Could not find a
        conversation" — and running it anyway would click a second set of rows.
        """
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        refs_mock = AsyncMock(
            return_value=[_ref("/messaging/thread/2-aaa/", "Jacki McMahan", "inbox")]
        )
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(reader, "_extract_conversation_thread_refs", refs_mock),
        ):
            urls = await reader._resolve_conversation_thread_urls("Jacki McMahan")

        assert [call.args[0] for call in nav_mock.await_args_list] == [
            "https://www.linkedin.com/messaging/"
        ]
        assert refs_mock.await_count == 1
        assert urls == ["https://www.linkedin.com/messaging/thread/2-aaa/"]

    async def test_an_empty_inbox_falls_back_to_the_messaging_search(self, mock_page):
        """A thread buried below the scrolled inbox window is the last resort."""
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        refs_mock = AsyncMock(
            side_effect=[
                [],
                [_ref("/messaging/thread/2-ddd/", "Jacki McMahan", "search")],
            ]
        )
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(reader, "_extract_conversation_thread_refs", refs_mock),
        ):
            urls = await reader._resolve_conversation_thread_urls("Jacki McMahan")

        assert [call.args[0] for call in nav_mock.await_args_list] == [
            "https://www.linkedin.com/messaging/",
            "https://www.linkedin.com/messaging/?searchTerm=Jacki+McMahan",
        ]
        assert refs_mock.await_args_list[1].kwargs == {
            "limit": None,
            "context": "search",
            "name_filter": "Jacki McMahan",
        }
        assert urls == ["https://www.linkedin.com/messaging/thread/2-ddd/"]


class TestOpenConversationByUsername:
    async def test_a_negative_index_is_refused_before_any_page_work(self, mock_page):
        """Argument checks run before navigation, so a bad call costs no hop.

        Reaching LinkedIn first would spend a profile navigation and a
        display-name read on a request that was never going to be served.
        """
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        with patch.object(PageNavigator, "_navigate_to_page", nav_mock):
            with pytest.raises(LinkedInScraperException, match="non-negative"):
                await reader._open_conversation_by_username("jacki", index=-1)

        nav_mock.assert_not_awaited()
        mock_page.goto.assert_not_awaited()

    async def test_a_traversal_username_is_refused_before_any_page_work(
        self, mock_page
    ):
        """Identifier validation sits on the same side of the navigation.

        The traversal value rather than a merely invalid one: a bare
        identifier builds the same URL whether or not it was normalized, so it
        is the only input whose result differs. This case is the conversation
        half of the table in
        ``tests/test_scraping.py::TestEveryNormalizedEntryPoint``, which the
        method left when the reader took it.
        """
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        with patch.object(PageNavigator, "_navigate_to_page", nav_mock):
            with pytest.raises(InvalidReferenceError):
                await reader._open_conversation_by_username("../../feed")

        nav_mock.assert_not_awaited()
        mock_page.goto.assert_not_awaited()

    async def test_a_profile_without_a_readable_name_is_refused(self, mock_page):
        """The display name is the only key the sidebar can be matched on."""
        reader = _reader(mock_page)
        resolve = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                ProfilePageReader,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(reader, "_resolve_conversation_thread_urls", resolve),
        ):
            with pytest.raises(
                LinkedInScraperException, match="Could not resolve a display name"
            ):
                await reader._open_conversation_by_username("jacki")

        resolve.assert_not_awaited()


class TestGetInbox:
    async def test_returns_inbox_section(self, mock_page):
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Conversation A\nConversation B"),
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await reader.get_inbox(limit=10)

        assert result == {
            "url": "https://www.linkedin.com/messaging/",
            "sections": {"inbox": "Conversation A\nConversation B"},
        }

    async def test_an_empty_inbox_omits_the_optional_keys(self, mock_page):
        """``sections`` stays but is empty, and ``references`` is absent.

        An empty-string section and an empty reference list are both
        indistinguishable from a read that worked and found nothing, which is
        the opposite of what an empty page means.
        """
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root(""),
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await reader.get_inbox(limit=5)

        assert result == {
            "url": "https://www.linkedin.com/messaging/",
            "sections": {},
        }

    async def test_includes_conversation_thread_refs(self, mock_page):
        """Click-captured thread refs lead, anchor-derived ones follow."""
        reader = _reader(mock_page)
        thread_refs = [
            _ref("/messaging/thread/2-abc123/", "Tony Chan", "inbox"),
            _ref("/messaging/thread/2-def456/", "Paul Jasper", "inbox"),
        ]
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Tony Chan\nPaul Jasper"),
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=thread_refs,
            ),
        ):
            result = await reader.get_inbox(limit=10)

        assert result["references"]["inbox"] == thread_refs

    @pytest.mark.parametrize(
        ("limit", "attempts"),
        [(5, 1), (10, 1), (20, 2), (55, 5)],
    )
    async def test_the_scroll_budget_is_one_attempt_per_ten_requested_rows(
        self, mock_page, limit, attempts
    ):
        """A floor of one, and one more attempt per ten rows asked for.

        Both halves are invisible in the result: without the floor a
        ``limit`` below ten scrolls not at all and reads whatever the first
        screen held, and a raised budget clicks through rows nobody asked for.
        """
        reader = _reader(mock_page)
        scroll_mock = AsyncMock()
        refs_mock = AsyncMock(return_value=[])
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_scroll_main_scrollable_region", scroll_mock),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("rows"),
            ),
            patch.object(reader, "_extract_conversation_thread_refs", refs_mock),
        ):
            await reader.get_inbox(limit=limit)

        scroll_mock.assert_awaited_once_with(
            position="bottom", attempts=attempts, pause_time=0.5
        )
        refs_mock.assert_awaited_once_with(limit=limit, context="inbox")


class TestGetConversation:
    async def test_returns_conversation_by_thread_id(self, mock_page):
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Hello!\nHi there!"),
            ),
        ):
            result = await reader.get_conversation(thread_id="abc123")

        nav_mock.assert_awaited_once_with(
            "https://www.linkedin.com/messaging/thread/abc123/"
        )
        assert result["sections"]["conversation"] == "Hello!\nHi there!"

    async def test_a_thread_id_wins_over_a_username_and_its_index(self, mock_page):
        """``index`` is documented as ignored whenever ``thread_id`` is given.

        Honouring it alongside the id would send the direct path through the
        inbox enumeration it exists to skip, clicking rows and marking them
        read on a call that named its thread outright.
        """
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        open_by_username = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch.object(reader, "_open_conversation_by_username", open_by_username),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Hello!"),
            ),
        ):
            await reader.get_conversation(
                linkedin_username="jacki-old", thread_id="abc123", index=1
            )

        open_by_username.assert_not_awaited()
        nav_mock.assert_awaited_once_with(
            "https://www.linkedin.com/messaging/thread/abc123/"
        )

    async def test_strips_conversation_page_chrome(self, mock_page):
        """Conversation chrome is trimmed before the generic noise pass.

        The sidebar preview carries a generic noise marker, so the generic
        pass alone would truncate the page before the thread was ever read.
        """
        raw = (
            "Ada: Preview belonging to a different conversation\n"
            "Open the options list in your conversation with Ada and Grace\n"
            "Hello!\n"
            "Maximize compose field\n"
            "Open send options"
        )
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root(raw),
            ),
        ):
            result = await reader.get_conversation(thread_id="abc123")

        assert result["sections"]["conversation"] == "Hello!"

    async def test_the_thread_scroll_walks_back_to_the_top(self, mock_page):
        """Three attempts, upward: a thread's oldest message is at the top.

        Scrolling to the bottom instead reaches the composer and loads nothing,
        so the read would return only the messages already on screen.
        """
        reader = _reader(mock_page)
        scroll_mock = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_scroll_main_scrollable_region", scroll_mock),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Hello!"),
            ),
        ):
            await reader.get_conversation(thread_id="abc123")

        scroll_mock.assert_awaited_once_with(position="top", attempts=3, pause_time=0.5)

    async def test_no_identifier_is_refused_before_any_page_work(self, mock_page):
        """Neither argument means there is nothing to open, and nothing is."""
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        with patch.object(PageNavigator, "_navigate_to_page", nav_mock):
            with pytest.raises(LinkedInScraperException, match="at least one of"):
                await reader.get_conversation()

        nav_mock.assert_not_awaited()
        mock_page.goto.assert_not_awaited()

    async def test_an_unusable_thread_id_is_refused_before_any_page_work(
        self, mock_page
    ):
        """Normalization runs on the identifier before it becomes a URL."""
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        with patch.object(PageNavigator, "_navigate_to_page", nav_mock):
            with pytest.raises(InvalidReferenceError):
                await reader.get_conversation(thread_id="../../feed")

        nav_mock.assert_not_awaited()
        mock_page.goto.assert_not_awaited()

    @pytest.mark.parametrize(
        ("index", "expected"),
        [(0, "2-newer"), (1, "2-older")],
    )
    async def test_by_username_the_index_selects_positionally(
        self, mock_page, index, expected
    ):
        """0-based, against the resolver's order, and the default is the first."""
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                ProfilePageReader,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Jacki McMahan",
            ),
            patch.object(
                reader,
                "_resolve_conversation_thread_urls",
                new_callable=AsyncMock,
                return_value=[
                    "https://www.linkedin.com/messaging/thread/2-newer/",
                    "https://www.linkedin.com/messaging/thread/2-older/",
                ],
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("msg"),
            ),
        ):
            if index == 0:
                await reader.get_conversation(linkedin_username="jacki-old")
            else:
                await reader.get_conversation(
                    linkedin_username="jacki-old", index=index
                )

        target_calls = [
            call.args[0]
            for call in nav_mock.await_args_list
            if call.args and "/messaging/thread/" in call.args[0]
        ]
        assert target_calls == [
            f"https://www.linkedin.com/messaging/thread/{expected}/"
        ]

    async def test_by_username_an_index_past_the_end_raises(self, mock_page):
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                ProfilePageReader,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Jacki McMahan",
            ),
            patch.object(
                reader,
                "_resolve_conversation_thread_urls",
                new_callable=AsyncMock,
                return_value=["https://www.linkedin.com/messaging/thread/2-only/"],
            ),
        ):
            with pytest.raises(LinkedInScraperException, match="out of range"):
                await reader.get_conversation(linkedin_username="jacki-old", index=5)

    async def test_by_username_no_threads_raises_could_not_find(self, mock_page):
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                ProfilePageReader,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Jacki McMahan",
            ),
            patch.object(
                reader,
                "_resolve_conversation_thread_urls",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            with pytest.raises(
                LinkedInScraperException, match="Could not find a conversation"
            ):
                await reader.get_conversation(linkedin_username="jacki-old")


class TestSearchConversations:
    async def test_returns_search_results(self, mock_page):
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Result 1\nResult 2"),
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await reader.search_conversations("hello world")

        assert "Result 1" in result["sections"]["search_results"]
        # Search must be driven by the searchTerm URL parameter, not by typing
        # into the searchbox -- the URL form is reliable across SPA mounts and
        # preserves the search filter across click-to-capture navigations.
        nav_mock.assert_awaited_once_with(
            "https://www.linkedin.com/messaging/?searchTerm=hello+world"
        )

    async def test_includes_conversation_thread_refs(self, mock_page):
        """Per-result thread URLs, capped by the caller's ``limit``."""
        reader = _reader(mock_page)
        thread_refs = [
            _ref("/messaging/thread/2-abc/", "Jacki McMahan", "search_results"),
            _ref("/messaging/thread/2-def/", "Jacki McMahan", "search_results"),
        ]
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Jacki McMahan\nJacki McMahan"),
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=thread_refs,
            ) as mock_refs,
        ):
            result = await reader.search_conversations("Jacki")

        mock_refs.assert_awaited_once_with(limit=20, context="search_results")
        assert {ref["url"] for ref in result["references"]["search_results"]} == {
            "/messaging/thread/2-abc/",
            "/messaging/thread/2-def/",
        }

    async def test_an_empty_result_page_omits_the_optional_keys(self, mock_page):
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root(""),
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await reader.search_conversations("nothing matches")

        assert result == {"url": mock_page.url, "sections": {}}

    async def test_the_search_page_is_not_scrolled_before_it_is_read(self, mock_page):
        """The search sidebar renders its whole result set at once.

        The inbox and a thread both scroll first; this one deliberately does
        not, and a scroll added here would pace every search by half a second
        per attempt for nothing.
        """
        reader = _reader(mock_page)
        scroll_mock = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_scroll_main_scrollable_region", scroll_mock),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Result 1"),
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            await reader.search_conversations("hello")

        scroll_mock.assert_not_awaited()


class TestScrollMainScrollableRegion:
    async def test_each_attempt_is_one_evaluation_paced_by_the_delay(self, mock_page):
        """The loop runs the program once per attempt and pauses after each.

        Dropping the pause runs every scroll before the browser has appended
        anything, which reads as a sidebar with nothing more to load.
        """
        reader = _reader(mock_page)
        mock_page.evaluate = AsyncMock(return_value=True)

        await reader._scroll_main_scrollable_region(
            position="bottom", attempts=3, pause_time=0.25
        )

        assert mock_page.evaluate.await_count == 3
        assert [call.args[1] for call in mock_page.evaluate.await_args_list] == [
            {"position": "bottom"}
        ] * 3

    async def test_a_zero_budget_evaluates_nothing(self, mock_page):
        reader = _reader(mock_page)
        mock_page.evaluate = AsyncMock(return_value=True)

        await reader._scroll_main_scrollable_region(position="top", attempts=0)

        mock_page.evaluate.assert_not_awaited()


class TestWaitForMainText:
    async def test_the_wait_carries_the_requested_minimum_and_timeout(self, mock_page):
        reader = _reader(mock_page)

        await reader._wait_for_main_text(
            minimum_length=250, timeout=1234, log_context="Messaging inbox"
        )

        call = mock_page.wait_for_function.await_args
        assert call.kwargs["arg"] == {"minimumLength": 250}
        assert call.kwargs["timeout"] == 1234

    async def test_a_page_that_never_fills_is_logged_and_not_raised(
        self, mock_page, caplog
    ):
        """A sparse page is still a page: the read continues on whatever is
        there rather than failing the whole call."""
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        reader = _reader(mock_page)
        mock_page.wait_for_function = AsyncMock(
            side_effect=PlaywrightTimeoutError("never filled")
        )

        with caplog.at_level(
            "DEBUG", logger="linkedin_mcp_server.scraping.conversations"
        ):
            await reader._wait_for_main_text(log_context="Messaging inbox")

        assert "Messaging inbox content did not appear" in caplog.text
