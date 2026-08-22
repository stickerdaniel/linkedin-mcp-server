"""Human-like timing and interaction, to avoid bot-traffic fingerprints.

Two of the cheapest automation tells are a *constant* delay between actions and
a page that is never touched. Fixed pauses give the traffic a detectable
period; a frozen cursor across page loads is an obvious non-human signal. This
module removes both cheaply: every deliberate pause is jittered around its base,
and each navigation is followed by a few small, randomized mouse movements.

It mirrors what the established LinkedIn tools converged on -- random pauses
between steps and human-like mouse motion -- rather than attempting full
behavioural cloning. Nothing here changes *what* is fetched, only the timing
and cursor entropy around it, so it is safe to apply on every navigation.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

logger = logging.getLogger(__name__)

# One process-wide RNG. Not seeded, so the timing differs run to run.
_rng = random.Random()


def jitter(base: float, spread: float = 0.5) -> float:
    """A value near ``base``, scaled by +/- ``spread`` (never negative).

    ``jitter(2.0)`` returns something in ~[1.0, 3.0]. The point is that no two
    pauses are equal, so the traffic carries no fixed period to lock onto.
    """
    spread = max(0.0, min(spread, 0.95))
    return max(0.0, base * _rng.uniform(1 - spread, 1 + spread))


async def human_pause(base: float, spread: float = 0.5) -> None:
    """Sleep for a jittered interval around ``base`` seconds."""
    await asyncio.sleep(jitter(base, spread))


# Fraction of the budget spent on deliberate pauses. The remainder is headroom
# for the Send click after typing and for any composer setup already spent
# before it. The pause phase is measured against a wall clock (below), so the
# per-key browser round-trips count against it too.
_TYPING_SLEEP_FRACTION = 0.8


async def human_type(
    page: Any,
    text: str,
    *,
    typo_rate: float = 0.06,
    budget_seconds: float = 30.0,
) -> None:
    """Type ``text`` the way a person does: jittered per-key timing, an
    occasional longer "thinking" pause, and now and then a wrong keystroke that
    is immediately backspaced and corrected.

    The net typed result is exactly ``text`` -- a typo is always a wrong char
    followed by one Backspace and the right char, so nothing is left behind.
    A uniform per-key delay (the old fixed 15ms) is itself a tell; this removes
    it. Used on the message composer, the one place the scraper types.

    ``budget_seconds`` bounds the total typing time. Human cadence is ~0.16s per
    character, so a long message (thousands of characters) would otherwise run
    for minutes and overrun the caller's tool timeout. The deliberate pauses run
    only until a wall-clock deadline (a fraction of the budget); past it the
    remaining characters are typed at the browser's own pace with no added
    pauses. Because the deadline is checked against ``time.monotonic()``, the
    serial keyboard round-trips count against it too -- so total typing time is
    bounded by ``budget_seconds`` regardless of message length *or* per-key I/O
    latency, not merely the summed sleeps. Short messages (the common case)
    finish well inside the deadline and type at full natural cadence.

    MCP does not surface the client's timeout to the server, so there is no
    caller deadline to inherit; ``budget_seconds`` is the knob. The default
    (30s) sits well under typical tool timeouts; pass a smaller value if a
    caller runs an unusually short one.
    """
    kb = page.keyboard
    # Wall-clock cutoff for deliberate pauses; keyboard I/O advances the clock
    # too, so a long message stops pausing once real elapsed reaches the budget.
    pause_until = time.monotonic() + budget_seconds * _TYPING_SLEEP_FRACTION
    for ch in text:
        pacing = time.monotonic() < pause_until
        # Occasionally slip: type an adjacent-ish wrong letter, pause, undo it.
        if pacing and ch.isalpha() and _rng.random() < typo_rate:
            wrong = _rng.choice("asdfghjklqwertyuiop")
            await kb.type(wrong)
            await asyncio.sleep(_rng.uniform(0.09, 0.28))
            await kb.press("Backspace")
            await asyncio.sleep(_rng.uniform(0.05, 0.16))

        await kb.type(ch)
        if pacing:
            delay = _rng.uniform(0.04, 0.17)
            # Now and then, pause as if thinking or reading back.
            if _rng.random() < 0.07:
                delay += _rng.uniform(0.3, 0.9)
            await asyncio.sleep(delay)


async def humanize_after_nav(page: Any) -> None:
    """Move the cursor along a few short randomized hops after a page load.

    Best-effort and fully guarded: a mouse-move failure must never break a
    scrape. The movement is small (interior of the viewport), stepped so it is
    not a teleport, and separated by sub-second jittered pauses -- enough to
    keep the cursor from being frozen across navigations without adding
    meaningful latency.
    """
    try:
        vp = getattr(page, "viewport_size", None) or {"width": 1280, "height": 720}
        w, h = int(vp["width"]), int(vp["height"])
        for _ in range(_rng.randint(1, 3)):
            x = _rng.randint(int(w * 0.2), int(w * 0.8))
            y = _rng.randint(int(h * 0.2), int(h * 0.8))
            await page.mouse.move(x, y, steps=_rng.randint(4, 12))
            await asyncio.sleep(_rng.uniform(0.05, 0.35))
    except Exception as e:  # pragma: no cover - defensive; never fail a scrape
        logger.debug("humanize_after_nav skipped: %s", e)
