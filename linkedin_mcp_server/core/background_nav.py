"""Periodic background navigation to simulate real browser usage patterns.

Navigates to random non-LinkedIn sites at 30-60 minute intervals to build
browsing history and cache, making the browser fingerprint more realistic.
"""

import asyncio
import logging
import random

from patchright.async_api import Page

from .browser_lock import browser_lock

logger = logging.getLogger(__name__)

# Strong reference to prevent GC (Council: Gemini)
_bg_task: asyncio.Task[None] | None = None

# Random search queries for Google
_SEARCH_QUERIES = [
    "python programming tutorial",
    "best restaurants near me",
    "weather forecast today",
    "latest technology news",
    "how to learn data science",
    "remote work tips",
    "healthy recipes",
    "travel destinations 2026",
    "machine learning basics",
    "productivity tools",
    "software engineering career",
    "home office setup ideas",
]

# Site generators — NO LinkedIn (avoid session blocks)
_SITE_URLS = [
    "https://en.wikipedia.org/wiki/Special:Random",
    "https://news.ycombinator.com",
    "https://github.com/trending",
    "https://stackoverflow.com/questions?tab=Active",
    "https://medium.com/tag/technology",
]


def _random_google_url() -> str:
    query = random.choice(_SEARCH_QUERIES)
    return f"https://www.google.com/search?q={query.replace(' ', '+')}"


def _get_random_sites(count: int = 2) -> list[str]:
    """Pick count random sites from the pool."""
    sites = [_random_google_url()] + random.sample(
        _SITE_URLS, min(count - 1, len(_SITE_URLS))
    )
    random.shuffle(sites)
    return sites[:count]


async def _visit_site(page: Page, url: str) -> None:
    """Visit a site with human-like interactions. Max 15s time budget per site."""
    from .stealth import random_mouse_move, hover_random_links

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=10000)

        # Simulate reading
        await asyncio.sleep(random.uniform(2.0, 5.0))

        # Random mouse movements
        await random_mouse_move(page, count=random.randint(2, 4))

        # Scroll a few times
        for _ in range(random.randint(2, 5)):
            await page.mouse.wheel(0, random.randint(200, 500))
            await asyncio.sleep(random.uniform(0.5, 1.5))

        # Hover over some links
        await hover_random_links(page, max_links=random.randint(1, 3))

        # More reading time
        await asyncio.sleep(random.uniform(1.0, 3.0))

        logger.debug("Background nav visited %s", url)
    except Exception:
        logger.debug("Background nav failed to visit %s", url, exc_info=True)


async def _background_navigation_loop(page: Page) -> None:
    """Main background navigation loop. Runs until cancelled."""
    while True:
        # Wait 30-60 minutes between cycles
        interval = random.uniform(30 * 60, 60 * 60)
        logger.debug("Background nav sleeping for %.0f minutes", interval / 60)
        await asyncio.sleep(interval)

        # Check if a tool call is pending — abort if so
        if browser_lock.locked():
            logger.debug(
                "Background nav skipping cycle — browser lock held by tool call"
            )
            continue

        try:
            async with browser_lock:
                sites = _get_random_sites(count=random.randint(1, 3))
                for url in sites:
                    # Abort mid-cycle if someone else wants the lock
                    # (can't check perfectly, but we keep visits short)
                    await _visit_site(page, url)

                # Return to blank page after background navigation
                try:
                    await page.goto("about:blank", timeout=5000)
                except Exception:
                    pass

                logger.debug("Background nav cycle complete (%d sites)", len(sites))
        except Exception:
            logger.debug("Background nav cycle error", exc_info=True)


async def start_background_navigation(page: Page) -> None:
    """Start the background navigation task. Idempotent — stops existing task first."""
    global _bg_task

    # Stop existing task if running (idempotent restart)
    await stop_background_navigation()

    _bg_task = asyncio.create_task(_background_navigation_loop(page))
    logger.info("Background navigation task started")


async def stop_background_navigation() -> None:
    """Stop the background navigation task. cancel() + await (Council: Gemini)."""
    global _bg_task

    task = _bg_task
    _bg_task = None

    if task is None or task.done():
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    logger.info("Background navigation task stopped")
