"""Randomized pacing between browser actions.

A scraper that acts on a fixed cadence is trivially separable from a person
by timing alone: every navigation exactly 2.0s apart, every scroll exactly
0.5s. This module supplies the jitter, and is the only place in the codebase
that calls ``random``.

Two magnitudes, because two kinds of action are being paced. A *full* pause
stands for a decision — opening a page, pressing a button — and uses the
configured range. A *skim* pause stands for a glance — one more scroll, the
next row in a list — and uses a quarter of it, because a person skimming a
list does not deliberate for seconds per row.

Off by default and off means off: with ``enabled`` false nothing sleeps here
and the caller's existing timing is untouched.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import random

logger = logging.getLogger(__name__)

# A glance costs a quarter of a decision. Not measured against human data —
# chosen so that a 1-5s decision range yields 0.25-1.25s per skim, which keeps
# a 50-row enumeration inside a tool-call budget while never firing two
# requests in the same instant.
SKIM_FRACTION = 0.25


@dataclass(frozen=True)
class HumanPacing:
    """How long to wait between browser actions.

    Passed to components as a parameter rather than read from the config
    singleton: the singleton parses ``sys.argv`` lazily, and a component that
    re-reads it mid-call has already been the cause of a wrong timeout budget
    in this codebase.
    """

    enabled: bool
    min_seconds: float
    max_seconds: float

    @classmethod
    def disabled(cls) -> HumanPacing:
        """Pacing that never sleeps — the default everywhere."""
        return cls(enabled=False, min_seconds=0.0, max_seconds=0.0)

    def full_delay(self) -> float:
        """Seconds to wait before a deliberate action."""
        return random.uniform(self.min_seconds, self.max_seconds)

    def skim_delay(self) -> float:
        """Seconds to wait before a glancing action."""
        return random.uniform(
            self.min_seconds * SKIM_FRACTION, self.max_seconds * SKIM_FRACTION
        )


async def human_pause(pacing: HumanPacing | None, reason: str) -> None:
    """Wait a decision-sized interval, or return at once when disabled."""
    if pacing is None or not pacing.enabled:
        return
    delay = pacing.full_delay()
    logger.debug("Human pause %.2fs before %s", delay, reason)
    await asyncio.sleep(delay)


async def skim_pause(pacing: HumanPacing | None, reason: str) -> None:
    """Wait a glance-sized interval, or return at once when disabled."""
    if pacing is None or not pacing.enabled:
        return
    delay = pacing.skim_delay()
    logger.debug("Skim pause %.2fs before %s", delay, reason)
    await asyncio.sleep(delay)
