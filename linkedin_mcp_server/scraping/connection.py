"""Locale-independent connection-state detection from action-area DOM signals.

LinkedIn translates every visible label, but the URLs it links to and the
ARIA attributes it sets do not depend on UI language. Detection here uses:

* ``/in/USER/edit/intro/`` anchor → self profile
* ``/preload/custom-invite/?vanityName=USER`` anchor → connectable
* ``/messaging/compose/`` anchor presence inside the top-card action root,
  combined with attribute-presence checks on action buttons
  (``aria-label`` set vs. unset on ``<button>``s) → 1st-degree vs. follow-only

Per ``AGENTS.md`` Scraping Rules, classification logic relies on URL
patterns and attribute *presence* — never on the values of locale-dependent
text labels like "Connect", "Follow", or "1st".

The single text-based fallback that remains is incoming-request detection
(Accept/Ignore present in the top-card text). LinkedIn does not expose a
distinctive URL or attribute for this state. Per the same rules, that
fallback lives behind an explicit per-locale label table that is trivial
to extend.
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


# Per AGENTS.md Scraping Rules: text-only signals must live behind an
# explicit per-locale table. This table covers incoming-request detection
# only — the one state without a structural URL/attribute signal we have
# verified against the live DOM. Extend with additional ("xx", (a, b))
# entries once the labels are confirmed against a real profile.
INCOMING_REQUEST_LABELS: dict[str, tuple[str, str]] = {
    "en": ("Accept", "Ignore"),
}


# Bound the text scan to the top-card region. The previous implementation
# cut at the first occurrence of "About"/"Experience"/"Education" — but
# those sentinel words are themselves locale-dependent, so a fixed
# character budget is the locale-clean replacement. ~600 chars is enough
# to comfortably cover name, headline, location, and the action-button
# row in every locale we have observed.
_TOP_CARD_CHAR_BUDGET = 600


@dataclass(frozen=True)
class ActionSignals:
    """Structural signals read from the top-card action area.

    All fields are locale-independent: each is the result of either a URL
    pattern match or the *presence* (not value) of an ARIA attribute.
    Detection downstream never reads the contents of an aria-label —
    only whether it is set on a button — so the verb portion of labels
    like "Follow {Name}" or "Folgen {Name}" is irrelevant.
    """

    has_invite_anchor: bool
    """``a[href*="/preload/custom-invite/?vanityName={user}"]`` exists in
    ``document`` (covers both the in-DOM action area and portal-rendered
    More-menu overlays). vanityName scoping prevents false positives from
    Connect anchors targeting other profiles on the page."""

    has_compose_anchor_in_action_root: bool
    """``a[href*="/messaging/compose/"]`` exists *inside* the action root
    found by walking up from any compose anchor in ``<main>``. This is the
    Message anchor in the top-card action button row."""

    has_edit_intro_anchor: bool
    """``a[href*="/in/{user}/edit/intro/"]`` exists in ``<main>``. Only
    rendered when viewing your own profile."""

    has_labeled_action_button: bool
    """At least one ``<button>`` with an ``aria-label`` attribute exists
    inside the action root. Primary action ``<button>``s (Follow,
    Connect, Save in Sales Navigator) carry ``aria-label`` for screen
    readers. The profile More button uses ``aria-expanded`` instead and
    is not counted here. Absence of any labeled button means there is no
    primary action ``<button>`` targeting this person."""

    has_labeled_action_anchor: bool
    """At least one ``<a>`` with an ``aria-label`` attribute exists
    inside the action root. LinkedIn renders the Pending state as an
    ``<a>`` (linking to the profile URL) with an ``aria-label`` like
    "Pending, click to withdraw invitation sent to {Name}", whereas the
    Message anchor carries only ``aria-disabled``. The label *value* is
    locale-dependent and not read; presence-on-an-``<a>`` is the
    locale-independent Pending signal."""


def detect_connection_state(
    profile_text: str,
    signals: ActionSignals,
) -> ConnectionState:
    """Determine the relationship state for a profile.

    Resolution order:

    1. ``self_profile`` — edit-intro anchor (URL).
    2. ``connectable`` — vanityName invite anchor (URL).
    3. ``pending`` — labeled action ``<a>`` in the action root (the
       Pending control LinkedIn renders for invitations awaiting
       response).
    4. ``incoming_request`` — locale-table text fallback. The one
       AGENTS.md-sanctioned text-based signal; extend
       :data:`INCOMING_REQUEST_LABELS` to add locales.
    5. ``already_connected`` — compose anchor present in action root and
       no labeled action button. (1st-degree connections render Message
       as the primary action; there is no Follow/Connect button.)
    6. ``follow_only`` — compose anchor present in action root and at
       least one labeled action ``<button>`` (Follow / Save in Sales
       Navigator), but no invite anchor anywhere. The
       ``connect_with_person`` write-gate prevents the deeplink from
       firing on this state.
    7. ``unavailable`` — fallthrough (e.g. profile pages where the
       action area could not be located at all).
    """
    if signals.has_edit_intro_anchor:
        return "self_profile"
    if signals.has_invite_anchor:
        return "connectable"
    if signals.has_labeled_action_anchor:
        return "pending"
    if _has_incoming_request_text(profile_text):
        return "incoming_request"
    if signals.has_compose_anchor_in_action_root:
        if signals.has_labeled_action_button:
            return "follow_only"
        return "already_connected"
    return "unavailable"


def _has_incoming_request_text(profile_text: str) -> bool:
    """Return True if any locale's Accept+Ignore label pair appears in the
    bounded top-card prefix of ``profile_text``.

    This is the single text-based detector retained from the previous
    implementation. Per AGENTS.md it is gated behind an explicit locale
    table; new languages are added by appending to
    :data:`INCOMING_REQUEST_LABELS`. The character budget keeps the scan
    inside the top-card region without depending on locale-dependent
    section sentinels.

    Each label is matched with newline boundaries — LinkedIn renders
    each action button on its own line in the top-card text, and
    line-bounded matching prevents false positives from the same
    substring appearing inside a name or headline (e.g. "Ignored" or
    "Acceptance Speech" would not match "Ignore" / "Accept").
    """
    if not profile_text:
        return False
    head = profile_text[:_TOP_CARD_CHAR_BUDGET]
    for accept, ignore in INCOMING_REQUEST_LABELS.values():
        if _label_present(head, accept) and _label_present(head, ignore):
            return True
    return False


def _label_present(head: str, label: str) -> bool:
    """Return True if ``label`` appears as a complete, line-bounded token
    inside ``head``. Allows the label to start at the beginning of the
    head and to end at the end of the head."""
    pattern = re.compile(r"(?:^|\n)" + re.escape(label) + r"(?:\n|$)")
    return pattern.search(head) is not None
