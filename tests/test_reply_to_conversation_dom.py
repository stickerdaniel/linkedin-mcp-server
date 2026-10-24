"""Browser-DOM tests for reply_to_conversation's thread and composer binding.

The unit suite mocks ``page.evaluate``, so which editor and which button the
production JavaScript picks on a thread page needs a real DOM. The page is
synthetic: it claims nothing about LinkedIn's markup beyond what the scoping
rules read (``main``, a form, a textbox, submit buttons, message-list items).
No LinkedIn request or write is made.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin.message_sender import MessageSender
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

THREAD_ID = "2-abc=="
THREAD_ROUTE = "https://www.linkedin.com/messaging/thread/2-abc==/"
MESSAGE = "Thanks, happy to talk this week"

COMPOSER = """
  <form id="composer-scope" onsubmit="return false">
    <div id="composer" role="textbox" contenteditable="true"
         style="display:block;width:200px;height:30px"></div>
    <button id="send" type="submit">Send</button>
  </form>
"""

# Quick replies in their own form and typed as submit buttons: the worst case
# for a picker that looked for "a submit button near the thread".
QUICK_REPLIES = """
  <form id="quick-replies" onsubmit="return false">
    <button class="chip" type="submit">Yes, interested...</button>
    <button class="chip" type="submit">No thanks...</button>
  </form>
"""

SEND_JS = """
  const send = document.getElementById('send');
  if (send) send.addEventListener('click', event => {
    event.preventDefault();
    const composer = document.getElementById('composer');
    const entry = document.createElement('div');
    entry.dataset.viewName = 'message-list-item';
    entry.dataset.eventUrn = 'client-opaque-id';
    const unit = document.createElement('span');
    unit.textContent = composer.innerText;
    entry.appendChild(unit);
    document.getElementById('thread').appendChild(entry);
    setTimeout(() => entry.setAttribute('data-event-urn', 'server-opaque-id'), 0);
    composer.textContent = '';
  });
  for (const chip of document.querySelectorAll('.chip')) {
    chip.addEventListener('click', event => {
      event.preventDefault();
      document.body.dataset.chipClicked = chip.textContent;
    });
  }
"""


# A send button SEND_JS leaves alone, for scripts that stand in for LinkedIn's
# answer to the submit.
SILENT_COMPOSER = COMPOSER.replace('id="send"', 'id="silent-send"')

# The submit renders a client placeholder, then LinkedIn inserts a separate
# node under a server message URN, headed by a link to its sender. With
# ``echo`` false the submit is ignored and only the sender's message arrives,
# carrying the same text.
SERVER_ARM_JS = """
  function serverMessage({sender, echo}) {
    document.getElementById('silent-send').addEventListener('click', event => {
      event.preventDefault();
      const composer = document.getElementById('composer');
      const text = composer.innerText;
      const thread = document.getElementById('thread');
      const placeholder = document.createElement('div');
      placeholder.dataset.viewName = 'message-list-item';
      placeholder.dataset.eventUrn = 'client-uuid';
      placeholder.textContent = text;
      if (echo) {
        thread.appendChild(placeholder);
        composer.textContent = '';
      }
      setTimeout(() => {
        const entry = document.createElement('div');
        entry.dataset.viewName = 'message-list-item';
        entry.dataset.eventUrn = 'urn:li:msg_message:(thread,server-new)';
        const link = document.createElement('a');
        link.href = `https://www.linkedin.com/in/${sender}/`;
        entry.appendChild(link);
        const unit = document.createElement('span');
        unit.textContent = text;
        entry.appendChild(unit);
        thread.appendChild(entry);
        placeholder.remove();
      }, 50);
    });
  }
"""


def server_arm_page(*, sender: str, echo: bool) -> str:
    script = (
        f"<script>{SERVER_ARM_JS}"
        f"serverMessage({{sender: '{sender}', echo: {str(echo).lower()}}});</script>"
    )
    return thread_page(composer=SILENT_COMPOSER, outside_main=script)


def thread_page(
    *, composer: str = COMPOSER, extras: str = "", outside_main: str = ""
) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8"><title>Messaging</title></head>
  <body>
    <main>
      <section id="sidebar">
        <ul><li><label aria-label="Select conversation with Bob">
          <h3>Bob</h3><h2>Bob</h2>
        </label></li></ul>
      </section>
      <section id="pane">
        <header>
          <h2>Ada Lovelace</h2>
          <a href="https://www.linkedin.com/in/ada-lovelace/">View profile</a>
        </header>
        <div id="thread">
          <div data-view-name="message-list-item" data-event-urn="inmail-id">
            <span>Would you be open to a chat about a role?</span>
          </div>
        </div>
        {extras}
        {composer}
      </section>
    </main>
    {outside_main}
    <script>{SEND_JS}</script>
  </body>
</html>
"""


@pytest.fixture
async def dom_page():
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            page = await browser.new_page()
        except Exception as exc:
            if os.environ.get("CI"):
                raise
            pytest.skip(f"chromium unavailable: {exc}")
        page.set_default_timeout(600)
        try:
            yield page
        finally:
            await browser.close()


async def reply(page, html: str, *, confirm_send: bool = True) -> dict:
    async def fulfill(route):
        await route.fulfill(status=200, content_type="text/html", body=html)

    await page.route("https://www.linkedin.com/**", fulfill)
    await page.goto(THREAD_ROUTE)
    session = PageSession(page)
    sender = MessageSender(session, PageNavigator(session))
    with patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock):
        return await sender.reply_to_conversation(
            THREAD_ID, MESSAGE, confirm_send=confirm_send
        )


async def chip_clicked(page) -> str | None:
    return await page.evaluate("() => document.body.dataset.chipClicked || null")


async def composer_text(page) -> str | None:
    return await page.evaluate(
        "() => document.getElementById('composer')?.innerText ?? null"
    )


class TestReplyToConversationDom:
    async def test_inmail_reply_sends_free_text_past_the_quick_replies(self, dom_page):
        result = await reply(dom_page, thread_page(extras=QUICK_REPLIES))

        assert result["status"] == "sent", result
        assert result["sent"] is True
        assert result["retry_safe"] is False
        assert result["participant"] == "Ada Lovelace"
        assert result["thread_id"] == THREAD_ID
        assert await chip_clicked(dom_page) is None
        # The message list sits outside the composer's form; the reply is
        # confirmed there, in the thread pane.
        assert await dom_page.evaluate(
            "() => document.querySelector("
            "'#thread [data-event-urn=\"server-opaque-id\"]').innerText"
        ) == (MESSAGE)

    async def test_dry_run_types_nothing_and_names_the_participant(self, dom_page):
        result = await reply(
            dom_page, thread_page(extras=QUICK_REPLIES), confirm_send=False
        )

        assert result["status"] == "confirmation_required"
        assert result["participant"] == "Ada Lovelace"
        assert "Ada Lovelace" in result["message"]
        assert await composer_text(dom_page) == ""
        assert await chip_clicked(dom_page) is None

    async def test_quick_replies_alone_are_not_a_composer(self, dom_page):
        result = await reply(dom_page, thread_page(composer="", extras=QUICK_REPLIES))

        assert result["status"] == "composer_unavailable"
        assert result["retry_safe"] is True
        assert await chip_clicked(dom_page) is None

    async def test_a_quick_reply_inside_the_composer_form_is_ambiguous(self, dom_page):
        composer = COMPOSER.replace(
            "</form>",
            '<button class="chip" type="submit">Yes, interested...</button></form>',
        )

        result = await reply(dom_page, thread_page(composer=composer))

        assert result["status"] == "send_unavailable"
        assert result["retry_safe"] is True
        assert await chip_clicked(dom_page) is None
        assert await composer_text(dom_page) == ""

    async def test_a_composer_outside_main_is_not_the_threads(self, dom_page):
        overlay = f"<aside id='overlay'>{COMPOSER}</aside>"

        result = await reply(dom_page, thread_page(composer="", outside_main=overlay))

        assert result["status"] == "composer_unavailable"
        assert await composer_text(dom_page) == ""

    async def test_a_second_open_composer_makes_the_page_ambiguous(self, dom_page):
        overlay = (
            "<aside><form><div role='textbox' contenteditable='true' "
            "style='display:block;width:200px;height:30px'></div>"
            "<button type='submit'>Send</button></form></aside>"
        )

        result = await reply(dom_page, thread_page(outside_main=overlay))

        assert result["status"] == "composer_unavailable"
        assert await composer_text(dom_page) == ""

    async def test_a_draft_in_the_thread_composer_is_left_alone(self, dom_page):
        composer = COMPOSER.replace(
            'height:30px"></div>', 'height:30px">half-written draft</div>'
        )

        result = await reply(dom_page, thread_page(composer=composer))

        assert result["status"] == "composer_occupied"
        assert await composer_text(dom_page) == "half-written draft"

    async def test_a_sidebar_name_is_never_reported_as_the_participant(self, dom_page):
        html = thread_page().replace("<h2>Ada Lovelace</h2>", "")

        result = await reply(dom_page, html, confirm_send=False)

        assert result["status"] == "confirmation_required"
        assert result["participant"] is None

    async def test_own_reply_under_a_server_urn_is_confirmed(self, dom_page):
        result = await reply(
            dom_page, server_arm_page(sender="grace-hopper-example", echo=True)
        )

        assert result["status"] == "sent", result
        assert result["sent"] is True

    async def test_participant_message_with_the_same_text_is_not_confirmed(
        self, dom_page
    ):
        """A reply names no recipient, so the header's participant stands in."""
        result = await reply(
            dom_page, server_arm_page(sender="ada-lovelace", echo=False)
        )

        assert result["status"] == "send_unconfirmed", result
        assert result["sent"] is False
        assert result["retry_safe"] is False

    async def test_a_pane_linking_no_participant_confirms_nothing(self, dom_page):
        html = server_arm_page(sender="grace-hopper-example", echo=True).replace(
            '<a href="https://www.linkedin.com/in/ada-lovelace/">View profile</a>', ""
        )

        result = await reply(dom_page, html)

        assert result["status"] == "send_unconfirmed", result
        assert result["sent"] is False
