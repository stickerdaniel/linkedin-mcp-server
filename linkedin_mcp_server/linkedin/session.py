"""Shared page binding and browser helper boundaries for page workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import asyncio
import time

from patchright.async_api import Page

from linkedin_mcp_server.core.destination import raise_if_off_linkedin
from linkedin_mcp_server.core.utils import (
    detect_rate_limit,
    handle_modal_close,
    scroll_job_sidebar,
    scroll_to_bottom,
)


# Pacing between page navigations. Owned by the boundary that performs the
# pause rather than by any one workflow, because the person, company and job
# walks pace themselves the same way and a domain service may not import a
# peer's constant: two copies would let one relocation give two workflows
# different policies without anything failing.
NAV_DELAY = 2.0

# The key the read wrapper puts the document's own address under. Unlikely to
# collide with anything a read script returns, because a double answering
# without it is how the fallback below tells the two apart.
_DOCUMENT_ADDRESS = "__linkedinMcpDocumentAddress"
_NO_ARGUMENT = object()


@dataclass(frozen=True, slots=True)
class PageSession:
    """Immutable page adapter shared by every page workflow."""

    page: Page

    def monotonic(self) -> float:
        """Read the session clock."""
        return time.monotonic()

    async def delay(self, seconds: float) -> None:
        """Pause through the session delay boundary."""
        await asyncio.sleep(seconds)

    async def read_document(self, script: str, arg: Any = _NO_ARGUMENT) -> Any:
        """Run a read *script* and answer its result only from a LinkedIn page.

        *script* is a function expression. Its result comes back together with
        the address of the document it ran in, read in the same evaluation, so
        a redirect landing between navigation and this read cannot hand over
        another site's page as LinkedIn's. Every script whose result is read as
        LinkedIn content goes through here.

        Raises:
            OffLinkedInLandingError: When that document was not LinkedIn's.
        """
        wrapped = (
            "async (arg) => {\n"
            f"const value = await ({script})(arg);\n"
            f"return {{ value, {_DOCUMENT_ADDRESS}: location.href }};\n"
            "}"
        )
        if arg is _NO_ARGUMENT:
            result = await self.page.evaluate(wrapped)
        else:
            result = await self.page.evaluate(wrapped, arg)
        if isinstance(result, dict) and _DOCUMENT_ADDRESS in result:
            raise_if_off_linkedin(result[_DOCUMENT_ADDRESS])
            return result.get("value")
        # Only a test double answers with the bare value; the driver's address
        # stands in for the document's there.
        raise_if_off_linkedin(self.page.url)
        return result

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
