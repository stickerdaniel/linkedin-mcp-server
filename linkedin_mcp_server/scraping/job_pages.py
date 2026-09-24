"""Page-level reads behind the job search and saved-job list workflows.

A lower-level service than the workflows in `jobs.py` rather than a peer of
them: it navigates, scrolls, extracts and counts one page at a time, and
answers with a `JobPageCapture`. The pagination policy, the budgets and the
diagnostics that decide what a page *means* stay with the workflow.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import urlparse

import asyncio
import logging
import re
import socket
import time

from patchright.async_api import Page, Request, Route
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.core.utils import (
    _JOB_CARD_SELECTOR,
    _RAIL_PICK_JS,
    detect_rate_limit,
    handle_modal_close,
    scroll_job_sidebar,
    scroll_to_bottom,
)
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.scraping.capture import RATE_LIMIT_RETRY_DELAY
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
)
from linkedin_mcp_server.scraping.job_policy import (
    SAFETY_REDIRECT_PATH,
    SCROLL_DEADLINE_MAX,
    ApplyType,
    employer_apply_url,
    reaches_the_public_internet,
    route,
    same_job_search,
)
from linkedin_mcp_server.scraping.link_metadata import build_references
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession
from linkedin_mcp_server.scraping.text import (
    JobApplyTextTable,
    filter_linkedin_noise_lines,
    truncate_linkedin_noise,
)

logger = logging.getLogger(__name__)


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

# This posting's own external Apply, and nothing else's. The description
# heading is the boundary the state lines already use: above it sits this
# posting's control area, below it the "More jobs" cards, each carrying an
# Apply of its own. Unscoped, a posting with no control of its own would be
# called external on a neighbour's button and then click it, which LinkedIn
# counts as an apply on the neighbour. Easy Apply needs no boundary, being
# found by a URL only this posting's control can carry.
#
# Without the heading there is no boundary and the whole `main` is read rather
# than nothing: a posting whose description has not rendered yet still has its
# Apply, and refusing to look would answer `unknown` for it.
#
# The heading is found by walking text nodes rather than asking every element
# for its text. Both find it; the walk visits fewer nodes and copies none of
# them, and `textContent` on every element of a posting copies that posting
# once per level of nesting. That is worth the difference because the readiness
# poll runs this program on every frame for up to ten seconds.
_EXTERNAL_APPLY_JS = r"""
    const walk = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
    let heading = null;
    while (walk.nextNode()) {
        if (descriptionHeadings.includes((walk.currentNode.nodeValue || '').trim())) {
            heading = walk.currentNode;
            break;
        }
    }
    const above = (el) => !heading || Boolean(
        heading.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_PRECEDING
    );
    const externalButton = [...main.querySelectorAll('button')].find(
        (el) => above(el) && (el.innerText || '').trim() === externalLabel
    ) || null;
"""

# The attribute that ties the click to the button the read chose. Set in the
# breath before the click and never earlier, so that a re-render between the
# two fails the click rather than moving it onto another posting's control.
EXTERNAL_APPLY_MARK = "data-mcp-external-apply"

MARK_EXTERNAL_APPLY_JS = (
    """(opts) => {
    const {externalLabel, descriptionHeadings} = opts;
    const main = document.querySelector('main');
    if (!main) return false;
"""
    + _EXTERNAL_APPLY_JS
    + f"""    if (externalButton) {{
        externalButton.setAttribute('{EXTERNAL_APPLY_MARK}', '');
    }}
    return Boolean(externalButton);
}}"""
)

# This posting's apply control and state, once they render. Easy Apply is found
# by its URL, an anchor into the posting's own `/apply/` route that the "More
# jobs" cards, each linking its own posting, cannot match. The external button
# and the state lines are text from the locale table, and both are read above
# the description only. Measured on 2026-09-14: Easy Apply is an
# `<a href=".../jobs/view/<id>/apply/?openSDUIApplyFlow=true">`, the external
# control a `<button>` with no href whose text is the label.
APPLY_SIGNALS_JS = (
    r"""(opts) => {
    const {applyPath, externalLabel, descriptionHeadings, closedLines, appliedPattern} = opts;
    const main = document.querySelector('main');
    if (!main) return null;
    const pathOf = (anchor) => {
        try {
            return new URL(anchor.href).pathname.replace(/\/+$/, '');
        } catch (error) {
            return '';
        }
    };
"""
    + _EXTERNAL_APPLY_JS
    + r"""    const lines = (main.innerText || '').split('\n').map((line) => line.trim());
    const end = lines.findIndex((line) => descriptionHeadings.includes(line));
    const top = end === -1 ? [] : lines.slice(0, end);
    const applied = new RegExp(appliedPattern);
    return {
        easy_apply: [...main.querySelectorAll('a[href]')]
            .some((anchor) => pathOf(anchor) === applyPath),
        external: Boolean(externalButton),
        applied: top.some((line) => applied.test(line)),
        closed: top.some((line) => closedLines.includes(line)),
    };
}"""
)

APPLY_READY_JS = (
    "(opts) => {\n    const signals = (" + APPLY_SIGNALS_JS + ")(opts);\n"
    "    return Boolean(signals && Object.values(signals).some(Boolean));\n}"
)

# The link to the employer's site a dialog offers after Apply. Only inside a
# dialog, because the description's own outbound links pass through the same
# interstitial.
DIALOG_REDIRECT_JS = r"""(redirectPath) => {
    for (const dialog of document.querySelectorAll('dialog, [role="dialog"]')) {
        for (const anchor of dialog.querySelectorAll('a[href]')) {
            try {
                const url = new URL(anchor.href);
                if (url.pathname.replace(/\/+$/, '') === redirectPath) return url.href;
            } catch (error) {}
        }
    }
    return null;
}"""

# How long the apply control gets to render, and how long an external Apply gets
# to answer with its dialog or a tab. The employer's page gets what `goto` gets
# everywhere else.
_APPLY_READY_TIMEOUT = 10.0
_APPLY_ANSWER_TIMEOUT = 10.0
#: How long a refused tab gets to arrive as a page of its own. Refusing its
#: document answers before the tab exists, and a tab nobody closes outlives the
#: call.
_TAB_ARRIVES_TIMEOUT = 1.0
_APPLY_POLL = 0.25
_EMPLOYER_LOAD_TIMEOUT = 30.0


# The browser resolves a destination's name itself, so an address
# `reaches_the_public_internet` refuses can still be reached through a public
# name pointing at it. This asks the resolver the same question first.
#
# Not resolving is not evidence, so it loads: a configured proxy resolves for
# the browser, and this process may hold no DNS for that name at all. What
# stays uncovered is a name that answers one thing here and another in the
# browser, which nothing on this side can settle, and the cost of that is a
# request the caller never sees the body of.
async def _resolves_off_this_host(destination: str) -> bool:
    """Whether every address ``destination``'s name answers with is public."""
    host = urlparse(destination).hostname
    if not host:
        return False
    loop = asyncio.get_running_loop()
    try:
        answers = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return True
    # A sockaddr's first element is the address; the annotation widens it.
    return all(reaches_the_public_internet(str(answer[4][0])) for answer in answers)


# A tab's first document is issued before the tab's frame exists, so asking
# that request for its frame is what tells the two apart. The `window.open('',
# '_blank')`-then-assign shape has a frame by the time it navigates, and is
# caught by the frame belonging to another page instead.
def _is_this_pages_own(request: Request, page: Page) -> bool:
    """Whether ``request`` is the driven page's own document, not a tab's."""
    try:
        return request.frame.page is page
    except Exception:
        return False


@dataclass(frozen=True, slots=True)
class JobApplyRead:
    """How one posting takes applications, and where the employer's form is."""

    type: ApplyType
    url: str | None = None


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
    next, and what an answer means, belong to `jobs.JobScraper`.
    """

    def __init__(
        self,
        session: ScrapingSession,
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
        """Read how a posting takes applications, following an external Apply.

        Applied, closed and Easy Apply postings are answered without a click.
        An external posting's Apply is clicked, which LinkedIn counts as an
        apply click on the posting, and answers with a "Share your profile?"
        dialog or a tab. Continue is never clicked: the dialog says it shares
        the full profile with the job poster, and its link already names the
        destination. The employer's address is then loaded in this page to
        follow its redirects, and the page is left there.
        """
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        page = self._session.page
        opts = {
            "applyPath": f"/jobs/view/{job_id}/apply",
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

        signals = await page.evaluate(APPLY_SIGNALS_JS, opts)
        # Both states before any control, so a posting in either is never
        # clicked.
        if signals and signals["applied"]:
            return JobApplyRead("applied")
        if signals and signals["closed"]:
            return JobApplyRead("closed")
        if signals and signals["easy_apply"]:
            return JobApplyRead("easy_apply")
        if not signals or not signals["external"]:
            # A barrier served in place of the posting renders none of the
            # above either, and it needs the relogin path rather than a type.
            await self._navigator._raise_if_auth_barrier(url)
            return JobApplyRead("unknown")

        destination = await self._click_external_apply(text)
        if destination is None:
            return JobApplyRead("external")
        return JobApplyRead("external", await self._follow_to_employer(destination))

    async def _click_external_apply(self, text: JobApplyTextTable) -> str | None:
        """Click this posting's external Apply and read the address it reveals.

        The button clicked is the one the read chose, found again by the same
        boundary and clicked through a mark, so a "More jobs" card carrying the
        same word cannot take the click. Whichever answers is read: a dialog
        carrying the interstitial link, or a tab LinkedIn opens.

        A tab is read from the request that would have loaded it, and that
        request is refused, so the address is learned without being fetched.
        Reading it off the loaded tab instead would mean the browser had
        already asked for it, and an interstitial names its destination in a
        query parameter, which `employer_apply_url` answers with: the tab
        carries nothing that loading it would add. Whatever it names is then
        judged and loaded by `_follow_to_employer` like any other destination.
        """
        page = self._session.page
        opened: list[Page] = []
        offered: list[str] = []

        def record(tab: Page) -> None:
            opened.append(tab)

        async def refuse_a_tabs_document(intercepted: Route) -> None:
            request = intercepted.request
            if request.is_navigation_request() and not _is_this_pages_own(
                request, page
            ):
                offered.append(request.url)
                await intercepted.abort()
                return
            # Deferred rather than continued: another handler may own this one,
            # and continuing would answer past it.
            await intercepted.fallback()

        page.context.on("page", record)
        await page.context.route("**/*", refuse_a_tabs_document)
        try:
            marked = await page.evaluate(
                MARK_EXTERNAL_APPLY_JS,
                {
                    "externalLabel": text.external_apply_label,
                    "descriptionHeadings": list(text.description_headings),
                },
            )
            if not marked:
                return None
            button = page.locator(f"main button[{EXTERNAL_APPLY_MARK}]")
            await button.first.click(timeout=5000)
            deadline = self._session.monotonic() + _APPLY_ANSWER_TIMEOUT
            while self._session.monotonic() < deadline:
                if offered:
                    return employer_apply_url(offered[0])
                # A tab that has opened but not yet named an address is still
                # answered here: it reaches the refusal above when it navigates.
                href = await page.evaluate(DIALOG_REDIRECT_JS, SAFETY_REDIRECT_PATH)
                if isinstance(href, str):
                    return employer_apply_url(href)
                await self._session.delay(_APPLY_POLL)
            return None
        finally:
            await page.context.unroute("**/*", refuse_a_tabs_document)
            if offered and not opened:
                # `record` is still listening, and fills `opened` from here.
                with suppress(Exception):
                    await page.context.wait_for_event(
                        "page", timeout=_TAB_ARRIVES_TIMEOUT * 1000
                    )
            page.context.remove_listener("page", record)
            for tab in dict.fromkeys(opened):
                with suppress(Exception):
                    await tab.close()

    async def _follow_to_employer(self, destination: str) -> str | None:
        """The address the employer's link settles on, loaded in this page.

        Short links are common, Greenhouse's `grnh.se` among them, and name the
        hiring system only once they redirect. A load that fails or stops short
        answers with where it got, or with the link itself when that is not an
        employer's address. A destination that resolves back into this host is
        not loaded at all and answers None, which reads to the caller as a
        posting whose link could not be read.

        What the hops after it answer with is not judged, because nothing on
        this side can judge it. A route sees a navigation's first request and
        not the hop it redirects to, and fulfilling that hop's response does
        not bring the next one back through the route either. The request API,
        which can be asked for one hop at a time, resolves in the driver rather
        than in the browser: it cannot see `--host-resolver-rules`, and under a
        proxy that resolves for the browser it would not resolve the name at
        all. Walking the chain there would judge one resolution and load
        another, which is the rebinding it was meant to answer. The bound is
        that a destination is loaded and never read: the address the tool
        answers with passes through `employer_apply_url`, so a hop into private
        space is never reported, only fetched.
        """
        if not await _resolves_off_this_host(destination):
            logger.debug("Refused an apply destination resolving into this host")
            return None
        page = self._session.page
        try:
            await page.goto(
                destination, wait_until="load", timeout=_EMPLOYER_LOAD_TIMEOUT * 1000
            )
        except Exception as e:
            # The type alone: a driver error can quote a configured proxy URL.
            logger.debug(
                "%s did not finish loading (%s)", destination, type(e).__name__
            )
        return employer_apply_url(page.url) or destination

    async def _extract_job_ids(self, *, scoped: bool = False) -> list[str]:
        """Extract unique job IDs from job card links on the current page.

        Finds all `a[href*="/jobs/view/"]` links and extracts the numeric
        job ID from each href. Returns deduplicated IDs in DOM order.

        Args:
            scoped: Read only the results rail, chosen by the same rule the
                sidebar scroll uses. Off for lists that have no rail.
        """
        result = await self._session.page.evaluate(
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
        result = await self._session.page.evaluate(
            PROMOTED_JOB_IDS_JS, {"selector": _JOB_CARD_SELECTOR, "label": label}
        )
        if not isinstance(result, list):
            raise TypeError(f"Promoted job ids read returned {type(result).__name__}")
        return [job_id for job_id in result if isinstance(job_id, str)]

    async def _extract_search_page(
        self,
        url: str,
        section_name: str,
        scroll_deadline: float = SCROLL_DEADLINE_MAX,
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
        """
        charge = _ScrollCharge()
        try:
            result = await self._extract_search_page_once(
                url, section_name, scroll_deadline, charge=charge
            )
            if result.text != RATE_LIMITED_SECTION_TEXT:
                return self._captured(result, charge.seconds)

            logger.info(
                "Retrying search page %s after %.0fs backoff",
                url,
                RATE_LIMIT_RETRY_DELAY,
            )
            await asyncio.sleep(RATE_LIMIT_RETRY_DELAY)
            result = await self._extract_search_page_once(
                url, section_name, scroll_deadline / 2, charge=charge
            )
            if result.text == RATE_LIMITED_SECTION_TEXT:
                logger.warning("Search page %s still rate-limited after retry", url)
            return self._captured(result, charge.seconds)

        except LinkedInScraperException:
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
    ) -> ExtractedSection:
        """Single attempt to navigate, scroll sidebar, and extract innerText."""
        await self._navigator._navigate_to_page(url)
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
        text = await self._session.page.evaluate(
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

            except LinkedInScraperException:
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
        value = await self._session.page.evaluate(
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
