"""Human-like input timing and mouse movement for write-path browser actions.

Typing (``human_type``) applies to both engines equally -- Camoufox's own
``humanize=True`` (see ``core.engines.CamoufoxAdapter``) only covers cursor
movement, not keystroke timing. Mouse/scroll humanization
(``human_move_and_click``, ``human_scroll``) is a no-op passthrough on
Camoufox for exactly that reason: duplicating cursor humanization there
would be redundant work, not extra safety.
"""

from __future__ import annotations

import asyncio
import math
import random
from typing import Any

_PUNCTUATION_PAUSE_CHARS = frozenset(".,!?;:")


def _char_delay_seconds() -> float:
    """Sample a single keystroke interval.

    Log-normal (real inter-keystroke intervals are right-skewed: most key
    pairs land close together, with an occasional longer gap) with a
    median around 90ms, clamped to a sane range so a long message can't
    pathologically stall on an outlier sample.
    """
    return min(max(random.lognormvariate(math.log(0.09), 0.35), 0.02), 0.4)


async def human_type(
    page: Any, text: str, *, thinking_pause_chance: float = 0.08
) -> None:
    """Type *text* into the page's currently-focused element, humanized.

    Assumes the target element is already focused by the caller (LinkedIn's
    compose surfaces need a JS ``.focus()`` rather than Playwright's
    actionability-checked ``.fill()``/``.click()`` -- see the call site in
    ``scraping/extractor.py::send_message`` for why). Dispatches one
    character at a time through ``page.keyboard.type()``, which fires real
    keydown/input/keyup events on the focused element, with a sampled delay
    between characters instead of Playwright's fixed per-call delay.
    """
    for char in text:
        await page.keyboard.type(char)
        await asyncio.sleep(_char_delay_seconds())
        if char in _PUNCTUATION_PAUSE_CHARS:
            await asyncio.sleep(random.uniform(0.15, 0.35))
        elif random.random() < thinking_pause_chance:
            await asyncio.sleep(random.uniform(0.3, 0.8))


def _bezier_path(
    x0: float, y0: float, x1: float, y1: float, steps: int
) -> list[tuple[float, float]]:
    """Cubic Bezier path from (x0,y0) to (x1,y1) with randomized control points.

    Control points are offset perpendicular to the straight-line path (not
    just randomly in x/y) so the curve reads as a plausible arc rather than
    a jittery zigzag. Sampled with smoothstep easing (3u^2 - 2u^3) instead
    of linear ``t`` so the motion decelerates approaching the target, rather
    than arriving at constant speed and stopping instantly.
    """
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy) or 1.0
    perp_x, perp_y = -dy / dist, dx / dist
    spread = min(dist * 0.3, 120.0)
    c1x = x0 + dx * 0.33 + perp_x * random.uniform(-spread, spread)
    c1y = y0 + dy * 0.33 + perp_y * random.uniform(-spread, spread)
    c2x = x0 + dx * 0.66 + perp_x * random.uniform(-spread, spread)
    c2y = y0 + dy * 0.66 + perp_y * random.uniform(-spread, spread)

    points = []
    for i in range(steps + 1):
        t = i / steps
        u = t * t * (3 - 2 * t)  # smoothstep
        x = (
            (1 - u) ** 3 * x0
            + 3 * (1 - u) ** 2 * u * c1x
            + 3 * (1 - u) * u**2 * c2x
            + u**3 * x1
        )
        y = (
            (1 - u) ** 3 * y0
            + 3 * (1 - u) ** 2 * u * c1y
            + 3 * (1 - u) * u**2 * c2y
            + u**3 * y1
        )
        points.append((x, y))
    return points


async def _move_mouse_bezier(page: Any, target_x: float, target_y: float) -> None:
    """Move the mouse to (target_x, target_y) along a curved, eased path.

    Playwright doesn't expose the current cursor position, so each move
    starts from the viewport center -- a plausible-enough approximation
    for the goal here (avoid an instant teleport-click), not a claim of
    tracking true continuous cursor state across calls.
    """
    viewport = page.viewport_size or {"width": 1280, "height": 720}
    start_x, start_y = viewport["width"] / 2, viewport["height"] / 2
    steps = random.randint(20, 40)
    path = _bezier_path(start_x, start_y, target_x, target_y, steps)

    # ~30% chance of a slight overshoot-then-correct before settling,
    # matching the human tendency to overshoot fast targeted movements.
    if random.random() < 0.3:
        overshoot = (
            target_x + random.uniform(-15, 15),
            target_y + random.uniform(-15, 15),
        )
        path = path[:-1] + [overshoot, (target_x, target_y)]

    for x, y in path:
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.001, 0.005))


async def human_move_and_click(
    page: Any, locator: Any, *, engine: str, timeout: int | None = None
) -> None:
    """Move the mouse along a curved path, then click *locator*.

    No-op passthrough to a direct ``locator.click()`` when ``engine ==
    "camoufox"`` -- Camoufox already humanizes cursor movement natively
    (``humanize=True``), so this would duplicate it, not add safety.
    """
    click_kwargs = {"timeout": timeout} if timeout is not None else {}
    if engine == "camoufox":
        await locator.click(**click_kwargs)
        return

    try:
        box = await locator.bounding_box()
    except Exception:
        # bounding_box() failing is not a reason to abandon a real write
        # action -- degrade to a normal click rather than raise out of a
        # humanization nicety.
        box = None
    if box is None:
        # Not visible/attached -- fall back to Playwright's own click, which
        # raises its own actionability error with a clearer message.
        await locator.click(**click_kwargs)
        return

    target_x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
    target_y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
    await _move_mouse_bezier(page, target_x, target_y)
    await asyncio.sleep(random.uniform(0.03, 0.12))
    await page.mouse.click(target_x, target_y)


async def human_scroll(page: Any, total_delta: int, *, engine: str) -> None:
    """Scroll the page by *total_delta* (mouse.wheel dy) in humanized increments.

    No-op passthrough to a single ``mouse.wheel()`` call when ``engine ==
    "camoufox"`` for the same reason as :func:`human_move_and_click`.
    """
    if engine == "camoufox":
        await page.mouse.wheel(0, total_delta)
        return

    remaining = total_delta
    chunks = random.randint(3, 6)
    for i in range(chunks):
        divisor = chunks - i
        chunk = remaining // divisor if divisor else remaining
        await page.mouse.wheel(0, chunk)
        remaining -= chunk
        await asyncio.sleep(random.uniform(0.15, 0.45))
