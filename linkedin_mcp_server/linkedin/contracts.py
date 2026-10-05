"""Section contracts shared by every page workflow."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import anyio

from linkedin_mcp_server.linkedin.identifiers import (
    normalize_person_identifier,
    person_profile_url,
)
from linkedin_mcp_server.linkedin.link_metadata import Reference

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
# whole path has produced a result, cleanup included, and a cancellation in
# that window cannot be reported: it raises `CancelledError` past
# `except Exception`, and a cancelled scope discards whatever is returned from
# inside it. The tool's own deadline no longer lands there, because the send
# stops ahead of it and answers `send_unconfirmed` (#889). What is left is
# cancellation the server does not own, a client that cancels or goes away,
# and for that this line is the only record that a message may already have
# left.
SEND_INTERRUPTED_WARNING = (
    "Message submission was interrupted while in flight. The send outcome is "
    "unknown; check the conversation before retrying, as a retry may deliver "
    "the message twice."
)


def before_the_reply_deadline(
    limit: float = math.inf, *, shield: bool = False
) -> anyio.CancelScope:
    """Bound work that runs while a send's answer waits to leave.

    The scope ends after ``limit`` seconds and never later than halfway to the
    deadline the call runs under, so what follows keeps the other half to hand
    the answer back before that deadline discards it (#889). A shielded scope
    ignores that deadline, so without this bound a slow cleanup outlasts it.
    Without a deadline, or once the call is already cancelled and its answer
    gone, only ``limit`` applies.
    """
    now = anyio.current_time()
    end = now + limit
    deadline = anyio.current_effective_deadline()
    if now < deadline < math.inf:
        end = min(end, now + (deadline - now) / 2)
    return anyio.CancelScope(deadline=end, shield=shield)


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
