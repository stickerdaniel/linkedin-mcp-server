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

import os
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

# Clears the composer and delivers nothing, which is what an optimistic
# reset followed by a failed submission leaves behind. This is the case that
# separates "a new occurrence appeared" from "the editor went empty": a
# confirmation that subtracted the draft only while the editor held text
# would read the clearance itself as the delivery.
CLEARING_NOOP_SEND_JS = """
  document.getElementById('send').addEventListener('click', () => {
    document.body.setAttribute('data-clicked', 'true');
    document.getElementById('composer').textContent = '';
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

FOREIGN_DELIVERING_SEND_JS = """
  document.getElementById('send').addEventListener('click', () => {
    document.body.setAttribute('data-clicked', 'true');
    const composer = document.getElementById('composer');
    const text = composer.innerText;
    if (!text.trim()) return;
    const entry = document.createElement('div');
    entry.className = 'msg';
    entry.textContent = text;
    document.getElementById('foreign-thread').appendChild(entry);
    composer.textContent = '';
  });
"""

REMOVING_OWNER_SEND_JS = """
  document.getElementById('send').addEventListener('click', () => {
    document.body.setAttribute('data-clicked', 'true');
    const text = document.getElementById('composer').innerText;
    const entry = document.createElement('div');
    entry.className = 'msg';
    entry.textContent = text;
    document.getElementById('outside').appendChild(entry);
    document.getElementById('conversation').remove();
  });
"""

REPLACING_OWNER_SEND_JS = """
  document.getElementById('send').addEventListener('click', () => {
    document.body.setAttribute('data-clicked', 'true');
    const owner = document.getElementById('conversation');
    const replacement = owner.cloneNode(true);
    const composer = replacement.querySelector('#composer');
    const entry = document.createElement('div');
    entry.className = 'msg';
    entry.textContent = composer.innerText;
    replacement.querySelector('#thread').appendChild(entry);
    composer.textContent = '';
    owner.replaceWith(replacement);
  });
"""

STATUS_GROWTH_SEND_JS = """
  document.getElementById('send').addEventListener('click', () => {
    document.body.setAttribute('data-clicked', 'true');
    document.getElementById('conversation').insertAdjacentHTML(
      'beforeend', '<span>available</span>');
  });
"""

CLEARING_STATUS_GROWTH_SEND_JS = """
  document.getElementById('send').addEventListener('click', () => {
    document.body.setAttribute('data-clicked', 'true');
    document.getElementById('composer').textContent = '';
    document.getElementById('conversation').insertAdjacentHTML(
      'beforeend', '<span>available</span>');
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
            if os.environ.get("CI"):
                # CI installs chromium before this suite runs, so a launch
                # that fails there is a broken environment rather than an
                # absent browser. Skipping would take every case in this file
                # out of CI while the run stayed green.
                raise
            pytest.skip(f"chromium unavailable: {exc}")
        # The confirmation waits on the page-level default, so a failed send
        # has to give up quickly here.
        page.set_default_timeout(1500)
        try:
            yield page
        finally:
            await browser.close()


async def send(
    page, html: str, *, message: str = MESSAGE, confirm_send: bool = True
) -> dict:
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
        return await extractor.send_message(
            "fadi-eliwi", message, confirm_send=confirm_send
        )


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

        assert result["status"] == "invalid_message"
        assert result["sent"] is False
        assert result["retry_safe"] is True
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert await text_of(dom_page, "#composer") == draft
        assert await dom_page.locator("#thread .msg").count() == 1

    async def test_foreign_recipient_never_reaches_the_composer(self, dom_page):
        # Issue #861: the composer belongs to someone else. Nothing may be
        # typed and nothing may be clicked, so the message the caller wrote
        # cannot reach a person they never named.
        draft = "Private foreign draft"
        page = (
            compose_page(DELIVERING_SEND_JS, draft=draft)
            .replace(
                f'href="https://www.linkedin.com{PROFILE_PATH}"',
                'href="https://www.linkedin.com/in/someone-else/"',
            )
            .replace(
                "</section>",
                """<button aria-label="Close your draft conversation"
              onclick="document.body.dataset.closed = String(
                Number(document.body.dataset.closed || 0) + 1);
                document.getElementById('composer').remove()">Close</button>
              </section>""",
            )
        )
        result = await send(dom_page, page)

        observed = await dom_page.evaluate(
            """() => ({
                draft: document.getElementById('composer')?.innerText ?? null,
                closed: Number(document.body.dataset.closed || 0),
            })"""
        )
        assert result["sent"] is False
        assert result["status"] == "composer_unavailable"
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert observed == {"draft": draft, "closed": 0}
        assert await dom_page.locator("#thread .msg").count() == 1

    async def test_focus_restored_draft_is_left_untouched(self, dom_page):
        draft = "Confidential restored draft"
        page = compose_page(
            DELIVERING_SEND_JS
            + f"""
              document.getElementById('composer').addEventListener('focus', () => {{
                document.getElementById('composer').textContent = '{draft}';
              }});
            """
        ).replace(
            "</section>",
            """<button aria-label="Close your draft conversation"
              onclick="document.body.dataset.closed = String(
                Number(document.body.dataset.closed || 0) + 1);
                document.getElementById('composer').remove()">Close</button>
              </section>""",
        )

        result = await send(dom_page, page)

        observed = await dom_page.evaluate(
            """() => ({
                draft: document.getElementById('composer')?.innerText ?? null,
                closed: Number(document.body.dataset.closed || 0),
                clicked: document.body.dataset.clicked ?? null,
            })"""
        )
        assert result["status"] == "composer_occupied"
        assert result["sent"] is False
        assert result["retry_safe"] is True
        assert observed == {"draft": draft, "closed": 0, "clicked": None}
        assert await dom_page.locator("#thread .msg").count() == 1

    async def test_state_error_before_newline_typing_is_retryable(self, dom_page):
        original = LinkedInExtractor._read_message_composer_state
        calls = 0

        async def fail_last_pretyping_read(extractor, target):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("synthetic pre-typing state failure")
            return await original(extractor, target)

        with (
            patch.object(
                LinkedInExtractor,
                "_read_message_composer_state",
                autospec=True,
                side_effect=fail_last_pretyping_read,
            ),
            pytest.raises(RuntimeError, match="pre-typing state failure"),
        ):
            await send(dom_page, compose_page(""), message="First\nSecond")

        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert await text_of(dom_page, "#composer") == ""

    @pytest.mark.parametrize(
        ("button_html", "case"),
        [
            ('<button id="send" type="submit" disabled>Send</button>', "disabled"),
            (
                '<button id="send" type="submit">Send</button>'
                '<button type="submit">Other</button>',
                "ambiguous",
            ),
        ],
        ids=lambda value: value if value in {"disabled", "ambiguous"} else None,
    )
    @pytest.mark.parametrize(
        ("message", "retry_safe"),
        [("Single line", True), ("First\nSecond", False)],
        ids=["single-line", "newline"],
    )
    async def test_blocked_submit_preserves_retry_policy(
        self, dom_page, button_html, case, message, retry_safe
    ):
        page = compose_page("").replace(
            '<button id="send" type="submit">Send</button>', button_html
        )

        result = await send(dom_page, page, message=message)

        assert result["status"] == "send_unavailable", case
        assert result["sent"] is False
        assert result["retry_safe"] is retry_safe
        assert await dom_page.evaluate("document.body.dataset.clicked") is None

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
        # Nothing was submitted, so calling again cannot deliver twice.
        assert result["retry_safe"] is True
        assert await dom_page.evaluate("document.body.dataset.clicked") is None
        assert await text_of(dom_page, "#composer") == DRAFT.strip()
        assert await dom_page.locator("#thread .msg").count() == 1

    @pytest.mark.parametrize(
        ("confirm_send", "status"),
        [(True, "composer_occupied"), (False, "confirmation_required")],
        ids=["occupied", "dry-run"],
    )
    async def test_draft_refusal_never_closes_the_composer(
        self, dom_page, confirm_send, status
    ):
        draft = "Private draft"
        page = compose_page("", draft=draft).replace(
            "</section>",
            """<button aria-label="Close your draft conversation"
              onclick="document.body.dataset.closed = String(
                Number(document.body.dataset.closed || 0) + 1);
                document.getElementById('composer').remove()">Close</button>
              </section>""",
        )

        result = await send(dom_page, page, confirm_send=confirm_send)

        observed = await dom_page.evaluate(
            """() => ({
                draft: document.getElementById('composer')?.innerText ?? null,
                closed: document.body.dataset.closed ?? null,
            })"""
        )
        assert result["status"] == status
        assert result["sent"] is False
        assert result["retry_safe"] is True
        assert observed == {"draft": draft, "closed": None}

    async def test_ineffective_send_is_not_confirmed(self, dom_page):
        # The reported failure: Send is clicked, the handler does nothing,
        # and the message stays in the composer next to an identical earlier
        # copy in the thread. Neither is evidence of delivery. The click did
        # happen, though, so the answer is "unknown" rather than "not sent".
        result = await send(dom_page, compose_page(NOOP_SEND_JS))

        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"
        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        # The click happened, so a retry can deliver the message twice.
        assert result["retry_safe"] is False
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
        # The click happened, so a retry can deliver the message twice.
        assert result["retry_safe"] is False
        assert await text_of(dom_page, "#composer") == MESSAGE
        assert await dom_page.locator("#thread .msg").count() == 1

    async def test_a_cleared_composer_alone_does_not_confirm(self, dom_page):
        # The composer empties and nothing arrives in the thread. The count
        # outside every editor is the same before and after, so this stays
        # unconfirmed. Reading the clearance as delivery is the mistake this
        # case exists to catch: an implementation that stopped subtracting
        # the draft once the editor went empty would pass every other case
        # in this file and confirm here.
        result = await send(dom_page, compose_page(CLEARING_NOOP_SEND_JS))

        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"
        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        assert await text_of(dom_page, "#composer") == ""
        assert await dom_page.locator("#thread .msg").count() == 1

    @pytest.mark.parametrize(
        "send_js",
        [STATUS_GROWTH_SEND_JS, CLEARING_STATUS_GROWTH_SEND_JS],
        ids=["draft-remains", "draft-cleared"],
    )
    async def test_status_substring_growth_is_not_confirmed(self, dom_page, send_js):
        result = await send(dom_page, compose_page(send_js), message="a")

        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"
        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        assert await text_of(dom_page, "#thread") == MESSAGE
        assert await text_of(dom_page, "#conversation span") == "available"

    async def test_delivered_short_message_is_confirmed(self, dom_page):
        result = await send(dom_page, compose_page(DELIVERING_SEND_JS), message="a")

        assert result["status"] == "sent"
        assert result["sent"] is True
        assert result["retry_safe"] is False
        entries = dom_page.locator("#thread .msg")
        assert await entries.count() == 2
        assert (await entries.last.inner_text()).strip() == "a"

    async def test_multiline_whitespace_units_are_normalized(self, dom_page):
        await dom_page.set_content(compose_page(""))
        await dom_page.evaluate(
            """() => {
                const entry = document.createElement('div');
                entry.innerHTML = 'First<br>Second';
                document.getElementById('thread').appendChild(entry);
            }"""
        )
        extractor = LinkedInExtractor(dom_page)
        owner = await extractor._resolve_message_owner(TARGET)
        assert owner is not None
        try:
            count = await extractor._message_text_occurrences(
                "  First \n  Second  ", target=TARGET, owner=owner
            )
        finally:
            await extractor._dispose_message_owner(owner)

        assert count == 1

    async def test_foreign_thread_growth_is_not_confirmed(self, dom_page):
        console_errors: list[str] = []
        dom_page.on(
            "console",
            lambda message: (
                console_errors.append(message.text) if message.type == "error" else None
            ),
        )
        page = compose_page(FOREIGN_DELIVERING_SEND_JS).replace(
            "</main>",
            '<aside id="foreign-thread"></aside></main>',
        )
        result = await send(dom_page, page)

        assert console_errors == []
        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"
        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        assert await text_of(dom_page, "#composer") == ""
        assert await dom_page.locator("#thread .msg").count() == 1
        entries = dom_page.locator("#foreign-thread .msg")
        assert await entries.count() == 1
        assert (await entries.last.inner_text()).strip() == MESSAGE

    async def test_removed_owner_with_matching_text_is_not_confirmed(self, dom_page):
        page = compose_page(REMOVING_OWNER_SEND_JS).replace(
            "</main>",
            '<aside id="outside"></aside></main>',
        )
        result = await send(dom_page, page)

        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"
        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        assert await dom_page.locator("#conversation").count() == 0
        assert await text_of(dom_page, "#outside .msg") == MESSAGE

    async def test_replacement_owner_with_copied_bubble_is_not_confirmed(
        self, dom_page
    ):
        result = await send(dom_page, compose_page(REPLACING_OWNER_SEND_JS))

        assert await dom_page.evaluate("document.body.dataset.clicked") == "true"
        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        assert await text_of(dom_page, "#composer") == ""
        entries = dom_page.locator("#thread .msg")
        assert await entries.count() == 2
        assert (await entries.last.inner_text()).strip() == MESSAGE

    async def test_delivered_message_is_confirmed(self, dom_page):
        # Same page, same earlier copy, but the Send handler moves the text
        # into the thread. The count outside the composer grows, so this one
        # confirms where the ineffective click above did not.
        result = await send(dom_page, compose_page(DELIVERING_SEND_JS))

        assert result["status"] == "sent"
        assert result["sent"] is True
        assert result["retry_safe"] is False
        assert await text_of(dom_page, "#composer") == ""
        entries = dom_page.locator("#thread .msg")
        assert await entries.count() == 2
        assert (await entries.last.inner_text()).strip() == MESSAGE
