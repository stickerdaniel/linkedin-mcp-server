"""Tests for the section contracts every page workflow returns."""

from typing import Any

import pytest

from linkedin_mcp_server.linkedin import contracts
from linkedin_mcp_server.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    SEND_INTERRUPTED_WARNING,
    ExtractedSection,
    FilterValidationError,
    message_action_result,
    normalize_message_text,
    rate_limited_section_error,
    refuse_an_invalid_message,
)


class TestRateLimitedSection:
    def test_the_sentinel_text_is_what_reaches_the_client(self):
        # Pinned as a literal on purpose. Every other assertion in the suite
        # compares a result against this same constant, so it moves with any
        # edit and none of them can see the message a client would read.
        assert RATE_LIMITED_SECTION_TEXT == (
            "[Rate limited] LinkedIn blocked this section. "
            "Try again later or request fewer sections."
        )

    def test_the_reported_error_repeats_the_sentinel_verbatim(self):
        # The tools compare a section's text against the sentinel and then
        # report this error, so the two drifting apart would describe a
        # section the caller never saw.
        assert rate_limited_section_error() == {
            "error_type": "rate_limit",
            "error_message": RATE_LIMITED_SECTION_TEXT,
        }


class TestExtractedSection:
    def test_a_section_without_an_error_carries_none(self):
        section = ExtractedSection(text="Bill Gates", references=[])

        assert section.error is None

    def test_an_error_is_kept_beside_the_text(self):
        section = ExtractedSection(
            text="", references=[], error=rate_limited_section_error()
        )

        assert section.text == ""
        assert section.error == rate_limited_section_error()


class TestFilterValidationError:
    def test_it_is_still_a_value_error(self):
        # Direct extractor callers catch ValueError; the tool wrappers catch
        # this subclass to surface the message past mask_error_details.
        assert issubclass(FilterValidationError, ValueError)


class TestMessageActionResult:
    def test_the_retry_contract_is_explicit_on_every_result(self):
        assert message_action_result(
            "https://www.linkedin.com/messaging/compose/",
            "sent",
            "Message submitted.",
            recipient_selected=True,
            sent=True,
            retry_safe=False,
        ) == {
            "url": "https://www.linkedin.com/messaging/compose/",
            "status": "sent",
            "message": "Message submitted.",
            "recipient_selected": True,
            "sent": True,
            "retry_safe": False,
        }

    def test_thread_id_is_present_only_when_the_caller_passes_it(self):
        omitted = message_action_result(
            "https://www.linkedin.com/messaging/compose/",
            "confirmation_required",
            "Set confirm_send=true to send the message.",
        )
        assert "thread_id" not in omitted

        named = message_action_result(
            "https://www.linkedin.com/messaging/thread/2-abc==/",
            "sent",
            "Message submitted.",
            recipient_selected=True,
            sent=True,
            retry_safe=False,
            thread_id="2-abc==",
        )
        assert named["thread_id"] == "2-abc=="
        assert set(named) == {
            "url",
            "status",
            "message",
            "recipient_selected",
            "sent",
            "retry_safe",
            "thread_id",
        }

        stayed = message_action_result(
            "https://www.linkedin.com/messaging/compose/",
            "sent",
            "Message submitted.",
            sent=True,
            retry_safe=False,
            thread_id=None,
        )
        assert "thread_id" in stayed
        assert stayed["thread_id"] is None

    def test_the_interruption_warning_names_duplicate_delivery(self):
        assert SEND_INTERRUPTED_WARNING == (
            "Message submission was interrupted while in flight. The send outcome "
            "is unknown; check the conversation before retrying, as a retry may "
            "deliver the message twice."
        )


class TestRefuseAnInvalidMessage:
    @pytest.mark.parametrize(
        "message",
        [
            "before\tafter",
            "line\n\t\nnext",
            "\t\nlater",
            "earlier\n\t",
            "ring\x07",
            "text\x7f",
            "next\x85line",
            "line\u2028separator",
            "paragraph\u2029separator",
        ],
        ids=[
            "tab-inside",
            "tab-only-interior-line",
            "tab-only-leading-line",
            "tab-only-trailing-line",
            "bel",
            "del",
            "U+0085",
            "U+2028",
            "U+2029",
        ],
    )
    def test_controls_and_other_separators_are_refused(self, message: str):
        assert refuse_an_invalid_message("alice", message) == message_action_result(
            "https://www.linkedin.com/in/alice/",
            "invalid_message",
            "Message must not contain control characters other than line breaks.",
        )

    def test_whitespace_is_refused_before_normal_message_text(self):
        assert refuse_an_invalid_message("alice", "   ") == message_action_result(
            "https://www.linkedin.com/in/alice/",
            "invalid_message",
            "Message must contain non-whitespace characters.",
        )

    def test_text_that_normalizes_to_nothing_is_blank(self):
        # Python's strip() keeps U+FEFF, so the first check lets it through;
        # LinkedIn's trim() removes it and would send nothing.
        assert refuse_an_invalid_message("alice", "\ufeff\n\ufeff") == (
            message_action_result(
                "https://www.linkedin.com/in/alice/",
                "invalid_message",
                "Message must contain non-whitespace characters.",
            )
        )

    @pytest.mark.parametrize(
        "message", ["Hello, Alice!", "line\nbreak", "First\r\nSecond"]
    )
    def test_text_and_line_breaks_are_accepted(self, message: str):
        assert refuse_an_invalid_message("alice", message) is None

    def test_the_refusal_calls_the_owner_constructor_directly(self, monkeypatch):
        calls: list[tuple[str, str, str]] = []
        sentinel: dict[str, Any] = {"owner": "contracts"}

        def constructor(url: str, status: str, message: str) -> dict[str, Any]:
            calls.append((url, status, message))
            return sentinel

        monkeypatch.setattr(contracts, "message_action_result", constructor)

        assert refuse_an_invalid_message("alice", "") is sentinel
        assert calls == [
            (
                "https://www.linkedin.com/in/alice/",
                "invalid_message",
                "Message must contain non-whitespace characters.",
            )
        ]


class TestNormalizeMessageText:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("First\r\nSecond", "First\nSecond"),
            ("First\rSecond", "First\nSecond"),
            ("a\r\n\rb", "a\n\nb"),
            ("\n\nText", "Text"),
            ("Text\n\n", "Text"),
            ("a\n   \nb", "a\n\nb"),
            ("a\n\u00a0\u3000\nb", "a\n\nb"),
            ("a\n\n\nb", "a\n\n\nb"),
            ("a \n b", "a \n b"),
            ("  first\nlast  ", "first\nlast"),
            ("\ufeffText\u00a0", "Text"),
            ("x  y", "x  y"),
            ("Hello", "Hello"),
        ],
        ids=[
            "crlf",
            "lone-cr",
            "crlf-then-cr",
            "leading-blank-lines",
            "trailing-blank-lines",
            "whitespace-only-line",
            "nbsp-only-line",
            "interior-blank-lines-kept",
            "interior-line-edges-kept",
            "message-ends-trimmed",
            "js-only-whitespace-trimmed",
            "inner-spaces-kept",
            "single-line",
        ],
    )
    def test_text_becomes_what_linkedin_sends(self, message: str, expected: str):
        assert normalize_message_text(message) == expected
