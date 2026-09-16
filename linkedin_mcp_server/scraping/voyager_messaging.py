"""Read the messaging inbox through LinkedIn's own Voyager GraphQL call.

Why this exists
---------------
``ConversationReader`` can only reach the conversations LinkedIn has painted
into the sidebar, and it harvests a thread id by *clicking* each row — which
marks it read. That makes a full inventory impossible: you cannot ask "have I
replied to everyone" without altering the thing you are measuring.

The web app itself does not work that way. It fetches conversations from
``voyagerMessagingGraphQL`` and renders the result, so every thread id is
already present in the payload as ``entityUrn``. Reading that same call gives
the whole mailbox, paged, with nothing clicked and nothing marked read.

Session handling is deliberately identical to the rest of the scraper: every
request is issued *inside the authenticated page* via ``page.evaluate``, so
cookies and CSRF come from the live session. No cookie file is read, no
credential is handled here, and there is no second auth path to keep in sync.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from linkedin_mcp_server.core.exceptions import LinkedInScraperException

logger = logging.getLogger(__name__)

MESSAGING_URL = "https://www.linkedin.com/messaging/"

# Substitutes a cursor into the query's `variables=(...)` blob. The paging
# query carries `nextCursor:` already, so replacement is enough; there is no
# need to understand the rest of the (non-JSON, Rest.li) encoding.
_CURSOR_RE = re.compile(r"nextCursor:[^,)]*")


class VoyagerMessagingReader:
    """Page the full conversation list without touching the DOM."""

    def __init__(self, session: Any, navigator: Any):
        self._session = session
        self._navigator = navigator

    # ------------------------------------------------------------------ #
    # Query discovery
    # ------------------------------------------------------------------ #
    async def _discover_paging_query(self) -> str:
        """Return the conversations query URL that accepts a cursor.

        The queryId is a *persisted* GraphQL hash: it pins a fixed variable
        signature and LinkedIn rotates it on every deploy. Two consequences
        drive this method's shape.

        First, it cannot be hardcoded — a pinned hash silently rots.

        Second, the query issued on page load accepts only ``mailboxUrn``.
        Appending ``count`` or ``lastUpdatedBefore`` to it returns **HTTP 200
        and the identical first page**, because unknown variables are dropped
        rather than rejected. A 200 that ignores the parameter is
        indistinguishable from one that honoured it, so the cursor-bearing
        query has to be observed rather than assumed. Clicking "Load more
        conversations" once is what makes the web app issue it.
        """
        page = self._session.page
        seen: list[str] = []

        def _capture(request: Any) -> None:
            url = request.url
            if "messengerConversations" in url and "nextCursor" in url:
                seen.append(url)

        page.on("request", _capture)
        try:
            await self._navigator._navigate_to_page(MESSAGING_URL)
            await self._session.check_rate_limit()

            # The control only mounts once the list bottom is reached.
            for _ in range(3):
                if seen:
                    break
                await page.evaluate(
                    """() => {
                        const main = document.querySelector('main');
                        if (!main) return;
                        const scrollable = [main, ...main.querySelectorAll('*')].filter(el => {
                            const s = getComputedStyle(el);
                            return (s.overflowY === 'auto' || s.overflowY === 'scroll')
                                && el.scrollHeight > el.clientHeight + 20;
                        });
                        scrollable.forEach(el => { el.scrollTop = el.scrollHeight; });
                    }"""
                )
                button = page.get_by_role(
                    "button", name=re.compile("load more conversation", re.I)
                )
                if not await button.count():
                    break
                await button.first.click(timeout=5000)
                await self._session.delay(2.0)
        finally:
            page.remove_listener("request", _capture)

        if not seen:
            raise LinkedInScraperException(
                "Could not observe a cursor-bearing messengerConversations "
                "query. The inbox may hold a single page, or LinkedIn changed "
                "the messaging client."
            )
        return seen[-1]

    # ------------------------------------------------------------------ #
    # Fetching
    # ------------------------------------------------------------------ #
    async def _fetch(self, url: str) -> dict[str, Any]:
        """Issue one Voyager GET from inside the authenticated page."""
        raw = await self._session.page.evaluate(
            """async (target) => {
                const m = document.cookie.match(/JSESSIONID="?([^";]+)/);
                if (!m) return {error: 'no JSESSIONID cookie in page context'};
                const r = await fetch(target, {
                    credentials: 'include',
                    headers: {
                        'csrf-token': m[1],
                        'accept': 'application/vnd.linkedin.normalized+json+2.1',
                    },
                });
                if (r.status !== 200) return {error: 'HTTP ' + r.status};
                return {body: await r.text()};
            }""",
            url,
        )
        if not isinstance(raw, dict) or raw.get("error"):
            raise LinkedInScraperException(
                f"Voyager conversations request failed: "
                f"{(raw or {}).get('error', 'unknown')}"
            )
        return json.loads(raw["body"])

    @staticmethod
    def _conversations(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Pull Conversation entities out of a normalized payload.

        With the ``normalized+json+2.1`` accept header the rows are URN
        pointers and the entities live in ``included``. ``elements`` is always
        null and the payload's own ``total`` cannot be trusted — it has been
        observed reading 0 against a non-empty collection — so the only honest
        count is the length of what is actually extracted here.
        """
        return [
            item
            for item in payload.get("included", [])
            if str(item.get("$type", "")).endswith("Conversation")
        ]

    @staticmethod
    def _participants(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for item in payload.get("included", []):
            if not str(item.get("$type", "")).endswith("MessagingParticipant"):
                continue
            member = (item.get("participantType") or {}).get("member") or {}
            first = (member.get("firstName") or {}).get("text") or ""
            last = (member.get("lastName") or {}).get("text") or ""
            out[item.get("entityUrn", "")] = {
                "name": f"{first} {last}".strip(),
                "headline": (member.get("headline") or {}).get("text") or "",
                "profile_urn": member.get("profileUrl") or item.get("hostIdentityUrn"),
            }
        return out

    def _normalize(
        self, conversation: dict[str, Any], participants: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        people = [
            participants[urn]
            for urn in conversation.get("*conversationParticipants", [])
            if urn in participants
        ]
        return {
            "thread_urn": conversation.get("entityUrn"),
            "thread_url": conversation.get("conversationUrl"),
            "title": conversation.get("title")
            or (conversation.get("headlineText") or {}).get("text"),
            "participants": [p["name"] for p in people if p["name"]],
            "headlines": [p["headline"] for p in people if p["headline"]],
            "last_activity_at": conversation.get("lastActivityAt"),
            "last_read_at": conversation.get("lastReadAt"),
            "read": conversation.get("read"),
            "unread_count": conversation.get("unreadCount"),
            "categories": conversation.get("categories"),
            "group_chat": conversation.get("groupChat"),
        }

    # ------------------------------------------------------------------ #
    # Public
    # ------------------------------------------------------------------ #
    @staticmethod
    def render_inbox_text(conversations: list[dict[str, Any]]) -> str:
        """Render conversations as an inbox-style text block.

        Keeps the Voyager path a drop-in superset of the DOM path: callers that
        only read ``sections["inbox"]`` keep working, while callers that want
        structure read ``conversations``.
        """
        lines: list[str] = ["Messaging", "Conversation List"]
        for c in conversations:
            who = ", ".join(c.get("participants") or []) or (
                c.get("title") or "Unknown"
            )
            unread = c.get("unread_count") or 0
            flag = f" [{unread} unread]" if unread else ""
            mailbox = ""
            cats = c.get("categories") or []
            if isinstance(cats, list) and cats:
                mailbox = f" ({'/'.join(str(x) for x in cats)})"
            lines.append(f"{who}{flag}{mailbox}")
            headlines = c.get("headlines") or []
            if headlines:
                lines.append(f"    {headlines[0]}")
        return "\n".join(lines)

    async def get_all_conversations(
        self, limit: int = 200, max_pages: int = 60
    ) -> dict[str, Any]:
        """Walk the mailbox by cursor and return normalized conversations.

        Terminates on the *extracted row count*, never on a reported total and
        never on anything the DOM says: the sidebar virtualizes and recycles
        nodes, so its row count has been observed going DOWN while more
        conversations were being loaded.
        """
        url = await self._discover_paging_query()

        collected: dict[str, dict[str, Any]] = {}
        pages = 0
        exhausted = False

        while pages < max_pages and len(collected) < limit:
            payload = await self._fetch(url)
            pages += 1

            rows = self._conversations(payload)
            people = self._participants(payload)
            for row in rows:
                urn = row.get("entityUrn")
                if urn and urn not in collected:
                    collected[urn] = self._normalize(row, people)

            cursor = None
            match = re.search(
                r'"nextCursor":"([^"]+)"', json.dumps(payload.get("data") or {})
            )
            if match:
                cursor = match.group(1)

            if not rows or not cursor:
                exhausted = True
                break

            url = _CURSOR_RE.sub(f"nextCursor:{cursor}", url)
            await self._session.delay(0.4)

        logger.info(
            "Voyager conversations: %d threads over %d page(s), exhausted=%s",
            len(collected),
            pages,
            exhausted,
        )
        return {
            "conversations": list(collected.values())[:limit],
            "count": min(len(collected), limit),
            "pages_fetched": pages,
            # False means the walk stopped on `limit` or `max_pages`, so the
            # mailbox holds more than was returned. Callers reconciling against
            # their own records must not read a truncated walk as a complete one.
            "exhausted": exhausted,
        }
