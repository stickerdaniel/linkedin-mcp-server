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

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
    RateLimitError,
)

logger = logging.getLogger(__name__)

MESSAGING_URL = "https://www.linkedin.com/messaging/"

# Structural, locale-independent handle for one conversation row.
CONVERSATION_ROW_SELECTOR = "main li label[aria-label]"

# Accessible names for the sidebar's paging control, per locale. `text.py`
# already pins the en string for this same control as `sidebar_end`; this is the
# documented-locale-table route AGENTS.md allows where no structural handle
# exists. Extend rather than translate at runtime.
LOAD_MORE_NAMES: tuple[str, ...] = ("Load more conversations",)

# Fallback when the locale is not in the table: a button that sits directly in
# the conversation list rather than inside one of its rows.
LOAD_MORE_STRUCTURAL_SELECTOR = "main > * button:not(li button)"

# Substitutes a cursor into the query's `variables=(...)` blob. The paging
# query carries `nextCursor:` already, so replacement is enough; there is no
# need to understand the rest of the (non-JSON, Rest.li) encoding.
_CURSOR_RE = re.compile(r",?nextCursor:[^,)]*")


def _set_cursor(url: str, cursor: str) -> str:
    """Swap the cursor in a query URL.

    Uses a replacement FUNCTION, not a template: `re.sub` interprets backslash
    escapes in a template string, so a cursor containing a backslash would
    either raise `re.error` or be silently rewritten as a backreference. The
    cursor is opaque server-issued text, so it is inserted verbatim.
    """
    return _CURSOR_RE.sub(lambda _: f",nextCursor:{cursor}", url, count=1)


def _drop_cursor(url: str) -> str:
    """Remove the cursor variable entirely, yielding a first-page request."""
    return _CURSOR_RE.sub(lambda _: "", url, count=1)


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
    async def _discover_query(self) -> tuple[str, bool]:
        """Return a CURSORLESS conversations query, and whether paging is available.

        Two things have to be true of the result and they pull in opposite
        directions.

        It must start at the FIRST page. The query that supports paging is the
        one the "load more" control issues, and that request already carries a
        cursor pointing past page one — returning it unchanged made every fresh
        walk silently skip the newest conversations. So the cursor is stripped
        here and put back only when a caller resumes.

        It must also work for a mailbox that fits on one page, where no "load
        more" control exists and no cursor-bearing request is ever emitted.
        Requiring one made the tool raise on a perfectly valid mailbox, so the
        page-load request is captured too and used as the fallback. The boolean
        says which happened: False means this mailbox has one page.

        The queryId is a persisted hash that LinkedIn rotates, and the
        page-load query accepts only ``mailboxUrn`` while silently ignoring
        added cursor variables, so neither query can be synthesised or pinned.
        Both are observed.
        """
        page = self._session.page
        page_load: list[str] = []
        cursored: list[str] = []

        def _capture(request: Any) -> None:
            url = request.url
            if "messengerConversations" not in url:
                return
            (cursored if "nextCursor" in url else page_load).append(url)

        page.on("request", _capture)
        try:
            await self._navigator._navigate_to_page(MESSAGING_URL)
            await self._session.check_rate_limit()

            # Ember hydrates the sidebar seconds after the document is ready, so
            # an absent control means "not rendered yet" far more often than
            # "no more conversations".
            try:
                await page.wait_for_selector(
                    CONVERSATION_ROW_SELECTOR, state="attached", timeout=15000
                )
            except PlaywrightTimeoutError:
                logger.debug("conversation sidebar did not mount within 15s")

            for _ in range(6):
                if cursored:
                    break
                await self._scroll_conversation_list()
                button = await self._load_more_control()
                if button is None:
                    await self._session.delay(1.5)
                    continue
                try:
                    await button.click(timeout=5000)
                except PlaywrightTimeoutError:
                    break
                await self._session.delay(2.5)
        finally:
            page.remove_listener("request", _capture)

        if cursored:
            return _drop_cursor(cursored[-1]), True
        if page_load:
            return page_load[-1], False
        raise LinkedInScraperException(
            "No messengerConversations request was observed. The messaging page "
            "did not load, or LinkedIn changed the messaging client."
        )

    async def _scroll_conversation_list(self) -> None:
        """Scroll the sidebar to its bottom, where the paging control mounts.

        Deliberately the same shape as `conversations._scroll_main_scrollable_region`:
        same `isScrollable` predicate, same largest-region choice. Two different
        programs doing one job would be two things to keep in step.
        """
        await self._session.page.evaluate(
            """() => {
                const main = document.querySelector('main');
                if (!main) return false;

                const isScrollable = element => {
                    const style = window.getComputedStyle(element);
                    return (
                        (style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                        element.scrollHeight > element.clientHeight + 20
                    );
                };

                const candidates = [main, ...main.querySelectorAll('*')].filter(isScrollable);
                const target = candidates.sort(
                    (left, right) => right.scrollHeight - left.scrollHeight
                )[0] || main;
                target.scrollTop = target.scrollHeight;
                return true;
            }"""
        )

    async def _load_more_control(self) -> Any | None:
        """Locate the paging control without depending on English.

        AGENTS.md requires button identity to be locale-independent or to come
        from an explicit documented locale table. The accessible name is the
        only reliable handle LinkedIn gives this control, so the table is the
        route taken, mirroring ``_MESSAGING_CHROME_STRINGS`` in ``text.py``
        which already pins this exact string for the same control. An unknown
        locale falls through to the structural probe rather than failing.
        """
        page = self._session.page
        for name in LOAD_MORE_NAMES:
            control = page.get_by_role("button", name=name, exact=False)
            if await control.count():
                return control.first

        # Structural fallback for locales not in the table: the control is the
        # only button inside the conversation list that is not part of a row.
        structural = page.locator(LOAD_MORE_STRUCTURAL_SELECTOR)
        if await structural.count():
            return structural.first
        return None

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
                if (r.status !== 200) return {error: 'HTTP ' + r.status, status: r.status};
                return {body: await r.text()};
            }""",
            url,
        )
        if not isinstance(raw, dict) or raw.get("error"):
            detail = (raw or {}).get("error", "unknown")
            status = (raw or {}).get("status")
            # Auth and rate-limit failures keep their own types so a caller can
            # tell "sign in again" and "slow down" apart from "this broke", and
            # so neither is retried as though it were transient noise.
            if status in (401, 403):
                raise AuthenticationError(
                    f"Voyager conversations request rejected: {detail}"
                )
            if status == 429:
                raise RateLimitError(
                    f"Voyager conversations request rate limited: {detail}"
                )
            raise LinkedInScraperException(
                f"Voyager conversations request failed: {detail}"
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
        """Epoch milliseconds to a UTC ISO-8601 string, or None.

        UTC rather than local time on purpose. `.astimezone()` with no argument
        renders in whatever zone the machine happens to be in, which makes the
        same mailbox produce different timestamps on a laptop and a server --
        the value stops being a property of the data and becomes a property of
        the host. The raw epoch stays available as `last_activity_at`, so a
        caller that wants a local clock can convert with its own zone rather
        than inherit ours.
        """
        if not isinstance(epoch_ms, (int, float)) or epoch_ms <= 0:
            return None
        return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat(
            timespec="minutes"
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

    async def _diagnose_empty(self, url: str) -> str:
        """Decide WHY a page came back empty, before anyone reports a zero.

        An empty page is the shared failure mode of at least four different
        situations here: a genuinely empty mailbox, an argument the API
        silently rejected, a session that stopped being authoritative, and a
        payload whose shape changed under the parser. They are
        indistinguishable from the response alone, and the wrong reading is
        expensive in both directions -- a false zero says "nobody is waiting on
        you", a false alarm sends someone hunting a bug that is not there.

        So a zero is never taken at face value. This runs a POSITIVE CONTROL:
        the same endpoint, same session, same parser, with the filters stripped
        and a single row requested. If the control returns a row, the
        instrument works and the zero is real. If it does not, the zero is
        about the instrument, not the mailbox.
        """
        # Drop the cursor variable outright rather than blanking it. An empty
        # `nextCursor:` is a MALFORMED request, and a malformed request that
        # returns nothing would be read here as a broken instrument -- turning
        # this control into exactly the kind of unvalidated measurement it
        # exists to prevent.
        control = _drop_cursor(url)
        control = _COUNT_RE.sub(lambda _: "count:1", control)
        control = _CATEGORY_RE.sub(lambda _: "category:PRIMARY_INBOX", control)
        try:
            payload = await self._fetch(control)
        except Exception as exc:  # the control itself could not run
            return f"control-failed: {type(exc).__name__}: {exc}"

        rows = self._conversations(payload)
        if rows:
            return "verified-empty"
        if payload.get("included"):
            # Entities came back, but none of them parsed as a Conversation.
            # That is a SHAPE change, which is the one case that silently
            # turns a full mailbox into a zero.
            return "parse-failure: included entities present, zero Conversations"
        return "control-empty: session or endpoint returned nothing at all"

    async def get_all_conversations(
        self,
        limit: int = 200,
        max_pages: int = 60,
        cursor: str | None = None,
        quiet_for_days: int | None = None,
        awaiting_reply_only: bool = False,
        category: str | None = None,
        page_size: int = MAX_PAGE_SIZE,
        known_thread_urns: list[str] | None = None,
    ) -> dict[str, Any]:
        """Walk the mailbox by cursor and return normalized conversations.

        Terminates on the *extracted row count*, never on a reported total and
        never on anything the DOM says: the sidebar virtualizes and recycles
        nodes, so its row count has been observed going DOWN while more
        conversations were being loaded.
        """
        # Blank arguments are rejected, never coerced. Every one of these would
        # otherwise reach LinkedIn as a malformed variable and come back as an
        # empty page -- the same shape as a real answer, which is precisely the
        # confusion this whole module is built to avoid. A caller that passes a
        # blank means something went wrong upstream of here; saying so beats
        # answering "you have no conversations".
        if category is not None and not category.strip():
            raise LinkedInScraperException(
                "category was blank. Pass None for no filter, or one of: "
                f"{', '.join(sorted(KNOWN_CATEGORIES))}."
            )
        if cursor is not None and not cursor.strip():
            raise LinkedInScraperException(
                "cursor was blank. Pass None to start from the most recent "
                "conversations, or a next_cursor from a previous call."
            )
        if limit < 1:
            raise LinkedInScraperException(f"limit must be >= 1, got {limit}.")
        if max_pages < 1:
            raise LinkedInScraperException(f"max_pages must be >= 1, got {max_pages}.")
        if quiet_for_days is not None and quiet_for_days < 1:
            raise LinkedInScraperException(
                f"quiet_for_days must be >= 1, got {quiet_for_days}."
            )

        url, can_page = await self._discover_query()
        me = self._me_profile_id(url)
        if cursor:
            if not can_page:
                raise LinkedInScraperException(
                    "A cursor was supplied but this mailbox exposes no paging "
                    "query, so the cursor cannot be honoured. Call without one."
                )
            url = _set_cursor(url, cursor.strip())

        page_size = max(1, min(page_size, MAX_PAGE_SIZE))

        if category:
            category = category.strip().upper()
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

        # Converted here rather than by the caller: the extractor is a thin
        # delegate by design, so shaping arguments is this layer's job.
        known: set[str] = set(known_thread_urns or ())

        filters_active = bool(quiet_for_days is not None or awaiting_reply_only)

        collected: dict[str, dict[str, Any]] = {}
        raw_rows = 0
        scanned = 0
        pages = 0
        exhausted = False
        next_cursor: str | None = None
        seen_cursors: set[str] = set()
        if cursor:
            seen_cursors.add(cursor.strip())

        # Filters narrow what is RETURNED, never what is walked: the mailbox is
        # ordered by recency, so the dormant threads a reconnect pass wants sit
        # behind every recent one. `scanned` is reported so a caller can tell a
        # filtered-empty page from an empty mailbox.
        while pages < max_pages and len(collected) < limit:
            # Ask for no more than the caller still wants. Fetching a full page
            # and slicing to `limit` afterwards would DISCARD the overflow while
            # the cursor advanced past it, so those conversations could not be
            # recovered by resuming from `next_cursor` -- silent data loss
            # wearing the costume of an honoured limit.
            #
            # Filters are the exception: they narrow what is KEPT, so the number
            # of rows needed to find `limit` matches is unbounded and the page
            # stays full.
            if filters_active:
                request_size = page_size
            else:
                request_size = max(1, min(page_size, limit - len(collected)))
            url = _COUNT_RE.sub(lambda _: f"count:{request_size}", url)

            payload = await self._fetch(url)
            pages += 1

            rows = self._conversations(payload)
            raw_rows += len(rows)

            # A page with entities but no conversations is a shape change, not
            # an empty page. Fail loudly rather than let it read as a zero.
            if not rows and payload.get("included"):
                raise LinkedInScraperException(
                    "Conversations payload changed shape: "
                    f"{len(payload['included'])} included entities but zero "
                    "parsed as Conversation. Refusing to report this as an "
                    "empty mailbox."
                )

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
            if known and rows:
                page_urns = {r.get("entityUrn") for r in rows if r.get("entityUrn")}
                if page_urns and page_urns <= known:
                    exhausted = False
                    next_cursor = self._next_cursor(payload)
                    break

            next_cursor = self._next_cursor(payload)

            if not rows or not next_cursor:
                exhausted = True
                break

            # A repeated cursor means the server is handing back a page this
            # walk already requested. Continuing would re-fetch it until
            # max_pages, and returning it as next_cursor would hand a caller a
            # loop of their own. Deduplicating rows hides this rather than
            # stopping it, so the cursor itself is tracked.
            if next_cursor in seen_cursors:
                logger.warning(
                    "Conversations cursor repeated after %d page(s); stopping.",
                    pages,
                )
                next_cursor = None
                exhausted = True
                break
            seen_cursors.add(next_cursor)

            url = _set_cursor(url, next_cursor)
            await self._session.delay(0.4)

        logger.info(
            "Voyager conversations: %d kept of %d scanned over %d page(s), exhausted=%s",
            len(collected),
            scanned,
            pages,
            exhausted,
        )
        # Only an UNFILTERED walk that found nothing is ambiguous. If filters
        # were applied and rows were scanned, zero is a real answer about the
        # filter, and re-probing would only add noise.
        zero_reason: str | None = None
        if raw_rows == 0:
            zero_reason = await self._diagnose_empty(url)
            if zero_reason != "verified-empty":
                raise LinkedInScraperException(
                    f"Conversation walk returned nothing and the positive "
                    f"control did not clear it ({zero_reason}). Treating this "
                    "as an instrument failure rather than an empty mailbox."
                )
        elif not collected:
            zero_reason = "filtered-empty"

        return {
            "conversations": list(collected.values())[:limit],
            "count": min(len(collected), limit),
            "scanned": scanned,
            # None when rows were returned. "verified-empty" means a positive
            # control confirmed the mailbox really is empty; "filtered-empty"
            # means rows existed but the filters excluded them all.
            "zero_reason": zero_reason,
            # Pass back into `cursor` to continue where this walk stopped.
            # None once exhausted.
            "next_cursor": None if exhausted else next_cursor,
            "pages_fetched": pages,
            # False means the walk stopped on `limit` or `max_pages`, so the
            # mailbox holds more than was returned. Callers reconciling against
            # their own records must not read a truncated walk as a complete one.
            "exhausted": exhausted,
        }
