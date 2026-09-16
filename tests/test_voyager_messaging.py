"""Tests for the Voyager conversations walk.

Every defect this module can have shows up as an empty page, which is also
what a legitimately empty mailbox looks like. So most of these tests are about
one question: does a zero come back labelled, or does it come back bare?
"""

from __future__ import annotations

import time
import pytest

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
    RateLimitError,
)
from linkedin_mcp_server.scraping.voyager_messaging import (
    KNOWN_CATEGORIES,
    PAGE_SIZE,
    VoyagerMessagingReader,
)

ME = "ACoAAme"
QUERY_URL = (
    "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql"
    "?queryId=messengerConversations.deadbeef&variables="
    "(query:(predicateUnions:List((conversationCategoryPredicate:"
    "(category:PRIMARY_INBOX)))),count:20,"
    f"mailboxUrn:urn:li:fsd_profile:{ME},nextCursor:SEED)"
)


def _conversation(urn: str, *, last_activity: int, participants: list[str]) -> dict:
    return {
        "$type": "com.linkedin.messenger.Conversation",
        "entityUrn": urn,
        "conversationUrl": f"/messaging/thread/{urn}/",
        "lastActivityAt": last_activity,
        "unreadCount": 0,
        "categories": ["INBOX"],
        "*conversationParticipants": participants,
    }


def _participant(urn: str, first: str, last: str) -> dict:
    return {
        "$type": "com.linkedin.messenger.MessagingParticipant",
        "entityUrn": urn,
        "participantType": {
            "member": {
                "firstName": {"text": first},
                "lastName": {"text": last},
                "headline": {"text": f"{first} headline"},
            }
        },
    }


def _message(conversation: str, sender: str, text: str, delivered: int) -> dict:
    return {
        "$type": "com.linkedin.messenger.Message",
        "*conversation": conversation,
        "*sender": sender,
        "body": {"text": text},
        "deliveredAt": delivered,
    }


def _payload(conversations: list[dict], cursor: str | None, included=()) -> dict:
    return {
        "data": {
            "data": {
                "messengerConversationsByCategoryQuery": {
                    "metadata": ({"nextCursor": cursor} if cursor else {}),
                    "*elements": [c["entityUrn"] for c in conversations],
                }
            }
        },
        "included": list(conversations) + list(included),
    }


class _FakeSession:
    """Only the inter-page pacing is exercised here; no browser is involved."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def delay(self, seconds: float) -> None:
        self.delays.append(seconds)


class _Reader(VoyagerMessagingReader):
    """Reader with the browser replaced by a scripted list of payloads."""

    def __init__(self, pages: list[dict]):
        super().__init__(session=_FakeSession(), navigator=None)
        self._pages = pages
        self.fetched: list[str] = []
        self.discoveries = 0

    paging_state = "cursored"

    async def _discover_query_uncached(self) -> tuple[str, str]:  # type: ignore[override]
        # Overriding the UNCACHED variant on purpose: the cache lives in
        # `_discover_query`, so replacing that would test the double instead of
        # the code.
        self.discoveries += 1
        return QUERY_URL, self.paging_state

    async def _fetch(self, url: str) -> dict:  # type: ignore[override]
        self.fetched.append(url)
        return self._pages[min(len(self.fetched) - 1, len(self._pages) - 1)]


def _rows(n: int) -> list[dict]:
    return [
        _conversation(f"c{i}", last_activity=1000 - i, participants=[])
        for i in range(n)
    ]


class TestBlankInputs:
    """A blank argument must never reach LinkedIn and come back as a zero."""

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    async def test_blank_category_is_rejected(self, blank):
        reader = _Reader([_payload([], None)])
        with pytest.raises(LinkedInScraperException, match="category was blank"):
            await reader.get_conversations(category=blank)
        assert reader.fetched == [], "a blank must not cost a request"

    @pytest.mark.parametrize("blank", ["", "  "])
    async def test_blank_cursor_is_rejected(self, blank):
        reader = _Reader([_payload([], None)])
        with pytest.raises(LinkedInScraperException, match="cursor was blank"):
            await reader.get_conversations(cursor=blank)

    async def test_unknown_category_is_rejected_before_the_request(self):
        """An unknown category returns an empty page from LinkedIn, so it must
        be refused here rather than read back as 'you have none of those'."""
        reader = _Reader([_payload([], None)])
        with pytest.raises(LinkedInScraperException, match="Unknown category"):
            await reader.get_conversations(category="UNREAD")
        assert reader.fetched == []

    async def test_every_known_category_is_accepted(self):
        for category in KNOWN_CATEGORIES:
            reader = _Reader([_payload(_rows(1), None)])
            result = await reader.get_conversations(category=category)
            assert result["count"] == 1
            assert f"category:{category}" in reader.fetched[0]


class TestAtEndIsMeasuredNotInferred:
    """Taylor's three rules, which are the whole algorithm now."""

    async def test_fewer_than_a_full_page_is_the_end(self):
        reader = _Reader([_payload(_rows(23), None)])
        result = await reader.get_conversations()
        assert result["count"] == 23
        assert result["at_end"] is True

    async def test_a_full_page_is_not_the_end(self):
        reader = _Reader([_payload(_rows(PAGE_SIZE), "NEXT")])
        result = await reader.get_conversations()
        assert result["count"] == PAGE_SIZE
        assert result["at_end"] is False
        assert result["next_cursor"] == "NEXT"

    async def test_an_empty_page_after_a_cursor_is_unproven(self):
        reader = _Reader([_payload([], None)])
        result = await reader.get_conversations(cursor="SEED")
        assert result["count"] == 0
        assert result["at_end"] is None, "zero proves nothing either way"
        assert result["zero_reason"] == "after-cursor"

    async def test_always_asks_for_a_full_page(self):
        """A fixed page size is what lets 'fewer than asked for' mean 'the end'."""
        reader = _Reader([_payload(_rows(PAGE_SIZE), None)])
        await reader.get_conversations()
        assert f"count:{PAGE_SIZE}" in reader.fetched[0]


class TestEmptyFirstPageIsNeverBare:
    async def test_empty_with_passing_control_is_verified_empty(self):
        control = _payload(_rows(1), None)
        reader = _Reader([_payload([], None), control])
        result = await reader.get_conversations()
        assert result["count"] == 0
        assert result["zero_reason"] == "verified-empty"
        assert result["at_end"] is None

    async def test_empty_with_failing_control_raises(self):
        reader = _Reader([_payload([], None), _payload([], None)])
        with pytest.raises(LinkedInScraperException, match="positive control"):
            await reader.get_conversations()

    async def test_included_entities_but_no_conversations_is_a_parse_failure(self):
        payload = _payload([], None, included=[{"$type": "x.Y", "entityUrn": "u"}])
        reader = _Reader([payload])
        with pytest.raises(LinkedInScraperException, match="changed shape"):
            await reader.get_conversations()


class TestDiscoveryIsPaidOnce:
    """Discovery costs a navigation, a sidebar wait and a click. Paying it per
    page would make a caller-driven loop unusable."""

    async def test_repeated_calls_reuse_the_discovered_query(self):
        reader = _Reader([_payload(_rows(PAGE_SIZE), "A")])
        await reader.get_conversations()
        await reader.get_conversations(cursor="A")
        await reader.get_conversations(cursor="B")
        assert reader.discoveries == 1, "discovery must be cached for the session"
        assert len(reader.fetched) == 3, "but every page is still fetched"

    async def test_a_cursor_against_a_single_page_mailbox_is_refused(self):
        reader = _Reader([_payload(_rows(1), None)])
        reader.paging_state = "single-page"
        with pytest.raises(LinkedInScraperException, match="cannot be honoured"):
            await reader.get_conversations(cursor="SEED")


class TestReplyState:
    """`awaiting_my_reply` is the field the whole feature turns on."""

    def _reader_with_message(self, sender_profile: str):
        me_participant = f"urn:li:msg_messagingParticipant:urn:li:fsd_profile:{ME}"
        them = "urn:li:msg_messagingParticipant:urn:li:fsd_profile:ACoAthem"
        conv = _conversation("c1", last_activity=5, participants=[me_participant, them])
        included = [
            _participant(me_participant, "Taylor", "Medford"),
            _participant(them, "Dana", "Scully"),
            _message("c1", sender_profile, "hello there", 5),
        ]
        return _Reader([_payload([conv], None, included=included)])

    async def test_their_message_last_means_a_reply_is_owed(self):
        reader = self._reader_with_message(
            "urn:li:msg_messagingParticipant:urn:li:fsd_profile:ACoAthem"
        )
        c = (await reader.get_conversations())["conversations"][0]
        assert c["last_message_from_me"] is False
        assert c["awaiting_my_reply"] is True
        assert c["last_message_text"] == "hello there"

    async def test_my_message_last_means_no_reply_owed(self):
        reader = self._reader_with_message(
            f"urn:li:msg_messagingParticipant:urn:li:fsd_profile:{ME}"
        )
        c = (await reader.get_conversations())["conversations"][0]
        assert c["last_message_from_me"] is True
        assert c["awaiting_my_reply"] is False

    async def test_unknown_sender_is_none_not_false(self):
        """A guess in either direction is costly: False invents an owed reply,
        True hides one. Absent is the honest answer."""
        conv = _conversation("c1", last_activity=5, participants=[])
        reader = _Reader([_payload([conv], None)])
        c = (await reader.get_conversations())["conversations"][0]
        assert c["last_message_from_me"] is None
        assert c["awaiting_my_reply"] is None

    async def test_the_mailbox_owner_is_not_listed_as_a_participant(self):
        reader = self._reader_with_message(
            "urn:li:msg_messagingParticipant:urn:li:fsd_profile:ACoAthem"
        )
        c = (await reader.get_conversations())["conversations"][0]
        assert c["participants"] == ["Dana Scully"], "self must be excluded"


class TestReviewRegressions:
    """One test per defect found in review. A fix without a test is a hope."""

    async def test_cursor_with_backslash_is_inserted_literally(self):
        """re.sub interprets backslash escapes in a REPLACEMENT TEMPLATE, so a
        cursor containing one would raise re.error or become a backreference.
        LinkedIn's cursors are opaque base64, so this must be inserted verbatim."""
        reader = _Reader(
            [_payload([_conversation("c1", last_activity=1, participants=[])], None)]
        )
        nasty = r"ABC\g<0>\1DEF"
        await reader.get_conversations(cursor=nasty)
        assert f"nextCursor:{nasty}" in reader.fetched[0]

    async def test_positive_control_drops_the_cursor_rather_than_blanking_it(self):
        """An empty `nextCursor:` is a MALFORMED request. If the control were
        built that way, its empty response would be misread as a broken
        instrument and a genuinely empty mailbox would raise."""
        control = _payload(
            [_conversation("ctl", last_activity=1, participants=[])], None
        )
        reader = _Reader([_payload([], None), control])
        result = await reader.get_conversations()
        assert result["zero_reason"] == "verified-empty"
        control_url = reader.fetched[-1]
        assert "nextCursor:" not in control_url, "control must be cursorless"
        assert "count:1" in control_url

    @pytest.mark.parametrize(
        "status, expected",
        [(401, AuthenticationError), (403, AuthenticationError), (429, RateLimitError)],
    )
    async def test_auth_and_rate_limit_keep_their_own_types(self, status, expected):
        """Exercises the REAL _fetch. Collapsing these into a generic error lets
        `auto` fall back to the DOM path, which CLICKS rows and marks them read,
        so a transient failure would cause a write."""

        class _Page:
            async def evaluate(self, _script, _url):
                return {"error": f"HTTP {status}", "status": status}

        class _SessionWithPage(_FakeSession):
            page = _Page()

        # The REAL class, not the scripted _Reader, so _fetch's own error
        # handling is what runs.
        reader = VoyagerMessagingReader(session=_SessionWithPage(), navigator=None)
        with pytest.raises(expected):
            await reader._fetch(QUERY_URL)

    async def test_generic_http_failure_stays_a_scraper_exception(self):
        """Only auth and throttling are special-cased; everything else keeps the
        generic type so `auto` may still fall back."""

        class _Page:
            async def evaluate(self, _script, _url):
                return {"error": "HTTP 500", "status": 500}

        class _SessionWithPage(_FakeSession):
            page = _Page()

        reader = VoyagerMessagingReader(session=_SessionWithPage(), navigator=None)
        with pytest.raises(LinkedInScraperException) as excinfo:
            await reader._fetch(QUERY_URL)
        assert not isinstance(excinfo.value, (AuthenticationError, RateLimitError))


class TestDiscovery:
    """The two P1 bugs review found in discovery, each pinned."""

    def _reader_with_requests(self, urls: list[str]):
        """A reader whose page replays a scripted set of observed requests."""

        class _Req:
            def __init__(self, url):
                self.url = url

        class _Locator:
            async def count(self):
                return 0

        class _Page:
            def __init__(self):
                self.listeners = []

            def on(self, _event, cb):
                self.listeners.append(cb)

            def remove_listener(self, _event, cb):
                self.listeners.remove(cb)

            async def wait_for_selector(self, *a, **k):
                for cb in list(self.listeners):
                    for u in urls:
                        cb(_Req(u))

            async def evaluate(self, *a, **k):
                return None

            def get_by_role(self, *a, **k):
                return _Locator()

            def locator(self, *a, **k):
                return _Locator()

        class _Session(_FakeSession):
            def __init__(self):
                super().__init__()
                self.page = _Page()

            async def check_rate_limit(self):
                return None

        class _Nav:
            async def _navigate_to_page(self, _url):
                return None

        return VoyagerMessagingReader(session=_Session(), navigator=_Nav())

    async def test_a_fresh_walk_starts_at_the_first_page(self):
        """Discovery observes the cursor-bearing request the load-more control
        issues, and that cursor points PAST page one. Returning it unchanged
        made every fresh walk silently skip the newest conversations."""
        reader = self._reader_with_requests(
            [
                f"{QUERY_URL.split(',nextCursor')[0]})",
                QUERY_URL,
            ]
        )
        url, state = await reader._discover_query()
        assert state == "cursored"
        assert "nextCursor" not in url, "a fresh walk must not inherit a cursor"

    async def test_a_single_page_mailbox_is_not_an_error(self):
        """With one page there is no load-more control and no cursor-bearing
        request ever fires. Requiring one made a valid mailbox raise."""
        page_load = f"{QUERY_URL.split(',nextCursor')[0]})"
        reader = self._reader_with_requests([page_load])
        url, state = await reader._discover_query()
        assert state == "single-page"
        assert "nextCursor" not in url

    async def test_no_conversations_request_at_all_still_raises(self):
        """The genuinely broken case must stay distinguishable from the two
        above, rather than being swallowed by the new fallback."""
        reader = self._reader_with_requests([])
        with pytest.raises(LinkedInScraperException, match="No messengerConversations"):
            await reader._discover_query()


class TestTimestampsAreHostIndependent:
    """A local-time render made the same mailbox produce different output on a
    laptop and on CI, which is how the original bug was found."""

    EPOCH_MS = 1_700_000_000_000
    EXPECTED = "2023-11-14T22:13+00:00"

    async def test_the_rendered_timestamp_is_utc(self):
        """Runs everywhere, including Windows. On a UTC machine this alone
        would not have caught the original bug, which is why the varying test
        below exists as well."""
        conv = _conversation("c1", last_activity=self.EPOCH_MS, participants=[])
        reader = _Reader([_payload([conv], None)])
        result = await reader.get_conversations()
        assert result["conversations"][0]["last_activity_iso"] == self.EXPECTED

    @pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is Unix-only")
    async def test_the_render_does_not_move_with_the_machine_timezone(
        self, monkeypatch
    ):
        """The discriminating test: three zones, one expected answer.

        monkeypatch rather than manual os.environ juggling so the process zone
        is restored even when an assertion fails partway through -- a leaked TZ
        would silently colour every later test in the session.
        """
        conv = _conversation("c1", last_activity=self.EPOCH_MS, participants=[])
        renders = []
        for zone in ("UTC", "America/New_York", "Asia/Tokyo"):
            monkeypatch.setenv("TZ", zone)
            time.tzset()
            reader = _Reader([_payload([conv], None)])
            result = await reader.get_conversations()
            renders.append(result["conversations"][0]["last_activity_iso"])
        monkeypatch.undo()
        time.tzset()

        assert renders == [self.EXPECTED] * 3, renders


class TestCursorRepeat:
    async def test_a_repeated_cursor_is_withheld(self):
        """The caller is the loop now, and it cannot easily notice a server
        re-serving the same page: each page looks valid on its own."""
        reader = _Reader([_payload(_rows(PAGE_SIZE), "SAME")])
        result = await reader.get_conversations(cursor="SAME")
        assert result["next_cursor"] is None, "must not invite an endless loop"

    async def test_a_new_cursor_is_passed_through(self):
        reader = _Reader([_payload(_rows(PAGE_SIZE), "NEXT")])
        result = await reader.get_conversations(cursor="PREV")
        assert result["next_cursor"] == "NEXT"
