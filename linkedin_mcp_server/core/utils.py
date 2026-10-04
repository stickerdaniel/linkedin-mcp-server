"""Utility functions for page-reading operations."""

import asyncio
import logging
import time
from typing import Any

from patchright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .exceptions import RateLimitError

logger = logging.getLogger(__name__)

# Both card shapes. The classic result is an anchor to the job permalink;
# the redesigned search at `/jobs/search-results` renders cards that carry no
# permalink at all and keep the id only in `componentkey`. A selector matching
# just the anchor finds nothing there, which is what made the search return an
# empty `job_ids` while its text listed real jobs.
_JOB_CARD_SELECTOR = 'a[href*="/jobs/view/"], [componentkey^="job-card-component-ref-"]'

# The rule that decides which container holds the search results. Shared
# verbatim by the scroll and by id extraction, because extraction re-runs it
# rather than trusting a mark the scroll left behind: an attribute dies with
# the node it sits on, a re-render between the scroll returning and the ids
# being read leaves none, and nothing then distinguishes that from a page
# nobody scrolled. Everything outside the rail is not a search result: the
# detail pane carries its own permalink and, once opened, a similar-jobs
# module, and counting those advances the offset past results the rail never
# showed. Expects `selector` in scope.
_RAIL_PICK_JS = r"""
            const idOf = (node) => {
                const href = (node.getAttribute('href') || '').match(
                    /\/jobs\/view\/(?:[^/?#]*-)?(\d+)(?=[/?#]|$)/
                );
                if (href) return href[1];
                // The redesigned card has no permalink, so the attribute is
                // the only place the id exists. Anchored at the start, since
                // the selector already matched that prefix and a bare
                // `includes` would accept an unrelated key holding it.
                const key = (node.getAttribute('componentkey') || '').match(
                    /^job-card-component-ref-(\d+)/
                );
                return key ? key[1] : null;
            };
            const idsIn = (scope) => {
                const ids = new Set();
                for (const node of scope.querySelectorAll(selector)) {
                    const id = idOf(node);
                    if (id) ids.add(id);
                }
                return ids.size;
            };

            // Every scrollable ancestor of every card, collected fresh on
            // each pick because a re-render replaces the nodes. Scrollable by
            // its own overflow style and not by whether it currently
            // overflows: a result set short enough to fit inside the rail
            // left the rail out of the candidates entirely, and the detail
            // pane, which overflows on one job description, won by default.
            // Measured live on a full page: dropping the size test adds one
            // candidate, the pane's own parent at one job id, and nothing
            // that holds the rail and the pane together.
            const collect = () => {
                const found = [];
                for (const card of document.querySelectorAll(selector)) {
                    let node = card.parentElement;
                    while (node && node !== document.body) {
                        const style = window.getComputedStyle(node);
                        const overflowY = style.overflowY;
                        if ((overflowY === 'auto' || overflowY === 'scroll')
                            && !found.includes(node)) {
                            found.push(node);
                        }
                        node = node.parentElement;
                    }
                }
                return found;
            };

            // Most job ids wins, and a tie is not broken but kept: every
            // candidate holding the winning count is scrolled. Picking one
            // loses either way round, because a tie means one candidate
            // contains the other and only the inner one appends cards.
            // Measured on both shapes: a per-card wrapper inside a rail that
            // has rendered a single card leaves the rail unscrolled at one
            // card, and a scrollable container wrapping the rail leaves it
            // unscrolled at five. Live the two candidates are siblings, rail
            // 7 ids and pane 1, so the tie itself has not been observed;
            // scrolling both costs one extra assignment when it happens.
            const railGroup = () => {
                const nodes = collect();
                let best = 0;
                for (const node of nodes) {
                    best = Math.max(best, idsIn(node));
                }
                return best ? nodes.filter(n => idsIn(n) === best) : [];
            };

            // One node still represents the group for measuring growth: the
            // outermost of the tied, so its id count covers every card the
            // inner ones append.
            const pickRail = () => {
                let picked = null;
                for (const node of railGroup()) {
                    if (!picked || node.contains(picked)) picked = node;
                }
                return picked;
            };
"""

# One look at the rail and, when asked, one scroll of it. Synchronous on
# purpose: every wait and every repeat belongs to `scroll_job_sidebar`, so a
# cancelled call has nothing left running in the page. Cancelling the task that
# awaits `page.evaluate()` does not cancel a promise the page is running, and
# the polling loop that used to live here went on scrolling the shared page
# after a tool timeout had handed it to the next call (#763). No node is kept
# between steps: the pick is re-run on each one, which is also what follows a
# rail a re-render replaced.
_RAIL_STEP_JS = (
    r"""(opts) => {
            const {selector, scroll} = opts;
"""
    + _RAIL_PICK_JS
    + r"""
            if (!document.querySelectorAll(selector).length) {
                return {status: 'gone'};
            }
            const tied = railGroup();
            let rail = null;
            for (const node of tied) {
                if (!rail || node.contains(rail)) rail = node;
            }
            if (!rail) return {status: 'no-container'};

            // Measured before the scroll, so the batch it asks for reads as
            // growth against this step.
            const measured = {
                status: 'ok',
                cards: idsIn(rail),
                height: rail.scrollHeight,
            };
            if (scroll) {
                // Only the tied candidates nested with the pick. Two tied
                // siblings are the live shape, rail and detail pane, and
                // scrolling the pane loads its similar-jobs module into the
                // document, where the caller reads those ids as search
                // results. Measured on a 6-to-6 tie: the pane reached 31 ids
                // and the search returned 37, of which 31 were not results,
                // while the rail stayed at 6 because growth was then read
                // off the pane instead.
                for (const node of tied) {
                    if (node === rail
                        || node.contains(rail) || rail.contains(node)) {
                        node.scrollTop = node.scrollHeight;
                    }
                }
            }
            return measured;
        }"""
)


async def _rail_step(page: Page, *, scroll: bool) -> dict[str, Any]:
    """Measure the rail, scrolling it afterwards when ``scroll`` is set."""
    return await page.evaluate(
        _RAIL_STEP_JS, {"selector": _JOB_CARD_SELECTOR, "scroll": scroll}
    )


async def detect_rate_limit(page: Page) -> None:
    """Detect if LinkedIn has rate-limited or security-challenged the session.

    Checks (in order):
    1. URL contains /checkpoint or /authwall (security challenge)
    2. Body text contains rate-limit phrases on error-shaped pages (throttling)

    The body-text heuristic only runs on pages without a ``<main>`` element
    and with short body text (<2000 chars), since real rate-limit pages are
    minimal error pages.  This avoids false positives from profile content
    that happens to contain phrases like "slow down" or "try again later".

    Raises:
        RateLimitError: If any rate-limiting or security challenge is detected
    """
    # Check URL for security challenges
    current_url = page.url
    if "linkedin.com/checkpoint" in current_url or "authwall" in current_url:
        raise RateLimitError(
            "LinkedIn security checkpoint detected. "
            "You may need to verify your identity or wait before continuing.",
            suggested_wait_time=30,
        )

    # Check for rate limit messages — only on error-shaped pages.
    # Real rate-limit pages have no <main> element and short body text.
    # Normal LinkedIn pages (profiles, jobs) have <main> and long content
    # that may incidentally contain phrases like "slow down".
    try:
        has_main = await page.locator("main").count() > 0
        if has_main:
            return  # Normal page with content, skip body text heuristic

        body_text = await page.locator("body").inner_text(timeout=1000)
        if body_text and len(body_text) < 2000:
            body_lower = body_text.lower()
            if any(
                phrase in body_lower
                for phrase in [
                    "too many requests",
                    "rate limit",
                    "slow down",
                    "try again later",
                ]
            ):
                raise RateLimitError(
                    "Rate limit message detected on page.",
                    suggested_wait_time=30,
                )
    except RateLimitError:
        raise
    except PlaywrightTimeoutError:
        pass


async def scroll_to_bottom(
    page: Page, pause_time: float = 1.0, max_scrolls: int = 10
) -> None:
    """Scroll to the bottom of the page to trigger lazy loading.

    Args:
        page: Patchright page object
        pause_time: Time to pause between scrolls (seconds)
        max_scrolls: Maximum number of scroll attempts
    """
    for i in range(max_scrolls):
        previous_height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(pause_time)

        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == previous_height:
            logger.debug("Reached bottom after %d scrolls", i + 1)
            break


async def scroll_job_sidebar(
    page: Page,
    settle_timeout: float = 3.0,
    poll_interval: float = 0.15,
    min_budget: float = 0.4,
    max_scrolls: int = 10,
    deadline: float = 12.0,
) -> bool:
    """Scroll the job search sidebar until it stops producing cards.

    LinkedIn renders job search results in a scrollable sidebar container,
    not the main page body. Finding it means looking at every scrollable
    ancestor of every card: the job detail pane scrolls on its own, and a
    card may sit in a scrollable wrapper of its own. The rail is the
    candidate holding the most distinct job ids, so the pane's "similar
    jobs" module is not pulled into the page. Measured on a live search:
    the rail held 7 ids before scrolling and 11 after, the pane held 1
    throughout. Candidates tied with the pick are scrolled alongside it when
    one contains the other, because only the inner one appends cards. Tied
    siblings are not: those two are the rail and the pane, and scrolling the
    pane is what loads the similar-jobs module.

    There is no target count. How many cards a page yields belongs to
    LinkedIn, and assuming a number is what this function used to get wrong
    in the other direction. It stops when the rail stops growing, and
    ``search_jobs`` pages by what actually loaded.

    Each scroll waits for the next batch by polling instead of sleeping a
    fixed amount, because how long a batch takes belongs to the user's
    connection. A round that sees nothing waits once more at the full
    ``settle_timeout`` before the rail counts as exhausted: deciding that
    after a single fixed 0.5s look is what cost a measured search 4 of its
    11 cards. It is the wait that buys those cards and not the second
    scroll, which lands on a rail already at ``scrollHeight`` and fires no
    event; it is kept for the case where the rail moved during the first
    wait. A round therefore waits at most ``2 * settle_timeout``. Later
    rounds start from three times what the previous batch took, floored at
    ``min_budget``, which shortens the terminating round on a fast link.

    Returns whether a step on the page raised, which the caller needs and
    cannot see for itself. A navigation destroys the execution context and
    the step raises, and ``page.url`` still reports the address it left for
    about 6ms after that, measured over ten runs at 6ms min and max alike, so
    a caller sampling the URL right here compares two copies of the old one.
    Awaiting the load state does not close that window: the previous document
    is loaded already, so it returns at once.

    DOM dependency: scrolling requires an element reference, which innerText
    extraction cannot provide.

    Args:
        page: Patchright page object
        settle_timeout: Longest wait for one batch; a round may use it twice
        poll_interval: How often to look for the batch (seconds)
        min_budget: Smallest wait a fast connection may shrink to (seconds)
        max_scrolls: Backstop on scroll attempts; ``deadline`` is the real bound
        deadline: Wall-clock bound for the whole call, the wait for the
            first card included (seconds). That wait is separately capped at
            5s, so a longer deadline buys scrolling and not patience: a search
            page still holding no card after 5s is throttled or empty, and
            waiting the full deadline for it would cost that again on every
            one of ``max_pages`` navigations.
    """
    started = time.monotonic()
    if deadline <= 0:
        logger.debug("No scroll budget left for %s, skipping sidebar scroll", page.url)
        return False

    try:
        # Never zero: Patchright reads a zero timeout as no timeout at all
        # ("Pass `0` to disable timeout", `wait_for_selector` in the installed
        # 1.63.0 API), so a spent budget would wait on a page with no job card
        # until the tool is cancelled and every page gathered so far is thrown
        # away with it. A sub-millisecond deadline truncates to zero the same
        # way, which the guard above does not catch.
        await page.wait_for_selector(
            _JOB_CARD_SELECTOR, timeout=max(1, min(5000, int(deadline * 1000)))
        )
    except PlaywrightTimeoutError:
        logger.debug("No job card links found, skipping sidebar scroll")
        return False
    except Exception as exc:
        logger.warning("Job sidebar scroll failed, page may be short: %s", exc)
        return True

    # The wait above is part of the deadline, not extra time on top of it. A
    # slow link can spend it down to nothing before the first card appears, and
    # the caller sized this deadline to fit a whole search inside one tool call.
    hard_deadline = started + deadline
    if time.monotonic() >= hard_deadline:
        logger.debug("Deadline spent waiting for the first job card, skipping scroll")
        return False

    # Every wait is a sleep here and every look at the page is one synchronous
    # step, so a cancel lands between steps and the next one is never sent.
    # Moving a wait back into the page puts a loop there that outlives the
    # cancel; see `_RAIL_STEP_JS`.
    try:
        latest = await _rail_step(page, scroll=False)
        status = latest.get("status")
        if status == "gone":
            logger.debug("Job card link disappeared before evaluate, skipping scroll")
            return False
        if status == "no-container":
            logger.debug("No scrollable container found for job sidebar")
            return False

        async def grew_since(before: dict[str, Any], budget: float) -> bool:
            nonlocal latest
            until = min(time.monotonic() + budget, hard_deadline)
            while time.monotonic() < until:
                await asyncio.sleep(poll_interval)
                now = await _rail_step(page, scroll=False)
                if now.get("status") != "ok":
                    # A re-render can leave no rail for a moment. That is not
                    # growth, and the next step picks whatever replaced it.
                    continue
                latest = now
                # Growth is a larger id count or a taller rail. The pick is
                # re-run on every step, so a rail a re-render replaced is
                # measured in its successor, and adopting that successor is
                # not growth by itself: a framework that re-renders the same
                # cards would otherwise spend one of `max_scrolls` per render
                # and end the page while the batch it waited for is in flight.
                # A virtualized rail that swapped its ids while holding both
                # steady would read as exhausted here; LinkedIn has not been
                # observed doing that, and no sample pins it either way.
                if now["cards"] > before["cards"] or now["height"] > before["height"]:
                    return True
            return False

        started_with = latest["cards"]
        budget = settle_timeout
        scrolls = 0
        timed_out = False
        capped_out = False

        while True:
            if scrolls >= max_scrolls:
                capped_out = True
                break
            if time.monotonic() >= hard_deadline:
                timed_out = True
                break

            round_started = time.monotonic()
            before = await _rail_step(page, scroll=True)
            if before.get("status") == "ok":
                latest = before
            else:
                before = latest
            grew = await grew_since(before, budget)
            if not grew:
                # One confirmation round at the full budget: a batch slower
                # than the shrunken budget is not an empty rail.
                await _rail_step(page, scroll=True)
                grew = await grew_since(before, settle_timeout)
            if not grew:
                timed_out = time.monotonic() >= hard_deadline
                break

            took = time.monotonic() - round_started
            budget = min(settle_timeout, max(min_budget, took * 3))
            scrolls += 1
    except Exception as exc:
        # Scrolling is best effort: a navigation or a destroyed context during
        # a step must not discard the page the caller is about to read.
        logger.warning("Job sidebar scroll failed, page may be short: %s", exc)
        return True

    logger.debug(
        "Job sidebar holds %d cards, %+d from %d scrolls%s",
        latest["cards"],
        latest["cards"] - started_with,
        scrolls,
        " (deadline)" if timed_out else " (scroll cap reached)" if capped_out else "",
    )
    return False


async def handle_modal_close(page: Page) -> bool:
    """Close any popup modals that might be blocking content.

    Returns:
        True if a modal was closed, False otherwise
    """
    try:
        close_button = page.locator(
            'button[aria-label="Dismiss"], '
            'button[aria-label="Close"], '
            "button.artdeco-modal__dismiss"
        ).first

        if await close_button.is_visible(timeout=1000):
            await close_button.click()
            await asyncio.sleep(0.5)
            logger.debug("Closed modal")
            return True
    except PlaywrightTimeoutError:
        pass
    except Exception as e:
        logger.debug("Error closing modal: %s", e)

    return False
