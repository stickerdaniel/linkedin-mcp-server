"""Shared page binding and browser helper boundaries for scraping services."""

from __future__ import annotations

from dataclasses import dataclass, field

import asyncio
import time

from patchright.async_api import Page

from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.core.humanize import jitter
from linkedin_mcp_server.core.utils import (
    detect_rate_limit,
    handle_modal_close,
    scroll_job_sidebar,
    scroll_to_bottom,
)
from linkedin_mcp_server.limits import env_float
from linkedin_mcp_server.scraping.rate_limit import RateLimitBudget


# Pacing between page navigations. Owned by the boundary that performs the
# pause rather than by any one workflow, because the person, company and job
# walks pace themselves the same way and a domain service may not import a
# peer's constant: two copies would let one relocation give two workflows
# different policies without anything failing. Default for `NAV_DELAY_SECONDS`.
NAV_DELAY = 2.0


def nav_delay() -> float:
    """The pause between navigations, read at call time.

    Read at call time so an operator's environment replaces the default
    without an import-order dependency; see `linkedin_mcp_server.limits`.
    """
    return env_float(EnvironmentKeys.NAV_DELAY_SECONDS, NAV_DELAY)


@dataclass(frozen=True, slots=True)
class ScrapingSession:
    """Immutable page adapter shared by every scraping service.

    The binding is frozen; the rate-limit budget it carries is not. One
    session is built per tool call, so the budget spans the whole scrape,
    which is what lets a throttled scrape stop asking instead of sending one
    more navigation per remaining section.
    """

    page: Page
    rate_limit: RateLimitBudget = field(default_factory=RateLimitBudget)

    def monotonic(self) -> float:
        """Read the session clock."""
        return time.monotonic()

    async def delay(self, seconds: float) -> None:
        """Pause through the session delay boundary."""
        await asyncio.sleep(seconds)

    def jittered(self, seconds: float) -> float:
        """A pause length near `seconds`, through the session jitter boundary.

        Every deliberate pause is jittered here and nowhere else, so a test or
        a policy trace neutralises the randomness at one seam.
        """
        return jitter(seconds)

    async def pace(self, seconds: float) -> None:
        """Pause for a jittered interval around `seconds`.

        A constant delay between actions gives the traffic a period to lock
        onto; this is the pause every workflow takes between navigations.
        """
        await self.delay(self.jittered(seconds))

    async def claim_soft_retry(self, url: str) -> bool:
        """Take one retry from this scrape's soft rate-limit budget, pacing it."""
        return await self.rate_limit.claim_soft_retry(url, sleep=self.pace)

    async def check_rate_limit(self) -> None:
        """Raise when the current page is rate-limited or challenged."""
        await detect_rate_limit(self.page)

    async def dismiss_modal(self) -> bool:
        """Close an obstructing modal when one is present."""
        return await handle_modal_close(self.page)

    async def scroll_body(self, pause_time: float = 1.0, max_scrolls: int = 10) -> None:
        """Scroll the page body through the shared utility boundary."""
        await scroll_to_bottom(
            self.page,
            pause_time=pause_time,
            max_scrolls=max_scrolls,
        )

    async def scroll_job_sidebar(
        self,
        settle_timeout: float = 3.0,
        poll_interval: float = 0.15,
        min_budget: float = 0.4,
        max_scrolls: int = 10,
        deadline: float = 12.0,
    ) -> bool:
        """Scroll the job rail through the shared utility boundary."""
        return await scroll_job_sidebar(
            self.page,
            settle_timeout=settle_timeout,
            poll_interval=poll_interval,
            min_budget=min_budget,
            max_scrolls=max_scrolls,
            deadline=deadline,
        )
