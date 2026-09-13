"""Shared page binding and browser helper boundaries for scraping services."""

from __future__ import annotations

from dataclasses import dataclass

import asyncio
import time

from patchright.async_api import Page

from linkedin_mcp_server.core.utils import (
    detect_rate_limit,
    handle_modal_close,
    scroll_job_sidebar,
    scroll_to_bottom,
)


@dataclass(frozen=True, slots=True)
class ScrapingSession:
    """Immutable page adapter shared by every scraping service."""

    page: Page

    def monotonic(self) -> float:
        """Read the session clock."""
        return time.monotonic()

    async def delay(self, seconds: float) -> None:
        """Pause through the session delay boundary."""
        await asyncio.sleep(seconds)

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
