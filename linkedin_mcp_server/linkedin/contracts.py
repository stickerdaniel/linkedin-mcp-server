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


class _Omitted:
    """A result field the caller did not pass."""


_OMITTED = _Omitted()


def message_action_result(
    url: str,
    status: str,
    message: str,
    *,
    recipient_selected: bool = False,
    sent: bool = False,
    retry_safe: bool = True,
    thread_id: str | None | _Omitted = _OMITTED,
) -> dict[str, Any]:
    """Build a structured response for the send_message tool.

    ``sent`` is true only when the narrowly defined message-list UI transition
    was observed after submission. It does not prove delivery or that the
    recipient read the message. A caller keying a retry on it alone can re-send
    a message that may already have arrived, which is what ``retry_safe`` exists
    to say: it is false from the moment a submission is attempted, and true only
    while nothing can have left the composer.

    ``thread_id`` is present only when the caller passes it, including when
    the value is null. A confirmed send passes the conversation the message
    was observed in, or null when that page stayed on the compose route.
    """
    result = {
        "url": url,
        "status": status,
        "message": message,
        "recipient_selected": recipient_selected,
        "sent": sent,
        "retry_safe": retry_safe,
    }
    if not isinstance(thread_id, _Omitted):
        result["thread_id"] = thread_id
    return result


# ECMAScript WhiteSpace without the characters a message may not contain (tab,
# VT and FF are C0 controls). LinkedIn trims the composed text with JavaScript's
# `trim()` before sending, so a blank line and the ends of a message are judged
# by this set, never by Python's `str.isspace()`, which disagrees both ways:
# it counts U+001C-U+001F and U+0085 and misses U+FEFF.
_JS_WHITESPACE = frozenset(
    "\u0020\u00a0\ufeff\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u202f\u205f\u3000"
)
# Line structure is LF alone. These three are line or paragraph separators of
# their own (and U+0085 is one that JavaScript's `trim()` keeps while Python's
# `strip()` removes it), so they are refused rather than guessed at.
_REFUSED_SEPARATORS = frozenset("\u0085\u2028\u2029")


def _refused_character(character: str) -> bool:
    code = ord(character)
    return (
        (code < 32 and character not in "\r\n")
        or code == 127
        or character in _REFUSED_SEPARATORS
    )


def normalize_message_text(message: str) -> str:
    """Return the text LinkedIn would send for an accepted *message*.

    Callers refuse the message with ``refuse_an_invalid_message`` first, so no
    refused character reaches this. CRLF and a lone CR end a line like LF. A
    line made only of whitespace becomes empty, and the whole message is
    trimmed at both ends, as LinkedIn's own send does. Interior empty lines and
    spaces inside or at the edges of interior lines are kept.
    """
    text = message.replace("\r\n", "\n").replace("\r", "\n")
    lines = [
        "" if all(character in _JS_WHITESPACE for character in line) else line
        for line in text.split("\n")
    ]
    edges = "".join(_JS_WHITESPACE) + "\n"
    return "\n".join(lines).strip(edges)


def refuse_an_invalid_message(
    linkedin_username: str, message: str
) -> dict[str, Any] | None:
    """Return the shared browser-free refusal for an unsafe message."""
    reason = None
    if not message.strip():
        reason = "Message must contain non-whitespace characters."
    elif any(_refused_character(character) for character in message):
        # Keep the browser-side insertion contract to plain message text and
        # line breaks. Reject every other C0 control, DEL and the Unicode
        # separators before a session is acquired, so no control input can
        # reach the contenteditable surface.
        reason = "Message must not contain control characters other than line breaks."
    elif not normalize_message_text(message):
        reason = "Message must contain non-whitespace characters."
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
