"""Connection state detection from scraped LinkedIn profile text.

Parses the action area of a profile page (buttons near the top) to
determine the relationship state.

The browser context is launched with ``locale="en-US"`` (see
``core/browser.py``) in the hope that LinkedIn will render in English.
In practice LinkedIn ignores the Accept-Language header for logged-in
users and serves the UI in the language configured on the user's
*account* (Settings → Display language). As a result a US-locale
browser can still render ``Vernetzen`` / ``Folgen`` for an account set
to German, ``Se connecter`` / ``Suivre`` for French, and so on.

To keep the detector honest across accounts this module maintains a
small per-locale table of the button labels and section headings we
care about. New locales are a one-line addition to each table.
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

# Default locale used when detection fails (and for callers that receive
# ``already_connected`` / ``unavailable``, where no button needs clicking).
DEFAULT_LOCALE = "en"

# Button labels keyed by locale + state. The string stored here is the
# exact on-screen text the extractor will look for when clicking.
STATE_BUTTON_MAP_BY_LOCALE: dict[str, dict[ConnectionState, str]] = {
    "en": {"connectable": "Connect", "incoming_request": "Accept"},
    "de": {"connectable": "Vernetzen", "incoming_request": "Annehmen"},
    "fr": {"connectable": "Se connecter", "incoming_request": "Accepter"},
}

# The action-area label for each state in each locale. Used during
# detection; callers should prefer ``STATE_BUTTON_MAP_BY_LOCALE`` when
# clicking because some states (``pending``, ``follow_only``) have a
# detection label without a corresponding click target here.
_DETECTION_LABELS_BY_LOCALE: dict[str, dict[ConnectionState, str]] = {
    "en": {
        "pending": "Pending",
        "incoming_request": "Accept",
        "connectable": "Connect",
        "follow_only": "Follow",
    },
    "de": {
        "pending": "Ausstehend",
        "incoming_request": "Annehmen",
        "connectable": "Vernetzen",
        "follow_only": "Folgen",
    },
    "fr": {
        "pending": "En attente",
        "incoming_request": "Accepter",
        "connectable": "Se connecter",
        "follow_only": "Suivre",
    },
}

# The second label required to distinguish ``incoming_request`` from a
# merely ``connectable`` profile (``Accept`` alone could be a nav link).
_INCOMING_SECONDARY_BY_LOCALE: dict[str, str] = {
    "en": "Ignore",
    "de": "Ignorieren",
    "fr": "Ignorer",
}

# Degree indicator shown in the first ~300 chars of the profile.
_FIRST_DEGREE_MARKERS: tuple[str, ...] = (
    "\u00b7 1st",  # en
    "\u00b7 1.",  # de (Kontakt 1. Grades)
    "\u00b7 1er",  # fr
)

# aria-label fragments for the profile's "More" (three-dot) menu button.
# LinkedIn renders these per the account's display language; Playwright's
# ``aria-label*=`` selector treats each entry as a case-sensitive substring.
_MORE_ARIA_LABELS: tuple[str, ...] = ("More", "Mehr", "Plus")

# Section headings that mark the end of the action area. We merge every
# known translation into one regex so ``_extract_action_area`` doesn't
# need to know which locale it's in.
_SECTION_HEADINGS_BY_LOCALE: dict[str, tuple[str, ...]] = {
    "en": ("About", "Highlights", "Featured", "Activity", "Experience", "Education"),
    "de": (
        "Info",
        "Highlights",
        "Im Fokus",
        "Aktivitäten",
        "Erfahrung",
        "Ausbildung",
        "Empfohlen",
    ),
    "fr": ("Infos", "Sélection", "Activité", "Expérience", "Formation", "Infos ventes"),
}

_ACTION_AREA_END = re.compile(
    r"^(?:"
    + "|".join(
        re.escape(h)
        for headings in _SECTION_HEADINGS_BY_LOCALE.values()
        for h in headings
    )
    + r")\n",
    re.MULTILINE,
)


def _extract_action_area(profile_text: str) -> str:
    """Return the top portion of profile text containing action buttons.

    Cuts off at the first content section heading (About / Info / Infos…)
    to avoid matching "Follow" or "Connect" text that appears in sidebar
    suggestions, interests, or post content.
    """
    match = _ACTION_AREA_END.search(profile_text)
    if match:
        return profile_text[: match.start()]
    # Fallback: use first 500 chars if no section heading found
    return profile_text[:500]


def _contains(area: str, label: str) -> bool:
    """Whether ``label`` appears as a standalone line in the action area."""
    return f"\n{label}\n" in area or area.endswith(f"\n{label}")


def detect_connection_state(profile_text: str) -> tuple[ConnectionState, str]:
    """Detect the connection relationship from scraped profile text.

    Returns a tuple of ``(state, locale)``. The locale tells the caller
    which entry in ``STATE_BUTTON_MAP_BY_LOCALE`` to use when clicking.
    ``locale`` is always set — it falls back to :data:`DEFAULT_LOCALE`
    when the state itself is ``already_connected`` or ``unavailable``
    and no button needs to be located.
    """
    # 1st-degree connection indicator appears near the top, before buttons
    top = profile_text[:300]
    if any(marker in top for marker in _FIRST_DEGREE_MARKERS):
        return "already_connected", DEFAULT_LOCALE

    action_area = _extract_action_area(profile_text)

    # Try each locale's labels, preferring English so an en-locale session
    # never mis-detects on a ``Connect`` that survives in some sidebar
    # translation. Locales are checked in insertion order of the map.
    for locale, labels in _DETECTION_LABELS_BY_LOCALE.items():
        if _contains(action_area, labels["pending"]):
            return "pending", locale
        if _contains(action_area, labels["incoming_request"]) and _contains(
            action_area, _INCOMING_SECONDARY_BY_LOCALE[locale]
        ):
            return "incoming_request", locale
        if _contains(action_area, labels["connectable"]):
            return "connectable", locale
        if _contains(action_area, labels["follow_only"]):
            return "follow_only", locale

    return "unavailable", DEFAULT_LOCALE


def all_button_texts(state: ConnectionState) -> list[str]:
    """Every known on-screen label for a given state, across all locales.

    Handy for ``_open_more_menu`` and similar code that needs a locale-
    agnostic regex/aria-label list to drive Playwright locators.
    """
    seen: set[str] = set()
    out: list[str] = []
    for locale_map in STATE_BUTTON_MAP_BY_LOCALE.values():
        label = locale_map.get(state)
        if label and label not in seen:
            seen.add(label)
            out.append(label)
    return out
