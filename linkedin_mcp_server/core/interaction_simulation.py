"""Page-level interaction simulation between discrete scraping actions.

Extends -- does not replace -- ``core/humanize.py``'s per-action
humanization (``human_move_and_click``/``human_type``/``human_scroll``,
which stay focused on individual click/type/scroll calls). This module
adds idle-time *page* behavior a bot wouldn't otherwise produce between
actions: mouse jitter and paced scroll-and-pause patterns, tiered by
``StealthProfile.simulation``.

Deliberately does NOT port the more advanced fork's section-specific CSS
selectors (``.pv-text-details__left-panel`` and similar) for a "hover over
a content section" effect -- those are exactly the kind of brittle,
LinkedIn-layout-coupled selectors this codebase's extraction layer avoids
everywhere else (innerText/generic ``<main>``-based, not specific class
names that go stale on any LinkedIn redesign). The underlying idea (pause
attention on real content, not just scroll past it) is approximated
instead via generic fractional-scroll-position pauses, which need no
selector at all.
"""

from __future__ import annotations

import asyncio
import random

from patchright.async_api import Page

from .stealth_profile import SimulationLevel, StealthProfile

# Idle mouse movement stays well clear of viewport edges -- a jitter point
# right at x=0/y=0 is itself an unnatural signal (real cursor movement
# rarely parks exactly on a boundary).
_MOUSE_JITTER_MARGIN = 100
_DEFAULT_VIEWPORT = {"width": 1280, "height": 720}


async def simulate_mouse_jitter(page: Page, *, points: int | None = None) -> None:
    """A few small random mouse movements within the viewport, each
    followed by a short pause -- idle-time realism between discrete
    actions, independent of any click/type/scroll actually happening.
    """
    viewport = page.viewport_size or _DEFAULT_VIEWPORT
    margin = _MOUSE_JITTER_MARGIN
    width, height = viewport["width"], viewport["height"]
    n = points if points is not None else random.randint(2, 4)
    for _ in range(n):
        x = random.uniform(margin, max(margin + 1, width - margin))
        y = random.uniform(margin, max(margin + 1, height - margin))
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.1, 0.3))


async def simulate_paced_scroll(
    page: Page,
    *,
    positions: tuple[float, ...],
    delay_range: tuple[float, float],
) -> None:
    """Scroll through fractional page-height *positions* (0.0-1.0 of
    scrollHeight), pausing a random ``delay_range`` interval at each --
    more natural "reading" behavior than one instant jump to a target.
    """
    for fraction in positions:
        await page.evaluate(
            "window.scrollTo({top: document.body.scrollHeight * "
            f"{fraction}, behavior: 'smooth'}})"
        )
        await asyncio.sleep(random.uniform(*delay_range))


async def simulate_page_interaction(page: Page, profile: StealthProfile) -> None:
    """Run the interaction pattern matching ``profile.simulation``.

    NONE is a no-op. BASIC/MODERATE/COMPREHENSIVE progressively add more
    scroll passes and a chance of mouse jitter between them -- called once
    per page load (matching this codebase's per-section-is-a-navigation
    granularity), after real content has finished loading, as idle
    "looking at the page" behavior before extraction reads it.
    """
    level = profile.simulation
    if level == SimulationLevel.NONE:
        return

    scroll_delay = profile.delays.scroll

    if level == SimulationLevel.BASIC:
        await simulate_paced_scroll(
            page, positions=(0.3, 0.6, 1.0), delay_range=scroll_delay
        )
        await page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
        return

    if level == SimulationLevel.MODERATE:
        for fraction in (0.25, 0.5, 0.75, 1.0, 0.5):
            await page.evaluate(
                "window.scrollTo({top: document.body.scrollHeight * "
                f"{fraction}, behavior: 'smooth'}})"
            )
            await asyncio.sleep(random.uniform(*scroll_delay))
            if random.random() < 0.3:
                await simulate_mouse_jitter(page)
        return

    # COMPREHENSIVE: a full pass down, a jitter + reading pause, a partial
    # pass back up -- the fork's own tuning target for this tier.
    await simulate_paced_scroll(
        page,
        positions=(0.15, 0.3, 0.45, 0.6, 0.75, 0.9, 1.0),
        delay_range=scroll_delay,
    )
    await simulate_mouse_jitter(page)
    await asyncio.sleep(random.uniform(*profile.delays.reading))
    await simulate_paced_scroll(
        page,
        positions=(0.66, 0.33, 0.0),
        delay_range=(scroll_delay[0] * 0.5, scroll_delay[1] * 0.5),
    )
