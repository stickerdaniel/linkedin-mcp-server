"""Connection state detection from scraped LinkedIn profile text.

Parses the action area of a profile page (buttons near the top) to
determine the relationship state.  The browser locale is forced to
en-US so button text is always English, but we also support French
(fr-FR) as a fallback for users whose LinkedIn interface is in French.
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

# Button text to click for each actionable state.
# Detected language determines which label is used at click time.
STATE_BUTTON_MAP: dict[ConnectionState, str] = {
    "connectable": "Connect",
    "incoming_request": "Accept",
}

STATE_BUTTON_MAP_FR: dict[ConnectionState, str] = {
    "connectable": "Se connecter",
    "incoming_request": "Accepter",
}

# Markers that end the action area (section headings after the buttons)
# Supports both English and French headings.
_ACTION_AREA_END = re.compile(
    r"^(?:About|Highlights|Featured|Activity|Experience|Education"
    r"|Infos|Sélection|Activité|Expérience|Formation|Infos ventes)\n",
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


def _contains(area: str, label: str) -> bool:
    """Check if label appears as a standalone line in the action area."""
    return f"\n{label}\n" in area or area.endswith(f"\n{label}")


def detect_connection_state(profile_text: str) -> tuple[ConnectionState, bool]:
    """Detect the connection relationship from scraped profile text.

    Returns a tuple of (state, is_french) so the caller knows which
    button label to use when clicking.
    """
    # 1st-degree connection indicator (both · 1st and · 1er)
    top = profile_text[:300]
    if "\u00b7 1st" in top or "\u00b7 1er" in top:
        return "already_connected", False

    action_area = _extract_action_area(profile_text)

    # --- English detection ---
    if _contains(action_area, "Pending"):
        return "pending", False
    if _contains(action_area, "Accept") and _contains(action_area, "Ignore"):
        return "incoming_request", False
    if _contains(action_area, "Connect"):
        return "connectable", False
    if _contains(action_area, "Follow"):
        return "follow_only", False

    # --- French detection ---
    if _contains(action_area, "En attente"):
        return "pending", True
    if _contains(action_area, "Accepter") and _contains(action_area, "Ignorer"):
        return "incoming_request", True
    if _contains(action_area, "Se connecter"):
        return "connectable", True
    if _contains(action_area, "Suivre"):
        return "follow_only", True

    return "unavailable", False
