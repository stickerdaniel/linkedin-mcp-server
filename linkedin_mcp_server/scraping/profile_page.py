"""Identity a loaded person page states about itself."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from linkedin_mcp_server.scraping.session import ScrapingSession


class MessageTarget(Protocol):
    """The recipient identity a top-card compose action carries."""

    @property
    def profile_urn(self) -> str: ...


class MessageTargetResolution(Protocol):
    """One attempt at reading that identity off the current page."""

    @property
    def target(self) -> MessageTarget | None: ...


# The URN is read out of the same atomic top-card snapshot the messaging
# workflow resolves its recipient from, and that read still belongs to the
# facade until the message sender owns it. Taking it as a callable keeps the
# borrow explicit and one-directional: this module never learns what the
# facade is, and the day the sender owns the read, only the wiring moves.
ReadMessageTarget = Callable[[], Awaitable[MessageTargetResolution]]


class ProfilePageReader:
    """Read the profile URN and display name off the bound person page."""

    def __init__(
        self,
        session: ScrapingSession,
        read_message_target: ReadMessageTarget,
    ):
        self._session = session
        self._read_message_target = read_message_target

    async def _extract_profile_urn(self) -> str | None:
        """Extract a profile URN only from one unambiguous top-card snapshot."""
        resolution = await self._read_message_target()
        return resolution.target.profile_urn if resolution.target else None

    async def _read_profile_display_name(self) -> str | None:
        """Read the visible profile name from the current person page."""
        display_name = await self._session.page.evaluate(
            """() => {
                const heading = document.querySelector('main h1');
                const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
                if (heading) {
                    const headingText = normalize(
                        heading.innerText || heading.textContent || ''
                    );
                    if (headingText) return headingText;
                }

                const main = document.querySelector('main');
                if (!main) return '';
                const lines = (main.innerText || '')
                    .split('\\n')
                    .map(normalize)
                    .filter(Boolean);
                return lines[0] || '';
            }"""
        )
        if not isinstance(display_name, str):
            return None
        display_name = display_name.strip()
        return display_name or None
