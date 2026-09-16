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
from datetime import datetime, timezone
import re
from typing import Any

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import LinkedInScraperException

logger = logging.getLogger(__name__)

MESSAGING_URL = "https://www.linkedin.com/messaging/"

# Substitutes a cursor into the query's `variables=(...)` blob. The paging
# query carries `nextCursor:` already, so replacement is enough; there is no
# need to understand the rest of the (non-JSON, Rest.li) encoding.
_CURSOR_RE = re.compile(r"nextCursor:[^,)]*")
_COUNT_RE = re.compile(r"(?<![A-Za-z])count:[^,)]*")
_CATEGORY_RE = re.compile(r"(?<![A-Za-z])category:[^,)]*")

# Measured against the live API on 2026-09-16.
#
# count is honoured exactly (5 -> 5, 21 -> 21) up to 25. At 30 and above the
# response is EMPTY rather than an error, so an over-large page reads as an
# empty mailbox. 25 is the largest verified value; it is the default because
# round trips, not rows, are what the long dormant-contact walk pays for.
MAX_PAGE_SIZE = 25

# category IS filtered server-side: ARCHIVE, INMAIL, STARRED and SPAM each
# return a set whose date range differs from the unfiltered one, and SPAM
# reached 2025 rows in a single call with no paging at all.
#
# ⚠ An UNRECOGNISED category also returns an empty set rather than an error --
# confirmed with a deliberate nonsense value. So "category X returned nothing"
# never means "you have none of X" unless X is on this list.
KNOWN_CATEGORIES = frozenset(
    {"INBOX", "PRIMARY_INBOX", "ARCHIVE", "INMAIL", "STARRED", "SPAM"}
)


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

            # Wait for the sidebar to mount before looking for the control.
            # Ember hydrates the conversation list seconds after the document
            # is ready, and an absent button means "not rendered yet" far more
            # often than "no more conversations".
            try:
                await page.wait_for_selector(
                    "main li label[aria-label]", state="attached", timeout=15000
                )
            except PlaywrightTimeoutError:
                logger.debug("conversation sidebar did not mount within 15s")

            for _ in range(6):
                if seen:
                    break
                # The control only mounts once the list bottom is reached.
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
                    # Not necessarily exhausted -- give it another beat.
                    await self._session.delay(1.5)
                    continue
                try:
                    await button.first.click(timeout=5000)
                except PlaywrightTimeoutError:
                    break
                await self._session.delay(2.5)
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
    def _iso(epoch_ms: Any) -> str | None:
        """Epoch milliseconds to a local ISO-8601 string, or None."""
        if not isinstance(epoch_ms, (int, float)) or epoch_ms <= 0:
            return None
        return (
            datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
            .astimezone()
            .isoformat(timespec="minutes")
        )

    @staticmethod
    def _me_profile_id(query_url: str) -> str | None:
        """Pull the mailbox owner's profile id out of the query's variables.

        The mailbox belongs to the signed-in member, so ``mailboxUrn`` is the
        cheapest available identity for "me" — no extra request, and it cannot
        drift from the mailbox actually being read.
        """
        match = re.search(
            r"mailboxUrn[:%A-Za-z0-9]*?fsd_profile(?::|%3A)([A-Za-z0-9_-]+)", query_url
        )
        return match.group(1) if match else None

    @staticmethod
    def _messages_by_conversation(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Index the newest included Message per conversation urn."""
        latest: dict[str, dict[str, Any]] = {}
        for item in payload.get("included", []):
            if not str(item.get("$type", "")).endswith("Message"):
                continue
            conversation = item.get("*conversation")
            if not conversation:
                continue
            current = latest.get(conversation)
            if current is None or (item.get("deliveredAt") or 0) > (
                current.get("deliveredAt") or 0
            ):
                latest[conversation] = item
        return latest

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
        self,
        conversation: dict[str, Any],
        participants: dict[str, dict[str, Any]],
        last_message: dict[str, Any] | None = None,
        me_profile_id: str | None = None,
    ) -> dict[str, Any]:
        # Drop the mailbox owner: every thread contains him, so leaving him in
        # makes each row read "Taylor Medford, X" and surfaces his own headline
        # instead of the other person's.
        people = [
            participants[urn]
            for urn in conversation.get("*conversationParticipants", [])
            if urn in participants and not (me_profile_id and me_profile_id in urn)
        ]

        text = ((last_message or {}).get("body") or {}).get("text") or ""
        sender = str((last_message or {}).get("*sender") or "")
        # None rather than False when identity is unknown: a wrong "they spoke
        # last" would invent a reply that is owed, and a wrong "I spoke last"
        # would hide one. Absent is honest; guessed is not.
        from_me = (me_profile_id in sender) if (me_profile_id and sender) else None

        return {
            "last_message_text": text[:300],
            "last_message_at": self._iso(
                (last_message or {}).get("deliveredAt")
                or conversation.get("lastActivityAt")
            ),
            "last_message_from_me": from_me,
            # The whole point of the walk: True means their message is the most
            # recent, so a reply is owed. None means it could not be determined.
            "awaiting_my_reply": (not from_me) if from_me is not None else None,
            "thread_urn": conversation.get("entityUrn"),
            "thread_url": conversation.get("conversationUrl"),
            "title": conversation.get("title")
            or (conversation.get("headlineText") or {}).get("text"),
            "participants": [p["name"] for p in people if p["name"]],
            "headlines": [p["headline"] for p in people if p["headline"]],
            "last_activity_at": conversation.get("lastActivityAt"),
            "last_activity_iso": self._iso(conversation.get("lastActivityAt")),
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
            when = c.get("last_activity_iso") or ""
            unread = c.get("unread_count") or 0
            flag = f" [{unread} unread]" if unread else ""
            mailbox = ""
            cats = c.get("categories") or []
            if isinstance(cats, list) and cats:
                mailbox = f" ({'/'.join(str(x) for x in cats)})"
            header = f"{who} - {when}{flag}{mailbox}".replace(" - \n", "")
            lines.append(header.rstrip(" -").rstrip())

            headlines = c.get("headlines") or []
            if headlines:
                lines.append(f"    {headlines[0]}")

            text = (c.get("last_message_text") or "").replace("\n", " ").strip()
            if text:
                # "You:" mirrors LinkedIn's own inbox convention, so a reader
                # sees who spoke last without consulting a second field.
                speaker = "You: " if c.get("last_message_from_me") else ""
                lines.append(f"    {speaker}{text[:200]}")
            if c.get("awaiting_my_reply"):
                lines.append("    >> awaiting your reply")
        return "\n".join(lines)

    @staticmethod
    def _next_cursor(payload: dict[str, Any]) -> str | None:
        """Pull the paging cursor out of the query result's metadata.

        Walked structurally rather than matched against re-serialized JSON.
        The query key varies by mailbox view
        (``messengerConversationsByCategoryQuery`` vs a sync-token variant), so
        the shape is searched for instead of named; and a regex over
        ``json.dumps`` output is brittle in a way that bites silently here,
        since Python emits ``"nextCursor": "..."`` with a space that LinkedIn's
        own wire format does not have. A missed cursor does not raise, it just
        ends the walk one page in, which is indistinguishable from an inbox
        that really did fit on one page.
        """
        inner = (payload.get("data") or {}).get("data") or {}
        for value in inner.values():
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            if isinstance(metadata, dict):
                cursor = metadata.get("nextCursor")
                if isinstance(cursor, str) and cursor:
                    return cursor
        return None

    async def get_all_conversations(
        self,
        limit: int = 200,
        max_pages: int = 60,
        cursor: str | None = None,
        quiet_for_days: int | None = None,
        awaiting_reply_only: bool = False,
        category: str | None = None,
        page_size: int = MAX_PAGE_SIZE,
        stop_at_thread_urns: set[str] | None = None,
    ) -> dict[str, Any]:
        """Walk the mailbox by cursor and return normalized conversations.

        Terminates on the *extracted row count*, never on a reported total and
        never on anything the DOM says: the sidebar virtualizes and recycles
        nodes, so its row count has been observed going DOWN while more
        conversations were being loaded.
        """
        url = await self._discover_paging_query()
        me = self._me_profile_id(url)
        if cursor:
            url = _CURSOR_RE.sub(f"nextCursor:{cursor}", url)

        page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        url = _COUNT_RE.sub(f"count:{page_size}", url)

        if category:
            category = category.upper()
            if category not in KNOWN_CATEGORIES:
                # Refuse rather than let the API answer an unknown category with
                # an empty page, which is indistinguishable from "you have none".
                raise LinkedInScraperException(
                    f"Unknown category {category!r}. Known: "
                    f"{', '.join(sorted(KNOWN_CATEGORIES))}. An unrecognised "
                    "category returns an empty result rather than an error, so "
                    "it is rejected here instead of silently reading as zero."
                )
            url = _CATEGORY_RE.sub(f"category:{category}", url)

        cutoff_ms: float | None = None
        if quiet_for_days is not None:
            cutoff_ms = (
                datetime.now(tz=timezone.utc).timestamp() - quiet_for_days * 86400
            ) * 1000

        collected: dict[str, dict[str, Any]] = {}
        scanned = 0
        pages = 0
        exhausted = False
        next_cursor: str | None = None

        # Filters narrow what is RETURNED, never what is walked: the mailbox is
        # ordered by recency, so the dormant threads a reconnect pass wants sit
        # behind every recent one. `scanned` is reported so a caller can tell a
        # filtered-empty page from an empty mailbox.
        while pages < max_pages and len(collected) < limit:
            payload = await self._fetch(url)
            pages += 1

            rows = self._conversations(payload)
            people = self._participants(payload)
            messages = self._messages_by_conversation(payload)
            for row in rows:
                urn = row.get("entityUrn")
                if not urn or urn in collected:
                    continue
                scanned += 1
                record = self._normalize(row, people, messages.get(urn), me)
                if cutoff_ms is not None:
                    activity = row.get("lastActivityAt") or 0
                    if activity > cutoff_ms:
                        continue
                if awaiting_reply_only and not record.get("awaiting_my_reply"):
                    continue
                collected[urn] = record

            # Incremental sync: the mailbox is recency-ordered, so once a page
            # is entirely threads the caller already knows, everything behind it
            # is older and also known. This is what keeps a routine run at one
            # or two pages instead of re-walking the whole mailbox, and it is
            # the only real defence against a server side that cannot filter by
            # time (lastUpdatedBefore is ignored -- see module docstring).
            if stop_at_thread_urns and rows:
                page_urns = {r.get("entityUrn") for r in rows if r.get("entityUrn")}
                if page_urns and page_urns <= stop_at_thread_urns:
                    exhausted = False
                    next_cursor = self._next_cursor(payload)
                    break

            next_cursor = self._next_cursor(payload)

            if not rows or not next_cursor:
                exhausted = True
                break

            url = _CURSOR_RE.sub(f"nextCursor:{next_cursor}", url)
            await self._session.delay(0.4)

        logger.info(
            "Voyager conversations: %d kept of %d scanned over %d page(s), exhausted=%s",
            len(collected),
            scanned,
            pages,
            exhausted,
        )
        return {
            "conversations": list(collected.values())[:limit],
            "count": min(len(collected), limit),
            "scanned": scanned,
            # Pass back into `cursor` to continue where this walk stopped.
            # None once exhausted.
            "next_cursor": None if exhausted else next_cursor,
            "pages_fetched": pages,
            # False means the walk stopped on `limit` or `max_pages`, so the
            # mailbox holds more than was returned. Callers reconciling against
            # their own records must not read a truncated walk as a complete one.
            "exhausted": exhausted,
        }
