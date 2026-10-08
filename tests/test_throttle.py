"""What the server can say about a throttled LinkedIn.

Every case here starts from a response object shaped like Playwright's, because
the status is the whole point: nothing else in the server reads one.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.core.throttle import (
    reset_throttle_record,
    throttle_evidence,
    throttled_count,
    watch_responses,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.exceptions import SessionExpiredError


def response(
    status: int,
    url: str = "https://www.linkedin.com/messaging/thread/2-abc/",
    *,
    headers: dict[str, str] | None = None,
) -> SimpleNamespace:
    """A stand-in for the Playwright ``Response`` handed to the listener."""
    return SimpleNamespace(
        status=status, url=url, headers=headers or {}, request=Request()
    )


class Request:
    """A stand-in for a Playwright ``Request``, weak-referenceable like one."""


def handlers() -> dict[str, Callable[[Any], None]]:
    """Watch a fresh page and return its listeners by event name."""
    page = MagicMock()
    watch_responses(page)
    return {call.args[0]: call.args[1] for call in page.on.call_args_list}


def record(*responses: SimpleNamespace) -> None:
    """Feed requests and their responses to the listeners as Playwright would."""
    listeners = handlers()
    for item in responses:
        listeners["request"](item.request)
        listeners["response"](item)


class TestWhatTheListenerRecords:
    def test_a_refused_request_inside_a_served_page_is_counted(self):
        # The messaging page's own fetch. The page itself was served; only its
        # content was refused, which is what no other check here can see.
        record(
            response(200, "https://www.linkedin.com/messaging/"),
            response(429, "https://www.linkedin.com/voyager/api/messaging/x"),
        )

        assert throttled_count() == 1

    def test_linkedins_own_refusal_code_counts_too(self):
        # 999 is LinkedIn's "Request denied", the same answer under a name
        # only it uses. A record that knows 429 alone misses half of them.
        record(response(999, "https://www.linkedin.com/voyager/api/messaging/x"))

        assert throttled_count() == 1

    def test_a_served_response_records_nothing(self):
        record(response(200, "https://www.linkedin.com/voyager/api/messaging/x"))

        assert throttle_evidence() is None
        assert throttled_count() == 0

    def test_a_retry_after_date_is_left_unread(self):
        # Parsing an HTTP-date against a clock that may be wrong is worse than
        # answering with the default wait.
        record(
            response(
                429,
                "https://www.linkedin.com/feed/",
                headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"},
            )
        )

        evidence = throttle_evidence()
        assert evidence is not None and "asked for" not in evidence

    def test_a_response_it_cannot_read_is_dropped_rather_than_raised(self):
        # The listener runs inside Playwright's event dispatch. An exception
        # here surfaces nowhere useful and can take the page's event loop with
        # it; a service-worker response raises on `.frame` alone.
        broken = MagicMock()
        type(broken).status = property(lambda self: (_ for _ in ()).throw(RuntimeError))

        record(broken)

        assert throttled_count() == 0

    def test_the_record_is_emptied_for_the_next_call(self):
        record(response(429, "https://www.linkedin.com/feed/"))

        reset_throttle_record()

        assert throttle_evidence() is None
        assert throttled_count() == 0

    def test_one_page_is_watched_once(self):
        page = MagicMock()
        watch_responses(page)
        watch_responses(page)

        assert [call.args[0] for call in page.on.call_args_list] == [
            "request",
            "response",
        ]

    def test_another_hosts_refusal_is_not_linkedins(self):
        # A proxy or captive portal answering 429 is not LinkedIn asking for a
        # wait, and the sentence would say it was.
        record(
            response(429, "https://portal.example/blocked"),
            response(429, "https://linkedin.com.evil.test/voyager/api/x"),
        )

        assert throttled_count() == 0

    def test_a_request_the_previous_call_sent_is_not_this_calls(self):
        # Answered after the next call reset the record. Without the tag, the
        # previous call's refusal would explain this call's failure.
        listeners = handlers()
        late = response(429, "https://www.linkedin.com/voyager/api/messaging/x")
        listeners["request"](late.request)

        reset_throttle_record()
        listeners["response"](late)

        assert throttled_count() == 0
        assert throttle_evidence() is None


class TestTheEvidenceSentence:
    def test_it_names_the_status_the_count_and_the_path(self):
        record(
            response(429, "https://www.linkedin.com/voyager/api/messaging/a?q=1"),
            response(429, "https://www.linkedin.com/voyager/api/messaging/b?q=2"),
        )

        evidence = throttle_evidence()
        assert evidence is not None
        assert "HTTP 429" in evidence
        assert "2 requests" in evidence
        assert "/voyager/api/messaging/b" in evidence

    def test_it_leaves_the_query_out(self):
        record(response(429, "https://www.linkedin.com/voyager/api/x?keywords=secret"))

        evidence = throttle_evidence()
        assert evidence is not None and "secret" not in evidence

    def test_it_repeats_what_linkedin_asked_for(self):
        record(
            response(
                429,
                "https://www.linkedin.com/voyager/api/x",
                headers={"retry-after": "60"},
            )
        )

        evidence = throttle_evidence()
        assert evidence is not None and "60 seconds" in evidence

    def test_the_most_recent_refusal_is_named_past_the_sample_cap(self):
        # The sample stops growing at twenty; the sentence must not freeze on
        # the twentieth refusal while the count goes on.
        record(
            *(
                response(429, f"https://www.linkedin.com/voyager/api/old{n}")
                for n in range(30)
            ),
            response(429, "https://www.linkedin.com/voyager/api/latest"),
        )

        evidence = throttle_evidence()
        assert evidence is not None
        assert "31 requests" in evidence
        assert "/voyager/api/latest" in evidence

    def test_a_status_first_seen_past_the_sample_cap_is_named(self):
        record(
            *(
                response(429, f"https://www.linkedin.com/voyager/api/old{n}")
                for n in range(25)
            ),
            response(999, "https://www.linkedin.com/voyager/api/latest"),
        )

        evidence = throttle_evidence()
        assert evidence is not None and "HTTP 429 and HTTP 999" in evidence


class TestTheClientIsToldAboutThrottling:
    def test_an_unclassified_failure_is_no_longer_masked_to_nothing(self):
        # The reported failure: mask_error_details turns anything that is not
        # a ToolError into "Error calling tool", and the 429 the browser saw
        # reached the client as nothing at all.
        record(response(429, "https://www.linkedin.com/voyager/api/messaging/x"))

        with pytest.raises(ToolError, match="HTTP 429"):
            raise_tool_error(
                RuntimeError("list index out of range"), "get_conversation"
            )

    def test_an_unclassified_failure_is_still_masked_without_throttling(self):
        with pytest.raises(RuntimeError):
            raise_tool_error(
                RuntimeError("list index out of range"), "get_conversation"
            )

    def test_a_shaped_message_keeps_its_own_words(self):
        record(response(429, "https://www.linkedin.com/voyager/api/messaging/x"))

        with pytest.raises(ToolError) as caught:
            raise_tool_error(SessionExpiredError(), "get_conversation")

        assert "Session expired" in str(caught.value)
        assert "HTTP 429" in str(caught.value)

    def test_the_evidence_is_not_repeated_when_a_tool_wraps_twice(self):
        # 16 of the 18 tool catch sites hand the ToolError back through here.
        record(response(429, "https://www.linkedin.com/voyager/api/messaging/x"))

        with pytest.raises(ToolError) as caught:
            try:
                raise_tool_error(SessionExpiredError(), "get_conversation")
            except ToolError as shaped:
                raise_tool_error(shaped, "get_conversation")

        assert str(caught.value).count("HTTP 429") == 1

    def test_the_cause_still_reaches_a_middleware_in_one_hop(self):
        # The chain the daemon's auth signal is classified by. Appending
        # evidence must not add a link to it.
        record(response(429, "https://www.linkedin.com/voyager/api/messaging/x"))

        with pytest.raises(ToolError) as caught:
            raise_tool_error(SessionExpiredError(), "get_conversation")

        chain: list[type] = []
        current: BaseException | None = caught.value
        while current is not None:
            chain.append(type(current))
            current = current.__cause__
        assert chain == [ToolError, SessionExpiredError]

    def test_an_unclassified_failure_keeps_its_credentials_redacted(self, monkeypatch):
        # The catch-all redacts a proxy password before FastMCP logs the
        # exception with its traceback. Turning the failure into a ToolError
        # must not reach back past that to the original.
        from linkedin_mcp_server.config.schema import AppConfig

        # Built here rather than written out: a rendered traceback quotes the
        # source line that raised, and a literal there would match by itself.
        user, secret = "acct" + "zone9", "s3" + "cr3t"
        config = AppConfig()
        config.browser.proxy_server = "http://gate.example:7000"
        config.browser.proxy_username = user
        config.browser.proxy_password = secret
        monkeypatch.setattr("linkedin_mcp_server.config.get_config", lambda: config)
        record(response(429, "https://www.linkedin.com/voyager/api/messaging/x"))

        with pytest.raises(ToolError) as caught:
            raise_tool_error(
                RuntimeError(f"failed via http://{user}:{secret}@gate.example:7000"),
                "get_conversation",
            )

        rendered = "".join(
            traceback.format_exception(
                type(caught.value), caught.value, caught.value.__traceback__
            )
        )
        assert "HTTP 429" in rendered
        assert secret not in rendered
        assert isinstance(caught.value.__cause__, RuntimeError)


class TestTheRecordBelongsToOneCall:
    async def test_a_call_starts_with_the_previous_calls_record_cleared(
        self, monkeypatch
    ):
        """Otherwise yesterday's throttling explains today's failure.

        Nothing else clears it: the listener lives as long as the page, and a
        server holds one page for its whole life.
        """
        from unittest.mock import AsyncMock

        from linkedin_mcp_server.sequential_tool_middleware import (
            SequentialToolExecutionMiddleware,
        )

        record(response(429, "https://www.linkedin.com/voyager/api/messaging/x"))

        lease = MagicMock()
        lease.try_acquire.return_value = True
        monkeypatch.setattr(
            "linkedin_mcp_server.sequential_tool_middleware.get_profile_lease",
            lambda: lease,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.drivers.browser.note_call_started", lambda: None
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.drivers.browser.note_activity", lambda: None
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.drivers.browser.release_profile_if_idle_or_requested",
            AsyncMock(),
        )

        seen: dict[str, str | None] = {}

        async def call_next(context):
            seen["evidence"] = throttle_evidence()
            return "ok"

        context = MagicMock()
        context.message.name = "get_conversation"
        context.fastmcp_context = None

        await SequentialToolExecutionMiddleware().on_call_tool(context, call_next)

        assert seen["evidence"] is None
