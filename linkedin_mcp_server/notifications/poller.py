"""Background notification poller.

Runs as a long-lived asyncio task started during server lifespan. Every
POLL_INTERVAL_SECONDS it acquires the shared scraper lock, polls LinkedIn for
new messages and connection approvals, and pushes a ResourceUpdatedNotification
to all subscribed MCP sessions when changes are detected.
"""

from __future__ import annotations

import asyncio
import logging

from linkedin_mcp_server.drivers.browser import (
    ensure_authenticated,
    get_or_create_browser,
)
from linkedin_mcp_server.notifications.registry import add_events, notify_all
from linkedin_mcp_server.notifications.state import (
    NotificationState,
    load_state,
    save_state,
)
from linkedin_mcp_server.scraper_lock import get_scraper_lock
from linkedin_mcp_server.scraping import LinkedInExtractor

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS: int = 300  # 5 minutes


async def _poll_once(
    extractor: LinkedInExtractor, state: NotificationState
) -> list[dict]:
    """Run one poll cycle. Returns a list of new notification events."""
    events: list[dict] = []

    # -- Messages --
    try:
        thread_ids = await extractor.get_inbox_thread_ids(limit=10)
        new_threads = [
            tid for tid in thread_ids if tid not in state.last_message_thread_ids
        ]
        for tid in new_threads:
            events.append({"type": "new_message", "thread_id": tid})
        if new_threads:
            state.last_message_thread_ids.update(new_threads)
            logger.info("Detected %d new message thread(s)", len(new_threads))
    except Exception:
        logger.warning("Message poll failed", exc_info=True)

    # -- Connection approvals --
    try:
        snippets = await extractor.get_connection_approval_notifications()
        new_snippets = [
            s for s in snippets if s not in state.last_connection_approval_texts
        ]
        for snippet in new_snippets:
            events.append({"type": "connection_approved", "text": snippet})
        if new_snippets:
            state.last_connection_approval_texts.update(new_snippets)
            logger.info("Detected %d new connection approval(s)", len(new_snippets))
    except Exception:
        logger.warning("Connection approval poll failed", exc_info=True)

    return events


async def notification_poller() -> None:
    """Long-running coroutine that polls LinkedIn and pushes MCP notifications."""
    logger.info("Notification poller started (interval=%ds)", POLL_INTERVAL_SECONDS)
    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        logger.debug("Notification poll starting")
        try:
            async with get_scraper_lock():
                browser = await get_or_create_browser()
                await ensure_authenticated()
                extractor = LinkedInExtractor(browser.page)
                state = load_state()
                events = await _poll_once(extractor, state)

            if events:
                add_events(events)
                save_state(state)
                await notify_all()
                logger.info("Pushed %d notification event(s) to clients", len(events))
            else:
                logger.debug("No new LinkedIn activity detected")
        except asyncio.CancelledError:
            logger.info("Notification poller cancelled")
            return
        except Exception:
            logger.warning("Notification poll cycle failed", exc_info=True)
