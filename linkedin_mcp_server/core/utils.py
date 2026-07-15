"""Utility functions for scraping operations."""

import asyncio
import logging

from patchright.async_api import Page

from .engines import ENGINES
from .exceptions import RateLimitError

logger = logging.getLogger(__name__)

# A timeout raised by a Camoufox-driven page (vanilla Playwright) is a
# different class from one raised by a Patchright-driven page, even though
# both engines are launched by this server. Catch both everywhere a scraping
# timeout is expected to degrade gracefully instead of propagating uncaught.
# Union of every registered engine's timeout class(es) -- auto-extends when
# a new engine adapter is registered in core.engines, instead of needing a
# manual edit here too.
TIMEOUT_ERRORS = tuple(
    {cls for adapter in ENGINES.values() for cls in adapter.timeout_error_classes}
)


async def detect_rate_limit(page: Page) -> None:
    """Detect if LinkedIn has rate-limited or security-challenged the session.

    Checks (in order):
    1. URL-based auth barrier (security checkpoint / login wall)
    2. Body text contains rate-limit phrases on error-shaped pages (throttling)

    The body-text heuristic only runs on pages without a ``<main>`` element
    and with short body text (<2000 chars), since real rate-limit pages are
    minimal error pages.  This avoids false positives from profile content
    that happens to contain phrases like "slow down" or "try again later".

    Raises:
        ChallengeError: A recoverable interactive barrier was detected
            (security checkpoint, saved-account chooser, ...).
        BlockError: A hard login wall was detected (not authenticated).
        RateLimitError: Genuine server-side throttling was detected.
    """
    # Check 1: URL-based auth barrier. Delegates to core.auth's kind-aware
    # detector -- checkpoint/authwall URLs are exactly what that module's
    # _URL_PATTERN_KIND already classifies, so this used to duplicate that
    # logic here with a flat RateLimitError that discarded the recoverable
    # CHALLENGE vs. hard BLOCK distinction. Deferred import: core.auth
    # imports TIMEOUT_ERRORS from this module at its own module level, so a
    # top-level import here would be circular.
    from .auth import AuthBarrierKind, detect_auth_barrier_quick
    from .exceptions import BlockError, ChallengeError

    barrier = await detect_auth_barrier_quick(page)
    if barrier is not None:
        if barrier.kind == AuthBarrierKind.BLOCK:
            raise BlockError(f"LinkedIn auth block detected: {barrier}")
        raise ChallengeError(f"LinkedIn challenge detected: {barrier}")

    # Check 2: rate limit messages — only on error-shaped pages.
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
    except TIMEOUT_ERRORS:
        pass


_SCROLL_STEP_FRACTIONS = (0.25, 0.5, 0.75, 1.0)
_SCROLL_BACKOFF_MULTIPLIER = 1.5
_MAX_SCROLL_DELAY = 2.0


async def scroll_to_bottom(
    page: Page,
    pause_time: float = 1.0,
    max_scrolls: int = 10,
    *,
    max_wait_seconds: float = 15.0,
) -> None:
    """Scroll to the bottom of the page to trigger lazy loading.

    Smarter than a single jump-and-fixed-wait cycle in four ways:

    1. Skip-if-already-there: if the page is already scrolled to (near) the
       bottom before this even starts, there's nothing to trigger -- return
       immediately instead of paying at least one pause_time for nothing.
    2. Each pass scrolls through 25/50/75/100% of the current scrollHeight
       instead of one leap to the bottom -- some LinkedIn lazy-load
       triggers are viewport-intersection-based (fire as an element
       scrolls into view), not only "did you reach the very bottom".
    3. scrollHeight is re-measured after *every* step within a pass, not
       just once per full pass, so newly-inserted content is detected as
       soon as it appears rather than only at the next pass boundary.
    4. The wait between passes backs off (starting at pause_time, capped
       at 2.0s) instead of staying fixed, and is bounded by
       max_wait_seconds wall-clock in addition to max_scrolls -- a
       slow-loading page gets more patience without a fast-loading one
       paying a fixed tax on every single pass.

    Args:
        page: Patchright page object
        pause_time: Starting delay between passes (seconds); grows via
            backoff up to max_wait_seconds/_MAX_SCROLL_DELAY.
        max_scrolls: Maximum number of passes.
        max_wait_seconds: Wall-clock ceiling across all passes, independent
            of max_scrolls -- whichever bound is hit first stops the loop.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_seconds

    previous_height = await page.evaluate("document.body.scrollHeight")
    already_at_bottom = await page.evaluate(
        "(window.scrollY + window.innerHeight) >= (document.body.scrollHeight - 10)"
    )
    if already_at_bottom:
        logger.debug("scroll_to_bottom: already at the bottom, nothing to do")
        return

    delay = pause_time
    for i in range(max_scrolls):
        if loop.time() >= deadline:
            logger.debug(
                "scroll_to_bottom: max_wait_seconds reached after %d passes", i
            )
            break

        for fraction in _SCROLL_STEP_FRACTIONS:
            await page.evaluate(
                f"window.scrollTo(0, document.body.scrollHeight * {fraction})"
            )
            step_height = await page.evaluate("document.body.scrollHeight")
            if step_height != previous_height:
                previous_height = step_height

        await asyncio.sleep(delay)

        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == previous_height:
            logger.debug("Reached bottom after %d passes", i + 1)
            break
        previous_height = new_height
        delay = min(delay * _SCROLL_BACKOFF_MULTIPLIER, _MAX_SCROLL_DELAY)


async def scroll_job_sidebar(
    page: Page, pause_time: float = 1.0, max_scrolls: int = 10
) -> None:
    """Scroll the job search sidebar to load all job cards.

    LinkedIn renders job search results in a scrollable sidebar container,
    not the main page body. This function finds that container by locating
    a job card link and walking up to its scrollable ancestor, then scrolls
    it iteratively until no new content loads.

    Args:
        page: Patchright page object
        pause_time: Time to pause between scrolls (seconds)
        max_scrolls: Maximum number of scroll attempts
    """
    # Wait for at least one job card link to render before scrolling
    try:
        await page.wait_for_selector('a[href*="/jobs/view/"]', timeout=5000)
    except TIMEOUT_ERRORS:
        logger.debug("No job card links found, skipping sidebar scroll")
        return

    scrolled = await page.evaluate(
        """async ({pauseTime, maxScrolls}) => {
            const link = document.querySelector('a[href*="/jobs/view/"]');
            if (!link) return -2;

            let container = link.parentElement;
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
                return -1;
            }

            let scrollCount = 0;
            for (let i = 0; i < maxScrolls; i++) {
                const prevHeight = container.scrollHeight;
                container.scrollTop = container.scrollHeight;
                await new Promise(r => setTimeout(r, pauseTime * 1000));
                if (container.scrollHeight === prevHeight) break;
                scrollCount++;
            }
            return scrollCount;
        }""",
        {"pauseTime": pause_time, "maxScrolls": max_scrolls},
    )
    if scrolled == -2:
        logger.debug("Job card link disappeared before evaluate, skipping scroll")
    elif scrolled == -1:
        logger.debug("No scrollable container found for job sidebar")
    elif scrolled:
        logger.debug("Scrolled job sidebar %d times", scrolled)
    else:
        logger.debug("Job sidebar container found but no new content loaded")


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
    except TIMEOUT_ERRORS:
        pass
    except Exception as e:
        logger.debug("Error closing modal: %s", e)

    return False
