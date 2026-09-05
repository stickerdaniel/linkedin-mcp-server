# tests/test_send_message_confirmation_dom.py
"""Browser-DOM tests for the send_message post-send confirmation (issue #866).

The unit suite mocks ``page.evaluate``, so none of what decides "sent" ever
runs there: the JS focus, the keyboard typing into a contenteditable, the
Send click and the occurrence count taken across the resulting DOM. These
cases drive the production ``send_message`` path in headless chromium and
replace navigation and recipient discovery only, so every step the
confirmation depends on executes unchanged: recipient verification reads
this page's own identity and submission clicks this page's own button.
Skipped automatically when
chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.

Both fixtures place an identical earlier message in the thread, which is
what made "the text is somewhere on the page" worthless as evidence: the
composer holds the message before it is sent, and the earlier copy holds it
whether or not the send succeeds.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.extractor import (
    LinkedInExtractor,
    _ProfileMessageTarget,
)

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' wall-clock
#: timers.
#: Without that distribution mode the group mark is inert.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

DISPLAY_NAME = "Fadi Al Eliwi"
MESSAGE = "UNDELIVERED SENTINEL"
DRAFT = "Draft: "
COMPOSE_URL = "https://www.linkedin.com/messaging/compose/?recipient=ACoAAB"
PROFILE_PATH = "/in/fadi-eliwi/"
TARGET = _ProfileMessageTarget(
    profile_path=PROFILE_PATH,
    profile_urn="ACoAAB",
    compose_url=COMPOSE_URL,
    display_name=DISPLAY_NAME,
)

# Records the click as a body attribute and does nothing else: the button is
# visible and enabled, so the production JS click path succeeds while the
# message never leaves the composer.
NOOP_SEND_JS = """
  document.getElementById('send').addEventListener('click', () => {
    document.body.setAttribute('data-clicked', 'true');
  });
"""

# Goes read-only for the duration of an in-flight send and delivers nothing,
# which is what a React composer commonly does while it waits. The text stays
# on screen the whole time, so an occurrence subtracted at baseline has to
# stay subtracted or this no-op confirms itself.
READONLY_NOOP_SEND_JS = """
  document.getElementById('send').addEventListener('click', () => {
    document.body.setAttribute('data-clicked', 'true');
    document.getElementById('composer').setAttribute('contenteditable', 'false');
  });
"""

# Moves the composer's text into the thread as a plain, non-editable entry,
# which is what a delivered message looks like to the page.
DELIVERING_SEND_JS = """
  document.getElementById('send').addEventListener('click', () => {
    document.body.setAttribute('data-clicked', 'true');
    const composer = document.getElementById('composer');
    const text = composer.innerText;
    if (!text.trim()) return;
    const entry = document.createElement('div');
    entry.className = 'msg';
    entry.textContent = text;
    document.getElementById('thread').appendChild(entry);
    composer.textContent = '';
  });
"""


def compose_page(send_js: str, *, draft: str = "") -> str:
    """A compose surface holding one earlier copy of the same message.

    The recipient link sits beside the editor rather than inside it, which is
    what lets the production verification resolve an identity at all: draft
    content never authorizes anyone.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8"><title>Messaging</title></head>
  <body>
    <main>
      <section id="conversation">
        <a id="recipient" href="https://www.linkedin.com{PROFILE_PATH}">
          {DISPLAY_NAME}</a>
        <div id="thread">
          <div class="msg">{MESSAGE}</div>
        </div>
        <div id="composer" role="textbox" contenteditable="true"
          aria-label="Write a message…">{draft}</div>
        <button id="send" type="submit">Send</button>
      </section>
    </main>
    <script>{send_js}</script>
  </body>
</html>
"""


@pytest.fixture
async def dom_page():
    """Real chromium page, or skip when no browser is installed.

    Only launch/setup is guarded by the skip — the ``yield`` is outside it
    so an assertion failure or JS error in a test body is never swallowed
    into a skip.

    ``channel="chromium"`` names the browser this project installs. Without
    it Playwright picks the *binary* from the ``headless`` flag alone and
    asks for ``chromium-headless-shell``, which nothing here installs since
    the setup moved to ``--no-shell``: the launch would fail and every case
    in this file would skip itself, silently, wherever the real browser is.
    """
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        # The confirmation waits on the page-level default, so a failed send
        # has to give up quickly here.
        page.set_default_timeout(1500)
        try:
            yield page
        finally:
            await browser.close()


async def send(page, html: str, *, message: str = MESSAGE) -> dict:
    """Run the real send path against `html`, mocking discovery only.

    Only the two steps that need a live LinkedIn are replaced: reading the
    target off a profile page, and the messaging-URL guard, which cannot pass
    for the ``about:blank`` a ``set_content`` page reports. Both have their own
    unit tests. Everything the confirmation rests on runs here for real.
    """
    await page.set_content(html)
    extractor = LinkedInExtractor(page)
    with (
        patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(
            extractor,
            "_read_profile_message_target",
            new_callable=AsyncMock,
            return_value=TARGET,
        ),
        patch(
            "linkedin_mcp_server.scraping.extractor._message_page_url_is_safe",
            return_value=True,
        ),
    ):
        return await extractor.send_message("fadi-eliwi", message, confirm_send=True)


async def text_of(page, selector: str) -> str:
    return (await page.locator(selector).inner_text()).strip()


class TestSendConfirmationAgainstRealDom:
    @pytest.mark.parametrize("message", ["", " \t\n"], ids=["empty", "whitespace"])
    async def test_blank_message_leaves_existing_draft_untouched(
        self, dom_page, message
    ):
        draft = "Confidential draft"
        result = await send(
            dom_page,
            compose_page(DELIVERING_SEND_JS, draft=draft),
            message=message,
        )

        assert result["status"] == "message_unavailable"
        assert result["sent"] is False
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert await text_of(dom_page, "#composer") == draft
        assert await dom_page.locator("#thread .msg").count() == 1

    async def test_foreign_recipient_never_reaches_the_composer(self, dom_page):
        # Issue #861: the composer belongs to someone else. Nothing may be
        # typed and nothing may be clicked, so the message the caller wrote
        # cannot reach a person they never named.
        page = compose_page(DELIVERING_SEND_JS).replace(
            f'href="https://www.linkedin.com{PROFILE_PATH}"',
            'href="https://www.linkedin.com/in/someone-else/"',
        )
        result = await send(dom_page, page)

        assert result["sent"] is False
        assert result["status"] == "composer_unavailable"
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert await text_of(dom_page, "#composer") == ""
        assert await dom_page.locator("#thread .msg").count() == 1

    async def test_existing_draft_is_never_sent_along(self, dom_page):
        # Measured, and the reason this fixture carries a draft at all:
        # `element.focus()` leaves the caret at the *start* of a
        # contenteditable in Chromium, so a typed message lands in front of
        # whatever the composer already held and LinkedIn delivers the two as
        # one. The draft is the user's text and nobody asked for it to be
        # sent, so the send refuses here. Clearing it instead would trade the
        # leak for destroying it.
        result = await send(dom_page, compose_page(DELIVERING_SEND_JS, draft=DRAFT))

        assert result["status"] == "composer_occupied"
        assert result["sent"] is False
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert await text_of(dom_page, "#composer") == DRAFT.strip()
        assert await dom_page.locator("#thread .msg").count() == 1

    async def test_ineffective_send_is_not_confirmed(self, dom_page):
        # The reported failure: Send is clicked, the handler does nothing,
        # and the message stays in the composer next to an identical earlier
        # copy in the thread. Neither is evidence of delivery. The click did
        # happen, though, so the answer is "unknown" rather than "not sent".
        result = await send(dom_page, compose_page(NOOP_SEND_JS))

        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"
        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert await text_of(dom_page, "#composer") == MESSAGE
        assert await dom_page.locator("#thread .msg").count() == 1

    async def test_composer_going_read_only_does_not_confirm(self, dom_page):
        # The same no-op, except the composer stops being editable while it
        # waits. Counting only `[contenteditable="true"]` would stop
        # subtracting the unsent text at exactly that moment and read its
        # own draft as a delivery.
        result = await send(dom_page, compose_page(READONLY_NOOP_SEND_JS))

        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"
        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert await text_of(dom_page, "#composer") == MESSAGE
        assert await dom_page.locator("#thread .msg").count() == 1

    async def test_delivered_message_is_confirmed(self, dom_page):
        # Same page, same earlier copy, but the Send handler moves the text
        # into the thread. The count outside the composer grows, so this one
        # confirms where the ineffective click above did not.
        result = await send(dom_page, compose_page(DELIVERING_SEND_JS))

        assert result["status"] == "sent"
        assert result["sent"] is True
        assert await text_of(dom_page, "#composer") == ""
        entries = dom_page.locator("#thread .msg")
        assert await entries.count() == 2
        assert (await entries.last.inner_text()).strip() == MESSAGE
