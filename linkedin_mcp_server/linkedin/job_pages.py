"""Page-level reads behind the job search and saved-job list workflows.

A lower-level service than the workflows in `jobs.py` rather than a peer of
them: it navigates, scrolls, extracts and counts one page at a time, and
answers with a `JobPageCapture`. The pagination policy, the budgets and the
diagnostics that decide what a page *means* stay with the workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import asyncio
import logging
import re
import time
from urllib.parse import parse_qs, urlparse

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import LinkedInOperationError
from linkedin_mcp_server.core.utils import (
    _JOB_CARD_SELECTOR,
    _RAIL_PICK_JS,
    detect_rate_limit,
    handle_modal_close,
    scroll_job_sidebar,
    scroll_to_bottom,
)
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.linkedin.capture import RATE_LIMIT_RETRY_DELAY
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
)
from linkedin_mcp_server.linkedin.identifiers import job_view_url
from linkedin_mcp_server.linkedin.job_policy import (
    SAFETY_REDIRECT_PATH,
    SCROLL_DEADLINE_MAX,
    ApplyType,
    employer_apply_url,
    parse_posting_title,
    route,
    same_job_search,
)
from linkedin_mcp_server.linkedin.link_metadata import JOB_PATH_RE, build_references
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.text import (
    JobApplyTextTable,
    filter_linkedin_noise_lines,
    truncate_linkedin_noise,
)

logger = logging.getLogger(__name__)

# What the rate-limit retry has to have left after its backoff before it is
# worth starting. The retry is a whole page again, and a search page measured
# 6.5s on average (ten navigations of a Paris developer search, 83s). One
# admitted with less times out in navigation, which trades the rate-limit
# answer the first attempt already had for a timeout that explains nothing.
_RETRY_PAGE_ALLOWANCE = 6.5


# The id is the trailing run of digits, and LinkedIn serves the same job under
# both `/jobs/view/1967281839/` and `/jobs/view/<title>-at-<company>-1967281839/`.
# Anchoring the digits to the front of the segment loses the slugged form
# entirely, and reads `2026` out of a title that opens with a year.
#
# The slug is anything but a separator, not `[\w-]`: JS `\w` is ASCII, and a
# localized title reaches this as `d%C3%A9veloppeur-web-at-koul-3510216552`,
# where the `%` ends the match and the id is lost. Measured on the guest
# search API for `developpeur`, where 6 of 10 hrefs were percent-encoded.
# The authenticated pages this server visits serve bare ids today (measured
# across job search, collections and a French search: 0 slugs in 27 anchors),
# so this branch is defensive on both counts.
# `scoped` runs the sidebar's own rule again and reads only the container it
# names, because everything outside it is not a search result: the detail pane
# holds its own permalink and, once opened, a similar-jobs module, and counting
# those as rendered results advances the offset past results the rail never
# showed. Re-run rather than remembered, so a rail replaced between the scroll
# and this call is followed instead of silently widening the scope back to the
# document. `get_saved_jobs` reads the document, having no sidebar to scroll
# and no second list to be confused with.
JOB_IDS_JS = (
    r"""(opts) => {
    const {selector, scoped} = opts;
"""
    + _RAIL_PICK_JS
    + r"""
    const picked = scoped ? pickRail() : null;
    const scope = picked || document;
    const cards = scope.querySelectorAll(selector);
    const seen = new Set();
    const ids = [];
    for (const card of cards) {
        // `idOf` from the rail rule above, rather than a second copy of the
        // pattern: the rail is picked by counting ids, so a card shape one
        // side understands and the other does not would have extraction read
        // a container the pick never considered.
        const id = idOf(card);
        if (id && !seen.has(id)) {
            seen.add(id);
            ids.push(id);
        }
    }
    return {ids: ids, scoped: Boolean(picked)};
}"""
)

# The same scope as `JOB_IDS_JS`, since a pane job is not a result either. A
# card is the largest element around a job link that holds no other job, found
# by counting ids and not by class, so the classic `<li>` and the redesigned
# card are both found. Only the label is text, and it comes from the locale
# table.
PROMOTED_JOB_IDS_JS = (
    r"""(opts) => {
    const {selector, label} = opts;
"""
    + _RAIL_PICK_JS
    + r"""
    const scope = pickRail() || document;
    const cardOf = (node) => {
        let card = node;
        while (card.parentElement && card.parentElement !== scope
               && card.parentElement !== document.body
               && idsIn(card.parentElement) === 1) {
            card = card.parentElement;
        }
        return card;
    };
    const seen = new Set();
    const promoted = [];
    for (const node of scope.querySelectorAll(selector)) {
        const id = idOf(node);
        if (!id || seen.has(id)) continue;
        seen.add(id);
        const lines = (cardOf(node).innerText || '').split('\n')
            .map((line) => line.trim());
        if (lines.includes(label)) promoted.push(id);
    }
    return promoted;
}"""
)

# How many unpromoted results sit above the notice the redesigned search puts
# where its matches end, or null when nothing on the page reads as the notice.
# Null rather than zero, because zero is what calls the search a no-match. The
# notice is looked for in the whole document, the cards only in the rail,
# each read as `PROMOTED_JOB_IDS_JS` reads it.
#
# The notice is the smallest element whose text is the whole sentence, reached
# from a text node that opens it, so a sentence LinkedIn splits across inline
# elements is still found. Its text only matched when it was one text node,
# and a page whose notice was split then kept its unrelated postings.
ORGANIC_BEFORE_NOTICE_JS = (
    r"""(opts) => {
    const {selector, label, notice} = opts;
"""
    + _RAIL_PICK_JS
    + r"""
    const scope = pickRail() || document;
    const cardOf = (node) => {
        let card = node;
        while (card.parentElement && card.parentElement !== scope
               && card.parentElement !== document.body
               && idsIn(card.parentElement) === 1) {
            card = card.parentElement;
        }
        return card;
    };
    const isPromoted = (node) => (cardOf(node).innerText || '').split('\n')
        .map((line) => line.trim()).includes(label);
    const norm = (text) => (text || '').replace(/\s+/g, ' ').trim();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let mark = null;
    while (!mark && walker.nextNode()) {
        const piece = norm(walker.currentNode.data);
        if (!piece || !notice.startsWith(piece)) continue;
        let node = walker.currentNode;
        while (node && norm(node.textContent).length < notice.length) {
            node = node.parentNode;
        }
        if (node && norm(node.textContent) === notice) mark = node;
    }
    if (!mark) return null;
    const seen = new Set();
    let organic = 0;
    for (const node of scope.querySelectorAll(selector)) {
        const id = idOf(node);
        if (!id || seen.has(id)) continue;
        seen.add(id);
        const above = mark.compareDocumentPosition(node)
            & Node.DOCUMENT_POSITION_PRECEDING;
        if (above && !isPromoted(node)) organic += 1;
    }
    return organic;
}"""
)

# This posting's apply control and state, once they render. Easy Apply comes in
# two shapes. The anchor into the posting's own `/apply/` route
# (`<a href=".../jobs/view/<id>/apply/?openSDUIApplyFlow=true">`, measured on
# 2026-09-14) is found by its URL, which the "More jobs" cards, each linking its
# own posting, cannot match. The `<button>` with no href that replaced it
# (measured on 2026-09-25) is found by its text, above the description only; the
# cards below show the same words, as text inside their links.
#
# The external control is an `<a target="_blank">` into the off-site
# interstitial, which names the employer's page in its href (measured on
# 2026-09-19), so it is answered without a click. It and the state lines are
# read above the description heading only: below it sit the "More jobs" cards,
# each with an Apply of its own. Without the heading nothing is proven to be
# this posting's, and the read answers `unknown`.
#
# On 2026-09-14 the external control was a `<button>` with no href, whose click
# opened a "Share your profile?" dialog or a tab. LinkedIn counts that click as
# an apply on the posting, and no posting has shown the button since, so it is
# not read: a posting carrying it answers `unknown` and is never clicked.
#
# The heading is found by walking text nodes rather than asking every element
# for its text. Both find it; the walk visits fewer nodes and copies none of
# them, and `textContent` on every element of a posting copies that posting
# once per level of nesting. That is worth the difference because the readiness
# poll runs this program on every frame for up to ten seconds.
APPLY_SIGNALS_JS = r"""(opts) => {
    const {
        applyPath, redirectPath, easyApplyLabel, externalLabel, descriptionHeadings,
        closedLines, appliedPattern,
    } = opts;
    const main = document.querySelector('main');
    if (!main) return null;
    const pathOf = (anchor) => {
        try {
            return new URL(anchor.href).pathname.replace(/\/+$/, '');
        } catch (error) {
            return '';
        }
    };
    const walk = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
    let heading = null;
    while (walk.nextNode()) {
        if (descriptionHeadings.includes((walk.currentNode.nodeValue || '').trim())) {
            heading = walk.currentNode;
            break;
        }
    }
    const above = (el) => Boolean(
        heading && (heading.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_PRECEDING)
    );
    const lines = (main.innerText || '').split('\n').map((line) => line.trim());
    const end = lines.findIndex((line) => descriptionHeadings.includes(line));
    const top = end === -1 ? [] : lines.slice(0, end);
    const applied = new RegExp(appliedPattern);
    const anchors = [...main.querySelectorAll('a[href]')];
    const link = anchors.find((anchor) => above(anchor)
        && (anchor.innerText || '').trim() === externalLabel
        && pathOf(anchor) === redirectPath);
    return {
        bounded: Boolean(heading),
        easy_apply: anchors.some((anchor) => pathOf(anchor) === applyPath)
            || [...main.querySelectorAll('button')].some((el) => above(el)
                && (el.innerText || '').trim() === easyApplyLabel),
        external_link: link ? link.href : null,
        applied: top.some((line) => applied.test(line)),
        closed: top.some((line) => closedLines.includes(line)),
    };
}"""

# Ready once the description heading and a signal are both in. The Easy Apply
# anchor is found without the heading (the button is not), but the applied and
# closed lines are not, so a read that settled on the anchor alone could call
# such a posting open.
APPLY_READY_JS = (
    "(opts) => {\n    const signals = (" + APPLY_SIGNALS_JS + ")(opts);\n"
    "    return Boolean(signals && signals.bounded && (signals.easy_apply\n"
    "        || signals.external_link || signals.applied || signals.closed));\n}"
)

# How long the apply control gets to render.
_APPLY_READY_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class JobApplyRead:
    """How one posting takes applications, and where the employer's form is."""

    type: ApplyType
    url: str | None = None


# A search result card and its dismiss control, in both of LinkedIn's job
# searches, measured on signed-in en-US accounts. The job named only by
# `currentJobId` sits in the detail pane, which has no card and no dismiss
# control.
#
# - Classic `/jobs/search/` (2026-09-28): each result is an
#   `li[data-occludable-job-id]` holding the job id. The dismiss control is
#   the card's `button` around an icon with `data-test-icon="close-small"`
#   (some cards also carry a "More options" button around
#   `overflow-web-ios-small`).
# - Redesigned `/jobs/search-results/` (2026-10-14): each result is an element
#   whose `componentkey` is `job-card-component-ref-<job id>`, with a nested
#   element repeating that key, so only the outermost counts. The dismiss
#   control is its `button` around `svg#close-small`.
#
# In both, clicking it keeps the card and swaps the button for one around
# `undo-small`, which is the only undo there is: a reload drops the job from
# that search and every later one. Applied jobs carry the same control. The
# button's `aria-label` names the job in the account's language and is never
# read. More than one card for the id, or more than one dismiss button in it,
# is refused rather than guessed at.
SEARCH_CARD_JS = r"""(opts) => {
    const {jobId, mark} = opts;
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const refKey = '[componentkey^="job-card-component-ref-"]';
    const classic = [...document.querySelectorAll('li[data-occludable-job-id]')];
    const redesigned = [...document.querySelectorAll(refKey)].filter(
        (card) => !card.parentElement || !card.parentElement.closest(refKey)
    );
    const idOf = (card) => card.getAttribute('data-occludable-job-id')
        ?? card.getAttribute('componentkey').slice('job-card-component-ref-'.length);
    const cards = [...classic, ...redesigned];
    const own = cards.filter((card) => idOf(card) === jobId);
    const result = {cards: cards.length, matches: own.length, dismiss: 0, undo: 0};
    if (own.length !== 1) return result;
    const card = own[0];
    // Occluded cards render their content only near the viewport.
    card.scrollIntoView({block: 'center'});
    const around = (icon) => [...card.querySelectorAll('button')].filter(
        (button) => button.querySelector(
            '[data-test-icon="' + icon + '"], svg#' + icon
        )
    );
    const dismiss = around('close-small');
    result.dismiss = dismiss.length;
    result.undo = around('undo-small').length;
    if (dismiss.length === 1) dismiss[0].setAttribute(mark, '');
    return result;
}"""

_SEARCH_CARD_SELECTOR = (
    'li[data-occludable-job-id], [componentkey^="job-card-component-ref-"]'
)
# The job search parameters a landed search has to keep by value: what the
# search is about and where. The worldwide `geoId` is ours, never a location
# string LinkedIn turns into one, so its value is comparable.
_SEARCH_VALUE_PARAMS = frozenset({"keywords", "geoId"})
_DISMISS_MARK = "data-mcp-dismiss"

# How long a search gets to render its cards, a found card its buttons, and a
# clicked card its undo state.
_SEARCH_READY_TIMEOUT = 10.0
_SEARCH_CARD_RENDER_TIMEOUT = 5.0
_DISMISS_TIMEOUT = 5.0
# How long a posting gets to set its "<title> | <company> | LinkedIn" title.
_POSTING_TITLE_TIMEOUT = 10.0
_SEARCH_POLL = 0.25

SearchCardOutcome = Literal[
    # The dismiss button was clicked and the card turned to its undo state.
    "dismissed",
    # The card is already in its undo state.
    "already_dismissed",
    # No card for the job in the search's results, scrolled to the end.
    "absent",
    # The card is there but no single dismiss button could be told to be its.
    "no_button",
    # The button was clicked and the card did not change.
    "not_dismissed",
]


@dataclass(frozen=True, slots=True)
class JobPageCapture:
    """One job-list page read, plus what the reader alone can still answer.

    Everything a page attempt used to leave behind on the extractor travels
    here instead. That was one field, `_scroll_seconds`, and it was the only
    state crossing the page-attempt boundary without being a parameter or a
    return value; the budgets, the offset, the seen ids and the warnings were
    always the workflow's own locals and stay there.

    `landed_url` is where the browser actually is when the attempt ends, taken
    inside the reader because that is the same read the caller would make:
    nothing between this value and the caller's first look at the address
    awaits anything. The saved-jobs offset check is the one place that may not
    use it, because a page-count read runs first and can move the address; see
    `current_url`.
    """

    section: ExtractedSection
    landed_url: str
    scroll_seconds: float


@dataclass(slots=True)
class _ScrollCharge:
    """What the sidebar scroll has spent on one page, across both attempts.

    Mutable and private, unlike the capture built from it: the scroll books
    its own time in a `finally`, so an attempt that raises after scrolling
    still has to be charged, and a return value cannot carry that. It never
    leaves this module.
    """

    seconds: float = 0.0


class JobPageReader:
    """Read one job-search or saved-job page at a time.

    A page service under the job workflows rather than a peer of them. It
    consumes `PageNavigator` for navigation watching, document-origin checks
    and settling, and answers with a `JobPageCapture`; which page to ask for
    next, and what an answer means, belong to `jobs.JobReader`.
    """

    def __init__(
        self,
        session: PageSession,
        navigator: PageNavigator,
        content: PageContentReader,
    ):
        self._session = session
        self._navigator = navigator
        self._content = content

    @property
    def current_url(self) -> str:
        """Where the browser is right now, with nothing awaited to find out.

        The saved-jobs offset check needs the address as it stands *after* the
        page-count read, which the capture's `landed_url` predates. Neither an
        await nor a document-identity check on purpose: adding either would be
        a new observation on a path that has none today.
        """
        return self._session.page.url

    def _captured(
        self, section: ExtractedSection, scroll_seconds: float = 0.0
    ) -> JobPageCapture:
        """Seal one page attempt together with where it ended."""
        return JobPageCapture(
            section=section,
            landed_url=self._session.page.url,
            scroll_seconds=scroll_seconds,
        )

    async def read_apply_link(
        self, url: str, job_id: str, text: JobApplyTextTable
    ) -> JobApplyRead:
        """Read how a posting takes applications, and an external one's address.

        Nothing is clicked. An external Apply is a link whose href names the
        employer's address; a posting whose Apply is anything else answers
        `unknown`.

        The employer's address is answered as LinkedIn gives it and never
        loaded. It comes from whoever posted the job, and loading it would let
        a stranger's link, or any redirect behind it, send a request from this
        host to whatever the host can reach.
        """
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        page = self._session.page
        opts = {
            "applyPath": f"/jobs/view/{job_id}/apply",
            "redirectPath": SAFETY_REDIRECT_PATH,
            "easyApplyLabel": text.easy_apply_label,
            "externalLabel": text.external_apply_label,
            "descriptionHeadings": list(text.description_headings),
            "closedLines": list(text.closed_lines),
            "appliedPattern": text.applied_pattern.pattern,
        }
        try:
            await page.wait_for_function(
                APPLY_READY_JS, arg=opts, timeout=_APPLY_READY_TIMEOUT * 1000
            )
        except PlaywrightTimeoutError:
            logger.debug("No apply control or posting state rendered on %s", url)

        signals = await self._session.run_on_linkedin(APPLY_SIGNALS_JS, opts)
        # Both states before any control, so a posting in either never reads
        # as open.
        if signals and signals["applied"]:
            return JobApplyRead("applied")
        if signals and signals["closed"]:
            return JobApplyRead("closed")
        if signals and signals["easy_apply"]:
            return JobApplyRead("easy_apply")
        if signals and signals["external_link"]:
            return JobApplyRead(
                "external", employer_apply_url(signals["external_link"])
            )
        # A barrier served in place of the posting renders none of the above
        # either, and it needs the relogin path rather than a type.
        await self._navigator._raise_if_auth_barrier(url)
        return JobApplyRead("unknown")

    async def _extract_job_ids(self, *, scoped: bool = False) -> list[str]:
        """Extract unique job IDs from job card links on the current page.

        Finds all `a[href*="/jobs/view/"]` links and extracts the numeric
        job ID from each href. Returns deduplicated IDs in DOM order.

        Args:
            scoped: Read only the results rail, chosen by the same rule the
                sidebar scroll uses. Off for lists that have no rail.
        """
        result = await self._session.run_on_linkedin(
            JOB_IDS_JS, {"selector": _JOB_CARD_SELECTOR, "scoped": scoped}
        )
        if scoped and not result["scoped"]:
            # The whole document, because a page with nothing scrollable
            # rendered everything it has and returning no ids at all would
            # lose the results along with the detail pane's links. Said out
            # loud, because it is the one path where the offset can count
            # something the rail never showed, and it has not been observed:
            # live a search page has two scrollable candidates.
            logger.warning(
                "No results rail on %s, reading job ids from the whole document",
                self._session.page.url,
            )
        return result["ids"]

    async def _extract_promoted_job_ids(self, label: str) -> list[str]:
        """Ids of the results rail's cards that carry ``label`` as a line.

        Raises when the page answers with anything but a list, so a caller
        treating this as best effort cannot mistake a failed read for a page
        without promoted jobs.
        """
        result = await self._session.run_on_linkedin(
            PROMOTED_JOB_IDS_JS, {"selector": _JOB_CARD_SELECTOR, "label": label}
        )
        if not isinstance(result, list):
            raise TypeError(f"Promoted job ids read returned {type(result).__name__}")
        return [job_id for job_id in result if isinstance(job_id, str)]

    async def _count_organic_before_notice(self, notice: str, label: str) -> int | None:
        """Unpromoted results above ``notice``, or None without the notice.

        Raises when the page answers with anything else, so a caller treating
        this as best effort cannot read a failed read as zero, which would
        discard the page as a no-match.
        """
        result = await self._session.run_on_linkedin(
            ORGANIC_BEFORE_NOTICE_JS,
            {"selector": _JOB_CARD_SELECTOR, "label": label, "notice": notice},
        )
        if result is None:
            return None
        if isinstance(result, bool) or not isinstance(result, int):
            raise TypeError(f"Organic results read returned {type(result).__name__}")
        return result

    async def _extract_search_page(
        self,
        url: str,
        section_name: str,
        scroll_deadline: float = SCROLL_DEADLINE_MAX,
        page_deadline: float | None = None,
    ) -> JobPageCapture:
        """Extract innerText from a job search page with soft rate-limit retry.

        Mirrors the noise-only detection and single-retry behavior of
        ``SectionCapture`` so that callers get a ``RATE_LIMITED_SECTION_TEXT``
        sentinel instead of silent empty results.

        One charge for both attempts, and it survives an attempt that raises:
        the scroll below books what it spent in a ``finally``, and the error
        path here still answers with a capture the caller charges its budget
        from. Losing that let a page whose extraction failed after a full
        twelve-second scroll cost the search nothing.

        ``page_deadline`` is where the search's budget ends, on the monotonic
        clock. Both attempts navigate and scroll against it, and the retry is
        not started when its backoff and a page will not fit before it.
        """
        charge = _ScrollCharge()
        try:
            result = await self._extract_search_page_once(
                url,
                section_name,
                scroll_deadline,
                charge=charge,
                page_deadline=page_deadline,
            )
            if result.text != RATE_LIMITED_SECTION_TEXT:
                return self._captured(result, charge.seconds)

            if page_deadline is not None:
                left = page_deadline - time.monotonic()
                if left < RATE_LIMIT_RETRY_DELAY + _RETRY_PAGE_ALLOWANCE:
                    logger.info(
                        "Not retrying search page %s: %.1fs of the budget left",
                        url,
                        left,
                    )
                    return self._captured(result, charge.seconds)

            logger.info(
                "Retrying search page %s after %.0fs backoff",
                url,
                RATE_LIMIT_RETRY_DELAY,
            )
            await asyncio.sleep(RATE_LIMIT_RETRY_DELAY)
            result = await self._extract_search_page_once(
                url,
                section_name,
                scroll_deadline / 2,
                charge=charge,
                page_deadline=page_deadline,
            )
            if result.text == RATE_LIMITED_SECTION_TEXT:
                logger.warning("Search page %s still rate-limited after retry", url)
            return self._captured(result, charge.seconds)

        except LinkedInOperationError:
            raise
        except Exception as e:
            logger.warning("Failed to extract search page %s: %s", url, e)
            return self._captured(
                ExtractedSection(
                    text="",
                    references=[],
                    error=build_issue_diagnostics(
                        e,
                        context="extract_search_page",
                        target_url=url,
                        section_name=section_name,
                    ),
                ),
                charge.seconds,
            )

    async def _extract_search_page_once(
        self,
        url: str,
        section_name: str,
        scroll_deadline: float = SCROLL_DEADLINE_MAX,
        *,
        charge: _ScrollCharge,
        page_deadline: float | None = None,
    ) -> ExtractedSection:
        """Single attempt to navigate, scroll sidebar, and extract innerText."""
        await self._navigator._navigate_to_page(url, deadline=page_deadline)
        await detect_rate_limit(self._session.page)
        # Above the selector wait and the modal close, so the window this
        # opens covers everything read from here on. Taken between them, a
        # reload committing during either one became the baseline itself, and
        # `main_found` then described a document that no longer existed.
        origin = await self._navigator._document_origin()

        main_found = True
        try:
            await self._session.page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("No <main> element found on %s", url)
            main_found = False

        await handle_modal_close(self._session.page)

        # `scroll_job_sidebar` swallows whatever its evaluate raises, so that a
        # rail replaced mid-flight does not cost the caller the page it is
        # about to read. A navigation destroys that context the same way and is
        # not the same thing: what waits to be read is then an authwall or a
        # checkpoint, and extracting it returns login text under
        # `search_results` with nothing beside it to say so.
        #
        # The route is compared as well as watched, because a redirect can
        # finish before the listener is registered. Host and path, and not the
        # whole URL, because LinkedIn appends `currentJobId` to the query of a
        # search page by itself. Measured across three live searches: the path
        # never moved, and neither did the query. The host has to come along,
        # or a redirect that keeps the path reads as no redirect at all.
        #
        # Against the URL that was asked for, and not the one the page held
        # after navigating, or a redirect finishing before the scroll becomes
        # its own baseline and passes. Outside the `main_found` branch for the
        # same reason: a landing page with no `<main>` extracts to nothing, and
        # an empty section is what an exhausted search looks like.
        before = route(url)
        moved = False
        navigated = False
        with self._navigator._watching_navigations() as hops:
            if main_found:
                scroll_started = time.monotonic()
                if page_deadline is not None:
                    # The scroll deadline was worked out before navigating.
                    # A slow `goto`, the `<main>` wait and the modal close all
                    # spend time it does not know about, and the scroll's own
                    # clock only starts when it is called.
                    scroll_deadline = min(
                        scroll_deadline, max(0.0, page_deadline - scroll_started)
                    )
                try:
                    moved = await scroll_job_sidebar(
                        self._session.page, deadline=scroll_deadline
                    )
                finally:
                    # Only what the scroll spent. Charging the whole page
                    # charged navigation and extraction to a budget that
                    # exists to bound scrolling, so five slow navigations that
                    # scrolled instantly still left the pages behind them with
                    # nothing. Accumulated, because a retry scrolls a second
                    # time.
                    charge.seconds += time.monotonic() - scroll_started
            # `hops` is read and not waited on, so a healthy page pays
            # nothing for it. It is what a scroll that finished cleanly leaves
            # behind when the document was replaced anyway: the scroll never
            # raised, so it reports no movement, and a reload moves no route,
            # so neither of the other two says anything happened.
            if moved or hops or before != route(self._session.page.url):
                navigated = await self._navigator._settle_navigation(hops, origin)

        after = route(self._session.page.url)
        if navigated or moved or not main_found or before != after:
            # Any of the three is enough, and none implies the others. A reload
            # keeps the address, so an account picker served in place of the
            # search page changes nothing the comparison below can see; a
            # redirect that completed during the navigation moves the route
            # without the scroll ever raising; and a barrier page carries no
            # `<main>`, so the scroll it would have raised from never ran.
            # That third one is the shape this check exists for and the one it
            # missed: an exhausted search renders no `<main>` either, which is
            # why the check has to decide it rather than the absence alone.
            await self._navigator._raise_if_auth_barrier(url)
        if before != after and not same_job_search(before, after):
            # An expired session lands here as often as a layout change does,
            # and the two need different answers. A plain error is caught by
            # the generic handler above and returned as a section diagnostic,
            # so the browser stays registered and no re-login is offered; the
            # caller then repeats the search against the same barrier.
            raise RuntimeError(
                f"Page navigated to {self._session.page.url} while scrolling {url}"
            )

        raw_result = await self._content._extract_root_content(["main"])

        # The watcher covers the scroll and nothing else, and the read sits
        # outside it at both ends: a reload committing after the listener came
        # off, or during the extraction itself, moves no route and raises
        # nothing. The document says what neither the address nor the listener
        # can, and it is asked about the text that was actually read.
        if origin is not None and await self._navigator._document_origin() != origin:
            logger.debug("The search document was replaced before it was read")
            await self._navigator._raise_if_auth_barrier(url)

        raw = raw_result["text"]
        if raw_result["source"] == "body":
            logger.debug("No <main> at evaluation time on %s, using body fallback", url)
        elif not main_found:
            logger.debug(
                "<main> appeared after wait timeout on %s, sidebar scroll was skipped",
                url,
            )

        if not raw:
            return ExtractedSection(text="", references=[])
        truncated = truncate_linkedin_noise(raw)
        if not truncated and raw.strip():
            logger.warning(
                "Search page %s returned only LinkedIn chrome (likely rate-limited)",
                url,
            )
            return ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[])
        cleaned = filter_linkedin_noise_lines(truncated)
        return ExtractedSection(
            text=cleaned,
            references=build_references(
                raw_result["references"], section_name, apply_cap=False
            ),
        )

    async def _get_total_search_pages(self) -> int | None:
        """Read total page count from LinkedIn's pagination state element.

        Parses the "Page X of Y" text from ``.jobs-search-pagination__page-state``.
        Returns ``None`` when the element is absent or unparseable.

        NOTE: This is a deliberate DOM exception. The element has ``display: none``
        (screen-reader only), so the text never appears in ``innerText``. A class-based
        selector is the only reliable way to read it. Gracefully returns ``None`` if
        LinkedIn renames the class — pagination just falls back to ``max_pages``.
        """
        text = await self._session.run_on_linkedin(
            """() => {
                const el = document.querySelector(
                    '.jobs-search-pagination__page-state'
                );
                return el ? el.textContent.trim() : null;
            }"""
        )
        if not text:
            return None
        match = re.search(r"of\s+(\d+)", text)
        return int(match.group(1)) if match else None

    async def _extract_saved_jobs_page(
        self,
        url: str,
        section_name: str,
    ) -> JobPageCapture:
        """Extract innerText from a saved-jobs page with soft rate-limit retry."""
        with self._navigator._watching_navigations() as hops:
            try:
                result = await self._extract_saved_jobs_page_once(url, section_name)
                if result.text != RATE_LIMITED_SECTION_TEXT:
                    return self._captured(result)

                logger.info(
                    "Retrying saved jobs page %s after %.0fs backoff",
                    url,
                    RATE_LIMIT_RETRY_DELAY,
                )
                await asyncio.sleep(RATE_LIMIT_RETRY_DELAY)
                result = await self._extract_saved_jobs_page_once(url, section_name)
                if result.text == RATE_LIMITED_SECTION_TEXT:
                    logger.warning(
                        "Saved jobs page %s still rate-limited after retry", url
                    )
                return self._captured(result)

            except LinkedInOperationError:
                raise
            except Exception as e:
                logger.warning("Failed to extract saved jobs page %s: %s", url, e)
                # A navigation destroys the scroll's execution context, and
                # what waits behind it is a checkpoint as often as a layout
                # change. Turning that into a section diagnostic hands the
                # caller an empty list, leaves the browser registered and
                # offers no relogin, so the next call meets the same barrier.
                #
                # Whether one happened is the listener's answer and not the
                # address's: this list reaches `/jobs-tracker/` by a redirect
                # LinkedIn makes on purpose, so comparing against the URL that
                # was asked for finds a difference on every ordinary failure
                # and waits out a chain that is not running.
                #
                # No document baseline, so every hop counts. One is taken
                # before the search scroll, where the page is already loaded
                # and the only navigation to expect is one going wrong. Here
                # the block opens before this page's own navigation, so a
                # reading from the top belongs to the document that was left
                # and would call every ordinary failure a replacement. `None`
                # says so, and settling costs a moment on a path that has
                # already failed.
                try:
                    await self._navigator._settle_navigation(hops, None)
                except Exception:
                    logger.debug(
                        "Could not settle the route after a saved-jobs failure",
                        exc_info=True,
                    )
                await self._navigator._raise_if_auth_barrier(
                    self._session.page.url, navigation_error=e
                )
                return self._captured(
                    ExtractedSection(
                        text="",
                        references=[],
                        error=build_issue_diagnostics(
                            e,
                            context="extract_saved_jobs_page",
                            target_url=url,
                            section_name=section_name,
                        ),
                    )
                )

    async def _extract_saved_jobs_page_once(
        self,
        url: str,
        section_name: str,
    ) -> ExtractedSection:
        """Single attempt: navigate, scroll list, and extract innerText."""
        await self._navigator._navigate_to_page(url)
        await detect_rate_limit(self._session.page)
        # Taken after this page's own navigation, so it belongs to the
        # document about to be read rather than to the one that was left.
        origin = await self._navigator._document_origin()

        main_found = True
        try:
            await self._session.page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("No <main> element found on %s", url)
            main_found = False

        await handle_modal_close(self._session.page)
        if main_found:
            await scroll_to_bottom(self._session.page, pause_time=0.5, max_scrolls=5)
        else:
            # A picker served in place of the list keeps the list's address
            # and its title, so the route guard below sees an allowed page and
            # the body fallback returns the picker under `saved_jobs`. Missing
            # `<main>` is what is left, and an emptied list has none either,
            # which is why the check decides it rather than the absence.
            await self._navigator._raise_if_auth_barrier(self._session.page.url)

        # A picker served by a reload keeps this page's address and this
        # page's title, so the route guard reads it as the list. Nothing else
        # notices either: the scroll pauses half a second between rounds, and
        # a document replaced in that gap leaves no evaluation to raise, so
        # the extraction succeeds against the replacement and returns it under
        # `saved_jobs` with the browser left on a barrier.
        #
        # Asked after the read rather than before it, or the gap between the
        # two is a window of its own and the text that came back is not the
        # text the check judged.
        raw_result = await self._content._extract_root_content(["main"])
        if origin is not None and await self._navigator._document_origin() != origin:
            logger.debug("The saved-jobs document was replaced before it was read")
            await self._navigator._raise_if_auth_barrier(self._session.page.url)
        raw = raw_result["text"]
        if raw_result["source"] == "body":
            logger.debug("No <main> at evaluation time on %s, using body fallback", url)
        elif not main_found:
            logger.debug(
                "<main> appeared after wait timeout on %s, scroll was skipped",
                url,
            )

        if not raw:
            return ExtractedSection(text="", references=[])
        truncated = truncate_linkedin_noise(raw)
        if not truncated and raw.strip():
            logger.warning(
                "Saved jobs page %s returned only LinkedIn chrome (likely rate-limited)",
                url,
            )
            return ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[])
        cleaned = filter_linkedin_noise_lines(truncated)
        return ExtractedSection(
            text=cleaned,
            references=build_references(raw_result["references"], section_name),
        )

    async def _get_total_list_pages(self) -> int | None:
        """Read last page number from artdeco pagination buttons.

        Parses numeric page labels from ``ul.artdeco-pagination__pages``.
        Returns ``None`` when pagination is absent or unparseable.

        NOTE: This is a deliberate DOM exception, mirroring
        ``_get_total_search_pages``. The my-items pager exposes no page count
        in ``innerText`` and no stable attribute to count, so a design-system
        class is the only reachable signal. The labels are numerals rather
        than words, so no locale table is needed. A renamed class, or a locale
        serving non-ASCII numerals that ``parseInt`` cannot read, both yield
        ``None`` — pagination then falls back to ``max_pages`` and the
        no-new-ids early stop.
        """
        value = await self._session.run_on_linkedin(
            """() => {
                const buttons = document.querySelectorAll(
                    'ul.artdeco-pagination__pages li button'
                );
                if (!buttons.length) return null;
                const nums = [...buttons]
                    .map((b) => parseInt(b.textContent.trim(), 10))
                    .filter((n) => !Number.isNaN(n));
                return nums.length ? Math.max(...nums) : null;
            }"""
        )
        return int(value) if value is not None else None

    async def read_posting_title(self, job_id: str) -> tuple[str, str]:
        """Open ``job_id``'s posting and read its card title and company.

        Both come from the document title, which LinkedIn sets once the
        posting renders. Raises when the navigation ends anywhere but that
        posting, after offering the page to the auth-barrier check, or when
        the title never takes that shape.
        """
        url = job_view_url(job_id, "/")
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        page = self._session.page
        landed = JOB_PATH_RE.match(route(page.url)[1])
        if landed is None or landed.group(1) != job_id:
            await self._navigator._raise_if_auth_barrier(page.url)
            raise LinkedInOperationError(
                f"The navigation to job {job_id}'s posting ended on {page.url}"
            )
        deadline = self._session.monotonic() + _POSTING_TITLE_TIMEOUT
        while True:
            title = await page.title()
            parsed = parse_posting_title(title)
            if parsed is not None:
                return parsed
            if self._session.monotonic() >= deadline:
                raise LinkedInOperationError(
                    f"Job {job_id}'s posting never named its title and company: "
                    f"its page title read {title!r}"
                )
            await self._session.delay(_SEARCH_POLL)

    async def _open_job_search(self, url: str) -> None:
        """Navigate to a job search and wait for its result cards to render.

        Raises when the navigation ends anywhere but a job search, after
        offering the page to the auth-barrier check, or on a search that lost
        part of its query: a redirect to the bare search page keeps the route
        and drops the query whole, and its results are an answer to another
        question. The keywords and `geoId` are compared by value, since a
        search with other words or in another place is not the one asked
        for, and the other parameters by presence, as `search_jobs` does,
        since LinkedIn re-encodes some of them. `currentJobId`, which LinkedIn
        appends by itself, was never asked for and is not compared. A search with no results
        renders no card, so that wait ends in silence.
        """
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        page = self._session.page
        if not same_job_search(route(url), route(page.url)):
            await self._navigator._raise_if_auth_barrier(page.url)
            raise LinkedInOperationError(
                f"The job search navigation ended on {page.url}"
            )
        asked = parse_qs(urlparse(url).query)
        landed = parse_qs(urlparse(page.url).query)
        lost = sorted(
            name
            for name in asked
            if not landed.get(name)
            or (name in _SEARCH_VALUE_PARAMS and landed[name][0] != asked[name][0])
        )
        if lost:
            raise LinkedInOperationError(
                f"The job search lost its {', '.join(lost)} on the way to {page.url}"
            )
        await self._session.dismiss_modal()
        try:
            await page.wait_for_selector(
                _SEARCH_CARD_SELECTOR,
                state="attached",
                timeout=int(_SEARCH_READY_TIMEOUT * 1000),
            )
        except PlaywrightTimeoutError:
            logger.debug("No result card rendered on %s", url)

    async def _find_search_card(self, job_id: str) -> dict[str, int]:
        """Count ``job_id``'s cards and buttons, and mark its dismiss button."""
        found = await self._session.page.evaluate(
            SEARCH_CARD_JS, {"jobId": job_id, "mark": _DISMISS_MARK}
        )
        if not isinstance(found, dict):
            raise LinkedInOperationError("The job search page could not be read")
        return found

    async def _locate_search_card(self, job_id: str) -> dict[str, int]:
        """Find ``job_id``'s card, scrolling the rail once when it is not there.

        A found card is polled until its buttons render, since an occluded
        card is empty until it nears the viewport. A scroll that
        raised leaves the rest of the rail unread, so it is an error rather
        than a job the search does not list.
        """
        found = await self._find_search_card(job_id)
        if not found["matches"]:
            if await self._session.scroll_job_sidebar(deadline=SCROLL_DEADLINE_MAX):
                raise LinkedInOperationError(
                    "The job search results could not be scrolled to the end"
                )
            found = await self._find_search_card(job_id)
        deadline = self._session.monotonic() + _SEARCH_CARD_RENDER_TIMEOUT
        while (
            found["matches"] == 1
            and not (found["dismiss"] or found["undo"])
            and self._session.monotonic() < deadline
        ):
            await self._session.delay(_SEARCH_POLL)
            found = await self._find_search_card(job_id)
        return found

    async def search_lists_job(self, url: str, job_id: str, min_cards: int = 1) -> bool:
        """Whether the job search at ``url`` shows a card for ``job_id``.

        Only a search that rendered other cards can say the job is not among
        them: a reload that rendered none, blank or still loading, raises
        instead, since reading it as "not listed" would confirm a dismissal
        nothing confirmed. So does one that rendered fewer than ``min_cards``,
        which the caller sets from what the same search showed before, since
        a partly rendered rail may stop short of the job's card.
        """
        await self._open_job_search(url)
        found = await self._locate_search_card(job_id)
        if found["matches"]:
            return True
        if not found["cards"]:
            raise LinkedInOperationError(
                f"The job search at {url} rendered no result cards, so whether "
                f"it still lists job {job_id} could not be read"
            )
        if found["cards"] < min_cards:
            raise LinkedInOperationError(
                f"The job search at {url} rendered {found['cards']} result cards "
                f"where {min_cards} were expected, so whether it still lists job "
                f"{job_id} could not be read"
            )
        return False

    async def dismiss_search_card(
        self, url: str, job_id: str
    ) -> tuple[SearchCardOutcome, int]:
        """Click the dismiss button on ``job_id``'s card in the search at ``url``.

        Success is the card turning to its undo state; the caller confirms it
        against a fresh load of the search, which should render as many cards
        as this one, returned alongside the outcome, less the job's own.
        """
        await self._open_job_search(url)
        found = await self._locate_search_card(job_id)
        cards = found["cards"]
        if not found["matches"]:
            return "absent", cards
        if found["dismiss"] != 1:
            if found["matches"] == 1 and found["undo"] and not found["dismiss"]:
                return "already_dismissed", cards
            return "no_button", cards

        page = self._session.page
        await page.locator(f"button[{_DISMISS_MARK}]").click(timeout=5000)
        deadline = self._session.monotonic() + _DISMISS_TIMEOUT
        while self._session.monotonic() < deadline:
            await self._session.delay(_SEARCH_POLL)
            found = await self._find_search_card(job_id)
            if found["undo"] and not found["dismiss"]:
                return "dismissed", cards
        return "not_dismissed", cards
