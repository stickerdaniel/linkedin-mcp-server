# ruff: noqa: F811
"""Comment writes and receipts retain the original post, including reply scopes."""

import pytest

from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.post_actions import (
    CLEAR_EDITOR_JS,
    CLEAR_PREPARED_REPLY_JS,
    COUNT_TEXT_UNITS_JS,
    GUARDED_PIN_AUTHOR_MENTION_JS,
    GUARDED_SELECT_AUTHOR_MENTION_JS,
    OWN_EDITOR_JS,
    PIN_EDITOR_JS,
    SUBMIT_EDITOR_JS,
    PostActions,
)
from linkedin_mcp_server.linkedin.session import PageSession
from test_post_actions_dom import ENGLISH, POST_ID, _pinned, dom_page, plain_post  # noqa: F401
from test_post_receipts_dom import receipt
from test_post_replies_dom import open_reply, setup_reply
from test_post_identity_dom import AUTHOR, mention_picker

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]


async def write_scope(page, reply):
    await page.goto(f"https://www.linkedin.com/feed/update/urn:li:ugcPost:{POST_ID}/")
    if reply:
        post = await setup_reply(page)
        _parent, scope = await open_reply(page, post)
        assert await page.evaluate(CLEAR_PREPARED_REPLY_JS, scope)
    else:
        post = scope = await _pinned(page, plain_post(ENGLISH))
    pinned = await page.evaluate_handle(PIN_EDITOR_JS, {"scope": scope})
    editor = (await pinned.get_property("editor")).as_element()
    assert editor is not None
    return post, scope, editor


async def retarget(post):
    await post.evaluate("post => post.setAttribute('data-urn','urn:li:ugcPost:999')")


@pytest.mark.parametrize("reply", [False, True])
@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("boundary", ["type", "cleanup", "submit", "receipt"])
async def test_write_boundary_requires_original_post(
    dom_page, reply, changed, boundary
):
    post, scope, editor = await write_scope(dom_page, reply)
    text = "Exact approved text"
    if boundary in ("cleanup", "submit"):
        await editor.fill(text)
        await dom_page.evaluate(OWN_EDITOR_JS, {"editor": editor, "text": text})
    if boundary == "receipt":
        assert (
            await dom_page.evaluate(
                COUNT_TEXT_UNITS_JS,
                {"root": scope, "text": text, "captureBaseline": True},
            )
            == 0
        )
        target = dom_page.locator("#reply-block") if reply else scope
        await target.evaluate(
            "(node, html) => node.insertAdjacentHTML('afterend', html)"
            if reply
            else "(node, html) => node.insertAdjacentHTML('beforeend', html)",
            receipt(text),
        )
    if changed:
        await retarget(post)
    if boundary == "type":
        session = PageSession(dom_page)
        result = await PostActions(session, PageNavigator(session))._type_text(
            editor, text
        )
        assert result == ("not_owned" if changed else "typed")
        assert (await editor.inner_text()).strip() == ("" if changed else text)
    elif boundary == "cleanup":
        assert (
            await dom_page.evaluate(CLEAR_EDITOR_JS, {"editor": editor}) is not changed
        )
        assert (await editor.inner_text()).strip() == (text if changed else "")
    elif boundary == "submit":
        result = await dom_page.evaluate(
            SUBMIT_EDITOR_JS, {"scope": scope, "text": text}
        )
        assert (result == "submitted") is not changed
    else:
        count = await dom_page.evaluate(
            COUNT_TEXT_UNITS_JS, {"root": scope, "text": text}
        )
        assert count == (-1 if changed else 1)
    if changed:
        assert await dom_page.get_attribute("body", "data-clicked") is None
        assert await dom_page.get_attribute("body", "data-published") is None


@pytest.mark.parametrize("changed", [False, True])
async def test_automatic_reply_draft_cleanup_keeps_original_post(dom_page, changed):
    post = await setup_reply(dom_page)
    _parent, scope = await open_reply(dom_page, post)
    editor = await scope.query_selector('[contenteditable="true"]')
    if changed:
        await retarget(post)
    assert await dom_page.evaluate(CLEAR_PREPARED_REPLY_JS, scope) is not changed
    assert bool((await editor.inner_text()).strip()) is changed


@pytest.mark.parametrize("reply", [False, True])
async def test_owner_rechecks_original_post_at_final_submit(
    dom_page, monkeypatch, reply
):
    post, scope, _editor = await write_scope(dom_page, reply)
    session = PageSession(dom_page)
    owner = PostActions(session, PageNavigator(session))
    submit = owner._submit_editor

    async def retarget_then_submit(scope, text):
        await retarget(post)
        return await submit(scope, text)

    monkeypatch.setattr(owner, "_submit_editor", retarget_then_submit)
    result = await owner._write_and_submit(
        scope,
        dom_page.url,
        "Approved text",
        success_status="commented",
        unconfirmed_status="comment_unconfirmed",
        noun="comment",
    )
    assert result["acted"] is False
    assert result["retry_safe"] is True
    assert await dom_page.get_attribute("body", "data-clicked") is None
    assert await dom_page.get_attribute("body", "data-published") is None


@pytest.mark.parametrize("reply", [False, True])
async def test_retarget_before_blank_line_does_not_insert_newline(dom_page, reply):
    post, _scope, editor = await write_scope(dom_page, reply)
    await post.evaluate("""post => {
      const scope = post.closest('[data-component-type=LazyColumn]') || post;
      scope.addEventListener('input', event => {
        if(event.target.innerText === 'First') post.setAttribute('data-urn','urn:li:ugcPost:999');
      });
    }""")
    session = PageSession(dom_page)
    result = await PostActions(session, PageNavigator(session))._type_text(
        editor, "First\n"
    )
    assert result == "not_owned"
    assert await editor.inner_text() == "First"


@pytest.mark.parametrize("boundary", ["query", "selection", "pin"])
async def test_mention_draft_mutations_require_original_post(
    dom_page, monkeypatch, boundary
):
    post, _scope, editor = await write_scope(dom_page, False)
    await mention_picker(dom_page, editor)
    session = PageSession(dom_page)
    original = PageSession.run_on_linkedin

    async def intercept(self, program, *args, **kwargs):
        if (
            boundary == "selection" and program == GUARDED_SELECT_AUTHOR_MENTION_JS
        ) or (boundary == "pin" and program == GUARDED_PIN_AUTHOR_MENTION_JS):
            await retarget(post)
        return await original(self, program, *args, **kwargs)

    monkeypatch.setattr(PageSession, "run_on_linkedin", intercept)
    if boundary == "query":
        await post.evaluate("""post => post.querySelector('[contenteditable=true]').addEventListener(
          'click', () => post.setAttribute('data-urn','urn:li:ugcPost:999'), {once:true})""")
    result = await PostActions(session, PageNavigator(session))._type_text(
        editor, "Approved text", author=AUTHOR
    )
    assert result != "typed"
    assert "Approved text" not in await editor.inner_text()
    if boundary == "query":
        assert await editor.inner_text() == ""
    if boundary == "selection":
        assert await editor.inner_text() == "@Post Author"
        assert len(await editor.query_selector_all("[data-type=mention]")) == 0
