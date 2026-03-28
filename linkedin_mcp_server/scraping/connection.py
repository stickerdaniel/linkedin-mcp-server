"""Connection state detection from scraped LinkedIn profile text.

Parses the action area of a profile page (buttons near the top) to
determine the relationship state.  The browser locale is forced to
en-US so button text is always English.
"""

from __future__ import annotations

import re
from typing import Literal

ConnectionState = Literal[
    "already_connected",
    "pending",
    "incoming_request",
    "connectable",
    "follow_only",
    "unavailable",
]

# Button text to click for each actionable state (en-US locale)
STATE_BUTTON_MAP: dict[ConnectionState, str] = {
    "connectable": "Connect",
    "incoming_request": "Accept",
}

# Markers that end the action area (section headings after the buttons)
_ACTION_AREA_END = re.compile(
    r"^(?:About|Highlights|Featured|Activity|Experience|Education)\n",
    re.MULTILINE,
)


def _extract_action_area(profile_text: str) -> str:
    """Return the top portion of profile text containing action buttons.

    Cuts off at the first content section heading (About, Highlights, etc.)
    to avoid matching "Follow" or "Connect" text that appears in sidebar
    suggestions, interests, or post content.
    """
    match = _ACTION_AREA_END.search(profile_text)
    if match:
        return profile_text[: match.start()]
    # Fallback: use first 500 chars if no section heading found
    return profile_text[:500]


def detect_connection_state(profile_text: str) -> ConnectionState:
    """Detect the connection relationship from scraped profile text.

    Checks the degree indicator and action button labels that appear
    as standalone lines in the profile action area.
    """
    # 1st-degree connection indicator appears near the top, before buttons
    if "\u00b7 1st" in profile_text[:300]:
        return "already_connected"

    action_area = _extract_action_area(profile_text)

    if "\nPending\n" in action_area or action_area.endswith("\nPending"):
        return "pending"
    if "\nAccept\n" in action_area and "\nIgnore\n" in action_area:
        return "incoming_request"
    if "\nConnect\n" in action_area or action_area.endswith("\nConnect"):
        return "connectable"
    if "\nFollow\n" in action_area or action_area.endswith("\nFollow"):
        return "follow_only"
    return "unavailable"
