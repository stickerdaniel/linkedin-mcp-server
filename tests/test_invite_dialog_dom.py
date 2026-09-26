"""Invite-dialog submission against a real DOM with a chat overlay open.

Measured on LinkedIn in September 2026: after a message send, LinkedIn keeps
the conversation open as an overlay dialog on later pages, including the
custom-invite deeplink, so two dialogs are open when the invite renders.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from patchright.async_api import Page, async_playwright

from linkedin_mcp_server.scraping.connection_actions import ConnectionActions
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

INVITE_DIALOG = """
  <div role="dialog" id="invite">
    <h2>Add a note to your invitation?</h2>
    <button onclick="document.body.dataset.invite = 'note';
      const note = document.createElement('textarea');
      note.style.display = 'block';
      document.getElementById('invite').insertBefore(note, this);
      this.nextElementSibling.textContent = 'Send'">Add a note</button>
    <button onclick="document.body.dataset.invite = 'sent';
      const note = document.querySelector('#invite textarea');
      document.body.dataset.note = note ? note.value : '';
      document.getElementById('invite').remove()">Send without a note</button>
  </div>
"""

CHAT_OVERLAY = """
  <div role="dialog" id="chat">
    <form class="msg-form">
      <div role="textbox" contenteditable="true"
           style="display:block;width:200px;height:30px"></div>
      <button type="submit" disabled>Send</button>
      <button type="button" class="msg-form__send-toggle"
        onclick="document.body.dataset.chat = 'clicked'">Open send options</button>
    </form>
  </div>
"""


@pytest.fixture
async def dom_page():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield page
        finally:
            await browser.close()


def _actions(page) -> ConnectionActions:
    async def unreachable(_username: str) -> dict[str, Any]:
        raise AssertionError("the dialog cases never read a profile")

    session = ScrapingSession(cast(Page, page))
    return ConnectionActions(session, PageNavigator(session), unreachable)


@pytest.mark.parametrize(
    "body", [INVITE_DIALOG + CHAT_OVERLAY, CHAT_OVERLAY + INVITE_DIALOG]
)
async def test_invite_is_sent_past_an_open_chat_overlay(dom_page, body):
    await dom_page.set_content(f"<!DOCTYPE html><html><body>{body}</body></html>")

    submitted, note_sent, note_limit = await _actions(dom_page)._submit_invite_dialog(
        None
    )

    assert (submitted, note_sent, note_limit) == (True, False, None)
    assert await dom_page.evaluate("document.body.dataset.invite") == "sent"
    assert await dom_page.evaluate("document.body.dataset.chat") is None


async def test_chat_overlay_alone_is_not_an_invite_dialog(dom_page):
    await dom_page.set_content(
        f"<!DOCTYPE html><html><body>{CHAT_OVERLAY}</body></html>"
    )

    submitted, _, _ = await _actions(dom_page)._submit_invite_dialog(None)

    assert submitted is False
    assert await dom_page.evaluate("document.body.dataset.chat") is None


async def test_invite_note_is_sent_past_an_open_chat_overlay(dom_page):
    await dom_page.set_content(
        f"<!DOCTYPE html><html><body>{INVITE_DIALOG}{CHAT_OVERLAY}</body></html>"
    )

    submitted, note_sent, note_limit = await _actions(dom_page)._submit_invite_dialog(
        "Hello"
    )

    assert (submitted, note_sent, note_limit) == (True, True, None)
    assert await dom_page.evaluate("document.body.dataset.invite") == "sent"
    assert await dom_page.evaluate("document.body.dataset.note") == "Hello"
    assert await dom_page.evaluate("document.body.dataset.chat") is None
