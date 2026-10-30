"""Browser-DOM tests for archiving a conversation from its options menu.

The unit suite mocks ``page.evaluate``, so the programs behind
``ConversationReader.archive_conversation`` never execute there. These run the
whole action against a synthetic thread page in headless chromium, with the
navigation stubbed so every "reload" reads the same document. Each stubbed
load fetches LinkedIn's conversations query, answered with the folder the
page's state implies, the way the thread page loads its conversation. The
markup carries only what the code reads: sidebar rows as ``<li>`` with a
``label[aria-label]`` and an options button of their own, the thread's options
button named by screen-reader text, and a menu of ``[role="button"]`` items
that offers Archive whether or not the thread is archived, as LinkedIn's was
measured to, opened beside its button as LinkedIn's dropdown is. None of
LinkedIn's classes, so it is a claim about which signals are read, not about
LinkedIn's layout.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import json

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.exceptions import LinkedInOperationError
from linkedin_mcp_server.linkedin import conversations
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.conversations import ConversationReader
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.profile_page import ProfilePageReader
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.text import CONVERSATION_ACTIONS_EN_US

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

THREAD_URL = "https://www.linkedin.com/messaging/thread/2-abc/"
PREFIX = CONVERSATION_ACTIONS_EN_US.options_prefix
ARCHIVE = CONVERSATION_ACTIONS_EN_US.archive_item
CONVERSATIONS_URL = (
    "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql"
    "?queryId=messengerConversations.test"
)

# Any options button opens a menu for its own scope: the thread's Archive sets
# `data-archived` on the body, a row's records the click and nothing else.
# The menu opens in the button's own wrapper, where LinkedIn's dropdown was
# measured to be, unless the body says `data-portal`, which puts it at the end
# of the body, or `data-late`, which renders it 300 ms after the click;
# `data-pill` makes the click also reveal an Archive control elsewhere on the
# page at once.
# Every choice is recorded as an attribute rather than on `window`, because
# patchright evaluates in an isolated world that never sees the page's globals.
MENU_SCRIPT = """
const chosen = [];
const record = (entry) => {
    chosen.push(entry);
    document.body.dataset.chosen = JSON.stringify(chosen);
};
document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') document.querySelector('[role="menu"]')?.remove();
});
for (const button of document.querySelectorAll('main button[data-options]')) {
    button.addEventListener('click', () => {
        document.querySelector('[role="menu"]')?.remove();
        const scope = button.dataset.options;
        const labels = (document.body.dataset.items || 'Mark as unread,ARCHIVE,Delete')
            .split(',');
        const menu = document.createElement('div');
        menu.setAttribute('role', 'menu');
        for (const label of labels) {
            const item = document.createElement('div');
            item.setAttribute('role', 'button');
            item.innerHTML = '<div><p>' + label + '</p></div>';
            item.addEventListener('click', () => {
                record([scope, label]);
                menu.remove();
                if (scope !== 'thread' || document.body.hasAttribute('data-refuse')) return;
                if (label === 'ARCHIVE') document.body.setAttribute('data-archived', '');
            });
            menu.appendChild(item);
        }
        const place = () => (document.body.hasAttribute('data-portal')
            ? document.body : button.parentElement).appendChild(menu);
        if (document.body.hasAttribute('data-late')) setTimeout(place, 300);
        else place();
        if (document.body.hasAttribute('data-pill') && !document.querySelector('[data-pill-item]')) {
            // A control elsewhere on the page that the click happens to reveal.
            const pill = document.createElement('button');
            pill.setAttribute('role', 'button');
            pill.setAttribute('data-pill-item', '');
            pill.textContent = 'ARCHIVE';
            pill.addEventListener('click', () => record(['pill', 'ARCHIVE']));
            document.querySelector('main').appendChild(pill);
        }
    });
}
""".replace("ARCHIVE", ARCHIVE)


def options(scope: str, names: str) -> str:
    return (
        f'<div><button data-options="{scope}"><span style="position:absolute;'
        f'width:1px;height:1px;overflow:hidden">{PREFIX} {names}</span>…</button>'
        "</div>"
    )


def row(name: str) -> str:
    return (
        f'<li><label aria-label="Select conversation with {name}"></label>'
        f"<div>{name}</div>{options('row:' + name, name)}</li>"
    )


def thread_page(
    *, headers: int = 1, archived: bool = False, body: str = "", items: str = ""
) -> str:
    """A messaging page: the sidebar, then the open thread's header."""
    attributes = (" data-archived" if archived else "") + body
    if items:
        attributes += f' data-items="{items}"'
    header = "".join(options("thread", "Ada Lovelace") for _ in range(headers))
    return (
        f"<body{attributes}><main>"
        f"<ul>{row('Ada Lovelace')}{row('Grace Hopper')}</ul>"
        f"<section><h2>Ada Lovelace</h2>{header}"
        f'<ul><li data-view-name="message-list-item"><div role="button">{ARCHIVE}'
        f"</div></li></ul></section></main><script>{MENU_SCRIPT}</script></body>"
    )


def conversations_payload(thread_id: str, categories: list[str]) -> str:
    """The conversations query's answer naming ``thread_id`` in ``categories``."""
    conversation = {
        "_type": "com.linkedin.messenger.Conversation",
        "backendUrn": f"urn:li:messagingThread:{thread_id}",
        "categories": categories,
    }
    return json.dumps({"data": {"messengerConversationsByIds": [conversation]}})


@pytest.fixture
async def dom_page():
    async def answer(route):
        query = parse_qs(urlparse(route.request.url).query)
        if "folder" in query:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=conversations_payload(
                    query["thread"][0], query["folder"][0].split(",")
                ),
            )
            return
        await route.fulfill(status=200, content_type="text/html", body="<p>ok</p>")

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            context = await browser.new_context()
            page = await context.new_page()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {exc}")
        await context.route("**/*", answer)
        try:
            yield page
        finally:
            await browser.close()


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    monkeypatch.setattr(conversations, "_THREAD_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(conversations, "_THREAD_MENU_TIMEOUT", 1.0)
    monkeypatch.setattr(conversations, "NAV_DELAY", 0.0)


async def _no_message_target() -> Any:
    raise AssertionError("archiving never reads a message target")


async def serve(page, html: str, url: str = THREAD_URL) -> None:
    await page.goto(url)
    await page.set_content(html)


async def load_conversation(page, url: str) -> None:
    """A stubbed load: fetch the conversations query for the served thread.

    The folder is the page's own state: `data-folder` when the body names
    one, else archived or inbox by `data-archived`. `data-silent` loads no
    conversation at all.
    """
    state = await page.evaluate(
        """() => ({
            silent: document.body.hasAttribute('data-silent'),
            folder: document.body.dataset.folder ||
                (document.body.hasAttribute('data-archived')
                    ? 'ARCHIVE' : 'INBOX,PRIMARY_INBOX'),
        })"""
    )
    if state["silent"]:
        return
    thread = url.rstrip("/").rsplit("/", 1)[1]
    query = f"{CONVERSATIONS_URL}&thread={thread}&folder={state['folder']}"
    await page.evaluate("(url) => fetch(url).then(response => response.text())", query)


async def archive(page, thread_id: str = "2-abc") -> dict[str, Any]:
    """Archive on the served page the way the workflow does, reloads stubbed."""
    session = PageSession(page)
    reader = ConversationReader(
        session,
        PageNavigator(session),
        PageContentReader(session),
        ProfilePageReader(session, _no_message_target),
    )

    async def navigate(url: str) -> None:
        await load_conversation(page, url)

    with patch.object(reader._navigator, "_navigate_to_page", side_effect=navigate):
        return await reader.archive_conversation(thread_id)


async def chosen(page) -> list[list[str]]:
    return await page.evaluate("() => JSON.parse(document.body.dataset.chosen || '[]')")


async def test_the_thread_is_archived_from_its_own_menu_and_confirmed(dom_page):
    await serve(dom_page, thread_page())

    result = await archive(dom_page)

    assert result == {
        "url": THREAD_URL,
        "thread_id": "2-abc",
        "participant": "Ada Lovelace",
        "status": "archived",
    }
    assert await chosen(dom_page) == [["thread", ARCHIVE]]
    assert await dom_page.locator("body[data-archived]").count() == 1


async def test_a_second_call_clicks_nothing(dom_page):
    await serve(dom_page, thread_page())

    await archive(dom_page)
    again = await archive(dom_page)

    assert again["status"] == "already_archived"
    assert again["participant"] == "Ada Lovelace"
    assert await chosen(dom_page) == [["thread", ARCHIVE]]
    assert await dom_page.locator("body[data-archived]").count() == 1


async def test_an_archived_thread_is_left_alone_though_its_menu_offers_archive(
    dom_page,
):
    """LinkedIn's menu offers Archive on an archived thread too; its data decides."""
    await serve(dom_page, thread_page(archived=True))

    assert (await archive(dom_page))["status"] == "already_archived"
    assert await chosen(dom_page) == []
    assert await dom_page.locator('[role="menu"]').count() == 0


async def test_an_archived_thread_without_an_options_button_is_still_reported(
    dom_page,
):
    await serve(dom_page, thread_page(headers=0, archived=True))

    result = await archive(dom_page)

    assert result["status"] == "already_archived"
    assert result["participant"] is None
    assert await chosen(dom_page) == []


async def test_a_click_that_did_not_hold_is_not_reported_archived(dom_page):
    await serve(dom_page, thread_page(body=" data-refuse"))

    with pytest.raises(
        LinkedInOperationError,
        match=r"after a reload LinkedIn files it under \['INBOX', 'PRIMARY_INBOX'\]",
    ):
        await archive(dom_page)
    assert await chosen(dom_page) == [["thread", ARCHIVE]]


async def test_a_thread_whose_folder_never_loads_is_not_touched(dom_page):
    await serve(dom_page, thread_page(body=" data-silent"))

    with pytest.raises(LinkedInOperationError, match="never named its folder"):
        await archive(dom_page)
    assert await chosen(dom_page) == []
    assert await dom_page.locator('[role="menu"]').count() == 0


@pytest.mark.parametrize("folder", ["SPAM", "ARCHIVE,INBOX"])
async def test_a_thread_in_neither_or_both_folders_is_not_touched(dom_page, folder):
    await serve(dom_page, thread_page(body=f' data-folder="{folder}"'))

    with pytest.raises(LinkedInOperationError, match="exactly one of the inbox"):
        await archive(dom_page)
    assert await chosen(dom_page) == []
    assert await dom_page.locator('[role="menu"]').count() == 0


async def test_the_folder_of_another_thread_is_not_read_as_this_ones(dom_page):
    """Only the conversation naming this thread counts, not one beside it."""
    await serve(dom_page, thread_page(archived=True))
    session = PageSession(dom_page)
    reader = ConversationReader(
        session,
        PageNavigator(session),
        PageContentReader(session),
        ProfilePageReader(session, _no_message_target),
    )

    async def navigate(url: str) -> None:
        await load_conversation(
            dom_page, "https://www.linkedin.com/messaging/thread/2-other/"
        )

    with patch.object(reader._navigator, "_navigate_to_page", side_effect=navigate):
        with pytest.raises(LinkedInOperationError, match="never named its folder"):
            await reader.archive_conversation("2-abc")
    assert await chosen(dom_page) == []


async def test_a_menu_with_neither_item_chooses_nothing_and_says_what_it_held(
    dom_page,
):
    """The refusal quotes the menu, so a live run shows LinkedIn's real labels."""
    await serve(
        dom_page, thread_page(body=" data-pill", items="Mark as unread,Star,Mute")
    )
    menus_open: list[int] = []

    async def trace(page, step, *, extra=None):
        menus_open.append(await page.locator('[role="menu"]').count())

    with patch.object(conversations, "record_page_trace", side_effect=trace):
        with pytest.raises(LinkedInOperationError) as raised:
            await archive(dom_page)

    message = str(raised.value)
    assert "did not offer exactly one 'Archive'" in message
    assert "Nothing was clicked" in message
    assert "1 options button matched" in message
    assert (
        "Its click revealed 3 visible item(s) in div: "
        "'Mark as unread' (div[role=button]), 'Star' (div[role=button]), "
        "'Mute' (div[role=button])."
    ) in message
    assert "aria-expanded=None" in message
    assert menus_open == [1]
    assert await dom_page.locator('[role="menu"]').count() == 0
    assert await chosen(dom_page) == []


async def test_an_archive_control_outside_the_dropdown_is_never_taken(dom_page):
    """Only items beside the button count, so a revealed pill is not the item."""
    await serve(dom_page, thread_page(body=" data-pill", items="Mark as unread"))

    with patch.object(conversations, "record_page_trace", new_callable=AsyncMock):
        with pytest.raises(LinkedInOperationError, match="did not offer"):
            await archive(dom_page)
    assert await chosen(dom_page) == []


async def test_a_dropdown_still_rendering_is_waited_for_not_a_control_elsewhere(
    dom_page,
):
    """An Archive control the click reveals first elsewhere is never the item."""
    await serve(dom_page, thread_page(body=" data-pill data-late"))

    result = await archive(dom_page)

    assert result["status"] == "archived"
    assert await chosen(dom_page) == [["thread", ARCHIVE]]


async def test_a_dropdown_away_from_its_button_is_refused_and_quoted(dom_page):
    await serve(dom_page, thread_page(body=" data-portal"))

    with patch.object(conversations, "record_page_trace", new_callable=AsyncMock):
        with pytest.raises(LinkedInOperationError) as raised:
            await archive(dom_page)

    assert (
        "Nothing appeared beside it; its click revealed elsewhere 3 visible "
        "item(s) in div[role=menu]: 'Mark as unread' (div[role=button]), "
        f"'{ARCHIVE}' (div[role=button]), 'Delete' (div[role=button])."
    ) in str(raised.value)
    assert await chosen(dom_page) == []


async def test_a_menu_offering_archive_twice_chooses_nothing(dom_page):
    await serve(dom_page, thread_page(items=f"{ARCHIVE},{ARCHIVE}"))

    with patch.object(conversations, "record_page_trace", new_callable=AsyncMock):
        with pytest.raises(LinkedInOperationError, match="did not offer exactly one"):
            await archive(dom_page)
    assert await chosen(dom_page) == []


async def test_two_thread_option_buttons_are_not_guessed_at(dom_page):
    await serve(dom_page, thread_page(headers=2))

    with patch.object(conversations, "record_page_trace", new_callable=AsyncMock):
        with pytest.raises(
            LinkedInOperationError, match="2 options button\\(s\\) matched"
        ):
            await archive(dom_page)
    assert await chosen(dom_page) == []


async def test_another_thread_is_never_acted_on(dom_page):
    """LinkedIn sends an unknown id elsewhere; that page's menu is not this one's."""
    await serve(
        dom_page,
        thread_page(),
        url="https://www.linkedin.com/messaging/thread/2-other/",
    )

    with pytest.raises(LinkedInOperationError, match="could not be opened"):
        await archive(dom_page)
    assert await chosen(dom_page) == []
