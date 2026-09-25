"""Tests for the messaging conversation owner.

The click-to-capture loop is the one thing in this module that changes state
on LinkedIn: selecting a row may mark the thread read. Every case that touches
the enumerator therefore asserts *which* rows were reached, not only what came
back. The JavaScript half of that ordering never executes under a mock and is
covered in ``tests/test_conversation_sidebar_dom.py`` instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
    RateLimitError,
)
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.conversations import (
    ConversationReader,
    _IndexGap,
    _StoppedRow,
    _ThreadRefScan,
    _ThreadResolution,
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


def _row(name: str, thread_id: str) -> dict[str, str]:
    """One attributed row as the browser program returns it."""
    return {"ariaLabel": f"Select conversation with {name}", "threadId": thread_id}


def _outcome(
    *rows: dict[str, str],
    stopped_at: dict[str, Any] | None = None,
    first_index_gap: dict[str, Any] | None = None,
    start_thread_id: str | None = None,
) -> dict[str, Any]:
    """The row program's raw return value."""
    return {
        "rows": list(rows),
        "stoppedAt": stopped_at,
        "firstIndexGap": first_index_gap,
        "startThreadId": start_thread_id,
    }


def _scan(
    *refs: Reference,
    stopped_at: _StoppedRow | None = None,
    first_index_gap: _IndexGap | None = None,
    start_thread_id: str | None = None,
    rows_available: bool = True,
) -> _ThreadRefScan:
    return _ThreadRefScan(
        refs=list(refs),
        stopped_at=stopped_at,
        first_index_gap=first_index_gap,
        start_thread_id=start_thread_id,
        rows_available=rows_available,
    )


def _stop(name: str, position: int) -> _StoppedRow:
    return _StoppedRow(f"Select conversation with {name}", position)


def _gap(name: str, position: int, preceded_by: int) -> _IndexGap:
    return _IndexGap(f"Select conversation with {name}", position, preceded_by)


def _thread(thread_id: str) -> str:
    return f"https://www.linkedin.com/messaging/thread/{thread_id}/"


MESSAGING = "https://www.linkedin.com/messaging/"
COMPOSE = "https://www.linkedin.com/messaging/compose/"
SEARCH_JACKI = "https://www.linkedin.com/messaging/?searchTerm=Jacki+McMahan"
PROFILE_JACKI = "https://www.linkedin.com/in/jacki/"
JACKI = "Jacki McMahan"

STARTED_ON_THREAD = (
    " The scan began on a thread path. An unchanged pre-click thread ID was"
    " not accepted as evidence for a row."
)
BYPASS = (
    " Use get_conversation(thread_id=...) with a known thread id to bypass"
    " row attribution."
)
REASON_NAVIGATION = (
    " that did not open a different thread within the poll budget. It may"
    " already be open, or the click may not have navigated in time."
)
REASON_MISSING_TARGET = (
    " that has no click target, so later rows cannot be numbered safely."
)
REASON_NAME_REJECTED = (
    " that did not pass the exact display-name check, so later rows cannot"
    " be numbered safely."
)


def _refusal(index: int, verified: int, reason: str, *, started: bool = False) -> str:
    return (
        f"Could not verify conversation index {index} for jacki: {verified} "
        f"conversation(s) were verified before a matching row{reason}"
        + (STARTED_ON_THREAD if started else "")
        + " Pass a known thread_id instead."
    )


def _stopped_message(row_name: str, *, started: bool = False) -> str:
    return (
        f'Click-derived conversation references stop before "{row_name}": '
        "clicking that row did not open a different thread path within the "
        "poll budget. No later rows were clicked by this scan."
        + (STARTED_ON_THREAD if started else "")
        + BYPASS
    )


ROWS_UNAVAILABLE = {
    "error_type": "conversation_rows_unavailable",
    "error_message": (
        "No conversation rows attached within 10 s after requesting "
        "https://www.linkedin.com/messaging/compose/, so no click-derived "
        "conversation references were produced. This can mean an empty list or "
        "a list that was unavailable. Inbox text and any anchor-derived "
        "references were read from https://www.linkedin.com/messaging/." + BYPASS
    ),
}


class TestExtractConversationThreadRefs:
    async def test_the_name_filter_reaches_the_browser_click_loop(self, mock_page):
        """The filter is applied in the browser, before any row is clicked.

        Forwarding it is the whole of the read-marking containment on the
        Python side: the loop skips a non-matching row without clicking it,
        and a filter that never arrives clicks every row in the sidebar.
        """
        reader = _reader(mock_page)
        captured: dict[str, object] = {}

        async def fake_evaluate(_js: str, arg: dict | None = None) -> dict:
            captured["arg"] = arg
            return _outcome()

        mock_page.evaluate = fake_evaluate

        await reader._extract_conversation_thread_refs(
            limit=50, context="inbox", name_filter="Jacki McMahan"
        )

        assert captured["arg"] == {"limit": 50, "nameFilter": "Jacki McMahan"}

    async def test_rows_that_never_attach_return_nothing_and_click_nothing(
        self, mock_page
    ):
        """A sidebar that never hydrates is unavailable, not an error.

        The early return is what keeps it from being a click loop over zero
        rows *after* a ten-second wait, so the evaluate assertion is the load
        bearing half. No scroll runs either: the scroll belongs to a list that
        attached. ``start_thread_id`` stays unknown, not measured.
        """
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        reader = _reader(mock_page)
        mock_page.wait_for_selector = AsyncMock(
            side_effect=PlaywrightTimeoutError("no rows")
        )
        mock_page.evaluate = AsyncMock(return_value=_outcome())
        scroll = AsyncMock()

        with patch.object(reader, "_scroll_main_scrollable_region", scroll):
            outcome = await reader._extract_conversation_thread_refs(
                limit=None, context="inbox", scroll_attempts=2
            )

        assert outcome == _ThreadRefScan(refs=[], rows_available=False)
        mock_page.evaluate.assert_not_awaited()
        scroll.assert_not_awaited()

    async def test_the_row_wait_is_structural_attached_and_bounded(self, mock_page):
        """Selector, state and timeout, none of which the refs themselves show.

        The selector is structural rather than a locale-dependent aria-label
        prefix; ``attached`` rather than ``visible`` because Ember-managed
        labels are reliably attached and not reliably visible.
        """
        reader = _reader(mock_page)
        mock_page.evaluate = AsyncMock(return_value=_outcome())

        await reader._extract_conversation_thread_refs(limit=None, context="inbox")

        mock_page.wait_for_selector.assert_awaited_once_with(
            "main li label[aria-label]", state="attached", timeout=10000
        )

    async def test_the_requested_scrolls_run_after_the_wait_and_before_the_read(
        self, mock_page
    ):
        """Wait, then scroll, then read the rows the scroll loaded.

        The page grows a second row only after two bottom scrolls, so a scan
        that read first, or scrolled before the list attached and then read,
        returns one row instead of two.
        """
        reader = _reader(mock_page)
        order: list[str] = []
        scrolled = 0

        async def wait_for_selector(*_args: Any, **_kwargs: Any) -> None:
            order.append("wait")

        async def evaluate(js: str, arg: Any = None) -> Any:
            nonlocal scrolled
            if "isScrollable" in js:
                order.append("scroll")
                scrolled += 1
                return True
            order.append("rows")
            rows = [_row("Ada", "2-ada")]
            if scrolled >= 2:
                rows.append(_row("Grace", "2-grace"))
            return _outcome(*rows)

        mock_page.wait_for_selector = wait_for_selector
        mock_page.evaluate = evaluate

        outcome = await reader._extract_conversation_thread_refs(
            limit=None, context="inbox", scroll_attempts=2
        )

        assert order == ["wait", "scroll", "scroll", "rows"]
        assert [ref["url"] for ref in outcome.refs] == [
            "/messaging/thread/2-ada/",
            "/messaging/thread/2-grace/",
        ]

    async def test_every_ref_carries_the_callers_context_label(self, mock_page):
        """The label is how a consumer tells an inbox row from a search hit."""
        reader = _reader(mock_page)
        mock_page.evaluate = AsyncMock(
            return_value=_outcome(_row("Jacki McMahan", "2-aaa"))
        )

        outcome = await reader._extract_conversation_thread_refs(
            limit=None, context="search_results"
        )

        assert outcome.refs == [
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
            return_value=_outcome(
                {"ariaLabel": "Select conversation with ", "threadId": "2-aaa"}
            )
        )

        outcome = await reader._extract_conversation_thread_refs(
            limit=None, context="inbox"
        )

        assert outcome.refs == [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-aaa/",
                "context": "inbox",
            }
        ]

    async def test_the_stop_gap_and_start_thread_reach_python_unmodified(
        self, mock_page
    ):
        """Raw labels and considered-label positions, including an empty label.

        The stop carries the row's label as the browser read it, so an empty
        one must still be a stop: a truthiness check would drop it and the
        scan would read as complete.
        """
        reader = _reader(mock_page)
        mock_page.evaluate = AsyncMock(
            return_value=_outcome(
                _row("Tess", "2-t"),
                stopped_at={"ariaLabel": "", "position": 3},
                first_index_gap={
                    "ariaLabel": "Select conversation with Tess",
                    "position": 1,
                    "precededBy": 1,
                },
                start_thread_id="2-open",
            )
        )

        outcome = await reader._extract_conversation_thread_refs(
            limit=None, context="inbox", name_filter="Tess"
        )

        assert outcome.stopped_at is not None
        assert outcome == _ThreadRefScan(
            refs=[_ref("/messaging/thread/2-t/", "Tess", "inbox")],
            stopped_at=_StoppedRow("", 3),
            first_index_gap=_gap("Tess", 1, 1),
            start_thread_id="2-open",
            rows_available=True,
        )

    async def test_an_attached_list_with_nothing_to_click_is_still_available(
        self, mock_page
    ):
        reader = _reader(mock_page)
        mock_page.evaluate = AsyncMock(return_value=_outcome())

        outcome = await reader._extract_conversation_thread_refs(
            limit=None, context="inbox"
        )

        assert outcome == _ThreadRefScan(refs=[], rows_available=True)


class TestResolveConversationThreadUrls:
    async def test_the_inbox_leg_scans_the_compose_page_without_a_text_wait(
        self, mock_page, session_boundaries
    ):
        """Compose, rate check, modal, then the filtered scan with two scrolls.

        Bare ``/messaging/`` opens a thread before its rows attach, and the
        open row's click cannot be verified, so the inbox leg never scans
        there. The two bottom scrolls now run inside the scan, after the rows
        attach; the resolver itself neither scrolls nor waits for text.
        """
        reader = _reader(mock_page)
        order: list[Any] = []
        nav = AsyncMock(side_effect=lambda url: order.append(("navigate", url)))
        session_boundaries.check_rate_limit.side_effect = lambda: order.append(
            "rate_limit"
        )
        session_boundaries.dismiss_modal.side_effect = lambda: order.append("modal")

        async def scan(**kwargs: Any) -> _ThreadRefScan:
            order.append(("scan", kwargs))
            return _scan(_ref("/messaging/thread/2-aaa/", JACKI, "inbox"))

        text_wait = AsyncMock()
        scroll = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav),
            patch.object(reader, "_wait_for_main_text", text_wait),
            patch.object(reader, "_scroll_main_scrollable_region", scroll),
            patch.object(reader, "_extract_conversation_thread_refs", scan),
        ):
            resolution = await reader._resolve_conversation_thread_urls(JACKI)

        assert order == [
            ("navigate", COMPOSE),
            "rate_limit",
            "modal",
            (
                "scan",
                {
                    "limit": None,
                    "context": "inbox",
                    "name_filter": JACKI,
                    "scroll_attempts": 2,
                },
            ),
        ]
        text_wait.assert_not_awaited()
        scroll.assert_not_awaited()
        assert resolution == _ThreadResolution([_thread("2-aaa")])

    async def test_matches_keep_the_order_the_sidebar_gave_them(self, mock_page):
        """LinkedIn renders newest activity first and nothing here reorders it.

        ``index`` in the caller is positional against exactly this list, so a
        reversal silently reassigns every index a caller ever recorded.
        """
        reader = _reader(mock_page)
        scan = _scan(
            _ref("/messaging/thread/2-newest/", JACKI, "inbox"),
            _ref("/messaging/thread/2-middle/", JACKI, "inbox"),
            _ref("/messaging/thread/2-oldest/", JACKI, "inbox"),
        )
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=scan,
            ),
        ):
            resolution = await reader._resolve_conversation_thread_urls(JACKI)

        assert resolution.eligible_urls == [
            _thread("2-newest"),
            _thread("2-middle"),
            _thread("2-oldest"),
        ]
        assert resolution.barrier is None

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
            return_value=_scan(_ref("/messaging/thread/2-aaa/", JACKI, "inbox"))
        )
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_extract_conversation_thread_refs", refs_mock),
        ):
            resolution = await reader._resolve_conversation_thread_urls(JACKI)

        assert [call.args[0] for call in nav_mock.await_args_list] == [COMPOSE]
        assert refs_mock.await_count == 1
        assert resolution.eligible_urls == [_thread("2-aaa")]

    async def test_a_complete_empty_inbox_falls_back_to_the_messaging_search(
        self, mock_page
    ):
        """A thread buried below the scrolled inbox window is the last resort."""
        reader = _reader(mock_page)
        nav_mock = AsyncMock()
        refs_mock = AsyncMock(
            side_effect=[
                _scan(),
                _scan(_ref("/messaging/thread/2-ddd/", JACKI, "search")),
            ]
        )
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_extract_conversation_thread_refs", refs_mock),
        ):
            resolution = await reader._resolve_conversation_thread_urls(JACKI)

        assert [call.args[0] for call in nav_mock.await_args_list] == [
            COMPOSE,
            SEARCH_JACKI,
        ]
        assert refs_mock.await_args_list[1].kwargs == {
            "limit": None,
            "context": "search",
            "name_filter": JACKI,
        }
        assert resolution == _ThreadResolution([_thread("2-ddd")])

    async def test_a_row_wait_timeout_is_complete_empty_and_may_search(self, mock_page):
        """The real scan helper: no rows attached on compose, so nothing was
        clicked and nothing was evaluated, and the search is allowed."""
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        reader = _reader(mock_page)
        mock_page.wait_for_selector = AsyncMock(
            side_effect=[PlaywrightTimeoutError("no rows"), None]
        )
        mock_page.evaluate = AsyncMock(return_value=_outcome(_row(JACKI, "2-found")))
        nav_mock = AsyncMock()
        scroll = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(reader, "_scroll_main_scrollable_region", scroll),
        ):
            resolution = await reader._resolve_conversation_thread_urls(JACKI)

        assert [call.args[0] for call in nav_mock.await_args_list] == [
            COMPOSE,
            SEARCH_JACKI,
        ]
        mock_page.evaluate.assert_awaited_once()
        scroll.assert_not_awaited()
        assert resolution == _ThreadResolution([_thread("2-found")])


@dataclass
class _Opened:
    navigations: list[str]
    scans: AsyncMock
    root: AsyncMock
    error: LinkedInScraperException | None


async def _open_by_username(
    mock_page: Any, scans: list[_ThreadRefScan], *, index: int = 0
) -> _Opened:
    """Drive the real opener and resolver over scripted scan outcomes."""
    reader = _reader(mock_page)
    nav = AsyncMock()
    scan_mock = AsyncMock(side_effect=scans)
    root = AsyncMock(return_value=_root("msg"))
    error: LinkedInScraperException | None = None
    with (
        patch.object(PageNavigator, "_navigate_to_page", nav),
        patch.object(
            ProfilePageReader,
            "_read_profile_display_name",
            new_callable=AsyncMock,
            return_value=JACKI,
        ),
        patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
        patch.object(reader, "_scroll_main_scrollable_region", new_callable=AsyncMock),
        patch.object(reader, "_extract_conversation_thread_refs", scan_mock),
        patch.object(PageContentReader, "_extract_root_content", root),
    ):
        try:
            await reader.get_conversation(linkedin_username="jacki", index=index)
        except LinkedInScraperException as exc:
            error = exc
    return _Opened(
        navigations=[call.args[0] for call in nav.await_args_list],
        scans=scan_mock,
        root=root,
        error=error,
    )


def _jacki(thread_id: str, context: str = "inbox") -> Reference:
    return _ref(f"/messaging/thread/{thread_id}/", JACKI, context)


class TestUsernameResolutionFailsClosed:
    """An index is served only from the gap-free verified prefix.

    Every case drives the real opener and resolver; only the scan outcome,
    the page boundaries and the transcript read are scripted. A refusal must
    happen before the thread navigation and before any transcript capture.
    """

    async def test_a_complete_empty_inbox_uses_the_search_result(self, mock_page):
        opened = await _open_by_username(
            mock_page, [_scan(), _scan(_jacki("2-t1", "search"))]
        )

        assert opened.error is None
        assert opened.navigations == [
            PROFILE_JACKI,
            COMPOSE,
            SEARCH_JACKI,
            _thread("2-t1"),
        ]
        opened.root.assert_awaited_once()

    async def test_a_stopped_inbox_is_refused_without_searching(self, mock_page):
        """The search would offer a same-name thread for the stopped position."""
        opened = await _open_by_username(
            mock_page,
            [
                _scan(stopped_at=_stop(JACKI, 0)),
                _scan(_jacki("2-other", "search")),
            ],
        )

        assert str(opened.error) == _refusal(0, 0, REASON_NAVIGATION)
        assert opened.navigations == [PROFILE_JACKI, COMPOSE]
        assert opened.scans.await_count == 1
        opened.root.assert_not_awaited()

    async def test_an_already_open_first_match_is_not_substituted(self, mock_page):
        """The first matching row was the thread open at scan start.

        Its click moved nothing, so it stops the scan. A search that then
        offered the participant's other thread would answer index 0 with the
        wrong conversation.
        """
        opened = await _open_by_username(
            mock_page,
            [
                _scan(stopped_at=_stop(JACKI, 0), start_thread_id="2-t0"),
                _scan(_jacki("2-t1", "search")),
            ],
        )

        assert str(opened.error) == _refusal(0, 0, REASON_NAVIGATION, started=True)
        assert opened.navigations == [PROFILE_JACKI, COMPOSE]
        opened.root.assert_not_awaited()

    async def test_a_verified_prefix_serves_earlier_indices_only(self, mock_page):
        scans = [_scan(_jacki("2-t0"), stopped_at=_stop(JACKI, 1))]

        first = await _open_by_username(mock_page, list(scans), index=0)
        second = await _open_by_username(mock_page, list(scans), index=1)

        assert first.error is None
        assert first.navigations == [PROFILE_JACKI, COMPOSE, _thread("2-t0")]
        assert str(second.error) == _refusal(1, 1, REASON_NAVIGATION)
        assert second.navigations == [PROFILE_JACKI, COMPOSE]
        second.root.assert_not_awaited()

    async def test_a_first_row_without_a_click_target_numbers_nothing(self, mock_page):
        """The later match is not compressed into index 0."""
        opened = await _open_by_username(
            mock_page, [_scan(_jacki("2-t1"), first_index_gap=_gap(JACKI, 0, 0))]
        )

        assert str(opened.error) == _refusal(0, 0, REASON_MISSING_TARGET)
        assert opened.navigations == [PROFILE_JACKI, COMPOSE]
        opened.root.assert_not_awaited()

    async def test_a_middle_row_without_a_click_target_ends_the_prefix(self, mock_page):
        scans = [
            _scan(
                _jacki("2-t0"),
                _jacki("2-t2"),
                first_index_gap=_gap(JACKI, 1, 1),
            )
        ]

        first = await _open_by_username(mock_page, list(scans), index=0)
        second = await _open_by_username(mock_page, list(scans), index=1)

        assert first.navigations[-1] == _thread("2-t0")
        assert str(second.error) == _refusal(1, 1, REASON_MISSING_TARGET)
        assert _thread("2-t2") not in second.navigations

    @pytest.mark.parametrize(
        ("scan", "reason"),
        [
            (
                _scan(
                    first_index_gap=_gap(JACKI, 0, 0),
                    stopped_at=_stop(JACKI, 1),
                ),
                REASON_MISSING_TARGET,
            ),
            (
                _scan(
                    stopped_at=_stop(JACKI, 0),
                    first_index_gap=_gap(JACKI, 1, 0),
                ),
                REASON_NAVIGATION,
            ),
        ],
        ids=["gap-then-stop", "stop-then-gap"],
    )
    async def test_the_earlier_barrier_names_the_reason(self, mock_page, scan, reason):
        opened = await _open_by_username(mock_page, [scan])

        assert str(opened.error) == _refusal(0, 0, reason)
        opened.root.assert_not_awaited()

    async def test_a_python_name_rejection_is_a_barrier_not_a_filter(self, mock_page):
        """The browser collapses whitespace and Python does not.

        A double-spaced label the browser admitted fails the exact check here.
        Dropping it and counting on would make the clean match after it
        index 0, which is not the thread at position 0.
        """
        opened = await _open_by_username(
            mock_page,
            [
                _scan(
                    _ref("/messaging/thread/2-spaced/", "Jacki  McMahan", "inbox"),
                    _jacki("2-clean"),
                )
            ],
        )

        assert str(opened.error) == _refusal(0, 0, REASON_NAME_REJECTED)
        assert opened.navigations == [PROFILE_JACKI, COMPOSE]

    async def test_a_name_rejection_before_a_later_gap_wins(self, mock_page):
        opened = await _open_by_username(
            mock_page,
            [
                _scan(
                    _jacki("2-t0"),
                    _ref("/messaging/thread/2-spaced/", "Jacki  McMahan", "inbox"),
                    _jacki("2-t2"),
                    first_index_gap=_gap(JACKI, 3, 3),
                )
            ],
            index=1,
        )

        assert str(opened.error) == _refusal(1, 1, REASON_NAME_REJECTED)

    async def test_a_name_rejection_after_an_earlier_gap_does_not_replace_it(
        self, mock_page
    ):
        opened = await _open_by_username(
            mock_page,
            [
                _scan(
                    _jacki("2-t0"),
                    _ref("/messaging/thread/2-spaced/", "Jacki  McMahan", "inbox"),
                    first_index_gap=_gap(JACKI, 1, 1),
                )
            ],
            index=1,
        )

        assert str(opened.error) == _refusal(1, 1, REASON_MISSING_TARGET)

    @pytest.mark.parametrize(
        ("search", "reason"),
        [
            (_scan(stopped_at=_stop(JACKI, 0)), REASON_NAVIGATION),
            (_scan(first_index_gap=_gap(JACKI, 0, 0)), REASON_MISSING_TARGET),
            (
                _scan(_ref("/messaging/thread/2-x/", "Jacki  McMahan", "search")),
                REASON_NAME_REJECTED,
            ),
        ],
        ids=["stopped", "gapped", "rejected"],
    )
    async def test_the_search_leg_follows_the_same_prefix_rules(
        self, mock_page, search, reason
    ):
        opened = await _open_by_username(mock_page, [_scan(), search])

        assert str(opened.error) == _refusal(0, 0, reason)
        assert opened.navigations == [PROFILE_JACKI, COMPOSE, SEARCH_JACKI]
        opened.root.assert_not_awaited()

    async def test_a_missing_target_refusal_reports_a_thread_start(self, mock_page):
        """The start sentence describes the scan, not a click on that row.

        The row had no click target and was never clicked.
        """
        opened = await _open_by_username(
            mock_page,
            [_scan(first_index_gap=_gap(JACKI, 0, 0), start_thread_id="2-open")],
        )

        assert str(opened.error) == _refusal(0, 0, REASON_MISSING_TARGET, started=True)

    async def test_nothing_found_anywhere_is_still_could_not_find(self, mock_page):
        opened = await _open_by_username(mock_page, [_scan(), _scan()])

        assert str(opened.error) == "Could not find a conversation for jacki."
        assert opened.navigations == [PROFILE_JACKI, COMPOSE, SEARCH_JACKI]

    async def test_a_complete_list_keeps_the_out_of_range_error(self, mock_page):
        opened = await _open_by_username(mock_page, [_scan(_jacki("2-t0"))], index=3)

        assert str(opened.error) == (
            "index 3 out of range: only 1 thread(s) exist for jacki."
        )

    @pytest.mark.parametrize(
        "scan",
        [
            _scan(stopped_at=_stop(JACKI, 0)),
            _scan(first_index_gap=_gap(JACKI, 0, 0)),
            _scan(_ref("/messaging/thread/2-x/", "Jacki  McMahan", "inbox")),
        ],
        ids=["navigation", "missing_target", "name_rejected"],
    )
    async def test_a_refusal_is_the_base_scraper_error_not_a_bad_reference(
        self, mock_page, scan
    ):
        """A valid username the page could not verify keeps its diagnostics.

        ``InvalidReferenceError`` is a subclass, so ``pytest.raises`` on the
        base class would accept it; the error mapper would then drop the issue
        diagnostics and present the refusal as the caller's mistake.
        """
        opened = await _open_by_username(mock_page, [scan])

        assert opened.error is not None
        assert type(opened.error) is LinkedInScraperException
        assert not isinstance(opened.error, InvalidReferenceError)


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
                return_value=_scan(),
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
                return_value=_scan(),
            ),
        ):
            result = await reader.get_inbox(limit=5)

        assert result == {
            "url": "https://www.linkedin.com/messaging/",
            "sections": {},
        }

    async def test_text_is_read_on_the_inbox_and_rows_are_clicked_on_compose(
        self, mock_page, session_boundaries
    ):
        """Two pages, in this order, with the text read before leaving the first.

        Reading after the compose navigation would return the compose page's
        text under the inbox's URL; scanning bare ``/messaging/`` would click
        the row it opened on its own, which cannot be verified.
        """
        reader = _reader(mock_page)
        order: list[Any] = []
        nav = AsyncMock(side_effect=lambda url: order.append(("navigate", url)))
        session_boundaries.check_rate_limit.side_effect = lambda: order.append(
            "rate_limit"
        )
        session_boundaries.dismiss_modal.side_effect = lambda: order.append("modal")

        async def text_wait(**_kwargs: Any) -> None:
            order.append("text_wait")

        async def scroll(**kwargs: Any) -> None:
            order.append(("scroll", kwargs["attempts"]))

        async def root(_content: Any, _selectors: Any) -> dict[str, Any]:
            order.append("root")
            return _root("Conversation A")

        async def scan(**kwargs: Any) -> _ThreadRefScan:
            order.append(("scan", kwargs))
            return _scan()

        with (
            patch.object(PageNavigator, "_navigate_to_page", nav),
            patch.object(reader, "_wait_for_main_text", text_wait),
            patch.object(reader, "_scroll_main_scrollable_region", scroll),
            patch.object(PageContentReader, "_extract_root_content", root),
            patch.object(reader, "_extract_conversation_thread_refs", scan),
        ):
            result = await reader.get_inbox(limit=20)

        assert order == [
            ("navigate", MESSAGING),
            "rate_limit",
            "text_wait",
            "modal",
            ("scroll", 2),
            "root",
            ("navigate", COMPOSE),
            "rate_limit",
            "modal",
            ("scan", {"limit": 20, "context": "inbox", "scroll_attempts": 2}),
        ]
        assert result["url"] == MESSAGING
        assert result["sections"] == {"inbox": "Conversation A"}

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
                return_value=_scan(*thread_refs),
            ),
        ):
            result = await reader.get_inbox(limit=10)

        assert result["references"]["inbox"] == thread_refs
        assert "section_errors" not in result

    async def test_click_captured_duplicate_keeps_its_richer_metadata(self, mock_page):
        """The first duplicate wins, so the click-captured ref must lead.

        Root anchors can expose the same URL with less or different metadata.
        Reversing the merge order would silently replace the participant name
        and inbox context captured from the conversation row.
        """
        reader = _reader(mock_page)
        url = "/messaging/thread/2-abc123/"
        click_ref = _ref(url, "Tony Chan", "inbox")
        anchor_ref = {
            "href": f"https://www.linkedin.com{url}",
            "text": "Open chat",
        }
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
                return_value=_root("Tony Chan", [anchor_ref]),
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=_scan(click_ref),
            ),
        ):
            result = await reader.get_inbox(limit=10)

        assert result["references"]["inbox"] == [click_ref]

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
        The compose scan gets the same budget, spent after its rows attach.
        """
        reader = _reader(mock_page)
        scroll_mock = AsyncMock()
        refs_mock = AsyncMock(return_value=_scan())
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
        refs_mock.assert_awaited_once_with(
            limit=limit, context="inbox", scroll_attempts=attempts
        )

    async def _inbox(
        self,
        mock_page: Any,
        scan: _ThreadRefScan,
        root: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
                return_value=root if root is not None else _root("Inbox text"),
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=scan,
            ),
        ):
            return await reader.get_inbox(limit=10)

    async def test_a_stopped_scan_reports_where_the_references_end(self, mock_page):
        verified = _ref("/messaging/thread/2-ada/", "Ada Lovelace", "inbox")

        result = await self._inbox(
            mock_page, _scan(verified, stopped_at=_stop("Bob Stall", 1))
        )

        assert result == {
            "url": MESSAGING,
            "sections": {"inbox": "Inbox text"},
            "references": {"inbox": [verified]},
            "section_errors": {
                "inbox": {
                    "error_type": "thread_attribution_stopped",
                    "error_message": _stopped_message("Bob Stall"),
                }
            },
        }

    async def test_a_stop_is_reported_without_text_and_for_an_unnamed_row(
        self, mock_page
    ):
        """The diagnostic survives an empty page, and an empty label is a stop."""
        result = await self._inbox(
            mock_page, _scan(stopped_at=_StoppedRow("", 0)), root=_root("")
        )

        assert result == {
            "url": MESSAGING,
            "sections": {},
            "section_errors": {
                "inbox": {
                    "error_type": "thread_attribution_stopped",
                    "error_message": _stopped_message("(unnamed row)"),
                }
            },
        }

    @pytest.mark.parametrize("start", [None, "2-open"])
    async def test_the_thread_start_sentence_appears_only_when_measured(
        self, mock_page, start
    ):
        result = await self._inbox(
            mock_page,
            _scan(stopped_at=_stop("Bob Stall", 0), start_thread_id=start),
        )

        assert result.get("section_errors") == {
            "inbox": {
                "error_type": "thread_attribution_stopped",
                "error_message": _stopped_message(
                    "Bob Stall", started=start is not None
                ),
            }
        }

    async def test_rows_that_never_attach_are_reported_and_anchors_survive(
        self, mock_page
    ):
        anchor = {
            "href": "https://www.linkedin.com/messaging/thread/2-anchor/",
            "text": "Anchor only",
        }

        result = await self._inbox(
            mock_page,
            _scan(rows_available=False),
            root=_root("Inbox text", [anchor]),
        )

        assert result == {
            "url": MESSAGING,
            "sections": {"inbox": "Inbox text"},
            "references": {
                "inbox": [
                    {
                        "kind": "conversation",
                        "url": "/messaging/thread/2-anchor/",
                        "text": "Anchor only",
                        "context": "inbox",
                    }
                ]
            },
            "section_errors": {"inbox": ROWS_UNAVAILABLE},
        }

    async def test_a_complete_scan_adds_no_error_mapping(self, mock_page):
        result = await self._inbox(mock_page, _scan())

        assert "section_errors" not in result

    @pytest.mark.parametrize("failing", ["navigate", "rate_limit"])
    async def test_a_failing_compose_leg_propagates_without_a_retry(
        self, mock_page, session_boundaries, failing
    ):
        """No broad catch: the compose leg's failure is the call's failure.

        Turning it into an empty scan would return the text with a diagnostic
        that blames the row list for an authentication or rate-limit stop.
        """
        reader = _reader(mock_page)
        nav = AsyncMock()
        if failing == "navigate":
            nav.side_effect = [None, LinkedInScraperException("navigation failed")]
        else:
            session_boundaries.check_rate_limit.side_effect = [
                None,
                RateLimitError("Rate limited"),
            ]
        scan = AsyncMock(return_value=_scan())
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                reader, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Inbox text"),
            ),
            patch.object(reader, "_extract_conversation_thread_refs", scan),
        ):
            with pytest.raises(LinkedInScraperException):
                await reader.get_inbox(limit=10)

        assert [call.args[0] for call in nav.await_args_list] == [MESSAGING, COMPOSE]
        scan.assert_not_awaited()


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
                return_value=_ThreadResolution(
                    [
                        "https://www.linkedin.com/messaging/thread/2-newer/",
                        "https://www.linkedin.com/messaging/thread/2-older/",
                    ]
                ),
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
                return_value=_ThreadResolution(
                    ["https://www.linkedin.com/messaging/thread/2-only/"]
                ),
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
                return_value=_ThreadResolution([]),
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
                return_value=_scan(),
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

    async def test_click_captured_search_refs_lead_anchor_refs(self, mock_page):
        """Search keeps click order and its metadata ahead of root anchors.

        The duplicate carries an equally rich but conflicting anchor label, so
        reversing ``conversation_refs + references`` changes both its metadata
        and the complete result order instead of passing through a set compare.
        """
        reader = _reader(mock_page)
        first = _ref("/messaging/thread/2-abc/", "Tony Chan", "search_results")
        duplicate = _ref("/messaging/thread/2-def/", "Paul Jasper", "search_results")
        anchors = [
            {
                "href": "https://www.linkedin.com/messaging/thread/2-def/",
                "text": "Open thread",
            },
            {
                "href": "https://www.linkedin.com/messaging/thread/2-anchor/",
                "text": "Anchor only",
            },
        ]
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root("Tony Chan\nPaul Jasper", anchors),
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=_scan(first, duplicate),
            ) as mock_refs,
        ):
            result = await reader.search_conversations("Jacki")

        mock_refs.assert_awaited_once_with(limit=20, context="search_results")
        assert result["references"]["search_results"] == [
            first,
            duplicate,
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-anchor/",
                "text": "Anchor only",
                "context": "search result",
            },
        ]

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
                return_value=_scan(),
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
                return_value=_scan(),
            ),
        ):
            await reader.search_conversations("hello")

        scroll_mock.assert_not_awaited()

    async def _search(
        self, mock_page: Any, scan: _ThreadRefScan, text: str = "Result 1"
    ) -> dict[str, Any]:
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(reader, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=_root(text),
            ),
            patch.object(
                reader,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=scan,
            ),
        ):
            return await reader.search_conversations("Jacki")

    async def test_a_stopped_search_scan_is_reported_under_search_results(
        self, mock_page
    ):
        verified = _ref("/messaging/thread/2-a/", "Ada", "search_results")

        result = await self._search(
            mock_page,
            _scan(verified, stopped_at=_stop("Bob", 1), start_thread_id="2-a0"),
        )

        assert result == {
            "url": mock_page.url,
            "sections": {"search_results": "Result 1"},
            "references": {"search_results": [verified]},
            "section_errors": {
                "search_results": {
                    "error_type": "thread_attribution_stopped",
                    "error_message": _stopped_message("Bob", started=True),
                }
            },
        }

    async def test_a_search_whose_rows_never_attach_adds_no_diagnostic(self, mock_page):
        """Scoped on purpose: here the text page is the scan page.

        Most searches without rows found nothing, and the inbox-only
        diagnostic on each of them would be noise. That is a reporting choice,
        not a claim that a timeout means no match.
        """
        result = await self._search(
            mock_page, _scan(rows_available=False), text="Some result text"
        )

        assert result == {
            "url": mock_page.url,
            "sections": {"search_results": "Some result text"},
        }


class TestScrollMainScrollableRegion:
    async def test_each_attempt_is_one_evaluation_paced_by_the_delay(
        self, mock_page, session_boundaries
    ):
        """The loop runs the program once per attempt and pauses after each.

        Dropping or zeroing the pause runs every scroll before the browser has
        appended anything, which reads as a sidebar with nothing more to load.
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
        assert session_boundaries.delay.await_count == 3
        assert [call.args for call in session_boundaries.delay.await_args_list] == [
            (0.25,),
            (0.25,),
            (0.25,),
        ]

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
