"""Connection state detection from structural DOM signals.

LinkedIn translates every visible label, but the URLs it links to do not
get translated. The action area at the top of a profile page exposes the
relationship state through anchor hrefs: a Connect button is always an
``<a href="/preload/custom-invite/?vanityName=...">``; the Message button
on a 1st-degree connection is always ``<a href="/messaging/compose/...">``.
Editing your own profile exposes ``<a href=".../edit/intro/">``.

These hrefs work as language-independent signals. Text-based fallbacks
remain available for the niche states (Pending and incoming requests)
where no anchor reliably exposes the state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ConnectionState = Literal[
    "already_connected",
    "pending",
    "incoming_request",
    "connectable",
    "follow_only",
    "self_profile",
    "unavailable",
]


@dataclass(frozen=True)
class ActionSignals:
    """Structural signals read from the profile action area.

    Each flag corresponds to the presence of a specific anchor href in the
    top of the page. None of them depend on visible text, so detection works
    in any locale.
    """

    has_invite_anchor: bool
    has_compose_anchor: bool
    has_edit_intro_anchor: bool


def detect_connection_state(
    profile_text: str,
    signals: ActionSignals | None = None,
) -> ConnectionState:
    """Determine the relationship state for a profile.

    Structural signals (URL hrefs) take priority. Text fallbacks only handle
    the rare states (Pending, incoming requests) that are not exposed via a
    distinctive anchor. The text checks remain English-only and can be
    extended without changing the structural happy path.
    """
    if signals is not None:
        if signals.has_edit_intro_anchor:
            return "self_profile"
        if signals.has_invite_anchor:
            return "connectable"

    # Text fallbacks for states without a unique anchor href
    if _has_incoming_request_text(profile_text):
        return "incoming_request"
    if _has_pending_text(profile_text):
        return "pending"

    if signals is not None and signals.has_compose_anchor:
        return "already_connected"

    if profile_text and "· 1st" in profile_text[:300]:
        return "already_connected"

    if signals is not None:
        return "follow_only"

    return _detect_from_text_only(profile_text)


_ACTION_AREA_END = re.compile(
    r"^(?:About|Highlights|Featured|Activity|Experience|Education)\n",
    re.MULTILINE,
)


def _action_area(profile_text: str) -> str:
    match = _ACTION_AREA_END.search(profile_text)
    if match:
        return profile_text[: match.start()]
    return profile_text[:500]


def _has_pending_text(profile_text: str) -> bool:
    area = _action_area(profile_text)
    return "\nPending\n" in area or area.endswith("\nPending")


def _has_incoming_request_text(profile_text: str) -> bool:
    area = _action_area(profile_text)
    return "\nAccept\n" in area and "\nIgnore\n" in area


def _detect_from_text_only(profile_text: str) -> ConnectionState:
    """Fallback when DOM signals are unavailable (e.g. unit tests)."""
    if profile_text and "· 1st" in profile_text[:300]:
        return "already_connected"
    area = _action_area(profile_text)
    if "\nConnect\n" in area or area.endswith("\nConnect"):
        return "connectable"
    if "\nFollow\n" in area or area.endswith("\nFollow"):
        return "follow_only"
    return "unavailable"
