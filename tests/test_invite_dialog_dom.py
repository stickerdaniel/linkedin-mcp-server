"""Invite-dialog submission against a real DOM with a chat overlay open.

Measured on LinkedIn in September 2026: after a message send, LinkedIn keeps
the conversation open as an overlay dialog on later pages, including the
custom-invite deeplink, so two dialogs are open when the invite renders.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import pytest
from patchright.async_api import Page, async_playwright

from linkedin_mcp_server.core.exceptions import OffLinkedInLandingError
from linkedin_mcp_server.linkedin.connection_actions import ConnectionActions
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

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
            # On a LinkedIn address, because the reads refuse any other
            # page, and `set_content` keeps the address it replaces.
            await page.route(
                "https://www.linkedin.com/**",
                lambda route: route.fulfill(content_type="text/html", body=""),
            )
            await page.goto(
                "https://www.linkedin.com/preload/custom-invite/?vanityName=testuser"
            )
            yield page
        finally:
            await browser.close()


def _actions(page) -> ConnectionActions:
    async def unreachable(_username: str) -> dict[str, Any]:
        raise AssertionError("the dialog cases never read a profile")

    session = PageSession(cast(Page, page))
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


PORTAL_URL = "https://portal.invalid/interstitial"

UPSELL_DIALOG = """
  <div role="dialog" id="upsell">
    <p>You're out of free custom notes.</p>
    <a href="https://www.linkedin.com/premium/products/">Try Premium</a>
  </div>
"""

#: Closes an open dialog on Escape, the way LinkedIn's dialogs do. A page
#: script, so it runs in the page's world and sees the real key event.
CLOSES_ON_ESCAPE = """
  <script>
    document.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      document.body.dataset.escaped = 'true';
      document.querySelectorAll('[role="dialog"]').forEach(d => d.remove());
    });
  </script>
"""

#: A portal that records any key it is sent.
RECORDS_KEYS = """<!DOCTYPE html><html><body><p>Portal</p>
  <script>
    document.addEventListener('keydown', event => {
      document.body.dataset.foreignKey = event.key;
    });
  </script>
</body></html>"""


async def test_escape_is_not_sent_to_a_portal_that_replaced_the_page(dom_page):
    """The upsell was read on LinkedIn; the page left before the dismissal.

    The review's case: the guarded snapshot answers LinkedIn's own text, a
    navigation to a portal completes before the caller acts on it, and the
    Escape meant to close LinkedIn's dialog would run the portal's handlers.
    """
    await dom_page.set_content(
        f"<!DOCTYPE html><html><body>{UPSELL_DIALOG}{CLOSES_ON_ESCAPE}</body></html>"
    )
    await dom_page.route(
        "https://portal.invalid/**",
        lambda route: route.fulfill(content_type="text/html", body=RECORDS_KEYS),
    )
    actions = _actions(dom_page)
    read_on_linkedin = actions._get_premium_upsell_message
    answers: list[str | None] = []

    async def read_then_leave(*args: Any, **kwargs: Any) -> str | None:
        answer = await read_on_linkedin(*args, **kwargs)
        answers.append(answer)
        await dom_page.goto(PORTAL_URL)
        return answer

    with (
        patch.object(actions, "_get_premium_upsell_message", read_then_leave),
        pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"),
    ):
        await actions._probe_invite_note_limit()

    assert answers and answers[0] is not None and "custom notes" in answers[0]
    assert dom_page.url == PORTAL_URL
    assert await dom_page.evaluate("document.body.dataset.foreignKey") is None


async def test_escape_still_closes_linkedins_dialog(dom_page):
    await dom_page.set_content(
        f"<!DOCTYPE html><html><body>{UPSELL_DIALOG}{CLOSES_ON_ESCAPE}</body></html>"
    )

    message = await _actions(dom_page)._probe_invite_note_limit()

    assert message is not None and "custom notes" in message
    assert await dom_page.evaluate("document.body.dataset.escaped") == "true"
    assert await dom_page.locator('[role="dialog"]').count() == 0


#: A dialog whose own handler closes it, with focus on a field inside it and a
#: body that can take focus, so a press that refocuses the body misses it.
FOCUSED_DIALOG_ON_A_FOCUSABLE_BODY = """<!DOCTYPE html><html>
<body tabindex="-1">
  <div role="dialog" id="upsell">
    <p>You're out of free custom notes.</p>
    <a href="https://www.linkedin.com/premium/products/">Try Premium</a>
    <input id="inside">
  </div>
  <script>
    const dialog = document.getElementById('upsell');
    dialog.addEventListener('keydown', event => {
      if (event.key === 'Escape') dialog.remove();
    });
    document.getElementById('inside').focus();
  </script>
</body></html>"""


async def test_escape_reaches_the_dialog_that_has_focus(dom_page):
    """Pressed where focus is, not on the body, which would take it away."""
    await dom_page.set_content(FOCUSED_DIALOG_ON_A_FOCUSABLE_BODY)

    message = await _actions(dom_page)._probe_invite_note_limit()

    assert message is not None and "custom notes" in message
    assert await dom_page.locator('[role="dialog"]').count() == 0
