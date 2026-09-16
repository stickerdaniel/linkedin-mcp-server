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
    """Put a cursor into a query URL, whether or not one is already there.

    Replacing is not enough. Discovery hands back a CURSORLESS url so that a
    fresh read starts at page one, so the common case is inserting into a url
    with no cursor to swap -- and a substitution that finds nothing silently
    does nothing, which would drop the cursor and serve page one forever.

    Uses a replacement FUNCTION rather than a template: `re.sub` interprets
    backslash escapes in a template string, so an opaque server-issued cursor
    containing one would either raise `re.error` or be rewritten as a
    backreference.
    """
    if _CURSOR_RE.search(url):
        return _CURSOR_RE.sub(lambda _: f",nextCursor:{cursor}", url, count=1)
    # No cursor to replace: append inside the variables=(...) block. A
    # replacement FUNCTION here too -- this branch is new, and it does not
    # inherit the template-escaping guarantee from the branch above.
    replaced, count = re.subn(
        r"\)(?!.*\))", lambda _: f",nextCursor:{cursor})", url, count=1
    )
    if count == 0:
        raise LinkedInScraperException(
            f"Could not place a cursor into the conversations query: {url!r} "
            "has no variables block to append to."
        )
    return replaced


def _drop_cursor(url: str) -> str:
    """Remove the cursor variable entirely, yielding a first-page request."""
    return _CURSOR_RE.sub(lambda _: "", url, count=1)


_COUNT_RE = re.compile(r"(?<![A-Za-z])count:[^,)]*")
_CATEGORY_RE = re.compile(r"(?<![A-Za-z])category:[^,)]*")

# Measured against the live API on 2026-09-16.
#
# count is honoured exactly (5 -> 5, 21 -> 21) up to 25. At 30 and above the
# response is EMPTY rather than an error, so an over-large page reads as an
# empty mailbox. 25 is the largest verified value, and it is always used: a
# fixed page size is what lets "fewer than asked for" mean "the end".
PAGE_SIZE = 25

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


# Discovery has to be cached ACROSS tool calls, not within one. A fresh
# LinkedInExtractor -- and so a fresh reader -- is built for every call, so an
# instance attribute lives exactly one page and the caller-driven loop would
# re-navigate, re-wait and re-click for each 25 conversations.
#
# The natural lifetime is the PAGE: a discovered queryId is valid for as long
# as that browser session is. The page it was discovered on is stored alongside
# it, so a browser restart hands over a different object and the cache lapses
# on its own rather than serving a query from a dead session.
_QUERY_CACHE: tuple[Any, str, str] | None = None


def _cached_query(page: Any) -> tuple[str, str] | None:
    if _QUERY_CACHE is None:
        return None
    cached_page, url, paging = _QUERY_CACHE
    return (url, paging) if cached_page is page else None


def _remember_query(page: Any, url: str, paging: str) -> None:
    global _QUERY_CACHE
    _QUERY_CACHE = (page, url, paging)


def forget_cached_query() -> None:
    """Drop the cached query so the next read rediscovers it."""
    global _QUERY_CACHE
    _QUERY_CACHE = None


class VoyagerMessagingReader:
    """Page the full conversation list without touching the DOM."""

    def __init__(self, session: Any, navigator: Any):
        self._session = session
        self._navigator = navigator

    # ------------------------------------------------------------------ #
    # Query discovery
    # ------------------------------------------------------------------ #
    async def _discover_query(self) -> tuple[str, str]:
        page = self._session.page
        cached = _cached_query(page)
        if cached is not None:
            return cached
        found = await self._discover_query_uncached()
        _remember_query(page, *found)
        return found

    async def _discover_query_uncached(self) -> tuple[str, str]:
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
        page-load request is captured too.

        The second return value says WHICH of three things happened, because
        "no paging query" has two very different causes and conflating them is
        worse than the bug it replaced:

        - ``"cursored"``  — paging works.
        - ``"single-page"`` — the control was never present across every
          attempt, so the mailbox plausibly has one page.
        - ``"unconfirmed"`` — the control WAS found and clicked, yet no
          cursor-bearing request followed. Something went wrong; this must not
          be mistaken for a short mailbox.

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

            control_seen = False
            for _ in range(6):
                if cursored:
                    break
                await self._scroll_conversation_list()
                button = await self._load_more_control()
                if button is None:
                    await self._session.delay(1.5)
                    continue
                control_seen = True
                try:
                    await button.click(timeout=5000)
                except PlaywrightTimeoutError:
                    break
                await self._session.delay(2.5)
        finally:
            page.remove_listener("request", _capture)

        if cursored:
            return _drop_cursor(cursored[-1]), "cursored"
        if page_load:
            return page_load[-1], "unconfirmed" if control_seen else "single-page"
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

    def _forward_cursor(
        self, payload: dict[str, Any], supplied: str | None
    ) -> str | None:
        """The next cursor, unless the server just handed back the current one."""
        cursor = self._next_cursor(payload)
        if cursor is not None and supplied is not None and cursor == supplied.strip():
            logger.warning("Conversations cursor repeated; withholding it.")
            return None
        return cursor

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

    @staticmethod
    def render_page_text(conversations: list[dict[str, Any]]) -> str:
        """Render a page as readable text.

        Exists to satisfy the repository contract that every scraping tool
        returns `{url, sections: {name: raw_text}}`, so a generic consumer can
        treat this tool like the others. The structured `conversations` list is
        the richer answer and is returned alongside it, not instead of it.
        """
        lines: list[str] = ["Conversations"]
        for conversation in conversations:
            who = ", ".join(conversation.get("participants") or []) or (
                conversation.get("title") or "Unknown"
            )
            when = conversation.get("last_activity_iso") or ""
            unread = conversation.get("unread_count") or 0
            flag = f" [{unread} unread]" if unread else ""
            lines.append(f"{who} - {when}{flag}".rstrip(" -").rstrip())

            headlines = conversation.get("headlines") or []
            if headlines:
                lines.append(f"    {headlines[0]}")

            text = (conversation.get("last_message_text") or "").replace("\n", " ")
            if text.strip():
                speaker = "You: " if conversation.get("last_message_from_me") else ""
                lines.append(f"    {speaker}{text.strip()[:200]}")
            if conversation.get("awaiting_my_reply"):
                lines.append("    >> awaiting your reply")
        return "\n".join(lines)

    async def _page_url(self, cursor: str | None, category: str | None) -> str:
        """Discover, validate and assemble the URL for one page.

        Both the first attempt and the post-rediscovery retry go through here.
        They used to assemble it separately, and the retry silently omitted the
        paging check -- so a rediscovery that came back "single-page" would have
        a cursor inserted into a query that ignores it, handing the caller page
        one as though it were page five: duplicated conversations and a mailbox
        traversal that stops early. Two call sites, one of them forgetting a
        rule, is the shape of that bug; one call site cannot have it.
        """
        url, paging = await self._discover_query()

        if paging == "unconfirmed":
            raise LinkedInScraperException(
                "The paging control was present and clicked, but no "
                "cursor-bearing conversations query followed, so only the first "
                "page is reachable and there is no way to tell a short mailbox "
                "from a failed one. Refusing to return a partial listing that "
                "would look complete."
            )
        if cursor and paging != "cursored":
            raise LinkedInScraperException(
                "A cursor was supplied but no paging query is available for "
                "this mailbox, so it cannot be honoured. Call without one."
            )

        url = _COUNT_RE.sub(lambda _: f"count:{PAGE_SIZE}", url)
        if category:
            normalized = category.strip().upper()
            if normalized not in KNOWN_CATEGORIES:
                # An unrecognised category returns an empty page rather than an
                # error, which would read as "you have none of those".
                raise LinkedInScraperException(
                    f"Unknown category {normalized!r}. Known: "
                    f"{', '.join(sorted(KNOWN_CATEGORIES))}. An unrecognised "
                    "category returns an empty result rather than an error, so "
                    "it is rejected here instead of silently reading as zero."
                )
            url = _CATEGORY_RE.sub(lambda _: f"category:{normalized}", url)
        return _set_cursor(url, cursor.strip()) if cursor else _drop_cursor(url)

    async def get_conversations(
        self,
        cursor: str | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        """Read ONE page of conversations. The caller decides whether to continue.

        Deliberately not a walk. An earlier version took a `limit` and paged
        internally, and every defect found in review lived in that machinery:
        slicing a page to `limit` while the cursor advanced past the remainder,
        an exhaustion probe that overfetched the same way, and an inference
        about where the mailbox ended. A single page has none of those, because
        the page IS the answer.

        `at_end` is measured, never inferred, by comparing what came back
        against what was asked for:

            fewer than PAGE_SIZE -> True.  The server had no more.
            exactly PAGE_SIZE    -> False. There is more; use `next_cursor`.
            nothing at all       -> None.  Says nothing either way.

        The agent calling this is already a loop. It does not need a second one
        hidden inside a tool call.
        """
        if category is not None and not category.strip():
            raise LinkedInScraperException(
                "category was blank. Pass None for no filter, or one of: "
                f"{', '.join(sorted(KNOWN_CATEGORIES))}."
            )
        if cursor is not None and not cursor.strip():
            raise LinkedInScraperException(
                "cursor was blank. Pass None for the first page, or a "
                "next_cursor from a previous call."
            )

        me = self._me_profile_id((await self._discover_query())[0])
        url = await self._page_url(cursor, category)

        try:
            payload = await self._fetch(url)
        except (AuthenticationError, RateLimitError):
            raise
        except LinkedInScraperException:
            # A cached query outlives the deploy that issued it: LinkedIn
            # rotates the persisted queryId and every later call fails against a
            # hash that no longer exists. Without this the session stays broken
            # until the server restarts. Rediscover once and retry, but only
            # when a cache was actually in play -- otherwise a genuinely broken
            # request would be retried forever.
            if _cached_query(self._session.page) is None:
                raise
            logger.info("Conversations query failed; rediscovering once.")
            forget_cached_query()
            url = await self._page_url(cursor, category)
            payload = await self._fetch(url)

        rows = self._conversations(payload)

        # Entities present but none parsed as a Conversation is a shape change,
        # which is the one failure that silently turns a full page into a zero.
        if not rows and payload.get("included"):
            raise LinkedInScraperException(
                "Conversations payload changed shape: "
                f"{len(payload['included'])} included entities but zero parsed "
                "as Conversation. Refusing to report this as an empty page."
            )

        people = self._participants(payload)
        messages = self._messages_by_conversation(payload)
        conversations = [
            self._normalize(row, people, messages.get(row.get("entityUrn", "")), me)
            for row in rows
        ]

        zero_reason: str | None = None
        if not rows:
            # A zero is never returned bare. On the first page a positive
            # control decides whether the instrument works at all; with a cursor
            # it may simply be the page after the last one.
            zero_reason = "after-cursor" if cursor else await self._diagnose_empty(url)
            if zero_reason not in {"verified-empty", "after-cursor"}:
                raise LinkedInScraperException(
                    f"An empty page came back and the positive control did not "
                    f"clear it ({zero_reason}). Treating this as an instrument "
                    "failure rather than an empty mailbox."
                )

        return {
            # `url` and `sections` keep this tool's result shape consistent with
            # every other scraping tool, per AGENTS.md. `conversations` is the
            # structured answer and is the one worth reading.
            "url": MESSAGING_URL,
            "sections": {"conversations": self.render_page_text(conversations)},
            "conversations": conversations,
            "count": len(conversations),
            "page_size": PAGE_SIZE,
            # Pass back as `cursor` to read the next page. None when the server
            # offered none, which for a full page means paging is unavailable
            # rather than that the mailbox ended.
            #
            # A cursor identical to the one supplied is the server re-serving
            # the page just read. Handing it back would invite the caller into
            # an endless loop, and the caller cannot easily notice: each page
            # looks perfectly valid on its own. Withheld instead.
            "next_cursor": self._forward_cursor(payload, cursor),
            # True only when the server returned fewer rows than asked for.
            # None means an empty page, which proves nothing either way and
            # must never be read as the end.
            "at_end": None if not rows else len(rows) < PAGE_SIZE,
            "zero_reason": zero_reason,
        }
