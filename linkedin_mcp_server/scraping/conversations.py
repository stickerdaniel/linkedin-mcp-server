"""Messaging inbox, thread and conversation-search workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote_plus

import logging
import re

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.identifiers import (
    messaging_thread_url,
    normalize_person_identifier,
    normalize_thread_id,
    person_profile_url,
)
from linkedin_mcp_server.scraping.link_metadata import (
    Reference,
    build_references,
    dedupe_references,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.profile_page import ProfilePageReader
from linkedin_mcp_server.scraping.session import ScrapingSession
from linkedin_mcp_server.scraping.text import (
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
        session: ScrapingSession,
        navigator: PageNavigator,
        content: PageContentReader,
        profile_page: ProfilePageReader,
    ):
        self._session = session
        self._navigator = navigator
        self._content = content
        self._profile_page = profile_page

    @staticmethod
    def _single_section_result(
        url: str,
        section_name: str,
        text: str,
        references: list[Reference] | None = None,
    ) -> dict[str, Any]:
        """Build a standard single-section scraping response."""
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
        """Wait for main content to populate enough text to scrape."""
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
            await self._session.page.evaluate(
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
        outcome: dict[str, Any] = await self._session.page.evaluate(
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
                    return match ? match[1] : null;
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
        The compose page lists the inbox rows without opening a thread, which
        bare `/messaging/` does. The inbox list is used before `?searchTerm=`
        because LinkedIn's messaging search frequently returns "We didn't find
        anything" for a participant whose thread is plainly present in the
        inbox (issue #434). ``name_filter`` is passed to the enumerator so only
        matching rows are clicked; clicking a row may mark it read, so
        unrelated threads stay untouched.

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
        matching row and no barrier. Both render newest activity first. An
        index at or past an incomplete prefix is refused rather than served
        from a row that could not be verified.
        """
        if index < 0:
            raise LinkedInScraperException(f"index must be non-negative (got {index}).")

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
            raise LinkedInScraperException(
                f"Could not resolve a display name for {linkedin_username}."
            )

        try:
            resolution = await self._resolve_conversation_thread_urls(display_name)
            thread_urls = resolution.eligible_urls
            if index >= len(thread_urls):
                # Not InvalidReferenceError: the username is valid and the page
                # could not be verified, which is worth an issue report.
                if resolution.barrier is not None:
                    raise LinkedInScraperException(
                        _unverified_index_message(index, linkedin_username, resolution)
                    )
                if not thread_urls:
                    raise LinkedInScraperException(
                        f"Could not find a conversation for {linkedin_username}."
                    )
                raise LinkedInScraperException(
                    f"index {index} out of range: only {len(thread_urls)} "
                    f"thread(s) exist for {linkedin_username}."
                )

            await self._navigator._navigate_to_page(thread_urls[index])
        except PlaywrightTimeoutError as exc:
            raise LinkedInScraperException(
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
