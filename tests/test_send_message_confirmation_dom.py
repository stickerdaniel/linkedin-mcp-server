# tests/test_send_message_confirmation_dom.py
"""Browser-DOM tests for the send_message safety contract (issue #866).

The unit suite mocks ``page.evaluate``, so recipient scoping, focus, submission,
and mutation observation need a real DOM. These tests run the production
JavaScript in headless Chromium without making a LinkedIn request or write.
"""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.extractor import (
    LinkedInExtractor,
    _ProfileMessageTarget,
    _ProfileMessageTargetResolution,
)

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

DISPLAY_NAME = "Fadi Al Eliwi"
MESSAGE = "UNDELIVERED SENTINEL"
COMPOSE_URL = "https://www.linkedin.com/messaging/compose/?recipient=ACoAAB"
PROFILE_PATH = "/in/fadi-eliwi/"
TARGET = _ProfileMessageTarget(
    profile_path=PROFILE_PATH,
    profile_urn="ACoAAB",
    compose_url=COMPOSE_URL,
    display_name=DISPLAY_NAME,
)

NOOP_SEND_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = 'true';
  });
"""

CLEARING_NOOP_SEND_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = 'true';
    document.getElementById('composer').textContent = '';
  });
"""

READONLY_NOOP_SEND_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = 'true';
    document.getElementById('composer').setAttribute('contenteditable', 'false');
  });
"""

FIXED_ID_BUBBLE_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = 'true';
    const composer = document.getElementById('composer');
    const entry = messageItem(composer.innerText, 'local-only');
    document.getElementById('thread').appendChild(entry);
    composer.textContent = '';
  });
"""

ID_TRANSITION_SEND_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = String(
      Number(document.body.dataset.clicked || 0) + 1);
    const composer = document.getElementById('composer');
    const entry = messageItem(composer.innerText, 'client-opaque-id');
    document.getElementById('thread').appendChild(entry);
    setTimeout(() => {
      entry.setAttribute('data-event-urn', 'server-opaque-id');
    }, 0);
    composer.textContent = '';
  });
"""

REPARENTED_BASELINE_SEND_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = 'true';
    const composer = document.getElementById('composer');
    const entry = document.querySelector('#thread [data-view-name="message-list-item"]');
    entry.remove();
    entry.querySelector('.message-unit').textContent = composer.innerText;
    document.getElementById('thread').appendChild(entry);
    entry.setAttribute('data-event-urn', 'server-reparented-id');
    composer.textContent = '';
  });
"""

GLOBAL_BASELINE_REPARENT_SEND_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = 'true';
    const composer = document.getElementById('composer');
    const entry = document.querySelector('#outside [data-view-name="message-list-item"]');
    document.getElementById('thread').appendChild(entry);
    entry.setAttribute('data-event-urn', 'server-reparented-id');
    composer.textContent = '';
  });
"""

REPLACED_NODE_SEND_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = 'true';
    const composer = document.getElementById('composer');
    const entry = messageItem(composer.innerText, 'client-opaque-id');
    document.getElementById('thread').appendChild(entry);
    const replacement = entry.cloneNode(true);
    replacement.setAttribute('data-event-urn', 'server-opaque-id');
    entry.replaceWith(replacement);
    composer.textContent = '';
  });
"""

MULTIPLE_CANDIDATES_SEND_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = 'true';
    const composer = document.getElementById('composer');
    for (const suffix of ['one', 'two']) {
      const entry = messageItem(composer.innerText, `client-${suffix}`);
      document.getElementById('thread').appendChild(entry);
      entry.setAttribute('data-event-urn', `server-${suffix}`);
    }
    composer.textContent = '';
  });
"""

DIFFERENT_TEXT_SEND_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = 'true';
    const entry = messageItem('different text', 'client-opaque-id');
    document.getElementById('thread').appendChild(entry);
    entry.setAttribute('data-event-urn', 'server-opaque-id');
    document.getElementById('composer').textContent = '';
  });
"""

OUTSIDE_OWNER_SEND_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = 'true';
    const composer = document.getElementById('composer');
    const entry = messageItem(composer.innerText, 'client-opaque-id');
    document.getElementById('outside').appendChild(entry);
    setTimeout(() => {
      entry.setAttribute('data-event-urn', 'server-opaque-id');
    }, 0);
    composer.textContent = '';
  });
"""

REPLACED_EDITOR_SEND_JS = (
    ID_TRANSITION_SEND_JS
    + """
  document.getElementById('send').addEventListener('click', () => {
    const editor = document.getElementById('composer');
    editor.replaceWith(editor.cloneNode(true));
  });
"""
)

REPLACED_OWNER_SEND_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = 'true';
    const owner = document.getElementById('conversation');
    const replacement = owner.cloneNode(true);
    const composer = replacement.querySelector('#composer');
    const entry = messageItem(composer.innerText, 'client-opaque-id');
    replacement.querySelector('#thread').appendChild(entry);
    entry.setAttribute('data-event-urn', 'server-opaque-id');
    composer.textContent = '';
    owner.replaceWith(replacement);
  });
"""


def history_item(path: str, *, hidden: bool = False) -> str:
    style = ' style="display:none"' if hidden else ""
    return f"""
      <div data-view-name="message-list-item" data-event-urn="history-id"{style}>
        <a href="https://www.linkedin.com{path}">Mentioned profile</a>
      </div>
    """


def compose_page(
    send_js: str,
    *,
    recipient_path: str | None = None,
    recipient_urn: str = "ACoAAB",
    recipient_hidden: bool = False,
    history_html: str = "",
    draft: str = "",
) -> str:
    hidden = ' style="display:none"' if recipient_hidden else ""
    recipient = ""
    if recipient_path is not None:
        recipient = f"""
          <div id="recipient" data-profile-urn="{recipient_urn}"{hidden}>
            <a href="https://www.linkedin.com{recipient_path}">{DISPLAY_NAME}</a>
          </div>
        """
    return f"""<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8"><title>Messaging</title></head>
  <body>
    <main>
      <section id="conversation" role="dialog">
        <div id="thread">
          <div class="msg" data-view-name="message-list-item"
               data-event-urn="existing-message-id">
            <span class="message-unit">{MESSAGE}</span>
          </div>
          {history_html}
        </div>
        <form id="composer-scope" onsubmit="return false">
          {recipient}
          <div id="composer" role="textbox" contenteditable="true"
               style="display:block;width:200px;height:30px">{draft}</div>
          <button id="send" type="submit">Send</button>
        </form>
      </section>
    </main>
    <script>
      function messageItem(text, eventUrn) {{
        const entry = document.createElement('div');
        entry.className = 'msg';
        entry.dataset.viewName = 'message-list-item';
        entry.dataset.eventUrn = eventUrn;
        const unit = document.createElement('span');
        unit.className = 'message-unit';
        unit.textContent = text;
        entry.appendChild(unit);
        return entry;
      }}
      {send_js}
    </script>
  </body>
</html>
"""


def profile_page(top_card: str, *, other: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8"><title>Profile</title></head>
  <body><main>{top_card}{other}</main></body>
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
        await page.route(
            "https://www.linkedin.com/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body='<!DOCTYPE html><html><head><meta charset="utf-8"></head></html>',
            ),
        )
        try:
            yield page
        finally:
            await browser.close()


async def send(
    page, html: str, *, message: str = MESSAGE, confirm_send: bool = True
) -> dict:
    await page.goto(COMPOSE_URL)
    await page.set_content(html)
    extractor = LinkedInExtractor(page)
    with (
        patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(
            extractor,
            "_read_profile_message_target",
            new_callable=AsyncMock,
            return_value=_ProfileMessageTargetResolution("resolved", TARGET),
        ),
        patch(
            "linkedin_mcp_server.scraping.extractor._message_page_url_is_safe",
            return_value=True,
        ),
    ):
        return await extractor.send_message(
            "fadi-eliwi", message, confirm_send=confirm_send
        )


async def read_profile_target(page, html: str) -> _ProfileMessageTargetResolution:
    async def fulfill(route):
        await route.fulfill(status=200, content_type="text/html", body=html)

    await page.route("https://www.linkedin.com/**", fulfill)
    await page.goto(f"https://www.linkedin.com{PROFILE_PATH}")
    return await LinkedInExtractor(page)._read_profile_message_target()


class TestProfileMessageTargetDom:
    async def test_history_only_compose_link_proves_action_absence(self, dom_page):
        html = profile_page(
            f"<section><h1>{DISPLAY_NAME}</h1></section>",
            other=(
                '<section data-view-name="message-list-item">'
                f'<a href="{COMPOSE_URL}">history</a></section>'
            ),
        )

        resolution = await read_profile_target(dom_page, html)

        assert resolution.status == "unavailable"
        assert resolution.target is None

    async def test_foreign_history_link_does_not_reject_top_card_target(self, dom_page):
        html = profile_page(
            '<section id="top-card">'
            f'<h1>{DISPLAY_NAME}</h1><a href="{COMPOSE_URL}">message</a>'
            "</section>",
            other=(
                '<section><a href="/messaging/compose/?recipient=OTHER">'
                "history</a></section>"
            ),
        )

        resolution = await read_profile_target(dom_page, html)
        target = resolution.target

        assert resolution.status == "resolved"
        assert target is not None
        assert target.profile_path == PROFILE_PATH
        assert target.profile_urn == "ACoAAB"
        assert target.compose_url == COMPOSE_URL

    async def test_later_bob_card_cannot_supply_alices_missing_action(self, dom_page):
        html = profile_page(
            '<section id="alice"><h1>Alice</h1></section>',
            other=(
                '<section id="bob"><h1>Bob</h1>'
                '<a href="/messaging/compose/?recipient=BOB">message</a></section>'
            ),
        )
        extractor = LinkedInExtractor(dom_page)

        resolution = await read_profile_target(dom_page, html)

        assert resolution.status == "unavailable"
        assert await extractor._extract_profile_urn() is None

    async def test_hidden_top_card_action_proves_action_absence(self, dom_page):
        html = profile_page(
            "<section>"
            f"<h1>{DISPLAY_NAME}</h1>"
            f'<a style="display:none" href="{COMPOSE_URL}">message</a>'
            "</section>"
        )

        resolution = await read_profile_target(dom_page, html)

        assert resolution.status == "unavailable"

    async def test_visibility_hidden_card_does_not_precede_visible_card(self, dom_page):
        html = profile_page(
            '<section style="visibility:hidden"><h1>Bob</h1>'
            '<a href="/messaging/compose/?recipient=BOB">message</a></section>',
            other=(
                f'<section><h1>Alice</h1><a href="{COMPOSE_URL}">message</a></section>'
            ),
        )

        resolution = await read_profile_target(dom_page, html)
        target = resolution.target

        assert resolution.status == "resolved"
        assert target is not None
        assert target.display_name == "Alice"
        assert target.profile_urn == "ACoAAB"
        assert target.compose_url == COMPOSE_URL

    async def test_later_sections_never_compete_with_first_top_card(self, dom_page):
        card = (
            "<section>"
            f'<h1>{DISPLAY_NAME}</h1><a href="{COMPOSE_URL}">message</a>'
            "</section>"
        )

        resolution = await read_profile_target(dom_page, profile_page(card, other=card))

        assert resolution.status == "resolved"
        assert resolution.target is not None
        assert resolution.target.profile_urn == "ACoAAB"

    async def test_waits_for_delayed_profile_message_action(self, dom_page):
        html = profile_page(f'<section id="top"><h1>{DISPLAY_NAME}</h1></section>')

        async def fulfill(route):
            await route.fulfill(status=200, content_type="text/html", body=html)

        await dom_page.route("https://www.linkedin.com/**", fulfill)
        await dom_page.goto(f"https://www.linkedin.com{PROFILE_PATH}")
        await dom_page.evaluate(
            f"""() => setTimeout(() => {{
                document.getElementById('top').insertAdjacentHTML(
                    'beforeend', '<a href="{COMPOSE_URL}">message</a>'
                );
            }}, 200)"""
        )
        started = time.monotonic()

        resolution = await LinkedInExtractor(dom_page)._read_profile_message_target()

        assert resolution.status == "resolved"
        assert resolution.target is not None
        assert resolution.target.profile_urn == "ACoAAB"
        assert time.monotonic() - started >= 0.15

    @pytest.mark.parametrize(
        "top_card",
        [
            "<div>still loading</div>",
            f"<section><h1>{DISPLAY_NAME}</h1><h1>Other</h1></section>",
            (
                f"<section><h1>{DISPLAY_NAME}</h1>"
                f'<a href="{COMPOSE_URL}">one</a>'
                '<a href="/messaging/compose/?recipient=OTHER">two</a></section>'
            ),
            (
                f"<section><h1>{DISPLAY_NAME}</h1>"
                f'<a aria-disabled="true" href="{COMPOSE_URL}">message</a></section>'
            ),
            (
                f"<section><h1>{DISPLAY_NAME}</h1>"
                '<a href="/messaging/compose/?recipient=">message</a></section>'
            ),
            (
                f"<section><h1>{DISPLAY_NAME}</h1>"
                '<a href="https://evil.example/messaging/compose/?recipient=ACoAAB">'
                "message</a></section>"
            ),
        ],
        ids=[
            "missing-section",
            "ambiguous-headings",
            "ambiguous-actions",
            "disabled-action",
            "malformed-recipient",
            "unsafe-url",
        ],
    )
    async def test_ambiguous_or_invalid_target_is_not_action_absence(
        self, dom_page, top_card
    ):
        resolution = await read_profile_target(dom_page, profile_page(top_card))

        assert resolution.status == "failed"
        assert resolution.target is None


class TestComposerRecipientDom:
    async def test_missing_local_identity_uses_profile_and_url_authority(
        self, dom_page
    ):
        result = await send(
            dom_page,
            compose_page(
                NOOP_SEND_JS,
                history_html=history_item(PROFILE_PATH),
            ),
        )

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"
        assert (await dom_page.locator("#composer").inner_text()).strip() == MESSAGE

    async def test_foreign_history_identity_does_not_reject_local_target(
        self, dom_page
    ):
        result = await send(
            dom_page,
            compose_page(
                NOOP_SEND_JS,
                history_html=history_item("/in/someone-else/"),
            ),
        )

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"

    async def test_foreign_local_identity_cannot_use_matching_history(self, dom_page):
        result = await send(
            dom_page,
            compose_page(
                NOOP_SEND_JS,
                recipient_path="/in/someone-else/",
                recipient_urn="OTHER",
                history_html=history_item(PROFILE_PATH),
            ),
        )

        assert result["status"] == "composer_unavailable"
        assert result["sent"] is False
        assert await dom_page.evaluate("document.body.dataset.clicked") is None

    async def test_hidden_local_identity_fails_closed(self, dom_page):
        result = await send(
            dom_page,
            compose_page(
                NOOP_SEND_JS,
                recipient_path=PROFILE_PATH,
                recipient_hidden=True,
            ),
        )

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"

    async def test_dry_run_ends_before_focus_or_entry(self, dom_page):
        html = compose_page(
            """
              document.getElementById('composer').addEventListener('focus', () => {
                document.body.dataset.focused = 'true';
              });
            """
        )

        result = await send(dom_page, html, confirm_send=False)

        assert result["status"] == "confirmation_required"
        assert result["recipient_selected"] is True
        assert await dom_page.evaluate("document.body.dataset.focused") is None
        assert (await dom_page.locator("#composer").inner_text()).strip() == ""

    async def test_existing_draft_is_left_untouched(self, dom_page):
        result = await send(
            dom_page,
            compose_page(ID_TRANSITION_SEND_JS, draft="Private draft"),
        )

        assert result["status"] == "composer_occupied"
        assert result["retry_safe"] is True
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert (await dom_page.locator("#composer").inner_text()).strip() == (
            "Private draft"
        )

    async def test_draft_restored_on_focus_is_left_untouched(self, dom_page):
        restored = "Confidential restored draft"
        html = compose_page(
            f"""
              document.getElementById('composer').addEventListener('focus', () => {{
                document.getElementById('composer').textContent = '{restored}';
              }});
            """
        )

        result = await send(dom_page, html)

        assert result["status"] == "composer_occupied"
        assert result["sent"] is False
        assert result["retry_safe"] is True
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert (await dom_page.locator("#composer").inner_text()).strip() == restored

    async def test_focus_switch_cannot_redirect_text_to_foreign_editor(self, dom_page):
        html = compose_page(
            """
              document.body.insertAdjacentHTML('beforeend', '<input id="foreign">');
              document.getElementById('composer').addEventListener('focus', () => {
                document.getElementById('foreign').focus();
              });
            """
        )

        result = await send(dom_page, html)

        assert result["status"] == "compose_interact_failed"
        assert await dom_page.locator("#composer").inner_text() == ""
        assert await dom_page.locator("#foreign").input_value() == ""
        assert await dom_page.evaluate("document.body.dataset.clicked") is None

    async def test_input_focus_switch_cleans_exact_owned_text(self, dom_page):
        html = compose_page(
            """
              document.body.insertAdjacentHTML('beforeend', '<input id="foreign">');
              document.getElementById('composer').addEventListener('input', () => {
                document.getElementById('foreign').focus();
              });
            """
        )

        result = await send(dom_page, html)

        assert result["status"] == "compose_interact_failed"
        assert await dom_page.locator("#composer").inner_text() == ""
        assert await dom_page.locator("#foreign").input_value() == ""
        assert await dom_page.evaluate("document.body.dataset.clicked") is None

    async def test_disabled_submit_enables_after_local_input_and_sends(self, dom_page):
        html = compose_page(
            ID_TRANSITION_SEND_JS
            + """
              document.getElementById('composer').addEventListener('input', () => {
                document.getElementById('send').disabled = false;
              });
            """
        ).replace(
            '<button id="send" type="submit">Send</button>',
            '<button id="send" type="submit" disabled>Send</button>',
        )

        result = await send(dom_page, html)

        assert result["status"] == "sent"
        assert result["sent"] is True
        assert await dom_page.evaluate("document.body.dataset.clicked") == "1"

    async def test_permanently_disabled_submit_cleans_exact_owned_text(self, dom_page):
        html = compose_page(
            NOOP_SEND_JS
            + """
              document.getElementById('composer').addEventListener('input', () => {
                document.body.dataset.inputCount = String(
                  Number(document.body.dataset.inputCount || 0) + 1);
              });
            """
        ).replace(
            '<button id="send" type="submit">Send</button>',
            '<button id="send" type="submit" disabled>Send</button>',
        )

        result = await send(dom_page, html)

        assert result["status"] == "send_unavailable"
        assert result["sent"] is False
        assert result["retry_safe"] is True
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert await dom_page.evaluate("document.body.dataset.inputCount") == "2"
        assert (await dom_page.locator("#composer").inner_text()).strip() == ""

    @pytest.mark.parametrize(
        "button_change",
        [
            "send.replaceWith(send.cloneNode(true));",
            "send.insertAdjacentHTML('afterend', '<button type=submit>Other</button>');",
        ],
        ids=["replaced", "ambiguous"],
    )
    async def test_submit_change_after_insertion_cleans_without_click(
        self, dom_page, button_change
    ):
        html = compose_page(
            NOOP_SEND_JS
            + f"""
              document.getElementById('composer').addEventListener('input', () => {{
                const send = document.getElementById('send');
                if (document.getElementById('composer').innerText) {{
                  {button_change}
                }}
              }});
            """
        )

        result = await send(dom_page, html)

        assert result["status"] == "compose_interact_failed"
        assert result["retry_safe"] is True
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert (await dom_page.locator("#composer").inner_text()).strip() == ""

    async def test_ambiguous_submit_never_dispatches(self, dom_page):
        html = compose_page(NOOP_SEND_JS).replace(
            '<button id="send" type="submit">Send</button>',
            (
                '<button id="send" type="submit">Send</button>'
                '<button type="submit">Other</button>'
            ),
        )

        result = await send(dom_page, html)

        assert result["status"] == "send_unavailable"
        assert result["sent"] is False
        assert result["retry_safe"] is True
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert (await dom_page.locator("#composer").inner_text()).strip() == ""

    async def test_route_change_on_focus_invalidates_pinned_owner(self, dom_page):
        html = compose_page(
            """
              document.getElementById('composer').addEventListener('focus', () => {
                history.replaceState({}, '', '/messaging/thread/OTHER/');
              });
            """
        )

        result = await send(dom_page, html)

        assert result["status"] == "compose_interact_failed"
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert (await dom_page.locator("#composer").inner_text()).strip() == ""

    async def test_route_change_after_insertion_cleans_owned_text(self, dom_page):
        html = compose_page(
            """
              document.getElementById('composer').addEventListener('input', () => {
                if (document.getElementById('composer').innerText) {
                  history.replaceState({}, '', '/messaging/thread/OTHER/');
                }
              });
            """
        )

        result = await send(dom_page, html)

        assert result["status"] == "compose_interact_failed"
        assert result["retry_safe"] is True
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert (await dom_page.locator("#composer").inner_text()).strip() == ""

    async def test_modified_inserted_text_is_preserved_on_route_failure(self, dom_page):
        html = compose_page(
            """
              document.getElementById('composer').addEventListener('input', () => {
                const editor = document.getElementById('composer');
                if (editor.innerText === 'UNDELIVERED SENTINEL') {
                  editor.textContent += ' user edit';
                  history.replaceState({}, '', '/messaging/thread/OTHER/');
                }
              });
            """
        )

        result = await send(dom_page, html)

        assert result["status"] == "compose_interact_failed"
        assert result["retry_safe"] is True
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert await dom_page.locator("#composer").inner_text() == (
            MESSAGE + " user edit"
        )


class TestSendConfirmationDom:
    @pytest.mark.parametrize(
        "message",
        ["First\nSecond", "First\rSecond", "First\tSecond", "First\x7fSecond"],
        ids=["newline", "carriage-return", "tab", "del"],
    )
    async def test_control_characters_are_rejected_before_dom_interaction(
        self, dom_page, message
    ):
        result = await send(
            dom_page, compose_page(ID_TRANSITION_SEND_JS), message=message
        )

        assert result["status"] == "invalid_message"
        assert result["message"] == (
            "Message must not contain control characters or line breaks."
        )
        assert result["retry_safe"] is True
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert (await dom_page.locator("#composer").inner_text()).strip() == ""

    async def test_local_bubble_without_id_transition_is_not_confirmed(self, dom_page):
        result = await send(dom_page, compose_page(FIXED_ID_BUBBLE_JS))

        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"
        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        assert await dom_page.locator("#thread .msg").count() == 2

    async def test_same_node_opaque_id_transition_confirms_once(self, dom_page):
        result = await send(dom_page, compose_page(ID_TRANSITION_SEND_JS))

        assert result["status"] == "sent"
        assert result["sent"] is True
        assert result["retry_safe"] is False
        assert await dom_page.evaluate("document.body.dataset.clicked") == "1"
        entries = dom_page.locator("#thread .msg")
        assert await entries.count() == 2
        assert await entries.last.get_attribute("data-event-urn") == "server-opaque-id"
        assert (await entries.last.locator(".message-unit").inner_text()) == MESSAGE

    async def test_reparented_baseline_node_is_never_confirmed(self, dom_page):
        result = await send(dom_page, compose_page(REPARENTED_BASELINE_SEND_JS))

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"
        assert await dom_page.locator("#thread .msg").count() == 1
        assert (
            await dom_page.locator("#thread .msg").get_attribute("data-event-urn")
            == "server-reparented-id"
        )

    async def test_global_baseline_node_reparented_into_owner_is_never_confirmed(
        self, dom_page
    ):
        outside = f"""
          <aside id="outside">
            <div class="msg" data-view-name="message-list-item"
                 data-event-urn="outside-history-id">
              <span class="message-unit">{MESSAGE}</span>
            </div>
          </aside>
        """
        html = compose_page(GLOBAL_BASELINE_REPARENT_SEND_JS).replace(
            "</section>", f"</section>{outside}"
        )

        result = await send(dom_page, html)

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"
        assert await dom_page.locator("#outside .msg").count() == 0
        assert await dom_page.locator("#thread .msg").count() == 2
        assert (
            await dom_page.locator("#thread .msg").last.get_attribute("data-event-urn")
            == "server-reparented-id"
        )

    async def test_replaced_candidate_node_is_not_confirmed(self, dom_page):
        result = await send(dom_page, compose_page(REPLACED_NODE_SEND_JS))

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False

    async def test_multiple_matching_candidates_are_not_confirmed(self, dom_page):
        result = await send(dom_page, compose_page(MULTIPLE_CANDIDATES_SEND_JS))

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        assert await dom_page.locator("#thread .msg").count() == 3

    async def test_different_visible_message_unit_is_not_confirmed(self, dom_page):
        result = await send(dom_page, compose_page(DIFFERENT_TEXT_SEND_JS))

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False

    async def test_no_dom_mutation_is_not_confirmed(self, dom_page):
        result = await send(dom_page, compose_page(NOOP_SEND_JS))

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        assert (await dom_page.locator("#composer").inner_text()).strip() == MESSAGE

    @pytest.mark.parametrize(
        "send_js",
        [CLEARING_NOOP_SEND_JS, READONLY_NOOP_SEND_JS],
        ids=["cleared", "read-only"],
    )
    async def test_local_composer_state_change_alone_is_not_confirmed(
        self, dom_page, send_js
    ):
        result = await send(dom_page, compose_page(send_js))

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        assert await dom_page.locator("#thread .msg").count() == 1

    async def test_matching_bubble_outside_owner_is_not_confirmed(self, dom_page):
        html = compose_page(OUTSIDE_OWNER_SEND_JS).replace(
            "</section>", '</section><aside id="outside"></aside>'
        )

        result = await send(dom_page, html)

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        assert await dom_page.locator("#outside .msg").count() == 1
        assert await dom_page.locator("#thread .msg").count() == 1

    async def test_editor_replacement_after_submit_is_not_confirmed(self, dom_page):
        result = await send(dom_page, compose_page(REPLACED_EDITOR_SEND_JS))

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False

    async def test_owner_replacement_after_submit_is_not_confirmed(self, dom_page):
        result = await send(dom_page, compose_page(REPLACED_OWNER_SEND_JS))

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False

    async def test_cancelled_confirmation_cleans_observer_pins_and_handle(
        self, dom_page
    ):
        await dom_page.goto(COMPOSE_URL)
        await dom_page.set_content(compose_page(NOOP_SEND_JS))
        extractor = LinkedInExtractor(dom_page)
        resolve_owner = extractor._resolve_message_owner
        captured = {}

        async def capture_owner(target):
            owner = await resolve_owner(target)
            captured["owner"] = owner
            return owner

        async def wait_for_confirmation(*_args, **_kwargs):
            await anyio.sleep_forever()

        with (
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                extractor,
                "_read_profile_message_target",
                new_callable=AsyncMock,
                return_value=_ProfileMessageTargetResolution("resolved", TARGET),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor._message_page_url_is_safe",
                return_value=True,
            ),
            patch.object(
                extractor, "_resolve_message_owner", side_effect=capture_owner
            ),
            patch.object(
                extractor,
                "_message_send_confirmed",
                side_effect=wait_for_confirmation,
            ),
            pytest.raises(TimeoutError),
        ):
            with anyio.fail_after(0.5):
                await extractor.send_message("fadi-eliwi", MESSAGE, confirm_send=True)

        cleanup_state = await dom_page.evaluate(
            """() => {
                const owner = document.getElementById('conversation');
                return {
                    hasComposer: Object.hasOwn(owner, '__linkedinMcpComposer'),
                    hasConfirmations: Object.hasOwn(
                        owner, '__linkedinMcpConfirmations'
                    ),
                    markers: owner.querySelectorAll(
                        '[data-linkedin-mcp-candidate], '
                        + '[data-linkedin-mcp-editor], '
                        + '[data-linkedin-mcp-confirmation]'
                    ).length,
                };
            }"""
        )
        assert cleanup_state == {
            "hasComposer": False,
            "hasConfirmations": False,
            "markers": 0,
        }
        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"
        assert (await dom_page.locator("#composer").inner_text()).strip() == MESSAGE
        with pytest.raises(Exception, match="closed"):
            await captured["owner"].evaluate("owner => owner.isConnected")
