"""Section contracts shared by every scraping workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from linkedin_mcp_server.scraping.identifiers import (
    normalize_person_identifier,
    person_profile_url,
)
from linkedin_mcp_server.scraping.link_metadata import Reference

# Returned as section text when a page comes back with its content gone and
# only LinkedIn's own navigation and footer left.
#
# Read carefully: that condition is a *guess* that the page was throttled, not
# an observation of one. It arrived in d8b4c62 with no cited evidence, LinkedIn
# documents no such behaviour, and nobody here has reproduced it deliberately —
# doing so would mean provoking a real throttle on a real account. The log line
# hedges with "likely" for the same reason.
#
# The same empty shell could also be a layout change, a resource this account
# cannot see, or a load that gave up. A session LinkedIn ended is the one
# alternative already ruled out elsewhere: every navigation checks the URL
# against the auth-blocker patterns first, and a redirect to /login, /authwall
# or /checkpoint raises before extraction is reached. That check stays on URLs
# deliberately — body text would be a per-locale guess, and this project's
# rule is that classification never depends on text values.
RATE_LIMITED_SECTION_TEXT = "[Rate limited] LinkedIn blocked this section. Try again later or request fewer sections."

# A submission is in flight from the moment the send is dispatched until the
# whole path has produced a result, cleanup included, and an interruption in
# that window cannot be reported. FastMCP runs every tool inside
# `anyio.fail_after()`, so the deadline raises `CancelledError` past
# `except Exception` and discards any result returned from the cancelled
# scope. The caller gets a timeout that carries no `retry_safe`, and this line
# is then the only record that a message may already have left. Answering the
# caller instead needs the tool to know its own deadline, which is issue #889.
SEND_INTERRUPTED_WARNING = (
    "Message submission was interrupted while in flight. The send outcome is "
    "unknown; check the conversation before retrying, as a retry may deliver "
    "the message twice."
)


# The same in-flight blind spot as SEND_INTERRUPTED_WARNING, for the two post
# actions that publish. Worth its own line because these two are public: an
# interrupted comment or repost that did land is visible to everybody who sees
# the post, where an interrupted message is visible to one recipient.
POST_ACTION_INTERRUPTED_WARNING = (
    "A post action was interrupted while in flight. The outcome is unknown; "
    "open the post before retrying, as a retry may publish it twice."
)


def rate_limited_section_error() -> dict[str, str]:
    """The ``section_errors`` entry for a section that came back empty.

    One shape for every caller, because the alternative is what this codebase
    did until now: most call sites dropped the sentinel and returned the
    section as simply absent. An agent reading an empty section with no error
    concludes there was nothing to find and calls again, which is the opposite
    of what a rate limit asks for. Being told is what lets a client back off.

    Note this reports the *heuristic's* verdict, with the caveats on
    ``RATE_LIMITED_SECTION_TEXT`` above, and does not make it more accurate.
    What it changes is that a wrong verdict is now visible and can be argued
    with, where a silently missing section could not be.
    """
    return {
        "error_type": "rate_limit",
        "error_message": RATE_LIMITED_SECTION_TEXT,
    }


def message_action_result(
    url: str,
    status: str,
    message: str,
    *,
    recipient_selected: bool = False,
    sent: bool = False,
    retry_safe: bool = True,
) -> dict[str, Any]:
    """Build a structured response for the send_message tool.

    ``sent`` is true only when the narrowly defined message-list UI transition
    was observed after submission. It does not prove delivery or that the
    recipient read the message. A caller keying a retry on it alone can re-send
    a message that may already have arrived, which is what ``retry_safe`` exists
    to say: it is false from the moment a submission is attempted, and true only
    while nothing can have left the composer.
    """
    return {
        "url": url,
        "status": status,
        "message": message,
        "recipient_selected": recipient_selected,
        "sent": sent,
        "retry_safe": retry_safe,
    }


def post_action_result(
    url: str,
    status: str,
    message: str,
    *,
    acted: bool = False,
    retry_safe: bool = True,
    reaction: str | None = None,
) -> dict[str, Any]:
    """Build a structured response for a react, comment or repost attempt.

    ``acted`` carries the same narrow meaning ``sent`` carries above: the
    structural transition this server looks for was observed, not that LinkedIn
    accepted, kept or showed the action to anybody. ``retry_safe`` is the field
    a caller should key a retry on, and it is false from the moment a click that
    could land is dispatched. The distinction matters more here than for a
    message, because two of these three actions are public and a doubled repost
    is visible on the actor's own feed.

    ``reaction`` is present only for a react, naming the reaction that was
    asked for. It is the request, not a reading of what LinkedIn recorded.
    """
    result: dict[str, Any] = {
        "url": url,
        "status": status,
        "message": message,
        "acted": acted,
        "retry_safe": retry_safe,
    }
    if reaction is not None:
        result["reaction"] = reaction
    return result


def refuse_invalid_post_text(
    post_url: str, text: str, *, field: str
) -> dict[str, Any] | None:
    """Return the browser-free refusal for unsafe comment or repost text.

    Diverges from ``refuse_an_invalid_message`` on one character, deliberately.
    A newline is allowed here and refused there, because a comment or a repost
    commentary is routinely more than one paragraph while a chat message is
    not. Nothing about the insertion path changes: the text is inserted with
    ``insertText`` and submitted by clicking a button, so a line break is
    content and never a submit. Every other C0 control and DEL stays refused,
    which is what keeps the insertion contract to text.
    """
    reason = None
    if not text.strip():
        reason = f"{field} must contain non-whitespace characters."
    elif any(
        (ord(character) < 32 and character != "\n") or ord(character) == 127
        for character in text
    ):
        reason = f"{field} must not contain control characters. A newline is allowed."
    if reason is None:
        return None
    return post_action_result(post_url, "invalid_text", reason)


def refuse_an_invalid_message(
    linkedin_username: str, message: str
) -> dict[str, Any] | None:
    """Return the shared browser-free refusal for an unsafe message."""
    reason = None
    if not message.strip():
        reason = "Message must contain non-whitespace characters."
    elif any(ord(character) < 32 or ord(character) == 127 for character in message):
        # Keep the browser-side insertion contract to plain message text.
        # Reject every C0 control and DEL before a session is acquired so no
        # control input can reach the contenteditable surface.
        reason = "Message must not contain control characters or line breaks."
    if reason is None:
        return None
    return message_action_result(
        person_profile_url(normalize_person_identifier(linkedin_username), "/"),
        "invalid_message",
        reason,
    )


@dataclass
class ExtractedSection:
    """Text and compact references extracted from a loaded LinkedIn section."""

    text: str
    references: list[Reference]
    error: dict[str, Any] | None = None


class FilterValidationError(ValueError):
    """Invalid ``search_people`` filter input (network token / URN shape).

    Subclassing ``ValueError`` keeps backward-compatible behaviour for
    direct extractor callers (``pytest.raises(ValueError)`` matches), while
    letting the MCP tool wrapper catch this case precisely and surface the
    actionable message past ``mask_error_details``.
    """
