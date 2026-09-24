"""Tests for the section contracts every scraping workflow returns."""

from typing import Any

import pytest

from linkedin_mcp_server.scraping import contracts
from linkedin_mcp_server.scraping.contracts import (
    POST_ACTION_INTERRUPTED_WARNING,
    RATE_LIMITED_SECTION_TEXT,
    SEND_INTERRUPTED_WARNING,
    ExtractedSection,
    FilterValidationError,
    message_action_result,
    post_action_result,
    rate_limited_section_error,
    refuse_an_invalid_message,
    refuse_invalid_post_text,
)

POST_URL = "https://www.linkedin.com/feed/update/urn:li:ugcPost:7506667649444237313/"


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

    def test_the_interruption_warning_names_duplicate_delivery(self):
        assert SEND_INTERRUPTED_WARNING == (
            "Message submission was interrupted while in flight. The send outcome "
            "is unknown; check the conversation before retrying, as a retry may "
            "deliver the message twice."
        )


class TestPostActionResult:
    def test_the_retry_contract_is_explicit_on_every_result(self):
        assert post_action_result(
            POST_URL,
            "commented",
            "The comment was published.",
            acted=True,
            retry_safe=False,
        ) == {
            "url": POST_URL,
            "status": "commented",
            "message": "The comment was published.",
            "acted": True,
            "retry_safe": False,
        }

    def test_a_result_is_unacted_and_retry_safe_until_told_otherwise(self):
        # The safe defaults are the ones a refusal wants, and every refusal in
        # the owner is built by naming nothing but url, status and message.
        result = post_action_result(POST_URL, "confirmation_required", "Set the flag.")
        assert result["acted"] is False
        assert result["retry_safe"] is True

    def test_the_reaction_key_is_absent_unless_a_reaction_was_asked_for(self):
        assert "reaction" not in post_action_result(POST_URL, "commented", "done")
        assert (
            post_action_result(POST_URL, "reacted", "done", reaction="funny")[
                "reaction"
            ]
            == "funny"
        )

    def test_the_interruption_warning_names_duplicate_publication(self):
        assert POST_ACTION_INTERRUPTED_WARNING == (
            "A post action was interrupted while in flight. The outcome is "
            "unknown; open the post before retrying, as a retry may publish it "
            "twice."
        )


class TestRefuseInvalidPostText:
    @pytest.mark.parametrize("text", ["before\tafter", "text\x7f", "a\x00b", "a\rb"])
    def test_every_control_character_but_a_newline_is_refused(self, text: str):
        assert refuse_invalid_post_text(POST_URL, text, field="comment") == (
            post_action_result(
                POST_URL,
                "invalid_text",
                "comment must not contain control characters. A newline is allowed.",
            )
        )

    def test_a_newline_is_content_rather_than_a_control_character(self):
        # A comment is routinely more than one paragraph, which is where this
        # parts company with refuse_an_invalid_message. Nothing about the
        # insertion path changes: the submit is a button click, never Enter.
        assert refuse_invalid_post_text(POST_URL, "one\n\ntwo", field="comment") is None

    def test_whitespace_alone_is_refused(self):
        assert refuse_invalid_post_text(POST_URL, " \n ", field="commentary") == (
            post_action_result(
                POST_URL,
                "invalid_text",
                "commentary must contain non-whitespace characters.",
            )
        )

    def test_the_field_name_reaches_the_caller(self):
        refusal = refuse_invalid_post_text(POST_URL, "", field="commentary")
        assert refusal is not None
        assert refusal["message"].startswith("commentary ")

    def test_ordinary_text_is_accepted(self):
        assert refuse_invalid_post_text(
            POST_URL, "Great write-up", field="comment"
        ) is (None)


class TestRefuseAnInvalidMessage:
    @pytest.mark.parametrize("message", ["line\nbreak", "before\tafter", "text\x7f"])
    def test_every_c0_or_del_character_is_refused(self, message: str):
        assert refuse_an_invalid_message("alice", message) == message_action_result(
            "https://www.linkedin.com/in/alice/",
            "invalid_message",
            "Message must not contain control characters or line breaks.",
        )

    def test_whitespace_is_refused_before_normal_message_text(self):
        assert refuse_an_invalid_message("alice", "   ") == message_action_result(
            "https://www.linkedin.com/in/alice/",
            "invalid_message",
            "Message must contain non-whitespace characters.",
        )

    def test_safe_single_line_text_is_accepted(self):
        assert refuse_an_invalid_message("alice", "Hello, Alice!") is None

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
