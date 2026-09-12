"""Invitation actions taken on a loaded person page.

``connection.py`` answers what a profile's action area *says* about the
relationship and stays browser-free; this module is everything that reads
or touches that area: the structural signal probe, the More menu, the
incoming-request Accept click, the invite dialog, the non-submitting
note-quota probe and the verification re-read after a write.

Per the AGENTS.md Scraping Rules every decision here rests on a URL pattern
(``/preload/custom-invite/?vanityName=USER``, ``/in/USER/edit/intro/``,
``/messaging/compose/``), on the *presence* of an ARIA attribute
(``aria-label`` on a button versus an anchor, ``aria-expanded`` on the menu
opener) or on a structural count. No label value is read anywhere, so a
German or an opaquely labelled page classifies exactly as an English one;
``tests/test_action_signals_dom.py`` holds that line against a real DOM in
all four label sets.

The write gate is the reason the order of the checks below matters: the
invite deeplink fires only after ``has_invite_anchor`` is true, and the only
other thing that may open it is the note-quota probe, which never clicks a
primary button.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote_plus

import asyncio
import logging

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.scraping import connection
from linkedin_mcp_server.scraping.connection import ActionSignals
from linkedin_mcp_server.scraping.identifiers import (
    normalize_person_identifier,
    person_profile_url,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)

_DIALOG_SELECTOR = 'dialog[open], [role="dialog"]'
_DIALOG_PREMIUM_LINK_SELECTOR = (
    'dialog[open] a[href*="/premium/"], [role="dialog"] a[href*="/premium/"]'
)
_DIALOG_TEXTAREA_SELECTOR = '[role="dialog"] textarea, dialog textarea'

# Shared JS function that walks up from any /messaging/compose/ anchor
# inside <main> to find the smallest ancestor that satisfies the
# action-root predicate (>=2 interactive children, >=1 button). This is
# the top-card action row regardless of LinkedIn's class names.
#
# Inlined into both ACTION_SIGNALS_JS and OPEN_MORE_BUTTON_JS so a
# single change to the heuristic propagates to both call sites.
_FIND_ACTION_ROOT_FN_JS = r"""
function findActionRoot(main) {
  const composeAnchors = main.querySelectorAll('a[href*="/messaging/compose/"]');
  for (const a of composeAnchors) {
    let el = a.parentElement;
    while (el && el !== main) {
      const interactive = el.querySelectorAll('button, a').length;
      const buttons = el.querySelectorAll('button').length;
      if (interactive >= 2 && buttons >= 1) {
        return el;
      }
      el = el.parentElement;
    }
  }
  return null;
}
"""

# Shared JS function that fingerprints the incoming-request action row.
# Incoming-request profiles render no Message button in the top card, so
# findActionRoot (compose-anchor walk) cannot locate their action row and
# would mis-anchor on sidebar mutual-connection cards instead. This walk
# anchors on button[aria-expanded] (the More button) and validates the
# smallest multi-button ancestor against the fingerprint verified live
# 2026-06-11 on two German-locale incoming-request profiles:
#
#   [button aria-label (Accept)] [button aria-label (Ignore)]
#   [button aria-expanded, no aria-label (More)]
#
# All checks are attribute presence and structural counts per the
# AGENTS.md Scraping Rules — no label values are read. Every guard kills
# a known false positive: total-button-count === 3 and labeled === 2
# exclude video-player control bars (play/mute/captions all carry
# aria-label); the unlabeled-expander check excludes player settings
# expanders (the profile More button never carries aria-label); the
# DOM-order guard excludes bars with trailing labeled buttons; the
# compose/invite/labeled-anchor exclusions kill follow_only, pending,
# connected top cards and sidebar cards. The scan continues over ALL
# expander candidates because cover-video profiles render the player's
# expander before the top-card row in DOM order.
#
# The search is scoped to the top card — the first <section> of <main>
# (falling back to main's first child, then main). Profile pages render
# the action row in the top card; feed, "people also viewed", and other
# widgets live in later sections. Without the scope an unrelated widget
# elsewhere in main with the same button shape could be misclassified and
# its first labeled button clicked.
#
# Inlined into ACTION_SIGNALS_JS and CLICK_INCOMING_ACCEPT_JS so a
# single change to the fingerprint propagates to both call sites.
_FIND_INCOMING_ACTION_ROW_FN_JS = r"""
function findIncomingActionRow(main) {
  const scope = main.querySelector('section') || main.firstElementChild || main;
  const matches = [];
  for (const expander of scope.querySelectorAll('button[aria-expanded]')) {
    let el = expander.parentElement;
    while (el && el !== scope && el !== main) {
      if (el.querySelectorAll('button').length >= 2) {
        const buttons = el.querySelectorAll('button');
        const labeled = el.querySelectorAll('button[aria-label]');
        const expanders = el.querySelectorAll('button[aria-expanded]');
        if (
          buttons.length === 3 &&
          labeled.length === 2 &&
          expanders.length === 1 &&
          !expanders[0].hasAttribute('aria-label') &&
          expanders[0].compareDocumentPosition(labeled[1]) &
            Node.DOCUMENT_POSITION_PRECEDING &&
          !el.querySelector('a[href*="/messaging/compose/"]') &&
          !el.querySelector('a[href*="/preload/custom-invite/"]') &&
          !el.querySelector('a[aria-label]')
        ) {
          matches.push(el);
        }
        break;
      }
      el = el.parentElement;
    }
  }
  // Require a unique match: a profile's top card has exactly one action
  // row. Ambiguity (two rows matching the shape) is treated as no match so
  // the irreversible Accept click never fires on a guessed control.
  return matches.length === 1 ? matches[0] : null;
}
"""

# Locale-independent connection-state probe. Returns four booleans;
# per AGENTS.md Scraping Rules, every signal is based on URL patterns
# or ARIA-attribute *presence* — never on label text values.
#
# - hasInvite: vanityName-scoped invite anchor anywhere in document.
#   Searches document (not main) so a post-More-menu reread sees
#   portal-rendered menu items. The vanityName parameter is unique to
#   the target user, so document-wide search has no false-positive risk.
# - hasComposeInActionRoot: any /messaging/compose/ anchor exists inside
#   the action root. Scoped to main (not document) to avoid the More
#   menu's "Send profile in a message" anchor, which is a compose URL
#   but lives outside the action area.
# - hasEditIntro: edit-intro URL exists, only rendered on own profile.
# - hasLabeledActionButton: at least one <button[aria-label]> inside the
#   action root. Primary action buttons (Follow / Connect /
#   Save in Sales Navigator) carry aria-label for screen readers; the
#   profile More button uses aria-expanded instead and is not counted.
# - hasLabeledActionAnchor: at least one <a[aria-label]> inside the
#   action root. LinkedIn renders the Pending state as an anchor (linking
#   back to the profile URL) carrying aria-label like "Pending, click to
#   withdraw…". The Message anchor has only aria-disabled, so a labeled
#   anchor is the locale-independent Pending signal.
# - hasIncomingActionRow: the incoming-request fingerprint matched (see
#   _FIND_INCOMING_ACTION_ROW_FN_JS). Computed independently of
#   findActionRoot, which cannot locate the top-card row on incoming
#   profiles (no compose anchor there) and would mis-anchor on sidebar
#   cards.
#
# The username is CSS-escaped before interpolation into attribute
# selectors to defend against malformed inputs containing characters
# that would otherwise break the selector syntax (quotes, brackets).
ACTION_SIGNALS_JS = (
    r"""
((username) => {
"""
    + _FIND_ACTION_ROOT_FN_JS
    + _FIND_INCOMING_ACTION_ROW_FN_JS
    + r"""
  const main = document.querySelector('main');
  if (!main) return null;

  const safe = CSS.escape(username);
  const inviteSel = `a[href*="/preload/custom-invite/?vanityName=${safe}"]`;
  const editSel = `a[href*="/in/${safe}/edit/intro/"]`;

  const hasInvite = !!document.querySelector(inviteSel);
  const hasEditIntro = !!main.querySelector(editSel);

  const actionRoot = findActionRoot(main);

  let hasComposeInActionRoot = false;
  let hasLabeledActionButton = false;
  let hasLabeledActionAnchor = false;
  if (actionRoot) {
    hasComposeInActionRoot =
      !!actionRoot.querySelector('a[href*="/messaging/compose/"]');
    for (const b of actionRoot.querySelectorAll('button')) {
      if (b.hasAttribute('aria-label')) {
        hasLabeledActionButton = true;
        break;
      }
    }
    for (const a of actionRoot.querySelectorAll('a')) {
      if (a.hasAttribute('aria-label')) {
        hasLabeledActionAnchor = true;
        break;
      }
    }
  }

  return {
    hasInvite,
    hasComposeInActionRoot,
    hasEditIntro,
    hasLabeledActionButton,
    hasLabeledActionAnchor,
    hasIncomingActionRow: !!findIncomingActionRow(main),
  };
})
"""
)

# Open the profile's More button, located inside the action root via the
# aria-expanded attribute. The aria-expanded attribute uniquely identifies
# the menu opener without text labels (the More button has no aria-label,
# while Follow/Connect/Pending buttons do — the inverse pattern). Returns
# true iff the click landed; the caller waits for [role='menu'] visibility
# before re-scanning signals.
OPEN_MORE_BUTTON_JS = (
    r"""
(() => {
"""
    + _FIND_ACTION_ROOT_FN_JS
    + r"""
  const main = document.querySelector('main');
  if (!main) return false;
  const actionRoot = findActionRoot(main);
  if (!actionRoot) return false;
  const moreBtn = actionRoot.querySelector('button[aria-expanded]');
  if (!moreBtn) return false;
  moreBtn.click();
  return true;
})
"""
)

# Click Accept on an incoming-request profile. Accept is the FIRST labeled
# button in the fingerprinted row — primary actions render first in
# top-card action rows (Connect/Message lead on other profile states; the
# inverse of dialogs, where the primary button renders last). Clicking the
# second button would silently and irreversibly Ignore the request, so the
# click only fires when the full fingerprint matched.
CLICK_INCOMING_ACCEPT_JS = (
    r"""
(() => {
"""
    + _FIND_INCOMING_ACTION_ROW_FN_JS
    + r"""
  const main = document.querySelector('main');
  if (!main) return false;
  const row = findIncomingActionRow(main);
  if (!row) return false;
  row.querySelectorAll('button[aria-label]')[0].click();
  return true;
})
"""
)


def _connection_result(
    url: str,
    status: str,
    message: str,
    *,
    note_sent: bool = False,
    profile: str = "",
) -> dict[str, Any]:
    """Build a structured response for a profile connection attempt."""
    result: dict[str, Any] = {
        "url": url,
        "status": status,
        "message": message,
        "note_sent": note_sent,
    }
    if profile:
        result["profile"] = profile
    return result


# One main-profile read of one member, by username. The person workflow owns
# that read and this one only ever needs its ``main_profile`` text, so the
# borrow is a callable rather than the scraper: this module never learns what
# the facade is, and the day the read moves again only the wiring does.
ReadMainProfile = Callable[[str], Awaitable[dict[str, Any]]]


class ConnectionActions:
    """Send, accept and probe invitations for one LinkedIn member."""

    def __init__(
        self,
        session: ScrapingSession,
        navigator: PageNavigator,
        read_main_profile: ReadMainProfile,
    ):
        self._session = session
        self._navigator = navigator
        self._read_main_profile = read_main_profile

    async def _dialog_is_open(self, *, timeout: int = 1000) -> bool:
        """Return whether a dialog is currently open (structural check)."""
        locator = self._session.page.locator(_DIALOG_SELECTOR)
        try:
            if await locator.count() == 0:
                return False
            await locator.first.wait_for(state="visible", timeout=timeout)
            return True
        except Exception:
            return False

    async def _click_dialog_primary_button(self, *, timeout: int = 5000) -> bool:
        """Click the last (primary/Send) button in the open dialog.

        LinkedIn consistently places the primary action as the last button.
        Returns False (rather than raising) when the click is intercepted or
        times out, so callers can fall back to a keyboard submit.
        """
        buttons = self._session.page.locator(
            f"{_DIALOG_SELECTOR} button, {_DIALOG_SELECTOR} [role='button']"
        )
        count = await buttons.count()
        if count == 0:
            return False
        try:
            await buttons.nth(count - 1).click(timeout=timeout)
            return True
        except Exception:
            logger.debug("Primary dialog button click failed", exc_info=True)
            return False

    async def _fill_dialog_textarea(self, value: str, *, timeout: int = 5000) -> bool:
        """Fill the first textarea inside the open dialog (structural)."""
        locator = self._session.page.locator(_DIALOG_TEXTAREA_SELECTOR).first
        try:
            if await self._session.page.locator(_DIALOG_TEXTAREA_SELECTOR).count() == 0:
                return False
            await locator.fill(value, timeout=timeout)
            return True
        except Exception:
            return False

    async def _dismiss_dialog(self) -> None:
        """Dismiss any open dialog via Escape key (structural)."""
        await self._session.page.keyboard.press("Escape")
        try:
            await self._session.page.wait_for_selector(
                _DIALOG_SELECTOR, state="hidden", timeout=3000
            )
        except PlaywrightTimeoutError:
            pass

    async def _get_premium_upsell_message(self, *, timeout: int = 2500) -> str | None:
        """Return the raw LinkedIn Premium upsell dialog text when visible.

        LinkedIn intercepts invite-with-note flows with an upsell modal when
        the free personalized-note quota is exhausted. The detector itself is
        locale-independent: the modal links to ``/premium/...``. The returned
        message is the dialog text as rendered by LinkedIn, not a synthesized
        explanation.
        """
        locator = self._session.page.locator(_DIALOG_PREMIUM_LINK_SELECTOR).first
        try:
            await locator.wait_for(state="visible", timeout=timeout)
        except PlaywrightTimeoutError:
            return None
        except Exception:
            try:
                if not await locator.is_visible():
                    return None
            except Exception:
                return None

        try:
            message = await self._session.page.evaluate(
                """() => {
                    const link = document.querySelector(
                        'dialog[open] a[href*="/premium/"], [role="dialog"] a[href*="/premium/"]'
                    );
                    const dialog = link?.closest('dialog,[role="dialog"]');
                    return dialog?.innerText || dialog?.textContent || link?.innerText || '';
                }"""
            )
            if isinstance(message, str) and message.strip():
                return message.strip()
        except Exception:
            logger.debug("Could not read Premium upsell dialog text", exc_info=True)

        try:
            link_text = await locator.inner_text()
            if link_text.strip():
                return link_text.strip()
        except Exception:
            pass
        return "LinkedIn Premium upsell modal detected."

    async def _open_more_menu(self) -> bool:
        """Open the profile's More (three-dot) menu in a locale-independent way.

        Locates the More button structurally as ``actionRoot
        button[aria-expanded]`` — the action-root walk discriminates the
        profile More button from any other More-labelled buttons elsewhere
        on the page (notably the video-player More on profiles with
        background videos), and ``aria-expanded`` distinguishes the menu
        opener from primary action buttons (which carry ``aria-label``
        instead). Returns True iff the click landed and a ``[role='menu']``
        became visible. The caller is expected to follow up with
        ``_read_action_signals`` to scan the now-rendered menu items for
        the vanityName invite anchor; this helper does not classify menu
        contents itself.
        """
        try:
            clicked = await self._session.page.evaluate(OPEN_MORE_BUTTON_JS)
        except Exception:
            logger.debug("More button click via JS failed", exc_info=True)
            return False
        if not clicked:
            return False
        try:
            await self._session.page.wait_for_selector("[role='menu']", timeout=3000)
            return True
        except PlaywrightTimeoutError:
            logger.debug("More menu did not appear after click")
            return False

    async def _click_incoming_accept(self) -> bool:
        """Click Accept on an incoming-request profile, locale-independently.

        Delegates to ``CLICK_INCOMING_ACCEPT_JS``: the click fires only
        when the full incoming-row fingerprint matches, and it targets the
        FIRST labeled button (Accept renders before Ignore — primary
        actions lead in top-card rows). Clicking the second button would
        silently and irreversibly Ignore the request; the strict
        fingerprint plus the caller's verify-after-click are the
        mitigations. Returns True iff the click landed.
        """
        try:
            return bool(await self._session.page.evaluate(CLICK_INCOMING_ACCEPT_JS))
        except Exception:
            logger.debug("Incoming accept click via JS failed", exc_info=True)
            return False

    async def _read_action_signals(self, username: str) -> ActionSignals:
        """Read locale-independent structural signals for a profile's
        relationship state.

        Detection uses URL patterns and ARIA attribute presence only — never
        text values — per the AGENTS.md Scraping Rules. The vanityName invite
        anchor is searched document-wide because LinkedIn renders the More
        menu's contents in a portal-mounted ``[role='menu']`` outside ``<main>``;
        the URL is uniquely scoped to the target user, so document-wide
        search introduces no false positives. The compose anchor used for
        action-root discovery is scoped to ``<main>`` to avoid the
        portal-rendered "Send profile in a message" anchor that appears
        inside the More menu after click.
        """
        data = await self._session.page.evaluate(ACTION_SIGNALS_JS, username)
        if not isinstance(data, dict):
            return ActionSignals(
                has_invite_anchor=False,
                has_compose_anchor_in_action_root=False,
                has_edit_intro_anchor=False,
                has_labeled_action_button=False,
                has_labeled_action_anchor=False,
                has_incoming_action_row=False,
            )
        return ActionSignals(
            has_invite_anchor=bool(data.get("hasInvite")),
            has_compose_anchor_in_action_root=bool(data.get("hasComposeInActionRoot")),
            has_edit_intro_anchor=bool(data.get("hasEditIntro")),
            has_labeled_action_button=bool(data.get("hasLabeledActionButton")),
            has_labeled_action_anchor=bool(data.get("hasLabeledActionAnchor")),
            has_incoming_action_row=bool(data.get("hasIncomingActionRow")),
        )

    async def _submit_invite_dialog(
        self, note: str | None
    ) -> tuple[bool, bool, str | None]:
        """Submit the invite dialog opened by the custom-invite deeplink.

        Returns ``(submitted, note_sent, note_limit_message)``.

        ``note_sent`` reports *delivery*, not textarea fill — it stays
        False on any failure path, including the Premium upsell that
        LinkedIn shows when the free personalized-note quota is exhausted.
        ``note_limit_message`` is the raw LinkedIn Premium dialog text when
        the upsell was detected; in that case ``submitted`` is False, the
        dialog is dismissed, and callers should surface that text directly.

        All interaction uses structural selectors and positional indexing
        — no localized text matching. Owns dialog cleanup: the dialog is
        dismissed on every failure path, callers must not dismiss again.
        """
        if not await self._dialog_is_open(timeout=5000):
            return False, False, None

        note_filled = False
        if note:
            textarea_count = await self._session.page.locator(
                _DIALOG_TEXTAREA_SELECTOR
            ).count()
            if textarea_count == 0:
                # Reveal the note textarea via the secondary action.
                # Two layouts are now in the wild and both place "Add a
                # note" at index ``btn_count - 2``:
                #   * Legacy invite dialog (3 buttons): dismiss, secondary
                #     "Add a note", primary "Send" -> nth(1) is secondary.
                #   * "Add a note to your invitation?" gating dialog (2
                #     buttons, rolled out 2026-05): "Add a note",
                #     "Send without a note" -> nth(0) is the only path
                #     that mounts the textarea. See issue #455.
                # If LinkedIn ever serves a 2-button dismiss/primary
                # no-note layout, the click below misroutes to dismiss;
                # the textarea-presence recheck via _fill_dialog_textarea
                # then fails and the caller returns connect_unavailable
                # without sending — the same outcome as today.
                buttons = self._session.page.locator(
                    f"{_DIALOG_SELECTOR} button, {_DIALOG_SELECTOR} [role='button']"
                )
                btn_count = await buttons.count()
                if btn_count >= 2:
                    await buttons.nth(btn_count - 2).click()
                    try:
                        await self._session.page.wait_for_selector(
                            _DIALOG_TEXTAREA_SELECTOR,
                            state="visible",
                            timeout=3000,
                        )
                    except PlaywrightTimeoutError:
                        logger.debug("Note textarea did not appear")
                    note_limit_message = await self._get_premium_upsell_message()
                    if note_limit_message is not None:
                        logger.info("Premium upsell blocked opening invite note editor")
                        await self._dismiss_dialog()
                        return False, False, note_limit_message

            note_filled = await self._fill_dialog_textarea(note)
            if not note_filled:
                note_limit_message = await self._get_premium_upsell_message()
                if note_limit_message is not None:
                    logger.info("Premium upsell blocked filling invite note")
                    await self._dismiss_dialog()
                    return False, False, note_limit_message
                await self._dismiss_dialog()
                return False, False, None

        sent = await self._click_dialog_primary_button()
        if not sent:
            # Fallback: focus the primary button positionally so a subsequent
            # Enter targets it instead of a focused textarea (where Enter
            # would just insert a newline).
            buttons = self._session.page.locator(
                f"{_DIALOG_SELECTOR} button, {_DIALOG_SELECTOR} [role='button']"
            )
            btn_count = await buttons.count()
            if btn_count > 0:
                try:
                    await buttons.nth(btn_count - 1).focus()
                    await self._session.page.keyboard.press("Enter")
                    sent = not await self._dialog_is_open(timeout=2000)
                except Exception:
                    logger.debug("Keyboard submit fallback failed", exc_info=True)
            if not sent:
                # The Send click can also fail because LinkedIn swapped the
                # invite dialog for the Premium upsell at submit time — the
                # original primary button is then detached or pointer-event
                # covered, so the click raises or times out. Check for the
                # upsell here so we surface the raw note-limit message
                # instead of dismissing silently and returning
                # connect_unavailable.
                if note:
                    note_limit_message = await self._get_premium_upsell_message()
                    if note_limit_message is not None:
                        logger.info(
                            "Premium upsell modal intercepted invite submit click"
                        )
                        await self._dismiss_dialog()
                        return False, False, note_limit_message
                await self._dismiss_dialog()
                return False, False, None

        # LinkedIn may swap the invite dialog for a Premium upsell when the
        # free note quota is exhausted. The textarea was filled but the
        # invite was not delivered — surface LinkedIn's raw dialog text.
        if note:
            note_limit_message = await self._get_premium_upsell_message()
            if note_limit_message is not None:
                logger.info("Premium upsell modal intercepted invite submit")
                await self._dismiss_dialog()
                return False, False, note_limit_message

        try:
            await self._session.page.wait_for_selector(
                _DIALOG_SELECTOR, state="hidden", timeout=5000
            )
        except PlaywrightTimeoutError:
            logger.debug("Invite dialog did not close after submit")

        return True, note_filled, None

    async def _probe_invite_note_limit(self) -> str | None:
        """Open the note editor only to read a Premium note-quota message.

        This is used when the profile did not expose the normal invite anchor.
        Navigating to the custom-invite deeplink and opening the note editor is
        non-destructive, but submitting would weaken the write gate for
        follow-only/unavailable profiles. Therefore this helper never clicks
        the primary Send button: it returns the raw LinkedIn Premium dialog
        text if LinkedIn shows it while opening the note editor, then
        dismisses the dialog.
        """
        if not await self._dialog_is_open(timeout=5000):
            return None
        note_limit_message = await self._get_premium_upsell_message(timeout=500)
        if note_limit_message is not None:
            await self._dismiss_dialog()
            return note_limit_message

        try:
            textarea_count = await self._session.page.locator(
                _DIALOG_TEXTAREA_SELECTOR
            ).count()
        except Exception:
            textarea_count = 0
        if textarea_count > 0:
            await self._dismiss_dialog()
            return None

        buttons = self._session.page.locator(
            f"{_DIALOG_SELECTOR} button, {_DIALOG_SELECTOR} [role='button']"
        )
        try:
            btn_count = await buttons.count()
        except Exception:
            btn_count = 0
        if btn_count >= 3:
            try:
                await buttons.nth(btn_count - 2).click()
            except Exception:
                logger.debug("Could not open invite note editor", exc_info=True)
            try:
                await self._session.page.wait_for_selector(
                    _DIALOG_TEXTAREA_SELECTOR,
                    state="visible",
                    timeout=3000,
                )
            except PlaywrightTimeoutError:
                logger.debug("Note textarea did not appear during quota probe")

        note_limit_message = await self._get_premium_upsell_message()
        await self._dismiss_dialog()
        return note_limit_message

    async def connect_with_person(
        self,
        username: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Send a LinkedIn connection request or accept an incoming one.

        Detection is locale-independent: classification uses URL patterns
        (vanityName invite anchor, edit-intro anchor) and ARIA-attribute
        presence on top-card buttons (`aria-label` for primary actions,
        `aria-expanded` for the More-menu opener). The deeplink-submit
        path is gated strictly on `has_invite_anchor=True` *after* the
        optional More-menu retry, so Pending and follow-only profiles
        cannot trigger a write. If a note was requested but no invite
        anchor is visible, the custom-invite deeplink may still be opened
        only as a non-submitting note-quota probe. Sending itself uses the
        ``/preload/custom-invite/?vanityName=`` deeplink, which works
        whether the user-visible Connect button is in the action bar
        or buried under the More menu.
        """
        username = normalize_person_identifier(username)
        url = person_profile_url(username, "/")

        profile = await self._read_main_profile(username)
        page_text = profile.get("sections", {}).get("main_profile", "")
        if not page_text:
            return _connection_result(
                url, "unavailable", "Could not read profile page."
            )

        signals = await self._read_action_signals(username)
        state = connection.detect_connection_state(signals)
        logger.info(
            "Connection signals for %s: state=%s signals=%s", username, state, signals
        )

        if state == "self_profile":
            return _connection_result(
                url,
                "connect_unavailable",
                "Cannot send a connection request to your own profile.",
                profile=page_text,
            )
        if state == "already_connected":
            return _connection_result(
                url,
                "already_connected",
                "You are already connected with this profile.",
                profile=page_text,
            )
        if state == "pending":
            return _connection_result(
                url,
                "pending",
                "A connection request is already pending for this profile.",
                profile=page_text,
            )

        if state == "incoming_request":
            # Accept clicks the first labeled button in the fingerprinted
            # row. There is deliberately no locale-text fallback: clicking
            # a button matched by exact text anywhere in the page risks
            # hitting the wrong control (or the Ignore button in another
            # locale), and accepting/ignoring is irreversible. When the
            # fingerprint does not match we report send_failed rather than
            # guess.
            clicked = await self._click_incoming_accept()
            if not clicked:
                return _connection_result(
                    url,
                    "send_failed",
                    "Could not find or click the Accept button.",
                    profile=page_text,
                )
            # LinkedIn propagates the accepted state asynchronously; an
            # immediate re-read can still render the old top card and
            # would report send_failed for a successful accept (observed
            # live 2026-06-11). Verify with one settle retry.
            verified_text = ""
            verified_state = None
            for attempt in range(2):
                if attempt:
                    await asyncio.sleep(3.0)
                verified = await self._read_main_profile(username)
                verified_text = verified.get("sections", {}).get("main_profile", "")
                verified_signals = await self._read_action_signals(username)
                verified_state = connection.detect_connection_state(verified_signals)
                if verified_state == "already_connected":
                    break
            if verified_state != "already_connected":
                return _connection_result(
                    url,
                    "send_failed",
                    "Accepted, but the profile did not transition to 1st-degree.",
                    profile=verified_text or page_text,
                )
            return _connection_result(
                url,
                "accepted",
                "Connection request accepted.",
                profile=verified_text,
            )

        # Follow-only profiles may have Connect hidden under the More menu
        # (high-follower / creator-mode profiles). Try opening it and
        # re-reading signals; if the vanityName invite anchor surfaces in
        # the menu, we can proceed with the deeplink. (The
        # has_invite_anchor=False guard is implicit: detect_connection_state
        # only returns "follow_only" after the has_invite_anchor branch
        # has already failed, so reaching this branch already implies it.)
        if state == "follow_only":
            opened = await self._open_more_menu()
            if opened:
                signals = await self._read_action_signals(username)
                # Close the menu before any subsequent navigation so it
                # doesn't intercept the upcoming page transition.
                try:
                    await self._session.page.keyboard.press("Escape")
                except Exception:
                    logger.debug("Escape after More-menu reread failed", exc_info=True)
                logger.info("Post-More signals for %s: signals=%s", username, signals)

        invite_url = (
            "https://www.linkedin.com/preload/custom-invite/"
            f"?vanityName={quote_plus(username)}"
        )

        # Write-gate: submit only when LinkedIn exposed the vanityName invite
        # anchor. When a note is requested without that anchor, open the
        # deeplink only as a non-submitting probe so we can report the Premium
        # note-quota block without accidentally sending from a follow-only or
        # otherwise unavailable profile.
        if not signals.has_invite_anchor:
            if note:
                logger.info(
                    "No visible invite anchor for %s; probing custom-invite deeplink "
                    "because a personalized note was requested",
                    username,
                )
                await self._navigator._navigate_to_page(invite_url)
                note_limit_message = await self._probe_invite_note_limit()
                if note_limit_message is not None:
                    return _connection_result(
                        url,
                        "custom_note_limit_reached",
                        note_limit_message,
                        note_sent=False,
                        profile=page_text,
                    )
            return _connection_result(
                url,
                "connect_unavailable",
                "LinkedIn did not expose a usable Connect action for this profile.",
                profile=page_text,
            )

        await self._navigator._navigate_to_page(invite_url)

        submitted, note_sent, note_limit_message = await self._submit_invite_dialog(
            note
        )
        if note_limit_message is not None:
            return _connection_result(
                url,
                "custom_note_limit_reached",
                note_limit_message,
                note_sent=False,
                profile=page_text,
            )
        if not submitted:
            return _connection_result(
                url,
                "connect_unavailable",
                "LinkedIn did not open a usable invite dialog for this profile.",
                profile=page_text,
            )

        verified = await self._read_main_profile(username)
        verified_text = verified.get("sections", {}).get("main_profile", "")
        verified_signals = await self._read_action_signals(username)
        verified_state = connection.detect_connection_state(verified_signals)

        if verified_signals.has_invite_anchor:
            return _connection_result(
                url,
                "send_failed",
                "Submitted the invite dialog but the profile still exposes Connect.",
                note_sent=note_sent,
                profile=verified_text or page_text,
            )

        return _connection_result(
            url,
            "connected",
            "Connection request sent."
            + (f" State after send: {verified_state}." if verified_state else ""),
            note_sent=note_sent,
            profile=verified_text or page_text,
        )
