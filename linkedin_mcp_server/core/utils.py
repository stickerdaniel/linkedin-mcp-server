"""Utility functions for scraping operations."""

import asyncio
import logging

from patchright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .exceptions import RateLimitError

logger = logging.getLogger(__name__)

_JOB_CARD_SELECTOR = 'a[href*="/jobs/view/"]'


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
    target_count: int,
    settle_timeout: float = 3.0,
    poll_interval: float = 0.15,
    min_budget: float = 0.4,
    max_scrolls: int = 10,
    deadline: float = 12.0,
) -> None:
    """Scroll the job search sidebar until the page's worth of cards is loaded.

    LinkedIn renders job search results in a scrollable sidebar container,
    not the main page body. Which container that is cannot be decided from a
    snapshot: the job detail pane scrolls on its own and carries both its own
    permalink and a "similar jobs" list, so it can hold more matches than the
    rail has rendered yet. Every scrollable ancestor of a card is scrolled,
    and the target counts the richest one, which is the one that grows.

    Cards are counted as distinct job ids rather than selector matches, since
    one card can carry several links to the same job.

    Each scroll waits for the next batch by polling instead of sleeping a
    fixed amount, because how long a batch takes belongs to the user's
    connection. A round that sees nothing scrolls once more at the full
    ``settle_timeout`` before the list counts as exhausted, so a single slow
    batch cannot end the page: that is what the fixed 0.5s sleep did, and one
    measured search lost 14 of 25 cards to it. The longest a round can wait is
    therefore ``2 * settle_timeout``. Later rounds start from three times what
    the previous batch took, floored at ``min_budget``, which shortens the
    terminating round on a fast connection.

    ``deadline`` bounds the whole call; ``search_jobs`` divides a search-wide
    budget over ``max_pages`` to keep a long search inside the tool timeout.

    DOM dependency: scrolling requires an element reference, which innerText
    extraction cannot provide.

    Args:
        page: Patchright page object
        target_count: Cards to stop at, i.e. the pagination page size
        settle_timeout: Longest wait for one batch; a round may use it twice
        poll_interval: How often to look for the batch (seconds)
        min_budget: Smallest wait a fast connection may shrink to (seconds)
        max_scrolls: Maximum number of scroll attempts
        deadline: Wall-clock bound for the whole call (seconds)
    """
    try:
        await page.wait_for_selector(_JOB_CARD_SELECTOR, timeout=5000)
    except PlaywrightTimeoutError:
        logger.debug("No job card links found, skipping sidebar scroll")
        return

    try:
        result = await page.evaluate(
            """async (opts) => {
            const {selector, targetCount, settleMs, pollMs,
                   minBudgetMs, maxScrolls, deadlineMs} = opts;

            const scrollableAncestor = (card) => {
                let node = card ? card.parentElement : null;
                while (node && node !== document.body) {
                    const style = window.getComputedStyle(node);
                    const overflowY = style.overflowY;
                    if ((overflowY === 'auto' || overflowY === 'scroll')
                        && node.scrollHeight > node.clientHeight) {
                        return node;
                    }
                    node = node.parentElement;
                }
                return null;
            };

            const idsIn = (scope) => {
                const ids = new Set();
                for (const node of scope.querySelectorAll(selector)) {
                    const source = node.getAttribute('href')
                        || node.getAttribute('componentkey') || '';
                    const match = source.match(
                        /(?:\/jobs\/view\/|job-card-component-ref-)(\d+)/
                    );
                    if (match) ids.add(match[1]);
                }
                return ids.size;
            };

            const cards = document.querySelectorAll(selector);
            if (!cards.length) return {status: 'gone'};

            let containers = [];
            for (const card of cards) {
                const candidate = scrollableAncestor(card);
                if (candidate && !containers.includes(candidate)) {
                    containers.push(candidate);
                }
            }
            if (!containers.length) {
                return {status: 'no-container', cards: idsIn(document)};
            }

            const live = () =>
                containers.filter((node) => document.contains(node));
            const countCards = () =>
                live().reduce((most, node) => Math.max(most, idsIn(node)), 0);
            const totalHeight = () =>
                live().reduce((sum, node) => sum + node.scrollHeight, 0);
            const scrollAll = () => {
                for (const node of live()) {
                    node.scrollTop = node.scrollHeight;
                }
            };

            const hardDeadline = Date.now() + deadlineMs;
            const grewSince = async (cardCount, height, budgetMs) => {
                const until = Math.min(Date.now() + budgetMs, hardDeadline);
                while (Date.now() < until) {
                    await new Promise(r => setTimeout(r, pollMs));
                    if (countCards() > cardCount || totalHeight() > height) {
                        return true;
                    }
                }
                return false;
            };

            let budgetMs = settleMs;
            let scrolls = 0;
            let timedOut = false;

            while (countCards() < targetCount && scrolls < maxScrolls) {
                if (Date.now() >= hardDeadline) {
                    timedOut = true;
                    break;
                }
                if (!live().length) {
                    // A re-render detaches the rail; polling the old nodes
                    // would measure a corpse until the deadline.
                    const again = scrollableAncestor(
                        document.querySelector(selector)
                    );
                    if (!again) break;
                    containers = [again];
                }

                const beforeCards = countCards();
                const beforeHeight = totalHeight();
                const started = Date.now();

                scrollAll();
                let grew = await grewSince(
                    beforeCards, beforeHeight, budgetMs
                );
                if (!grew) {
                    // One confirmation round at the full budget: a batch
                    // slower than the shrunken budget is not an empty list.
                    scrollAll();
                    grew = await grewSince(
                        beforeCards, beforeHeight, settleMs
                    );
                }
                if (!grew) {
                    timedOut = Date.now() >= hardDeadline;
                    break;
                }

                const took = Date.now() - started;
                budgetMs = Math.min(
                    settleMs, Math.max(minBudgetMs, took * 3)
                );
                scrolls++;
            }

            return {
                status: 'ok',
                scrolls,
                cards: countCards(),
                timedOut,
            };
        }""",
            {
                "selector": _JOB_CARD_SELECTOR,
                "targetCount": target_count,
                "settleMs": settle_timeout * 1000,
                "pollMs": poll_interval * 1000,
                "minBudgetMs": min_budget * 1000,
                "maxScrolls": max_scrolls,
                "deadlineMs": deadline * 1000,
            },
        )
    except Exception as exc:
        # Scrolling is best effort: a navigation or a destroyed context during
        # the evaluate must not discard the page the caller is about to read.
        logger.debug("Job sidebar scroll failed: %s", exc)
        return

    status = result.get("status")
    if status == "gone":
        logger.debug("Job card link disappeared before evaluate, skipping scroll")
    elif status == "no-container":
        logger.debug("No scrollable container found for job sidebar")
    elif result["timedOut"]:
        logger.debug(
            "Job sidebar hit the %.0fs deadline at %d of %d cards",
            deadline,
            result["cards"],
            target_count,
        )
    elif result["cards"] < target_count:
        logger.debug(
            "Job sidebar stopped at %d of %d cards after %d scrolls%s",
            result["cards"],
            target_count,
            result["scrolls"],
            " (scroll cap reached)" if result["scrolls"] >= max_scrolls else "",
        )
    else:
        logger.debug(
            "Job sidebar loaded %d cards after %d scrolls",
            result["cards"],
            result["scrolls"],
        )


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
