# tests/test_send_message_confirmation_dom.py
"""Browser-DOM tests for the send_message safety contract (issue #866).

The unit suite mocks ``page.evaluate``, so recipient scoping, focus, submission,
and mutation observation need a real DOM. These tests run the production
JavaScript in headless Chromium without making a LinkedIn request or write.
"""

from __future__ import annotations

import json
import os
import time
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin.message_sender import (
    MessageSender,
    _ProfileMessageTarget,
    _ProfileMessageTargetResolution,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.profile_page import ProfilePageReader
from linkedin_mcp_server.linkedin.session import PageSession


def _sender(page) -> MessageSender:
    session = PageSession(page)
    return MessageSender(session, PageNavigator(session))


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
# Synthetic profile URN of the signed-in member, who sends the message.
SELF_URN = "ACoAAS"

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


# Measured on LinkedIn in September 2026: an open thread first renders a
# placeholder with a client ID, then inserts a separate node with the server
# message URN and removes the placeholder.
SERVER_REPLACEMENT_SEND_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = 'true';
    const composer = document.getElementById('composer');
    const text = composer.innerText;
    const placeholder = messageItem(text, 'client-uuid');
    document.getElementById('thread').appendChild(placeholder);
    composer.textContent = '';
    setTimeout(() => {
      document.getElementById('thread').appendChild(
        messageItem(text, 'urn:li:msg_message:(self,server-new)', SELF_URN));
      placeholder.remove();
    }, 50);
  });
"""

# Measured on LinkedIn in September 2026: the first message of a new thread
# moves the route to /messaging/thread/<id>/ and remounts the whole
# conversation pane, composer included, with the message under its server URN.
# The pane's header links the other participant's profile outside every
# message item.
PANE_REMOUNT_SEND_JS = """
  function remountTo(path, urns, header = '/in/fadi-eliwi/') {
    document.getElementById('send').addEventListener('click', event => {
      event.preventDefault();
      document.body.dataset.clicked = 'true';
      const conversation = document.getElementById('conversation');
      const text = document.getElementById('composer').innerText;
      setTimeout(() => {
        history.pushState({}, '', path);
        const fresh = document.createElement('section');
        fresh.id = 'conversation-remounted';
        if (conversation.hasAttribute('role')) {
          fresh.setAttribute('role', conversation.getAttribute('role'));
        }
        fresh.innerHTML = '<a id="header">Participant</a>'
          + '<div id="thread-remounted"></div>'
          + '<form onsubmit="return false"><div role="textbox" '
          + 'contenteditable="true" style="display:block;width:200px;'
          + 'height:30px"></div><button type="submit">Send</button></form>';
        fresh.querySelector('#header').href = `https://www.linkedin.com${header}`;
        for (const entry of urns) {
          const [urn, otherText] = entry.split('|');
          fresh.querySelector('#thread-remounted').appendChild(
            messageItem(otherText || text, urn, SELF_URN));
        }
        conversation.replaceWith(fresh);
      }, 30);
    });
  }
"""

OLDER_HISTORY_SEND_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = 'true';
    const composer = document.getElementById('composer');
    const thread = document.getElementById('thread');
    thread.insertBefore(
      messageItem(composer.innerText, 'urn:li:msg_message:(self,older)'),
      thread.firstChild);
    composer.textContent = '';
  });
"""

STALE_SERVER_NODE_SEND_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = 'true';
    const entry = document.querySelector('#thread [data-view-name="message-list-item"]');
    const copy = entry.cloneNode(true);
    entry.remove();
    document.getElementById('thread').appendChild(copy);
    document.getElementById('composer').textContent = '';
  });
"""

# A submit LinkedIn ignores, followed by a message from the recipient that
# happens to carry the same text: in a headed item of its own, or as a
# follow-up to an earlier headed reply.
INCOMING_AFTER_NOOP_SEND_JS = """
  function incoming({headed}) {
    document.getElementById('send').addEventListener('click', event => {
      event.preventDefault();
      document.body.dataset.clicked = 'true';
      const text = document.getElementById('composer').innerText;
      setTimeout(() => {
        const thread = document.getElementById('thread');
        if (!headed) {
          thread.appendChild(messageItem(
            'earlier reply', 'urn:li:msg_message:(other,earlier)', RECIPIENT_URN));
        }
        thread.appendChild(messageItem(
          text, 'urn:li:msg_message:(other,incoming)',
          headed ? RECIPIENT_URN : undefined));
      }, 30);
    });
  }
"""

OWN_CONTINUATION_SEND_JS = """
  document.getElementById('thread').appendChild(messageItem(
    'earlier message', 'urn:li:msg_message:(self,earlier)', SELF_URN));
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = 'true';
    const composer = document.getElementById('composer');
    const text = composer.innerText;
    composer.textContent = '';
    setTimeout(() => {
      document.getElementById('thread').appendChild(
        messageItem(text, 'urn:li:msg_message:(self,continued)'));
    }, 30);
  });
"""

TWO_SERVER_NODES_SEND_JS = """
  document.getElementById('send').addEventListener('click', event => {
    event.preventDefault();
    document.body.dataset.clicked = 'true';
    const composer = document.getElementById('composer');
    for (const suffix of ['one', 'two']) {
      document.getElementById('thread').appendChild(
        messageItem(composer.innerText, `urn:li:msg_message:(self,${suffix})`));
    }
    composer.textContent = '';
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
      const SELF_URN = '{SELF_URN}';
      const RECIPIENT_URN = '{TARGET.profile_urn}';
      function messageItem(text, eventUrn, sender) {{
        const entry = document.createElement('div');
        entry.className = 'msg';
        entry.dataset.viewName = 'message-list-item';
        entry.dataset.eventUrn = eventUrn;
        // A sender header links the sender's profile URN twice; a follow-up
        // from the same sender has none.
        for (const label of sender ? ['', 'Sender'] : []) {{
          const link = document.createElement('a');
          link.href = `https://www.linkedin.com/in/${{sender}}/`;
          link.textContent = label;
          entry.appendChild(link);
        }}
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


# A claim about LinkedIn, not only about the algorithm. A multi-line bubble is
# built here the way LinkedIn's renderer builds it, as read from its public
# bundles (renderer 21n8dymcy7qe3tbr4kbnklkro.js, stylesheets
# 8ej77maal5yhg38yq0r08m98y.css and 1ntcyzykfazitrrwl0qzxiss5.css, fetched
# October 2026) and matched against pages captured in September 2026: one
# <br> per LF; per text run, the whitespace at either edge moved into a
# `white-space: pre` span holding one space; a URL as its own <a> run; the
# body <p> inside two <div>s that hold nothing else, with the sender link,
# name and time outside that pair. The two CSS rules are the bundles' own.
# Every bubble is built from the body text the test chooses, never from the
# composer, so a test decides what LinkedIn "received".
MULTILINE_BUBBLE_JS = r"""
  document.head.insertAdjacentHTML('beforeend',
    '<style>.msg-s-event-listitem__body { white-space: normal; }'
    + '.white-space-pre { white-space: pre !important; }</style>');
  const P = inner => '<p class="msg-s-event-listitem__body">' + inner + '</p>';
  const SHAPE = __SHAPE__;
  const escaped = text => Object.assign(
    document.createElement('span'), {textContent: text}).innerHTML;
  const PRE_SPACE = '<span class="white-space-pre"> </span>';
  function renderedLines(text) {
    return text.split('\n').map(line => line.split(/(https:\/\/\S+)/)
      .filter(Boolean)
      .map(run => {
        if (run.startsWith('https://')) {
          return `<a href="${run}"><!---->${escaped(run)}<!----></a>`;
        }
        const lead = /^\s+/.exec(run)?.[0] || '';
        const trail = /\S/.test(run) ? /\s+$/.exec(run)?.[0] || '' : '';
        const core = run.slice(lead.length, run.length - trail.length);
        return '<!---->' + (lead ? PRE_SPACE : '') + escaped(core)
          + (trail ? PRE_SPACE : '') + '<!---->';
      }).join('')).join('<br>');
  }
  function bubbleItem(text, eventUrn, sender) {
    const entry = document.createElement('div');
    entry.className = 'msg';
    entry.dataset.viewName = 'message-list-item';
    entry.dataset.eventUrn = eventUrn;
    const header = sender
      ? `<a href="https://www.linkedin.com/in/${sender}/"><img alt=""></a>`
        + '<span>Sender name</span>'
      : '';
    entry.innerHTML = `<div>${header}<time>10:42</time></div>`
      + SHAPE(renderedLines(text));
    return entry;
  }
  function remount(text, path, header, urns) {
    const conversation = document.getElementById('conversation');
    setTimeout(() => {
      history.pushState({}, '', path);
      const fresh = document.createElement('section');
      fresh.id = 'conversation-remounted';
      if (conversation.hasAttribute('role')) {
        fresh.setAttribute('role', conversation.getAttribute('role'));
      }
      fresh.innerHTML = '<a id="header">Participant</a>'
        + '<div id="thread-remounted"></div>'
        + '<form onsubmit="return false"><div role="textbox" '
        + 'contenteditable="true" style="display:block;width:200px;'
        + 'height:30px"><p><br></p></div><button type="submit">Send</button></form>';
      fresh.querySelector('#header').href = `https://www.linkedin.com${header}`;
      for (const urn of urns) {
        fresh.querySelector('#thread-remounted').appendChild(
          bubbleItem(text, urn, SELF_URN));
      }
      conversation.replaceWith(fresh);
    }, 30);
  }
  // `arm` picks how LinkedIn answers the click; `body` is what it renders.
  function sendAs(arm, body) {
    const thread = document.getElementById('thread');
    if (arm === 'other-header') {
      // On the full messaging page the header sits outside the composer's
      // form, so only the pane check can tell the participant apart.
      document.getElementById('conversation').removeAttribute('role');
    }
    if (arm === 'rerender') {
      thread.appendChild(
        bubbleItem(body, 'urn:li:msg_message:(self,old)', SELF_URN));
    }
    document.getElementById('send').addEventListener('click', event => {
      event.preventDefault();
      document.body.dataset.clicked = String(
        Number(document.body.dataset.clicked || 0) + 1);
      const composer = document.getElementById('composer');
      if (arm === 'opaque') {
        const entry = bubbleItem(body, 'client-opaque-id');
        thread.appendChild(entry);
        setTimeout(() => entry.setAttribute('data-event-urn', 'server-opaque-id'), 0);
      } else if (arm === 'removed-twin') {
        const twin = bubbleItem(body, 'client-twin-id');
        const entry = bubbleItem(body, 'client-opaque-id');
        thread.append(twin, entry);
        twin.remove();
        setTimeout(() => entry.setAttribute('data-event-urn', 'server-opaque-id'), 0);
      } else if (arm === 'server') {
        const placeholder = bubbleItem(body, 'client-uuid');
        thread.appendChild(placeholder);
        setTimeout(() => {
          thread.appendChild(bubbleItem(
            body, 'urn:li:msg_message:(self,server-new)', SELF_URN));
          placeholder.remove();
        }, 50);
      } else if (arm === 'remount') {
        remount(body, '/messaging/thread/2-abc==/', '/in/fadi-eliwi/',
          ['urn:li:msg_message:(self,server-first)']);
      } else if (arm === 'off-route') {
        remount(body, '/feed/', '/in/fadi-eliwi/',
          ['urn:li:msg_message:(self,server-first)']);
      } else if (arm === 'other-header') {
        remount(body, '/messaging/thread/2-other==/', '/in/ACoAAC/',
          ['urn:li:msg_message:(self,elsewhere)']);
      } else if (arm === 'incoming') {
        setTimeout(() => thread.appendChild(bubbleItem(
          body, 'urn:li:msg_message:(other,incoming)', RECIPIENT_URN)), 30);
      } else if (arm === 'older') {
        thread.insertBefore(
          bubbleItem(body, 'urn:li:msg_message:(self,older)', SELF_URN),
          thread.firstChild);
      } else if (arm === 'rerender') {
        const entry = thread.lastElementChild;
        const copy = entry.cloneNode(true);
        entry.remove();
        thread.appendChild(copy);
      } else if (arm === 'duplicate') {
        for (const suffix of ['one', 'two']) {
          thread.appendChild(bubbleItem(
            body, `urn:li:msg_message:(self,${suffix})`, SELF_URN));
        }
      }
      composer.replaceChildren();
    });
  }
"""

# The measured item: the body <p> in its two-<div> pair, inside one more <div>
# (the bubble) that adds nothing to the text here.
BUBBLE_SHAPE = "inner => '<div><div><div>' + P(inner) + '</div></div></div>'"
ACKNOWLEDGEMENT_ARMS = ["opaque", "server", "remount"]


def multiline_page(arm: str, body: str, *, shape: str = BUBBLE_SHAPE) -> str:
    """A compose page whose click makes LinkedIn answer with *body* via *arm*."""
    send_js = MULTILINE_BUBBLE_JS.replace("__SHAPE__", shape) + (
        f"sendAs({json.dumps(arm)}, {json.dumps(body)});"
    )
    # The empty composer holds one empty paragraph (measured live, September
    # 2026), which is where whole-message insertion yields one <p> per line.
    return compose_page(send_js, draft="<p><br></p>")


async def bubble_bodies(page) -> list[str]:
    return await page.evaluate(
        """() => Array.from(
            document.querySelectorAll('[data-view-name="message-list-item"] p')
        ).map(body => body.innerText)"""
    )


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
        # Setup only: goto and set_content load the routed test page, and the
        # sender under test never navigates, so its waits keep the 600ms above.
        # A loaded CI runner took longer than 600ms for that first load.
        page.set_default_navigation_timeout(10_000)
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
    sender = _sender(page)
    with (
        patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(
            sender,
            "_read_profile_message_target",
            new_callable=AsyncMock,
            return_value=_ProfileMessageTargetResolution("resolved", TARGET),
        ),
        patch(
            "linkedin_mcp_server.linkedin.message_sender._message_page_url_is_safe",
            return_value=True,
        ),
    ):
        return await sender.send_message(
            "fadi-eliwi", message, confirm_send=confirm_send
        )


async def read_profile_target(
    page, html: str, *, query: str = ""
) -> _ProfileMessageTargetResolution:
    async def fulfill(route):
        await route.fulfill(status=200, content_type="text/html", body=html)

    await page.route("https://www.linkedin.com/**", fulfill)
    await page.goto(f"https://www.linkedin.com{PROFILE_PATH}{query}")
    return await _sender(page)._read_profile_message_target()


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
        sender = _sender(dom_page)
        # The reader borrows the facade's top-card read until the message
        # sender owns it, so wiring it here is what the facade does.
        reader = ProfilePageReader(
            PageSession(dom_page), sender._read_profile_message_target
        )

        resolution = await read_profile_target(dom_page, html)

        assert resolution.status == "unavailable"
        assert await reader._extract_profile_urn() is None

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

    # Since late September 2026 LinkedIn lands /in/<name>/ on
    # /in/<name>/?isSelfProfile=false (#1181). The page script resolved that
    # card all along; the URL check after it refused every recipient.
    async def test_redesigned_profile_url_resolves_its_top_card(self, dom_page):
        html = profile_page(
            f'<section><h1>{DISPLAY_NAME}</h1><a href="{COMPOSE_URL}">message</a>'
            "</section>"
        )

        resolution = await read_profile_target(
            dom_page, html, query="?isSelfProfile=false"
        )

        assert resolution.status == "resolved"
        assert resolution.target is not None
        assert resolution.target.profile_path == PROFILE_PATH
        assert resolution.target.profile_urn == "ACoAAB"

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

        resolution = await _sender(dom_page)._read_profile_message_target()

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

    async def test_queryless_route_switch_before_second_state_fails_closed(
        self, dom_page
    ):
        alice_route = "https://www.linkedin.com/messaging/thread/ALICE/"
        bob_route = "https://www.linkedin.com/messaging/thread/BOB/"
        await dom_page.goto(alice_route)
        await dom_page.set_content(compose_page(NOOP_SEND_JS))
        sender = _sender(dom_page)
        read_state = sender._read_message_composer_state
        state_reads = 0

        async def switch_route(target):
            nonlocal state_reads
            state_reads += 1
            if state_reads == 2:
                await dom_page.evaluate(
                    "history.replaceState({}, '', '/messaging/thread/BOB/')"
                )
            return await read_state(target)

        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                sender,
                "_read_profile_message_target",
                new_callable=AsyncMock,
                return_value=_ProfileMessageTargetResolution("resolved", TARGET),
            ),
            patch.object(
                sender, "_read_message_composer_state", side_effect=switch_route
            ),
        ):
            result = await sender.send_message("fadi-eliwi", MESSAGE, confirm_send=True)

        assert result["status"] == "recipient_resolution_failed"
        assert result["sent"] is False
        assert result["retry_safe"] is True
        assert state_reads == 2
        assert dom_page.url == bob_route
        assert (await dom_page.locator("#composer").inner_text()).strip() == ""
        assert await dom_page.evaluate("document.body.dataset.clicked") is None

    async def test_queryless_route_switch_before_owner_resolution_fails_closed(
        self, dom_page
    ):
        alice_route = "https://www.linkedin.com/messaging/thread/ALICE/"
        bob_route = "https://www.linkedin.com/messaging/thread/BOB/"
        await dom_page.goto(alice_route)
        await dom_page.set_content(compose_page(NOOP_SEND_JS))
        await dom_page.evaluate(
            """() => {
                window.__originalComposer = document.getElementById('composer');
                window.__originalSend = document.getElementById('send');
            }"""
        )
        sender = _sender(dom_page)
        resolve_owner = sender._resolve_message_owner

        async def switch_route(target, *, expected_route):
            assert target == TARGET
            assert expected_route == alice_route
            await dom_page.evaluate(
                "history.replaceState({}, '', '/messaging/thread/BOB/')"
            )
            return await resolve_owner(target, expected_route=expected_route)

        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                sender,
                "_read_profile_message_target",
                new_callable=AsyncMock,
                return_value=_ProfileMessageTargetResolution("resolved", TARGET),
            ),
            patch.object(sender, "_resolve_message_owner", side_effect=switch_route),
        ):
            result = await sender.send_message("fadi-eliwi", MESSAGE, confirm_send=True)

        assert result["status"] == "recipient_resolution_failed"
        assert result["sent"] is False
        assert result["retry_safe"] is True
        assert dom_page.url == bob_route
        assert await dom_page.evaluate(
            """() => (
                document.getElementById('composer') === window.__originalComposer &&
                document.getElementById('send') === window.__originalSend
            )"""
        )
        assert (await dom_page.locator("#composer").inner_text()).strip() == ""
        assert await dom_page.evaluate("document.body.dataset.clicked") is None

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
        ["First\tSecond", "First\x7fSecond", "First\u2028Second"],
        ids=["tab", "del", "line-separator"],
    )
    async def test_control_characters_are_rejected_before_dom_interaction(
        self, dom_page, message
    ):
        result = await send(
            dom_page, compose_page(ID_TRANSITION_SEND_JS), message=message
        )

        assert result["status"] == "invalid_message"
        assert result["message"] == (
            "Message must not contain control characters other than line breaks."
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

    async def test_full_page_thread_beside_form_is_confirmed(self, dom_page):
        # The messaging page has no dialog: the composer <form> is the owner
        # and the message list is its sibling inside the conversation pane.
        html = compose_page(ID_TRANSITION_SEND_JS).replace(
            '<section id="conversation" role="dialog">', '<section id="conversation">'
        )

        result = await send(dom_page, html)

        assert result["status"] == "sent"
        assert result["sent"] is True

    async def test_full_page_bubble_outside_pane_is_not_confirmed(self, dom_page):
        html = (
            compose_page(OUTSIDE_OWNER_SEND_JS)
            .replace(
                '<section id="conversation" role="dialog">',
                '<section id="conversation">',
            )
            .replace("</section>", '</section><aside id="outside"></aside>')
        )

        result = await send(dom_page, html)

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False

    async def test_server_node_replacing_placeholder_is_confirmed(self, dom_page):
        result = await send(dom_page, compose_page(SERVER_REPLACEMENT_SEND_JS))

        assert result["status"] == "sent"
        assert result["sent"] is True

    @pytest.mark.parametrize(
        ("role", "header"),
        [(' role="dialog"', PROFILE_PATH), ("", f"/in/{TARGET.profile_urn}/")],
        ids=["overlay-vanity-header", "full-page-urn-header"],
    )
    async def test_new_thread_pane_remount_is_confirmed(self, dom_page, role, header):
        html = compose_page(
            PANE_REMOUNT_SEND_JS + "remountTo('/messaging/thread/2-abc==/', "
            f"['urn:li:msg_message:(self,server-first)'], '{header}');"
        ).replace(
            '<section id="conversation" role="dialog">',
            f'<section id="conversation"{role}>',
        )

        result = await send(dom_page, html)

        assert result["status"] == "sent"
        assert result["sent"] is True

    async def test_remount_to_a_thread_with_another_header_is_not_confirmed(
        self, dom_page
    ):
        # The submit did nothing and the page moved to a conversation with
        # someone else, whose only message is ours with the same text. On the
        # full page the header sits outside the composer's form.
        html = compose_page(
            PANE_REMOUNT_SEND_JS + "remountTo('/messaging/thread/2-other==/', "
            "['urn:li:msg_message:(self,elsewhere)'], '/in/ACoAAC/');"
        ).replace(
            '<section id="conversation" role="dialog">', '<section id="conversation">'
        )

        result = await send(dom_page, html)

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False

    @pytest.mark.parametrize("headed", [True, False], ids=["headed", "follow-up"])
    async def test_incoming_message_with_the_same_text_is_not_confirmed(
        self, dom_page, headed
    ):
        """A message from the recipient is no acknowledgement of this submit.

        The fixtures claim the algorithm plus the measured link shape (a sender
        header linking ``/in/<profile URN>``), not a full copy of LinkedIn's
        markup. The unheaded follow-up is an assumption; no live thread showed
        one yet.
        """
        html = compose_page(
            INCOMING_AFTER_NOOP_SEND_JS
            + f"incoming({{headed: {'true' if headed else 'false'}}});"
        )

        result = await send(dom_page, html)

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False

    async def test_own_follow_up_without_a_header_is_confirmed(self, dom_page):
        result = await send(dom_page, compose_page(OWN_CONTINUATION_SEND_JS))

        assert result["status"] == "sent"
        assert result["sent"] is True

    async def test_hidden_second_editor_in_the_full_page_pane_is_confirmed(
        self, dom_page
    ):
        html = (
            compose_page(SERVER_REPLACEMENT_SEND_JS)
            .replace(
                '<section id="conversation" role="dialog">',
                '<section id="conversation">',
            )
            .replace(
                '<div id="thread">',
                '<div role="textbox" contenteditable="true" style="display:none">'
                '</div><div id="thread">',
            )
        )

        result = await send(dom_page, html)

        assert result["status"] == "sent"
        assert result["sent"] is True

    async def test_pane_remount_off_the_message_route_is_not_confirmed(self, dom_page):
        html = compose_page(
            PANE_REMOUNT_SEND_JS
            + "remountTo('/feed/', ['urn:li:msg_message:(self,server-first)']);"
        )

        result = await send(dom_page, html)

        assert result["status"] == "send_unconfirmed"
        assert result["retry_safe"] is False

    async def test_send_started_in_an_open_thread_route_is_confirmed(self, dom_page):
        html = compose_page(
            "history.replaceState({}, '', '/messaging/thread/2-open==/');"
            + SERVER_REPLACEMENT_SEND_JS
        )

        result = await send(dom_page, html)

        assert result["status"] == "sent"
        assert result["sent"] is True

    async def test_send_moving_to_another_thread_is_not_confirmed(self, dom_page):
        html = compose_page(
            "history.replaceState({}, '', '/messaging/thread/2-open==/');"
            + PANE_REMOUNT_SEND_JS
            + "remountTo('/messaging/thread/2-other==/', "
            "['urn:li:msg_message:(self,server-first)']);"
        )

        result = await send(dom_page, html)

        assert result["status"] == "send_unconfirmed"
        assert result["retry_safe"] is False

    async def test_older_history_loaded_above_is_not_confirmed(self, dom_page):
        result = await send(dom_page, compose_page(OLDER_HISTORY_SEND_JS))

        assert result["status"] == "send_unconfirmed"
        assert result["retry_safe"] is False

    async def test_thread_route_with_other_history_is_not_confirmed(self, dom_page):
        # A thread route with earlier messages is some other conversation:
        # the first message of a new thread remounts a pane holding only it.
        html = compose_page(
            PANE_REMOUNT_SEND_JS + "remountTo('/messaging/thread/2-other==/', "
            "['urn:li:msg_message:(other,old)|earlier message', "
            "'urn:li:msg_message:(self,server-first)']);"
        )

        result = await send(dom_page, html)

        assert result["status"] == "send_unconfirmed"
        assert result["retry_safe"] is False

    async def test_two_server_nodes_with_the_text_are_not_confirmed(self, dom_page):
        result = await send(dom_page, compose_page(TWO_SERVER_NODES_SEND_JS))

        assert result["status"] == "send_unconfirmed"
        assert result["retry_safe"] is False

    async def test_remounted_pane_with_two_server_nodes_is_not_confirmed(
        self, dom_page
    ):
        html = compose_page(
            PANE_REMOUNT_SEND_JS + "remountTo('/messaging/thread/2-abc==/', "
            "['urn:li:msg_message:(self,one)', 'urn:li:msg_message:(self,two)']);"
        )

        result = await send(dom_page, html)

        assert result["status"] == "send_unconfirmed"

    async def test_rerendered_server_node_from_before_submit_is_not_confirmed(
        self, dom_page
    ):
        # The thread already holds a message with the same text; LinkedIn
        # re-rendering it must not read as an acknowledgement.
        html = compose_page(STALE_SERVER_NODE_SEND_JS).replace(
            'data-event-urn="existing-message-id"',
            'data-event-urn="urn:li:msg_message:(self,old)"',
        )

        result = await send(dom_page, html)

        assert result["status"] == "send_unconfirmed"
        assert result["retry_safe"] is False

    @pytest.mark.parametrize("confirm_send", [False, True])
    async def test_enter_to_send_preference_is_reported(self, dom_page, confirm_send):
        html = compose_page(
            "document.getElementById('toggle').addEventListener('click', () => {"
            "  document.body.dataset.clicked = 'true'; });"
        ).replace(
            '<button id="send" type="submit">Send</button>',
            '<button id="toggle" type="button" class="msg-form__send-toggle">'
            "Open send options</button>",
        )

        result = await send(dom_page, html, confirm_send=confirm_send)

        assert result["status"] == "enter_to_send_enabled"
        assert "Click Send to send" in result["message"]
        assert result["recipient_selected"] is True
        assert result["sent"] is False
        assert result["retry_safe"] is True
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert await dom_page.locator("#composer").inner_text() == ""

    async def test_unknown_send_toggle_does_not_claim_enter_preference(self, dom_page):
        html = compose_page(NOOP_SEND_JS).replace(
            '<button id="send" type="submit">Send</button>',
            '<button id="toggle" type="button" class="unknown-toggle">'
            "Open send options</button>",
        )

        result = await send(dom_page, html)

        assert result["status"] == "send_unavailable"
        assert result["sent"] is False
        assert result["retry_safe"] is True
        assert await dom_page.locator("#composer").inner_text() == ""

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
        sender = _sender(dom_page)
        resolve_owner = sender._resolve_message_owner
        captured = {}

        async def capture_owner(target, *, expected_route):
            owner = await resolve_owner(target, expected_route=expected_route)
            captured["owner"] = owner
            return owner

        confirmation_started = anyio.Event()

        async def wait_for_confirmation(*_args, **_kwargs):
            confirmation_started.set()
            await anyio.sleep_forever()

        async def expire_after_confirmation(scope: anyio.CancelScope):
            await confirmation_started.wait()
            scope.deadline = anyio.current_time() + 0.05

        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                sender,
                "_read_profile_message_target",
                new_callable=AsyncMock,
                return_value=_ProfileMessageTargetResolution("resolved", TARGET),
            ),
            patch(
                "linkedin_mcp_server.linkedin.message_sender._message_page_url_is_safe",
                return_value=True,
            ),
            patch.object(sender, "_resolve_message_owner", side_effect=capture_owner),
            patch.object(
                sender,
                "_message_send_confirmed",
                side_effect=wait_for_confirmation,
            ),
        ):
            async with anyio.create_task_group() as group:
                with pytest.raises(TimeoutError):
                    with anyio.fail_after(10) as scope:
                        group.start_soon(expire_after_confirmation, scope)
                        await sender.send_message(
                            "fadi-eliwi", MESSAGE, confirm_send=True
                        )
                group.cancel_scope.cancel()

        assert confirmation_started.is_set()
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


def _sent(result: dict) -> bool:
    return result["status"] == "sent" and result["sent"] is True


def _unconfirmed(result: dict) -> bool:
    return (
        result["status"] == "send_unconfirmed"
        and result["sent"] is False
        and result["retry_safe"] is False
    )


class TestMultiLineConfirmationDom:
    """Confirmation of a multi-line message against the measured bubble.

    Each acknowledgement arm is driven on its own: ``opaque`` is the same-node
    event-ID transition, ``server`` the placeholder replaced by a new server
    URN node, ``remount`` the compose route remounting as a new thread.
    """

    @pytest.mark.parametrize("arm", ACKNOWLEDGEMENT_ARMS)
    @pytest.mark.parametrize(
        ("message", "rendered"),
        [
            ("Line one\nLine two", "Line one\nLine two"),
            ("a\n\n\nb", "a\n\n\nb"),
            ("x  y\nz", "x y\nz"),
            (
                "See https://example.com/a\nThanks",
                "See https://example.com/a\nThanks",
            ),
        ],
        ids=["two-lines", "two-empty-lines", "collapsed-spaces", "link-line"],
    )
    async def test_rendered_body_confirms(self, dom_page, arm, message, rendered):
        result = await send(dom_page, multiline_page(arm, message), message=message)

        assert _sent(result), result
        assert await dom_page.evaluate("document.body.dataset.clicked") == "1"
        assert (await bubble_bodies(dom_page))[-1] == rendered

    @pytest.mark.parametrize("arm", ACKNOWLEDGEMENT_ARMS)
    @pytest.mark.parametrize(
        "body",
        [
            "Line one\nLine two",
            "Line one\n\n\nLine two",
            "Line two\n\nLine one",
            "Line one",
        ],
        ids=["lost-empty-line", "extra-empty-line", "reordered", "first-line-only"],
    )
    async def test_different_lines_are_not_confirmed(self, dom_page, arm, body):
        message = "Line one\n\nLine two"

        result = await send(dom_page, multiline_page(arm, body), message=message)

        assert _unconfirmed(result), result
        assert await dom_page.evaluate("document.body.dataset.clicked") == "1"

    @pytest.mark.parametrize("arm", ACKNOWLEDGEMENT_ARMS)
    @pytest.mark.parametrize(
        "shape",
        [
            "inner => '<div><div><div><p>Intro <span>' + inner"
            " + '</span> outro</p></div></div></div>'",
            "inner => '<div><div><div><p>Intro <a href=\"https://example.com/\">'"
            " + inner + '</a> outro</p></div></div></div>'",
            "inner => '<div><p>' + inner + '</p><div><div>' + P('Other text')"
            " + '</div></div></div>'",
            "inner => '<div><div><div>' + P(inner) + '</div></div><div><div>'"
            " + P('Second paragraph') + '</div></div></div>'",
            "inner => '<div><div><div>prefix' + P(inner) + 'suffix</div></div></div>'",
            "inner => '<div><div>Extra text<div>' + P(inner) + '</div></div></div>'",
            "inner => '<div><div><div><p><span>' + inner"
            " + '</span><br>extra</p></div></div></div>'",
            "inner => '<div><div><div><p>' + inner"
            " + '<img alt=\"\"></p></div></div></div>'",
            "inner => '<div><div><section>' + P(inner) + '</section></div></div>'",
            "inner => '<div><section><div>' + P(inner) + '</div></section></div>'",
            "inner => '<div>' + P(inner) + '</div>'",
            "inner => P(inner)",
        ],
        ids=[
            "span-inside-larger-body",
            "link-inside-larger-body",
            "matching-metadata-paragraph",
            "second-body-paragraph",
            "text-beside-body",
            "text-in-grandparent-only",
            "span-then-more-lines",
            "image-in-body",
            "parent-not-div",
            "grandparent-not-div",
            "grandparent-is-the-item",
            "parent-is-the-item",
        ],
    )
    async def test_body_outside_the_measured_boundary_is_not_confirmed(
        self, dom_page, arm, shape
    ):
        """The sent lines appear, but not as the item's one complete body.

        Synthetic counterexamples to the boundary, not layouts seen on
        LinkedIn. In each, the text "A\\nB" is still rendered somewhere in
        the item.
        """
        message = "A\nB"

        result = await send(
            dom_page, multiline_page(arm, message, shape=shape), message=message
        )

        assert _unconfirmed(result), result
        assert await dom_page.evaluate("document.body.dataset.clicked") == "1"

    @pytest.mark.parametrize("arm", ACKNOWLEDGEMENT_ARMS)
    async def test_extra_outer_div_around_the_pair_confirms(self, dom_page, arm):
        # The rule constrains the two immediate wrappers, not the depth.
        shape = (
            "inner => '<div><div><div><div>' + P(inner) + '</div></div></div></div>'"
        )

        result = await send(
            dom_page, multiline_page(arm, "A\nB", shape=shape), message="A\nB"
        )

        assert _sent(result), result

    @pytest.mark.parametrize("arm", ACKNOWLEDGEMENT_ARMS)
    @pytest.mark.parametrize(
        ("message", "body", "rendered", "confirmed"),
        [
            ("A\u2003\u2003B\nC", "A B\nC", "A B\nC", False),
            ("A\u00a0\u00a0B\nC", "A\u00a0\u00a0B\nC", "A\u00a0\u00a0B\nC", True),
            # The editor holds "line\u00a0" and LinkedIn sends that.
            ("line \nnext", "line\u00a0\nnext", "line \nnext", True),
            # The editor holds "\u00a0 B": the bubble differs from the input.
            ("A\n  B\nC", "A\n\u00a0 B\nC", "A\n B\nC", True),
        ],
        ids=["em-space-shown-as-space", "nbsp-kept", "nbsp-at-line-end", "nbsp-lead"],
    )
    async def test_whitespace_is_compared_exactly(
        self, dom_page, arm, message, body, rendered, confirmed
    ):
        result = await send(dom_page, multiline_page(arm, body), message=message)

        assert (await bubble_bodies(dom_page))[-1] == rendered
        if confirmed:
            assert _sent(result), result
        else:
            assert _unconfirmed(result), result

    @pytest.mark.parametrize("arm", ["opaque", "remount"])
    async def test_missing_prediction_is_not_confirmed(self, dom_page, arm):
        message = "Line one\nLine two"
        original = MessageSender._prepare_message_confirmation
        dropped = []

        async def prepare_then_drop(self, message, *, target, owner):
            token = await original(self, message, target=target, owner=owner)
            dropped.append(
                await self._page.evaluate(
                    """owner => {
                        const marker = owner.querySelector(
                            '[data-linkedin-mcp-confirmation]'
                        );
                        const had = marker.hasAttribute('data-linkedin-mcp-rendered');
                        marker.removeAttribute('data-linkedin-mcp-rendered');
                        return had;
                    }""",
                    owner,
                )
            )
            return token

        with patch.object(
            MessageSender, "_prepare_message_confirmation", prepare_then_drop
        ):
            result = await send(dom_page, multiline_page(arm, message), message=message)

        assert dropped == [True]
        assert _unconfirmed(result), result
        assert await dom_page.evaluate("document.body.dataset.clicked") == "1"

    @pytest.mark.parametrize(
        "arm",
        [
            "incoming",
            "older",
            "rerender",
            "duplicate",
            "off-route",
            "other-header",
            "removed-twin",
        ],
        ids=[
            "sent-by-recipient",
            "older-history",
            "baseline-rerendered",
            "two-server-nodes",
            "off-message-route",
            "other-participant",
            "removed-matching-twin",
        ],
    )
    async def test_existing_guards_hold_for_multi_line(self, dom_page, arm):
        """Each case renders the right body; an existing guard refuses it."""
        message = "Line one\n\nLine two"

        result = await send(dom_page, multiline_page(arm, message), message=message)

        assert _unconfirmed(result), result
        assert await dom_page.evaluate("document.body.dataset.clicked") == "1"


def acknowledging_page(
    arm: str, *, start: str | None = None, leave_to: str | None = None
) -> str:
    """A compose page whose click answers on one arm, maybe after a route change."""
    start_js = f"history.replaceState({{}}, '', {json.dumps(start)});" if start else ""
    leave_js = (
        f"history.pushState({{}}, '', {json.dumps(leave_to)});" if leave_to else ""
    )
    if arm == "opaque":
        ack = """
          const entry = messageItem(text, 'client-opaque-id');
          document.getElementById('thread').appendChild(entry);
          setTimeout(() => {
            entry.setAttribute('data-event-urn', 'server-opaque-id');
          }, 0);
          composer.textContent = '';
        """
    elif arm == "server":
        ack = """
          const placeholder = messageItem(text, 'client-uuid');
          document.getElementById('thread').appendChild(placeholder);
          composer.textContent = '';
          setTimeout(() => {
            document.getElementById('thread').appendChild(
              messageItem(text, 'urn:li:msg_message:(self,server-new)', SELF_URN));
            placeholder.remove();
          }, 50);
        """
    else:
        raise AssertionError(arm)
    return compose_page(
        f"""
          {start_js}
          document.getElementById('send').addEventListener('click', event => {{
            event.preventDefault();
            document.body.dataset.clicked = 'true';
            {leave_js}
            const composer = document.getElementById('composer');
            const text = composer.innerText;
            {ack}
          }});
        """
    )


_ROUTE_ARMS = ["opaque", "server"]
_OPEN_THREAD = "/messaging/thread/2-open==/"
_OTHER_THREAD = "/messaging/thread/2-other==/"
_COMPOSE_PATH = "/messaging/compose/?recipient=ACoAAB"
_COMPOSE_PATHS = [_COMPOSE_PATH, "/messaging/thread/new/"]


class TestConfirmationThread:
    """The thread id is the path the confirmation itself observed."""

    @pytest.mark.parametrize("arm", _ROUTE_ARMS)
    async def test_the_same_thread_confirms(self, dom_page, arm):
        result = await send(dom_page, acknowledging_page(arm, start=_OPEN_THREAD))

        assert _sent(result), result
        assert result["thread_id"] == "2-open=="

    @pytest.mark.parametrize("arm", _ROUTE_ARMS)
    async def test_a_move_to_another_thread_does_not_confirm(self, dom_page, arm):
        result = await send(
            dom_page,
            acknowledging_page(arm, start=_OPEN_THREAD, leave_to=_OTHER_THREAD),
        )

        assert _unconfirmed(result), result
        assert "thread_id" not in result
        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"

    @pytest.mark.parametrize("arm", _ROUTE_ARMS)
    @pytest.mark.parametrize("compose_path", _COMPOSE_PATHS)
    async def test_leaving_a_thread_for_compose_does_not_confirm(
        self, dom_page, arm, compose_path
    ):
        result = await send(
            dom_page,
            acknowledging_page(arm, start=_OPEN_THREAD, leave_to=compose_path),
        )

        assert _unconfirmed(result), result
        assert "thread_id" not in result
        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"

    @pytest.mark.parametrize("arm", _ROUTE_ARMS)
    @pytest.mark.parametrize("compose_path", _COMPOSE_PATHS)
    async def test_compose_staying_on_compose_has_no_thread(
        self, dom_page, arm, compose_path
    ):
        result = await send(dom_page, acknowledging_page(arm, start=compose_path))

        assert _sent(result), result
        assert result["thread_id"] is None

    @pytest.mark.parametrize("arm", _ROUTE_ARMS)
    async def test_compose_redirect_to_new_has_no_thread(self, dom_page, arm):
        result = await send(
            dom_page,
            acknowledging_page(arm, leave_to="/messaging/thread/new/"),
        )

        assert _sent(result), result
        assert result["thread_id"] is None

    @pytest.mark.parametrize("compose_path", _COMPOSE_PATHS)
    async def test_compose_opening_a_thread_reports_that_thread(
        self, dom_page, compose_path
    ):
        html = compose_page(
            PANE_REMOUNT_SEND_JS + "remountTo('/messaging/thread/2-abc==/', "
            "['urn:li:msg_message:(self,server-first)']);"
            f"history.replaceState({{}}, '', {json.dumps(compose_path)});"
        )

        result = await send(dom_page, html)

        assert _sent(result), result
        assert result["thread_id"] == "2-abc=="
        assert await dom_page.locator("#conversation").count() == 0

    async def test_server_replacement_confirms_after_the_marker_is_invalidated(
        self, dom_page
    ):
        from patchright.async_api import JSHandle

        seen: list[str | None] = []
        original = JSHandle.json_value

        async def json_value(self):
            seen.append(
                await dom_page.evaluate(
                    """() => {
                        const marker = document.querySelector(
                            '[data-linkedin-mcp-confirmation]'
                        );
                        return marker
                            ? marker.getAttribute('data-linkedin-mcp-invalid')
                            : null;
                    }"""
                )
            )
            return await original(self)

        with patch.object(JSHandle, "json_value", json_value):
            result = await send(dom_page, acknowledging_page("server"))

        assert seen == ["true"]
        assert _sent(result), result
        assert result["thread_id"] is None

    async def test_a_compose_thread_with_history_does_not_confirm(self, dom_page):
        """Prior history and no recipient header is some other conversation.

        Both arms: a candidate must not confirm a pane the server arm refuses.
        """
        result = await send(
            dom_page,
            acknowledging_page("opaque", leave_to="/messaging/thread/2-kept==/"),
        )

        assert _unconfirmed(result), result
        assert "thread_id" not in result
        assert (
            await dom_page.locator('[data-view-name="message-list-item"]').count() >= 2
        )

    async def test_owner_preserving_compose_to_thread_confirms(self, dom_page):
        """The owner stays, and the pane is one new thread.

        One visible item and the recipient linked outside the messages, which
        is the transition both arms admit. The section is not remounted.
        """
        result = await send(
            dom_page,
            compose_page(
                """
                  document.getElementById('send').addEventListener('click', event => {
                    event.preventDefault();
                    document.body.dataset.clicked = 'true';
                    const composer = document.getElementById('composer');
                    const text = composer.innerText;
                    composer.textContent = '';
                    history.pushState({}, '', '/messaging/thread/2-kept==/');
                    const thread = document.getElementById('thread');
                    thread.replaceChildren();
                    const header = document.createElement('a');
                    header.href = 'https://www.linkedin.com/in/fadi-eliwi/';
                    header.textContent = 'Participant';
                    thread.before(header);
                    const entry = messageItem(text, 'client-opaque-id');
                    thread.appendChild(entry);
                    setTimeout(() => {
                      entry.setAttribute('data-event-urn', 'server-opaque-id');
                    }, 0);
                  });
                """
            ),
        )

        assert _sent(result), result
        assert result["thread_id"] == "2-kept=="
        assert await dom_page.locator("#conversation").count() == 1

    @pytest.mark.parametrize("arm", _ROUTE_ARMS)
    async def test_thread_id_is_the_snapshot_not_a_later_route(self, dom_page, arm):
        from patchright.async_api import JSHandle

        original = JSHandle.json_value

        async def json_value(self):
            value = await original(self)
            if (
                isinstance(value, dict)
                and value.get("path") == "/messaging/thread/2-open==/"
            ):
                await dom_page.evaluate(
                    "() => history.pushState({}, '', '/messaging/thread/2-later==/')"
                )
            return value

        with patch.object(JSHandle, "json_value", json_value):
            result = await send(dom_page, acknowledging_page(arm, start=_OPEN_THREAD))

        assert _sent(result), result
        assert result["thread_id"] == "2-open=="
        assert dom_page.url.endswith("/messaging/thread/2-later==/")
