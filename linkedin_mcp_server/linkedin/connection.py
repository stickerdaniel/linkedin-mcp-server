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
text labels like "Connect", "Follow", or "1st". Incoming-request detection
is fully structural: the Accept/Ignore action row is fingerprinted by
attribute presence, element counts, and DOM order
(see :attr:`ActionSignals.has_incoming_action_row`), so no text labels are
read for any state.
"""

from __future__ import annotations

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

    has_incoming_action_row: bool
    """The top-card action row matches the incoming-request fingerprint:
    exactly three ``<button>``s in the smallest multi-button container
    around a ``button[aria-expanded]`` — two with ``aria-label``
    (Accept, Ignore) preceding one unlabeled expander (More) — and the
    container holds no compose anchor, no invite anchor, and no labeled
    ``<a>``. All checks are attribute presence and structural counts per
    the AGENTS.md Scraping Rules; no label values are read. Verified
    live 2026-06-11 against two German-locale incoming-request profiles.
    Computed independently of the compose-anchor action-root walk, which
    finds no top-card root on incoming profiles (they have no Message
    button) and would otherwise mis-anchor on sidebar cards."""


def detect_connection_state(signals: ActionSignals) -> ConnectionState:
    """Determine the relationship state for a profile from structural signals.

    Resolution order:

    1. ``self_profile`` — edit-intro anchor (URL).
    2. ``connectable`` — vanityName invite anchor (URL).
    3. ``incoming_request`` — structural action-row fingerprint. Must
       precede the pending check: incoming profiles have no Message
       button in the top card, so the compose-anchor action-root walk
       mis-anchors on sidebar cards whose labeled anchors would
       otherwise satisfy the pending signal.
    4. ``pending`` — labeled action ``<a>`` in the action root (the
       Pending control LinkedIn renders for invitations awaiting
       response).
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
    if signals.has_incoming_action_row:
        return "incoming_request"
    if signals.has_labeled_action_anchor:
        return "pending"
    if signals.has_compose_anchor_in_action_root:
        if signals.has_labeled_action_button:
            return "follow_only"
        return "already_connected"
    return "unavailable"
