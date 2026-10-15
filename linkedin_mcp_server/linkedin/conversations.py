"""Messaging inbox, thread and conversation-search workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import asyncio
import json
import logging
import re

import anyio
import anyio.lowlevel
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInOperationError,
)
from linkedin_mcp_server.debug_trace import record_page_trace
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.identifiers import (
    messaging_thread_url,
    normalize_person_identifier,
    normalize_thread_id,
    person_profile_url,
)
from linkedin_mcp_server.linkedin.link_metadata import (
    Reference,
    build_references,
    dedupe_references,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.profile_page import ProfilePageReader
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession
from linkedin_mcp_server.linkedin.text import (
    CONVERSATION_ACTIONS_EN_US,
    ConversationActionTextTable,
    strip_conversation_chrome,
    strip_linkedin_noise,
)

logger = logging.getLogger(__name__)

# Best-effort prefix strip for the en-US "Select conversation with " verb.
# Browser locale is forced to en-US (see BrowserManager) so this normally
# succeeds; the regex falls through silently for any other locale, in
# which case the full aria-label flows into the ref's text field rather
# than a stripped name.
_SELECT_CONVERSATION_PREFIX_RE = re.compile(
    r"^Select conversation with\s+", re.IGNORECASE
)


def strip_select_conversation_prefix(aria_label: str) -> str:
    """Drop the en-US selection verb from one conversation row's aria-label."""
    return _SELECT_CONVERSATION_PREFIX_RE.sub("", aria_label).strip()


# Click scans start here, never on bare `/messaging/`: that page opens a
# thread before the rows attach, and a click on the already-open row leaves the
# pathname unchanged, so it cannot be verified. Measured 2026-09-25, see
# docs/decisions/2026-09-25-fail-closed-thread-attribution.md.
_COMPOSE_URL = "https://www.linkedin.com/messaging/compose/"

_SCAN_STARTED_ON_THREAD = (
    " The scan began on a thread path. An unchanged pre-click thread ID was"
    " not accepted as evidence for a row."
)
_BYPASS_ROW_ATTRIBUTION = (
    " Use get_conversation(thread_id=...) with a known thread id to bypass"
    " row attribution."
)


@dataclass(frozen=True)
class _StoppedRow:
    """The row whose click did not open a different thread path.

    ``position`` counts considered labels, matching or not, so it orders a
    stop against an index gap from the same scan.
    """

    aria_label: str
    position: int


@dataclass(frozen=True)
class _IndexGap:
    """The first name-matching row that had no click target.

    ``preceded_by`` is how many rows had been attributed when it was skipped,
    which is where the index-eligible prefix ends.
    """

    aria_label: str
    position: int
    preceded_by: int


@dataclass(frozen=True)
class _ThreadRefScan:
    """One click scan: attributed rows plus why it may be incomplete.

    ``rows_available`` is false only when no row attached before the wait
    expired, in which case nothing was evaluated and ``start_thread_id`` is
    unknown rather than measured.
    """

    refs: list[Reference]
    stopped_at: _StoppedRow | None = None
    first_index_gap: _IndexGap | None = None
    start_thread_id: str | None = None
    rows_available: bool = True


_BarrierReason = Literal["navigation", "missing_target", "name_rejected"]


@dataclass(frozen=True)
class _Barrier:
    reason: _BarrierReason
    verified_before: int


@dataclass(frozen=True)
class _ThreadResolution:
    """Thread URLs a username index may select, and where they stop.

    ``eligible_urls`` is the gap-free prefix of matching rows in scan order.
    With a barrier, a matching row at the next position could not be
    verified, so no later row may be numbered.
    """

    eligible_urls: list[str]
    barrier: _Barrier | None = None
    start_thread_id: str | None = None


def _thread_attribution_stopped_error(scan: _ThreadRefScan) -> dict[str, str]:
    """The ``section_errors`` entry for a click scan that stopped at a row."""
    assert scan.stopped_at is not None
    row_name = (
        strip_select_conversation_prefix(scan.stopped_at.aria_label) or "(unnamed row)"
    )
    message = (
        f'Click-derived conversation references stop before "{row_name}": '
        "clicking that row did not open a different thread path within the "
        "poll budget. No later rows were clicked by this scan."
    )
    if scan.start_thread_id is not None:
        message += _SCAN_STARTED_ON_THREAD
    return {
        "error_type": "thread_attribution_stopped",
        "error_message": message + _BYPASS_ROW_ATTRIBUTION,
    }


def _conversation_rows_unavailable_error() -> dict[str, str]:
    """The ``section_errors`` entry for an inbox scan whose rows never attached.

    Only `get_inbox` reports it: its text and its click scan come from two
    different pages, so the text alone cannot show that the references are
    missing. A timeout does not prove the list is empty.
    """
    return {
        "error_type": "conversation_rows_unavailable",
        "error_message": (
            "No conversation rows attached within 10 s after requesting "
            f"{_COMPOSE_URL}, so no click-derived conversation references were "
            "produced. This can mean an empty list or a list that was "
            "unavailable. Inbox text and any anchor-derived references were read "
            "from https://www.linkedin.com/messaging/." + _BYPASS_ROW_ATTRIBUTION
        ),
    }


def _listing_section_errors(
    section: str, scan: _ThreadRefScan, *, report_unavailable_rows: bool
) -> dict[str, dict[str, str]] | None:
    if scan.stopped_at is not None:
        return {section: _thread_attribution_stopped_error(scan)}
    if report_unavailable_rows and not scan.rows_available:
        return {section: _conversation_rows_unavailable_error()}
    return None


def _resolution_from_scan(display_name: str, scan: _ThreadRefScan) -> _ThreadResolution:
    """Convert a filtered scan into the prefix an index may select from.

    The prefix ends at the earliest of: a matching row without a click target,
    a click that did not open a different thread, or an attributed row that
    fails the exact display-name check below. Rows after that point are never
    renumbered into it, because a caller's ``index`` would then open a
    different conversation than the one at that position.
    """
    target_name = display_name.strip().lower()
    gap, stop = scan.first_index_gap, scan.stopped_at
    reason: _BarrierReason | None = None
    candidates = len(scan.refs)
    if gap is not None and (stop is None or gap.position < stop.position):
        reason, candidates = "missing_target", gap.preceded_by
    elif stop is not None:
        reason = "navigation"

    eligible: list[str] = []
    for ref in scan.refs[:candidates]:
        # name_filter already gated the clicks; this enforces exact equality
        # Python-side. The browser collapses whitespace and this does not, so
        # a row it admitted can still fail here, and that ends the prefix.
        if (ref.get("text") or "").strip().lower() != target_name:
            return _ThreadResolution(
                eligible,
                _Barrier("name_rejected", len(eligible)),
                scan.start_thread_id,
            )
        eligible.append(f"https://www.linkedin.com{ref['url']}")
    barrier = None if reason is None else _Barrier(reason, len(eligible))
    return _ThreadResolution(eligible, barrier, scan.start_thread_id)


_UNVERIFIED_REASONS: dict[_BarrierReason, str] = {
    "navigation": (
        " that did not open a different thread within the poll budget. It may"
        " already be open, or the click may not have navigated in time."
    ),
    "missing_target": (
        " that has no click target, so later rows cannot be numbered safely."
    ),
    "name_rejected": (
        " that did not pass the exact display-name check, so later rows cannot"
        " be numbered safely."
    ),
}


def _unverified_index_message(
    index: int, username: str, resolution: _ThreadResolution
) -> str:
    barrier = resolution.barrier
    assert barrier is not None
    message = (
        f"Could not verify conversation index {index} for {username}: "
        f"{barrier.verified_before} conversation(s) were verified before a "
        f"matching row{_UNVERIFIED_REASONS[barrier.reason]}"
    )
    if resolution.start_thread_id is not None:
        message += _SCAN_STARTED_ON_THREAD
    return message + " Pass a known thread_id instead."


# The opened thread's options button, found by its accessible name: the
# `aria-label` or the screen-reader text inside it, whichever carries the
# table's prefix. The chrome table found that name in the page's innerText,
# so it is at least there as text. Every sidebar row holds one too, inside the `<li>` whose
# `label[aria-label]` names the row, so those are skipped, and more than one
# left is refused rather than guessed at. Marked for the click only when
# exactly one matches.
THREAD_OPTIONS_JS = r"""(opts) => {
    const {prefix, mark} = opts;
    const main = document.querySelector('main');
    if (!main) return null;
    const visible = (el) => {
        const box = el.getBoundingClientRect();
        return box.width > 0 && box.height > 0;
    };
    const inRow = (el) => {
        const row = el.closest('li');
        return !!(row && row.querySelector('label[aria-label]'));
    };
    const squash = (value) => (value || '').replace(/\s+/g, ' ').trim();
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const names = [];
    for (const button of main.querySelectorAll('button')) {
        if (!visible(button) || inRow(button)) continue;
        // The shortest match is the name alone, without the icon's text.
        const name = [
            button.getAttribute('aria-label'),
            ...[button, ...button.querySelectorAll('*')].map(el => el.textContent),
        ]
            .map(squash)
            .filter(value => value.startsWith(prefix))
            .sort((left, right) => left.length - right.length)[0];
        if (name) names.push([button, name]);
    }
    if (names.length === 1) names[0][0].setAttribute(mark, '');
    return {
        triggers: names.length,
        name: names.length === 1 ? names[0][1] : null,
    };
}"""

# The thread menu's items, found by structure rather than by LinkedIn's
# classes: the visible item-like elements with short text, innermost, that the
# options button's click revealed. They are told from what was already on the
# page by the ``seen`` attribute the survey puts on every visible item before
# the click, element by element, because a message in the thread can read the
# same as a menu item. Only those inside the button's own parent are its menu:
# measured on 2026-09-26 and 2026-10-15, the dropdown is `div[role=button]`
# items (`div[role=link]` for Manage settings) there. The scope never widens,
# so a control the click reveals elsewhere on the page, while the dropdown is
# still rendering or because LinkedIn moved it, is never one of its items.
# Shared by the two programs below, which start by unpacking ``after``,
# ``trigger`` and ``seen``.
_THREAD_MENU_PRELUDE_JS = r"""
    const visible = (el) => {
        const box = el.getBoundingClientRect();
        return box.width > 0 && box.height > 0;
    };
    const squash = (value) => (value || '').replace(/\s+/g, ' ').trim();
    const itemSelector = '[role="button"], [role="menuitem"], [role="menuitemradio"], '
        + '[role="menuitemcheckbox"], [role="option"], button, a, li, [tabindex]';
    const found = [...document.querySelectorAll(itemSelector)].filter(el => {
        const text = squash(el.innerText);
        return visible(el) && text && text.length <= 80;
    });
    const items = found.filter(el => !found.some(other => other !== el && el.contains(other)));
    const button = document.querySelector('[' + trigger + ']');
    const revealed = after
        ? items.filter(el => el !== button && !el.hasAttribute(seen))
        : [];
    const menu = button?.parentElement || null;
    const menuItems = menu ? revealed.filter(el => menu.contains(el)) : [];
"""

# The menu's items whose whole text is the archive label. Marked for the
# click only when it is the one match.
THREAD_MENU_ITEMS_JS = (
    r"""(opts) => {
    const {archive, trigger, seen, mark} = opts;
    const after = true;
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }"""
    + _THREAD_MENU_PRELUDE_JS
    + r"""
    const archiveItems = menuItems.filter(el => squash(el.innerText) === archive);
    if (archiveItems.length === 1) archiveItems[0].setAttribute(mark, '');
    return {archive: archiveItems.length};
}"""
)

# Before the click, marks everything already visible as seen. After it, evidence
# for a menu the read above could not use: the items beside the button, or
# when there are none, whatever the click revealed elsewhere, with the
# container holding them and the options button. Nothing is clicked.
THREAD_MENU_SURVEY_JS = (
    r"""(opts) => {
    const {after, trigger, seen} = opts;"""
    + _THREAD_MENU_PRELUDE_JS
    + r"""
    if (!after) {
        for (const old of document.querySelectorAll('[' + seen + ']')) {
            old.removeAttribute(seen);
        }
        for (const el of items) el.setAttribute(seen, '');
        return {seen: items.length};
    }
    const describe = (el) => {
        if (!el) return null;
        const role = el.getAttribute('role');
        const classes = [...el.classList].slice(0, 3).map(name => '.' + name).join('');
        return el.tagName.toLowerCase() + (role ? '[role=' + role + ']' : '')
            + (el.id ? '#' + el.id : '') + classes;
    };
    const beside = menuItems.length > 0;
    const added = beside ? menuItems : revealed;
    let container = beside ? menu : added[0]?.parentElement || null;
    while (container && !added.every(el => container.contains(el))) {
        container = container.parentElement;
    }
    const controls = button?.getAttribute('aria-controls');
    return {
        added: added.slice(0, 25).map(el => ({
            element: describe(el), text: squash(el.innerText),
        })),
        addedTotal: added.length,
        beside,
        container: describe(container),
        trigger: button ? {
            element: describe(button),
            expanded: button.getAttribute('aria-expanded'),
            haspopup: button.getAttribute('aria-haspopup'),
            controls: describe(controls ? document.getElementById(controls) : null),
        } : null,
    };
}"""
)

# The attributes that tie a click to the element the read chose.
_THREAD_OPTIONS_MARK = "data-mcp-thread-options"
_THREAD_ARCHIVE_MARK = "data-mcp-thread-archive"
_THREAD_SEEN_MARK = "data-mcp-thread-seen"

# How long the thread gets to load its folder and render its options button,
# and its menu to open.
_THREAD_READY_TIMEOUT = 10.0
_THREAD_MENU_TIMEOUT = 5.0
_THREAD_POLL = 0.25

# The folder a thread is in is read from LinkedIn's own messaging data, never
# from its menu: measured on 2026-10-15, an archived thread opened by its URL
# still offers Archive in its options menu. The client loads the open thread
# with the `messengerConversations` query, from the list when the thread is in
# the loaded window and by id otherwise, and each `Conversation` carries its
# `categories`: `["ARCHIVE"]` for the archived thread, `["INBOX",
# "PRIMARY_INBOX"]` for one in the inbox, and `["ARCHIVE", "INMAIL"]` for
# archived InMails in the same response.
_CONVERSATIONS_GRAPHQL_PATH = "/voyager/api/voyagerMessagingGraphQL/graphql"
_CONVERSATIONS_QUERY = "messengerConversations."
_CONVERSATION_TYPE = "com.linkedin.messenger.Conversation"
_ARCHIVE_CATEGORY = "ARCHIVE"
_INBOX_CATEGORY = "INBOX"

_ThreadArchiveState = Literal[
    # LinkedIn files it in the inbox and its menu offers Archive, marked for
    # the click.
    "archivable",
    # LinkedIn files it as archived. Its menu is not opened.
    "archived",
    # LinkedIn's data never named the thread's folder, or named both or
    # neither of the inbox and the archive.
    "no_folder",
    # No single options button could be told to be the thread's.
    "no_options",
    # The menu of an inbox thread showed no Archive item, or more than one.
    "no_item",
]


@dataclass(frozen=True)
class _ThreadArchiveRead:
    state: _ThreadArchiveState
    participant: str | None
    # For a failed read, what the page showed instead, in error-message words.
    evidence: str = ""


def is_conversations_payload_response(url: str) -> bool:
    """Whether ``url`` is a messaging query that loads conversations."""
    parsed = urlparse(url)
    return (
        parsed.netloc == "www.linkedin.com"
        and parsed.path == _CONVERSATIONS_GRAPHQL_PATH
        and any(
            query_id.startswith(_CONVERSATIONS_QUERY)
            for query_id in parse_qs(parsed.query).get("queryId", [])
        )
    )


def thread_categories(payload: Any, thread_key: str) -> list[str] | None:
    """The categories ``payload`` gives the conversation ``thread_key``.

    The conversation is the `Conversation` whose `backendUrn` names exactly that
    thread. None when the payload holds none, or holds it without a list of
    category strings.
    """
    urn = f"urn:li:messagingThread:{thread_key}"
    found: list[str] | None = None
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        categories = node.get("categories")
        if (
            node.get("_type") == _CONVERSATION_TYPE
            and node.get("backendUrn") == urn
            and isinstance(categories, list)
            and all(isinstance(category, str) for category in categories)
        ):
            found = list(categories)
        stack.extend(node.values())
    return found


class _ThreadFolderListener:
    """Read one thread's categories from the conversation payloads a load fetches.

    Installed before the navigation, because the open thread's conversation
    arrives with the page's first queries. Keeps the categories of the last
    payload that named the thread.
    """

    _READ_DRAIN_TIMEOUT = 2.0
    _CANCEL_DRAIN_TIMEOUT = 1.0

    def __init__(self, page: Any, thread_key: str):
        self._page = page
        self._thread_key = thread_key
        self._pending: list[asyncio.Task[None]] = []
        self.categories: list[str] | None = None

    def install(self) -> None:
        self._page.on("response", self._handle_response)

    def remove(self) -> None:
        try:
            # The registered method itself: Playwright matches listeners by
            # identity (see feed.py).
            self._page.remove_listener("response", self._handle_response)
        except Exception:
            logger.debug("Failed to remove thread folder listener", exc_info=True)

    def _handle_response(self, response: Any) -> None:
        if not is_conversations_payload_response(response.url):
            return

        async def _read() -> None:
            try:
                body = await response.body()
                payload = json.loads(body)
            except Exception:
                return
            categories = thread_categories(payload, self._thread_key)
            if categories is not None:
                self.categories = categories

        self._pending.append(asyncio.create_task(_read()))

    async def drain(self) -> None:
        """Bounded teardown for the response reads, as the permalink listener's."""
        pending = self._pending
        if not pending:
            return
        try:
            await asyncio.wait(pending, timeout=self._READ_DRAIN_TIMEOUT)
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            with anyio.CancelScope(shield=True):
                try:
                    await asyncio.wait(pending, timeout=self._CANCEL_DRAIN_TIMEOUT)
                finally:
                    leftover = [task for task in pending if not task.done()]
                    for task in pending:
                        if task.done() and not task.cancelled():
                            task.exception()
                    if leftover:
                        logger.warning(
                            "Thread folder listener tasks did not drain after "
                            "cancel; leaking %d task(s)",
                            len(leftover),
                        )
            await anyio.lowlevel.checkpoint()
        self._pending = [task for task in pending if not task.done()]


def _folder_of(categories: list[str] | None) -> Literal["archived", "inbox"] | None:
    """The one folder ``categories`` put a thread in, or None."""
    if categories is None:
        return None
    archived = _ARCHIVE_CATEGORY in categories
    inbox = _INBOX_CATEGORY in categories
    if archived == inbox:
        return None
    return "archived" if archived else "inbox"


def _folder_finding(categories: list[str] | None) -> str:
    """Say which folder LinkedIn's data put a thread in, for an error message."""
    if categories is None:
        return "LinkedIn's messaging data never named its folder"
    return f"LinkedIn files it under {categories!r}"


def _menu_evidence(survey: dict[str, Any]) -> str:
    """Say what the options button opened, from a survey taken after its click."""
    trigger = survey.get("trigger") or {}
    parts = [
        f"The options button was {trigger.get('element')} with "
        f"aria-expanded={trigger.get('expanded')!r}, "
        f"aria-haspopup={trigger.get('haspopup')!r} and aria-controls naming "
        f"{trigger.get('controls')}."
    ]
    added = survey.get("added") or []
    if not added:
        parts.append("Its click revealed no visible item.")
    else:
        listed = ", ".join(f"{item['text']!r} ({item['element']})" for item in added)
        total = survey.get("addedTotal", len(added))
        where = (
            "Its click revealed"
            if survey.get("beside")
            else "Nothing appeared beside it; its click revealed elsewhere"
        )
        parts.append(
            f"{where} {total} visible item(s) in {survey.get('container')}: {listed}."
        )
    return " ".join(parts)


def _on_thread(url: str, thread_key: str) -> bool:
    """Whether ``url`` is the thread ``thread_key``, compared decoded."""
    parsed = urlparse(url)
    match = re.fullmatch(r"/messaging/thread/([^/]+)/?", parsed.path)
    return (
        parsed.netloc == "www.linkedin.com"
        and match is not None
        and unquote(match.group(1)) == thread_key
    )


class ConversationReader:
    """Own every workflow whose subject is a LinkedIn messaging thread.

    The sidebar is the reason this reads the page directly rather than through
    `SectionCapture`: LinkedIn renders conversation rows with no anchor href,
    no thread-id attribute and no embedded URN, so a thread id can only be had
    by clicking a row and reading the SPA URL the click lands on. That click
    may mark the row read, which is the closest thing to a write anywhere in
    this module and why every caller filters by participant name *before* a
    row is ever clicked.
    """

    def __init__(
        self,
        session: PageSession,
        navigator: PageNavigator,
        content: PageContentReader,
        profile_page: ProfilePageReader,
        action_text: ConversationActionTextTable = CONVERSATION_ACTIONS_EN_US,
    ):
        self._session = session
        self._navigator = navigator
        self._content = content
        self._profile_page = profile_page
        self._action_text = action_text

    @staticmethod
    def _single_section_result(
        url: str,
        section_name: str,
        text: str,
        references: list[Reference] | None = None,
    ) -> dict[str, Any]:
        """Build a standard single-section page response."""
        result: dict[str, Any] = {"url": url, "sections": {}}
        if text:
            result["sections"][section_name] = text
            if references:
                result["references"] = {section_name: references}
        return result

    async def _wait_for_main_text(
        self,
        *,
        minimum_length: int = 100,
        timeout: int = 10000,
        log_context: str,
    ) -> None:
        """Wait for main content to populate enough text to read."""
        try:
            await self._session.page.wait_for_function(
                """({ minimumLength }) => {
                    const main = document.querySelector('main');
                    if (!main) return false;
                    return main.innerText.length > minimumLength;
                }""",
                arg={"minimumLength": minimum_length},
                timeout=timeout,
            )
        except PlaywrightTimeoutError:
            logger.debug("%s content did not appear", log_context)

    async def _scroll_main_scrollable_region(
        self,
        *,
        position: Literal["top", "bottom"],
        attempts: int,
        pause_time: float = 0.5,
    ) -> None:
        """Scroll the largest scrollable region inside main when one exists."""
        for _ in range(attempts):
            await self._session.run_on_linkedin(
                """({ position }) => {
                    const main = document.querySelector('main');
                    if (!main) return false;

                    const isScrollable = element => {
                        const style = window.getComputedStyle(element);
                        return (
                            (style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                            element.scrollHeight > element.clientHeight + 20
                        );
                    };

                    const candidates = [main, ...main.querySelectorAll('*')].filter(isScrollable);
                    const target = candidates.sort(
                        (left, right) => right.scrollHeight - left.scrollHeight
                    )[0] || main;
                    target.scrollTop = position === 'top' ? 0 : target.scrollHeight;
                    return true;
                }""",
                {"position": position},
            )
            await self._session.delay(pause_time)

    async def _extract_conversation_thread_refs(
        self,
        limit: int | None,
        context: str,
        *,
        name_filter: str | None = None,
        scroll_attempts: int = 0,
    ) -> _ThreadRefScan:
        """Click each visible conversation item and capture the thread URL.

        Works for both the compose-page sidebar and the URL-driven
        search-results sidebar (`/messaging/?searchTerm=…`), which share the
        same DOM shape: each conversation row is an ``<li>`` containing a
        ``<label>`` with an ``aria-label`` attribute carrying the participant
        name. The caller navigates first; the scan never chooses its page.

        LinkedIn renders the sidebar with no ``<a href>`` tags, no
        ``data-thread-id`` attributes, and no embedded URNs — clicking each
        row and reading the SPA URL is the only reliable extraction path.
        Pass ``limit=None`` to consider every visible row.

        A row is attributed a thread id only when, after its click, the
        pathname carries a thread id different from the one it carried
        immediately before that click. The first click that does not produce
        one stops the scan and no later row is clicked, because that click may
        still land and would then be credited to whichever row came next.

        When ``name_filter`` is provided, every row's aria-label is still read
        but only rows whose cleaned participant name equals it (case-insensitive)
        are clicked; non-matching rows are skipped without clicking. Clicking a
        row may mark it as read, so the filter keeps the read-marking side effect
        scoped to the requested participant when resolving by username.

        ``scroll_attempts`` bottom scrolls run after the rows attach and before
        they are read, with the same heuristic as the inbox text page.
        """
        # The conversation list mounts after main text settles, so wait
        # explicitly for at least one label rather than relying on
        # _wait_for_main_text alone (which only checks chrome text). LinkedIn
        # routinely takes several seconds to hydrate the messaging sidebar
        # after a navigation; an empty sidebar (zero matches) returns on
        # timeout.
        #
        # Selector is structural (`main li label[aria-label]`) rather than
        # text-prefix-based (`aria-label^="Select conversation"`) so it
        # survives any LinkedIn locale — the verb in the aria-label is
        # locale-dependent, the attribute's presence inside a list-item label
        # is not.
        #
        # Wait on `state="attached"` instead of the default `visible`:
        # Ember-managed labels are reliably attached but Playwright's
        # visibility heuristic doesn't always consider them visible.
        try:
            await self._session.page.wait_for_selector(
                "main li label[aria-label]",
                state="attached",
                timeout=10000,
            )
        except PlaywrightTimeoutError:
            logger.debug(
                "conversation labels did not appear within 10s (context=%s)",
                context,
            )
            return _ThreadRefScan(refs=[], rows_available=False)

        if scroll_attempts > 0:
            await self._scroll_main_scrollable_region(
                position="bottom", attempts=scroll_attempts, pause_time=0.5
            )

        # The Ember click handler lives on an inner div; the <li> and <label>
        # don't trigger SPA navigation.  No role/aria attributes exist on the
        # clickable element, so class-name selectors are unavoidable here.
        # The aria-label value flows through unmodified — Python strips any
        # known locale prefix to derive a clean participant name for refs.
        outcome: dict[str, Any] = await self._session.run_on_linkedin(
            """async ({ limit, nameFilter }) => {
                const labels = Array.from(document.querySelectorAll(
                    'main li label[aria-label]'
                ));
                const cap = (limit == null)
                    ? labels.length
                    : Math.min(labels.length, limit);
                // Normalize the optional participant filter by whitespace and
                // case only. The en-US verb is stripped from row labels below,
                // never from the filter, which is a display name. Only a
                // matching row is clicked; clicking marks a row read, so
                // unrelated threads must not be clicked.
                const wanted = (nameFilter || '')
                    .replace(/\\s+/g, ' ').trim().toLowerCase();
                // Thread identity is the pathname's thread id and nothing
                // else: a query, hash or trailing-slash change on the same
                // thread is not a move, and a thread marker in a query is not
                // a thread.
                const idOf = () => {
                    const match = location.pathname.match(
                        /^\\/messaging\\/thread\\/([^/]+)\\/?$/
                    );
                    if (!match) return null;
                    try {
                        return decodeURIComponent(match[1]) === 'new' ? null : match[1];
                    } catch {
                        return null;
                    }
                };
                const outcome = {
                    rows: [],
                    stoppedAt: null,
                    firstIndexGap: null,
                    startThreadId: idOf(),
                };
                for (let position = 0; position < cap; position++) {
                    const label = labels[position];
                    const ariaLabel = label.getAttribute('aria-label') || '';
                    const rowName = ariaLabel
                        .replace(/^Select conversation with\\s+/i, '')
                        .replace(/\\s+/g, ' ').trim().toLowerCase();
                    if (wanted && rowName !== wanted) continue;
                    const clickTarget = label.closest('li')
                        ?.querySelector('div[class*="listitem__link"]');
                    if (!clickTarget) {
                        // Skipped, not clicked. A listing loses only this
                        // row; a username index cannot count past it.
                        if (wanted && outcome.firstIndexGap === null) {
                            outcome.firstIndexGap = {
                                ariaLabel,
                                position,
                                precededBy: outcome.rows.length,
                            };
                        }
                        continue;
                    }
                    // Read fresh for every row: after an earlier row moved the
                    // page, the thread that was open at scan start is a real
                    // destination for its own row.
                    const before = idOf();
                    clickTarget.click();
                    // Poll for the SPA URL to settle on a different thread.
                    // The Ember click handler can take a moment to bind after
                    // the label mounts, and a fixed sleep races the click.
                    let after = null;
                    for (let waits = 0; waits < 12; waits++) {
                        await new Promise(r => setTimeout(r, 100));
                        const now = idOf();
                        if (now !== null && now !== before) {
                            after = now;
                            break;
                        }
                    }
                    // Stop, never continue: this click may still land, and a
                    // later row polled meanwhile would be handed its thread.
                    // Nor is the pre-click id credited to this row.
                    if (after === null) {
                        outcome.stoppedAt = { ariaLabel, position };
                        break;
                    }
                    outcome.rows.push({ ariaLabel, threadId: after });
                }
                return outcome;
            }""",
            {"limit": limit, "nameFilter": name_filter},
        )
        refs: list[Reference] = []
        for conv in outcome["rows"]:
            ref: Reference = {
                "kind": "conversation",
                "url": f"/messaging/thread/{conv['threadId']}/",
                "context": context,
            }
            name = strip_select_conversation_prefix(conv.get("ariaLabel", ""))
            if name:
                ref["text"] = name
            refs.append(ref)
        stopped = outcome["stoppedAt"]
        gap = outcome["firstIndexGap"]
        return _ThreadRefScan(
            refs=refs,
            stopped_at=(
                None
                if stopped is None
                else _StoppedRow(stopped["ariaLabel"], stopped["position"])
            ),
            first_index_gap=(
                None
                if gap is None
                else _IndexGap(gap["ariaLabel"], gap["position"], gap["precededBy"])
            ),
            start_thread_id=outcome["startThreadId"],
        )

    async def _resolve_conversation_thread_urls(
        self, display_name: str
    ) -> _ThreadResolution:
        """Resolve the thread URLs a username ``index`` may select from.

        Scans the compose page's conversation list with click-to-capture
        because LinkedIn renders the messaging sidebar with no anchor hrefs, no
        data-thread attributes, and no embedded URNs — clicking each row and
        reading the resulting SPA URL is the only available extraction path.
        The compose page is the scan destination because, in the dated
        observation, it listed the inbox rows without opening a thread, while
        bare `/messaging/` opened one. It is not guaranteed never to open a
        thread; each row is still judged by its own click. The inbox list is
        used before `?searchTerm=` because LinkedIn's messaging search
        frequently returns "We didn't find anything" for a participant whose
        thread is plainly present in the inbox (issue #434). ``name_filter`` is
        passed to the enumerator so only matching rows are clicked; clicking a
        row may mark it read, so unrelated threads stay untouched.

        Matches by case-insensitive equality on the cleaned participant name
        derived from the row's aria-label, which tolerates duplicate threads
        with the same participant. Browser locale is forced to en-US so the
        verb prefix strips reliably; in any other locale the comparison fails
        cleanly with "Could not find a conversation" rather than returning
        a wrong-thread match.

        The search runs only when the inbox scan observed no matching row and
        hit no barrier, for instance when the rows never attached or the
        thread sits below the scrolled window. It never runs to extend or
        replace an inbox scan that stopped or has a gap, because a search
        result would then take the place of a position the inbox could not
        verify.

        Positions are those of the observed scan, which is not proven to order
        rows exactly as bare `/messaging/` does. Open a buried duplicate thread
        directly via ``thread_id`` (enumerate IDs with
        ``search_conversations``).
        """
        # Never bare `/messaging/` here: see _COMPOSE_URL.
        await self._navigator._navigate_to_page(_COMPOSE_URL)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        inbox = _resolution_from_scan(
            display_name,
            await self._extract_conversation_thread_refs(
                limit=None,
                context="inbox",
                name_filter=display_name,
                scroll_attempts=2,
            ),
        )
        # An incomplete inbox answer is returned as it is. Searching after a
        # barrier could substitute a same-name thread for the position the
        # inbox could not verify.
        if inbox.eligible_urls or inbox.barrier is not None:
            return inbox

        # Fallback: LinkedIn's messaging search. Unreliable (often returns
        # "We didn't find anything" even for present threads, see #434), so it
        # runs only when the inbox scan came up empty — e.g. a thread buried
        # below the scrolled inbox window.
        await self._navigator._navigate_to_page(
            f"https://www.linkedin.com/messaging/?searchTerm={quote_plus(display_name)}"
        )
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        await self._wait_for_main_text(log_context="Messaging search results")
        return _resolution_from_scan(
            display_name,
            await self._extract_conversation_thread_refs(
                limit=None, context="search", name_filter=display_name
            ),
        )

    async def _open_conversation_by_username(
        self, linkedin_username: str, index: int = 0
    ) -> None:
        """Open the ``index``-th conversation thread for the named participant.

        ``index`` is 0-based over the verified prefix of matching rows from the
        compose-page scan, or from the search scan when the inbox scan found no
        matching row and no barrier. Positions follow the observed scan order.
        An index at or past an incomplete prefix is refused rather than served
        from a row that could not be verified.
        """
        if index < 0:
            raise LinkedInOperationError(f"index must be non-negative (got {index}).")

        linkedin_username = normalize_person_identifier(linkedin_username)
        profile_url = person_profile_url(linkedin_username, "/")
        await self._navigator._navigate_to_page(profile_url)
        await self._session.check_rate_limit()

        try:
            await self._session.page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("Profile page did not load for %s", linkedin_username)

        await self._session.dismiss_modal()
        display_name = await self._profile_page._read_profile_display_name()
        if not display_name:
            raise LinkedInOperationError(
                f"Could not resolve a display name for {linkedin_username}."
            )

        try:
            resolution = await self._resolve_conversation_thread_urls(display_name)
            thread_urls = resolution.eligible_urls
            if index >= len(thread_urls):
                # Not InvalidReferenceError: the username is valid and the page
                # could not be verified, which is worth an issue report.
                if resolution.barrier is not None:
                    raise LinkedInOperationError(
                        _unverified_index_message(index, linkedin_username, resolution)
                    )
                if not thread_urls:
                    raise LinkedInOperationError(
                        f"Could not find a conversation for {linkedin_username}."
                    )
                raise LinkedInOperationError(
                    f"index {index} out of range: only {len(thread_urls)} "
                    f"thread(s) exist for {linkedin_username}."
                )

            await self._navigator._navigate_to_page(thread_urls[index])
        except PlaywrightTimeoutError as exc:
            raise LinkedInOperationError(
                "Messaging search results did not load in time."
            ) from exc

    async def get_inbox(self, limit: int = 20) -> dict[str, Any]:
        """List recent conversations from the messaging inbox."""
        url = "https://www.linkedin.com/messaging/"
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._wait_for_main_text(log_context="Messaging inbox")
        await self._session.dismiss_modal()

        scrolls = max(1, limit // 10)
        await self._scroll_main_scrollable_region(
            position="bottom", attempts=scrolls, pause_time=0.5
        )

        raw_result = await self._content._extract_root_content(["main"])
        raw = raw_result["text"]
        cleaned = strip_linkedin_noise(raw) if raw else ""
        references: list[Reference] = (
            build_references(raw_result["references"], "inbox") if cleaned else []
        )

        # LinkedIn's conversation sidebar uses JS click handlers instead of
        # <a> tags, so anchor extraction cannot capture thread IDs.  Click each
        # conversation item and read the resulting SPA URL to build references.
        # The text above stays from bare `/messaging/`; the clicks run on the
        # compose page, never here, see _COMPOSE_URL.
        await self._navigator._navigate_to_page(_COMPOSE_URL)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        scan = await self._extract_conversation_thread_refs(
            limit=limit, context="inbox", scroll_attempts=scrolls
        )
        if scan.refs:
            references = dedupe_references(scan.refs + references)

        result = self._single_section_result(
            url,
            "inbox",
            cleaned,
            references=references,
        )
        section_errors = _listing_section_errors(
            "inbox", scan, report_unavailable_rows=True
        )
        if section_errors:
            result["section_errors"] = section_errors
        return result

    async def get_conversation(
        self,
        linkedin_username: str | None = None,
        thread_id: str | None = None,
        index: int = 0,
    ) -> dict[str, Any]:
        """Read a specific messaging conversation by thread ID or username.

        ``index`` (0-based) selects which thread to open when a participant has
        multiple conversation threads — e.g. an organic 1-on-1 plus a separate
        InMail. Ignored when ``thread_id`` is provided. Use
        ``search_conversations`` to enumerate thread IDs first if disambiguation
        by index is impractical.

        Side effect when looked up by username: resolution enumerates the
        compose page's conversation list and click-visits only the row(s)
        matching the participant's display name to capture the thread ID (no
        anchor hrefs or thread-id attributes exist in the sidebar). Each visit
        selects the row in the LinkedIn UI and may mark it as read. Pass
        ``thread_id`` directly to skip this enumeration.
        """
        if not linkedin_username and not thread_id:
            raise InvalidReferenceError(
                "Provide at least one of linkedin_username or thread_id"
            )

        if thread_id:
            thread_id = normalize_thread_id(thread_id)
            await self._navigator._navigate_to_page(
                messaging_thread_url(thread_id, "/")
            )
        else:
            await self._open_conversation_by_username(
                linkedin_username or "", index=index
            )

        await self._session.check_rate_limit()
        await self._wait_for_main_text(log_context="Conversation")
        await self._session.dismiss_modal()
        await self._scroll_main_scrollable_region(
            position="top", attempts=3, pause_time=0.5
        )

        raw_result = await self._content._extract_root_content(["main"])
        raw = raw_result["text"]
        # Conversation chrome first: a sidebar preview containing a generic
        # noise marker would otherwise truncate the page before the thread
        # markers are ever seen.
        cleaned = strip_conversation_chrome(raw) if raw else ""
        cleaned = strip_linkedin_noise(cleaned) if cleaned else ""
        references = (
            build_references(raw_result["references"], "conversation")
            if cleaned
            else []
        )
        return self._single_section_result(
            self._session.page.url,
            "conversation",
            cleaned,
            references=references,
        )

    async def search_conversations(
        self, keywords: str, limit: int = 20
    ) -> dict[str, Any]:
        """Search messages by keyword.

        Uses LinkedIn's ``?searchTerm=`` URL parameter to drive the search
        rather than typing into the searchbox — the URL form is reliable
        regardless of how soon the messaging SPA mounts its searchbox role,
        and (critically) preserves the search filter across click-to-capture
        navigations so per-thread refs can be enumerated.

        ``limit`` caps how many search-result rows the click-to-capture loop
        visits. Each visit selects the row in LinkedIn's UI (and may mark it
        as read), so a low cap is preferable for noisy queries.
        """
        search_url = (
            f"https://www.linkedin.com/messaging/?searchTerm={quote_plus(keywords)}"
        )
        await self._navigator._navigate_to_page(search_url)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        await self._wait_for_main_text(log_context="Messaging search")

        raw_result = await self._content._extract_root_content(["main"])
        raw = raw_result["text"]
        cleaned = strip_linkedin_noise(raw) if raw else ""
        references: list[Reference] = (
            build_references(raw_result["references"], "search_results")
            if cleaned
            else []
        )

        # Same click-to-capture path as get_inbox: LinkedIn's search sidebar
        # has no anchor hrefs or thread-id attributes, so the only way to
        # surface per-result thread IDs is to click each row and read the SPA
        # URL. URL-driven search keeps the filter active across clicks.
        scan = await self._extract_conversation_thread_refs(
            limit=limit, context="search_results"
        )
        if scan.refs:
            references = dedupe_references(scan.refs + references)

        result = self._single_section_result(
            self._session.page.url,
            "search_results",
            cleaned,
            references=references,
        )
        # A search whose rows never attached is not reported: here the text
        # page is the scan page, and most such searches simply found nothing.
        section_errors = _listing_section_errors(
            "search_results", scan, report_unavailable_rows=False
        )
        if section_errors:
            result["section_errors"] = section_errors
        return result

    async def archive_conversation(self, thread_id: str) -> dict[str, Any]:
        """Archive one messaging thread from its own options menu.

        Opens ``/messaging/thread/<id>/``, which marks it read, and reads the
        thread's folder from the conversation data the page loads before
        touching anything. The menu cannot tell: an archived thread opened by
        its URL still offers Archive. A thread already archived is reported as
        such and its menu is never opened; only one in the inbox has Archive
        chosen. After the click the thread is loaded again and LinkedIn must
        file it as archived, because a click the page took is not yet a
        change LinkedIn kept.

        Returns:
            {url, thread_id, participant, status: "archived" | "already_archived"}
        """
        thread_key = unquote(normalize_thread_id(thread_id))
        url = messaging_thread_url(thread_key, "/")
        first = await self._read_thread_archive_state(url, thread_key)
        status = "already_archived"
        if first.state == "archivable":
            if not _on_thread(self._session.page.url, thread_key):
                await self._session.page.keyboard.press("Escape")
                raise LinkedInOperationError(
                    f"Thread {thread_key} was left for {self._session.page.url} "
                    "before Archive could be chosen. Nothing was clicked."
                )
            await self._session.page.locator(f"[{_THREAD_ARCHIVE_MARK}]").click(
                timeout=5000
            )
            await self._session.delay(NAV_DELAY)
            after = await self._read_thread_folder(url, thread_key)
            if _folder_of(after) != "archived":
                raise LinkedInOperationError(
                    f"Archive was chosen for thread {thread_key}, but after a "
                    f"reload {_folder_finding(after)}."
                )
            status = "archived"
        elif first.state != "archived":
            raise LinkedInOperationError(
                f"Thread {thread_key} was not archived: "
                f"{self._read_finding(first.state)}.{first.evidence}"
            )
        return {
            "url": url,
            "thread_id": thread_key,
            "participant": first.participant,
            "status": status,
        }

    def _read_finding(self, state: _ThreadArchiveState) -> str:
        """What a failed read found, in the words of the error it ends up in."""
        text = self._action_text
        if state == "no_options":
            return (
                "its options button could not be found: no single button named "
                f"{text.options_prefix!r} outside the conversation list"
            )
        if state == "no_item":
            return (
                f"its options menu did not offer exactly one {text.archive_item!r}. "
                "Nothing was clicked"
            )
        return "LinkedIn did not file it in exactly one of the inbox or the archive"

    async def _read_thread_folder(self, url: str, thread_key: str) -> list[str] | None:
        """Open the thread and read its categories from the conversation data.

        Raises when the navigation ends anywhere but that thread, after
        offering the page to the auth-barrier check: LinkedIn sends an id it
        does not know elsewhere, and that page belongs to another thread.
        Returns None when no payload named the thread before the deadline.
        """
        page = self._session.page
        listener = _ThreadFolderListener(page, thread_key)
        listener.install()
        try:
            await self._navigator._navigate_to_page(url)
            await self._session.check_rate_limit()
            if not _on_thread(page.url, thread_key):
                await self._navigator._raise_if_auth_barrier(page.url)
                raise LinkedInOperationError(
                    f"Thread {thread_key} could not be opened: the navigation "
                    f"ended on {page.url}."
                )
            await self._session.dismiss_modal()
            deadline = self._session.monotonic() + _THREAD_READY_TIMEOUT
            while listener.categories is None:
                if self._session.monotonic() >= deadline:
                    break
                await self._session.delay(_THREAD_POLL)
            return listener.categories
        finally:
            listener.remove()
            await listener.drain()

    async def _read_thread_archive_state(
        self, url: str, thread_key: str
    ) -> _ThreadArchiveRead:
        """Read the thread's folder and, in the inbox, its menu's Archive item.

        Leaves the menu open with the Archive item marked when the state is
        ``archivable``, and closed otherwise. An archived thread's menu is
        never opened, and a thread whose options button cannot be found is
        still reported archived when LinkedIn files it so.
        """
        text = self._action_text
        page = self._session.page
        categories = await self._read_thread_folder(url, thread_key)
        folder = _folder_of(categories)
        if folder is None:
            return _ThreadArchiveRead(
                "no_folder", None, f" {_folder_finding(categories)}."
            )

        options: dict[str, Any] = {"triggers": 0, "name": None}
        deadline = self._session.monotonic() + _THREAD_READY_TIMEOUT
        while True:
            found = await page.evaluate(
                THREAD_OPTIONS_JS,
                {"prefix": text.options_prefix, "mark": _THREAD_OPTIONS_MARK},
            )
            if isinstance(found, dict):
                options = found
                if options["triggers"] == 1:
                    break
            if self._session.monotonic() >= deadline:
                if folder == "archived":
                    return _ThreadArchiveRead("archived", None)
                await record_page_trace(
                    page, "archive-conversation-no-options", extra=options
                )
                return _ThreadArchiveRead(
                    "no_options",
                    None,
                    f" {options['triggers']} options button(s) matched.",
                )
            await self._session.delay(_THREAD_POLL)
        name = options["name"] or ""
        participant = name[len(text.options_prefix) :].strip() or None
        if folder == "archived":
            return _ThreadArchiveRead("archived", participant)

        await page.evaluate(
            THREAD_MENU_SURVEY_JS,
            {
                "after": False,
                "trigger": _THREAD_OPTIONS_MARK,
                "seen": _THREAD_SEEN_MARK,
            },
        )
        await page.locator(f"main button[{_THREAD_OPTIONS_MARK}]").click(timeout=5000)
        deadline = self._session.monotonic() + _THREAD_MENU_TIMEOUT
        while True:
            menu = await page.evaluate(
                THREAD_MENU_ITEMS_JS,
                {
                    "archive": text.archive_item,
                    "trigger": _THREAD_OPTIONS_MARK,
                    "seen": _THREAD_SEEN_MARK,
                    "mark": _THREAD_ARCHIVE_MARK,
                },
            )
            if menu["archive"] == 1:
                return _ThreadArchiveRead("archivable", participant)
            if self._session.monotonic() >= deadline:
                break
            await self._session.delay(_THREAD_POLL)
        # The menu is still open: read it and take its picture before closing.
        survey = await page.evaluate(
            THREAD_MENU_SURVEY_JS,
            {"after": True, "trigger": _THREAD_OPTIONS_MARK, "seen": _THREAD_SEEN_MARK},
        )
        await record_page_trace(page, "archive-conversation-menu", extra=survey)
        await page.keyboard.press("Escape")
        return _ThreadArchiveRead(
            "no_item",
            participant,
            f" 1 options button matched. {_menu_evidence(survey)} A screenshot "
            "of the open menu is in the trace directory when tracing is on.",
        )
