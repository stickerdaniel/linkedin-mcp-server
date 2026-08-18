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
    max_scrolls: int = 10,
) -> None:
    """Scroll the job search sidebar until the page's worth of cards is loaded.

    LinkedIn renders job search results in a scrollable sidebar container,
    not the main page body. This function finds that container through a job
    card link and scrolls it until ``target_count`` cards are present or the
    list stops growing.

    Each scroll waits for the next batch by polling instead of sleeping a
    fixed amount, because how long a batch takes is a property of the user's
    connection. ``settle_timeout`` bounds the wait for the first batch; every
    later round waits three times what the previous batch actually took, so a
    fast connection stops early and a slow one is given the full budget.

    DOM dependency: scrolling requires an element reference, which innerText
    extraction cannot provide.

    Args:
        page: Patchright page object
        target_count: Cards to stop at, i.e. the pagination page size
        settle_timeout: Upper bound per round for a batch to appear (seconds)
        poll_interval: How often to look for the batch (seconds)
        max_scrolls: Maximum number of scroll attempts
    """
    try:
        await page.wait_for_selector(_JOB_CARD_SELECTOR, timeout=5000)
    except PlaywrightTimeoutError:
        logger.debug("No job card links found, skipping sidebar scroll")
        return

    result = await page.evaluate(
        """async ({selector, targetCount, settleMs, pollMs, maxScrolls}) => {
            const countCards = () => document.querySelectorAll(selector).length;
            const first = document.querySelector(selector);
            if (!first) return {status: 'gone'};

            let container = first.parentElement;
            while (container && container !== document.body) {
                const style = window.getComputedStyle(container);
                const overflowY = style.overflowY;
                if ((overflowY === 'auto' || overflowY === 'scroll')
                    && container.scrollHeight > container.clientHeight) {
                    break;
                }
                container = container.parentElement;
            }

            if (!container || container === document.body) {
                return {status: 'no-container', cards: countCards()};
            }

            const minWaitMs = 400;
            let budgetMs = settleMs;
            let scrolls = 0;

            while (countCards() < targetCount && scrolls < maxScrolls) {
                const beforeCards = countCards();
                const beforeHeight = container.scrollHeight;
                container.scrollTop = container.scrollHeight;

                const started = Date.now();
                const deadline = started + budgetMs;
                let grew = false;
                while (Date.now() < deadline) {
                    await new Promise(r => setTimeout(r, pollMs));
                    if (countCards() > beforeCards
                        || container.scrollHeight > beforeHeight) {
                        grew = true;
                        break;
                    }
                }
                if (!grew) break;

                const took = Date.now() - started;
                budgetMs = Math.min(settleMs, Math.max(minWaitMs, took * 3));
                scrolls++;
            }

            return {status: 'ok', scrolls, cards: countCards()};
        }""",
        {
            "selector": _JOB_CARD_SELECTOR,
            "targetCount": target_count,
            "settleMs": settle_timeout * 1000,
            "pollMs": poll_interval * 1000,
            "maxScrolls": max_scrolls,
        },
    )

    status = result.get("status")
    if status == "gone":
        logger.debug("Job card link disappeared before evaluate, skipping scroll")
    elif status == "no-container":
        logger.debug("No scrollable container found for job sidebar")
    elif result["cards"] < target_count:
        logger.debug(
            "Job sidebar stopped at %d of %d cards after %d scrolls",
            result["cards"],
            target_count,
            result["scrolls"],
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
