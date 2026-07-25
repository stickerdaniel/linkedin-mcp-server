# tests/test_action_signals_dom.py
"""Browser-DOM tests for the incoming-request action-row fingerprint.

The unit suite mocks ``page.evaluate``, so the JS in ``_ACTION_SIGNALS_JS``
and ``_CLICK_INCOMING_ACCEPT_JS`` never executes there. These tests run the
real JS against synthetic HTML in headless chromium. Fixtures use German
labels throughout: the fingerprint must classify without reading any label
text. Skipped automatically when chromium is not installed (CI installs no
browser; run locally after ``uv run patchright install chromium``).

Fixture structure mirrors the live DOM dumps of two incoming-request
profiles (2026-06-11): three buttons sharing one parent, Accept and Ignore
carrying aria-label, More carrying aria-expanded without aria-label, plus
sidebar cards with labeled compose anchors and other-user invite anchors.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.extractor import (
    _ACTION_SIGNALS_JS,
    _CLICK_INCOMING_ACCEPT_JS,
)

pytestmark = pytest.mark.browser_dom


# Each constant is a full <section>. The top card is always the first
# section of <main>; the fingerprint is scoped there, so sidebar and feed
# widgets live in later sections and must never match.

INCOMING_ACTION_ROW = """
  <div class="actions">
    <button type="button" aria-label="Kontaktanfrage von Eric Langlouis annehmen"
      onclick="document.body.setAttribute('data-clicked','accept')">Annehmen</button>
    <button type="button" aria-label="Kontaktanfrage von Eric Langlouis ignorieren"
      onclick="document.body.setAttribute('data-clicked','ignore')">Ignorieren</button>
    <button type="button" aria-expanded="false">Mehr</button>
  </div>
"""

INCOMING_TOP_CARD = f"""
<section class="topcard">
  <h1>Eric Langlouis</h1>
  {INCOMING_ACTION_ROW}
</section>
"""

VIDEO_PLAYER_BAR = """
  <div class="player">
    <button type="button" aria-label="Abspielen">▶</button>
    <button type="button" aria-label="Stummschalten">🔇</button>
    <button type="button" aria-label="Untertitel">CC</button>
    <button type="button" aria-label="Vollbild">⛶</button>
    <button type="button" aria-expanded="false" aria-label="Einstellungen">⚙</button>
  </div>
"""

# Cover-video profile: the player's expander renders before the action row
# within the same top card. The scan must skip it and still find the row.
INCOMING_TOP_CARD_WITH_COVER = f"""
<section class="topcard">
  <h1>Eric Langlouis</h1>
  {VIDEO_PLAYER_BAR}
  {INCOMING_ACTION_ROW}
</section>
"""

SIDEBAR_SECTION = """
<section class="sidebar">
  <div class="card">
    <a href="https://www.linkedin.com/in/julien-f/">Julien</a>
    <a href="/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3AAAA"
      aria-label="Nachricht an Julien senden">Nachricht</a>
  </div>
  <div class="card">
    <a href="https://www.linkedin.com/in/rahul-g/">Rahul</a>
    <a href="/preload/custom-invite/?vanityName=rahul-g"
      aria-label="Rahul als Kontakt einladen">Vernetzen</a>
  </div>
  <button type="button">Mehr anzeigen</button>
</section>
"""

# A widget elsewhere in main with the exact incoming-row shape (two labeled
# buttons + one unlabeled expander). It must NOT match because it lives in a
# later section, outside the scoped top card.
UNRELATED_MATCHING_WIDGET = """
<section class="feed">
  <div class="actions">
    <button type="button" aria-label="Gefällt mir">A</button>
    <button type="button" aria-label="Kommentieren">B</button>
    <button type="button" aria-expanded="false">Mehr</button>
  </div>
</section>
"""

CONNECTED_TOP_CARD = """
<section class="topcard">
  <h1>Fadi Al Eliwi</h1>
  <div class="actions">
    <a href="/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3ABBB"
      aria-disabled="false">Nachricht</a>
    <button type="button" aria-expanded="false">Mehr</button>
  </div>
</section>
"""

FOLLOW_ONLY_TOP_CARD = """
<section class="topcard">
  <h1>Verena</h1>
  <div class="actions">
    <button type="button" aria-label="Verena folgen">Folgen</button>
    <a href="/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3ACCC">Nachricht</a>
    <button type="button" aria-expanded="false">Mehr</button>
  </div>
</section>
"""

PENDING_TOP_CARD = """
<section class="topcard">
  <h1>Florian</h1>
  <div class="actions">
    <a href="/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3ADDD">Nachricht</a>
    <a href="https://www.linkedin.com/in/florian/"
      aria-label="Ausstehend, klicken zum Zurückziehen">Ausstehend</a>
    <button type="button" aria-expanded="false">Mehr</button>
  </div>
</section>
"""

EXPANDER_FIRST_BAR = """
<section class="hostile">
  <button type="button" aria-expanded="false">⚙</button>
  <button type="button" aria-label="Aktion A">A</button>
  <button type="button" aria-label="Aktion B">B</button>
</section>
"""

EXTRA_BUTTON_ROW = """
<section class="hostile">
  <button type="button" aria-label="Aktion A">A</button>
  <button type="button" aria-label="Aktion B">B</button>
  <button type="button" aria-expanded="false">Mehr</button>
  <button type="button">Extra</button>
</section>
"""


def _page_html(*sections: str) -> str:
    return f"<html><body><main>{''.join(sections)}</main></body></html>"


@pytest.fixture
async def dom_page():
    """Real chromium page, or skip when no browser is installed.

    Only launch/setup is guarded by the skip — the ``yield`` is outside it
    so an assertion failure or JS error in a test body is never swallowed
    into a skip.
    """
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield page
        finally:
            await browser.close()


async def _signals(page, html: str) -> dict:
    await page.set_content(html)
    return await page.evaluate(_ACTION_SIGNALS_JS, "testuser")


class TestIncomingActionRowFingerprint:
    async def test_incoming_row_detected_next_to_sidebar_cards(self, dom_page):
        data = await _signals(dom_page, _page_html(INCOMING_TOP_CARD, SIDEBAR_SECTION))
        assert data["hasIncomingActionRow"] is True

    async def test_video_player_bar_not_detected(self, dom_page):
        data = await _signals(
            dom_page, _page_html(CONNECTED_TOP_CARD, VIDEO_PLAYER_BAR)
        )
        assert data["hasIncomingActionRow"] is False

    async def test_expander_first_order_guard(self, dom_page):
        data = await _signals(dom_page, _page_html(EXPANDER_FIRST_BAR))
        assert data["hasIncomingActionRow"] is False

    async def test_preceding_nonmatching_expander_does_not_abort_scan(self, dom_page):
        # Cover-video layout: the player's expander renders before the
        # action row inside the same top card; the scan must continue past
        # it and still find the row.
        data = await _signals(dom_page, _page_html(INCOMING_TOP_CARD_WITH_COVER))
        assert data["hasIncomingActionRow"] is True

    async def test_matching_widget_outside_top_card_not_detected(self, dom_page):
        # F1 regression: a widget with the exact incoming-row shape in a
        # later section must not match — the scan is scoped to the top card.
        data = await _signals(
            dom_page, _page_html(CONNECTED_TOP_CARD, UNRELATED_MATCHING_WIDGET)
        )
        assert data["hasIncomingActionRow"] is False

    async def test_extra_unlabeled_button_fails_count_guard(self, dom_page):
        data = await _signals(dom_page, _page_html(EXTRA_BUTTON_ROW))
        assert data["hasIncomingActionRow"] is False

    async def test_follow_only_row_not_detected(self, dom_page):
        data = await _signals(dom_page, _page_html(FOLLOW_ONLY_TOP_CARD))
        assert data["hasIncomingActionRow"] is False

    async def test_pending_row_not_detected(self, dom_page):
        data = await _signals(dom_page, _page_html(PENDING_TOP_CARD))
        assert data["hasIncomingActionRow"] is False

    async def test_connected_row_not_detected(self, dom_page):
        data = await _signals(dom_page, _page_html(CONNECTED_TOP_CARD, SIDEBAR_SECTION))
        assert data["hasIncomingActionRow"] is False


class TestClickIncomingAccept:
    async def test_clicks_first_labeled_button_only(self, dom_page):
        await dom_page.set_content(_page_html(INCOMING_TOP_CARD, SIDEBAR_SECTION))
        clicked = await dom_page.evaluate(_CLICK_INCOMING_ACCEPT_JS)
        assert clicked is True
        # Patchright evaluates in an isolated world; page-world variables
        # are invisible there, but the DOM is shared — the inline onclick
        # records the click as a body attribute.
        recorded = await dom_page.evaluate("document.body.getAttribute('data-clicked')")
        assert recorded == "accept"

    async def test_no_click_without_fingerprint_match(self, dom_page):
        await dom_page.set_content(_page_html(FOLLOW_ONLY_TOP_CARD, VIDEO_PLAYER_BAR))
        clicked = await dom_page.evaluate(_CLICK_INCOMING_ACCEPT_JS)
        assert clicked is False
