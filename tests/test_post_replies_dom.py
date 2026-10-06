# ruff: noqa: F811
"""Real Chromium proof of exact-parent reply ownership; all traffic is synthetic."""

from unittest.mock import AsyncMock

import pytest

from linkedin_mcp_server.linkedin.post_actions import (
    PostActions,
    COUNT_TEXT_UNITS_JS,
    OWN_EDITOR_JS,
    SUBMIT_EDITOR_JS,
)
from linkedin_mcp_server.linkedin.post_comments import (
    READ_COMMENTS_JS,
    PIN_PARENT_COMMENT_JS,
    OPEN_REPLY_EDITOR_JS,
    PIN_REPLY_CONTEXT_JS,
    CLEAR_PREPARED_REPLY_JS,
)
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from test_post_actions_dom import dom_page  # noqa: F401 -- shared real Chromium fixture
from test_post_actions_dom import _pinned, ENGLISH, POST_ID, sdui_post
from test_post_identity_dom import avatar, ACTOR

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]
PARENT = f"urn:li:comment:(activity:{POST_ID},12345)"
OTHER = f"urn:li:comment:(activity:{POST_ID},123456)"
AUTHOR = {"path": "/in/commenter", "name": "Comment Member", "avatar": "AUTHOR"}


def comment(urn, *, body="Parent body", name="Comment Member"):
    return f"""<div id="replaceableComment_{urn}" componentkey="replaceableComment_{urn}">
      <div componentkey="CommentComponentReference_{urn}">
        <a href="/in/commenter/">{avatar("AUTHOR").replace("<img ", f'<img alt="View {name}&#39;s profile" ')}</a>
        <a href="/in/commenter/"><p><span>{name}  You</span><span aria-hidden="true">{name}<span> &#8226; You</span></span></p><p>Biography, not the body</p></a>
        <p data-testid="expandable-text-box">{body}</p>
        <button type="button" aria-label="Reply">Reply</button>
      </div></div>"""


async def setup_reply(page):
    await page.goto(f"https://www.linkedin.com/feed/update/urn:li:ugcPost:{POST_ID}/")
    root = await _pinned(
        page,
        f'<div data-component-type="LazyColumn" data-testid="commentList">{sdui_post(ENGLISH)}<div id="parent-block">{comment(PARENT)}</div><div id="blank"></div><div id="boundary">{comment(OTHER, body="Other body")}</div></div>',
    )
    await page.evaluate(
        """html => {
      const parent = document.querySelector('#parent-block');
      parent.querySelector('button[aria-label=Reply]').onclick = () => {
        if (document.querySelector('#reply-block')) return;
        document.querySelector('#boundary').insertAdjacentHTML('beforebegin', html);
        const reply = document.querySelector('#reply-block');
        reply.querySelector('button').onclick = () => {
          document.body.setAttribute('data-published', 'yes');
          const text = reply.querySelector('[contenteditable=true]').innerText;
          const receipt = document.createElement('div');
          const urn = 'urn:li:comment:(activity:7506667649444237313,998)';
          receipt.id = 'replaceableComment_' + urn;
          receipt.innerHTML = '<div componentkey="CommentComponentReference_' + urn + '">'
            + '<a href="/in/actor/"><img src="https://media.licdn.com/dms/image/v2/PERSON/profile-displayphoto/x">Actor</a>'
            + '<p data-testid="expandable-text-box" data-receipt></p></div>';
          receipt.querySelector('[data-receipt]').innerText = text;
          reply.after(receipt); reply.remove();
        };
      };
    }""",
        f'<div id="reply-block">{avatar("PERSON")}<div role="textbox" contenteditable="true"><span data-type="mention" contenteditable="false"><strong>Comment Member</strong></span>&nbsp;</div><button type="button">Reply</button></div>',
    )
    return root


async def open_reply(page, root):
    parent = await page.evaluate_handle(
        PIN_PARENT_COMMENT_JS, {"post": root, "reference": PARENT}
    )
    assert parent.as_element() is not None
    assert await page.evaluate(OPEN_REPLY_EDITOR_JS, parent) == "opened"
    scope = await page.evaluate_handle(PIN_REPLY_CONTEXT_JS, parent)
    return parent, scope


async def owner_for(page, root, monkeypatch):
    owner = PostActions(PageSession(page), PageNavigator(PageSession(page)))
    monkeypatch.setattr(PostActions, "_resolve_actor", AsyncMock(return_value=ACTOR))
    monkeypatch.setattr(
        PostActions, "_open_post", AsyncMock(return_value=(page.url, POST_ID, {}))
    )
    monkeypatch.setattr(
        PostActions, "_pin_root", AsyncMock(return_value=root.as_element())
    )
    monkeypatch.setattr(
        PostActions,
        "_select_actor",
        AsyncMock(side_effect=lambda pinned, actor: pinned),
    )
    return owner


async def test_discovery_exact_urn_body_and_author_without_biography(dom_page):
    root = await setup_reply(dom_page)
    rows = await dom_page.evaluate(READ_COMMENTS_JS, {"post": root, "limit": 1})
    assert rows == [
        {
            "reference": PARENT,
            "text": "Parent body",
            "author": {"url": "/in/commenter", "name": "Comment Member"},
            "parent_reference": None,
            "parent_relationship": "not_exposed",
        }
    ]
    assert (
        await dom_page.evaluate(READ_COMMENTS_JS, {"post": root, "limit": 20}) != rows
    )


@pytest.mark.parametrize("change", ["missing", "duplicate", "substring"])
async def test_exact_parent_refuses_missing_duplicate_or_substring(dom_page, change):
    root = await setup_reply(dom_page)
    if change == "duplicate":
        await dom_page.evaluate(
            "document.querySelector('#parent-block').after(document.querySelector('#parent-block').cloneNode(true))"
        )
    else:
        await dom_page.evaluate("document.querySelector('#parent-block').remove()")
    target = PARENT if change != "missing" else PARENT.replace("12345)", "999)")
    handle = await dom_page.evaluate_handle(
        PIN_PARENT_COMMENT_JS, {"post": root, "reference": target}
    )
    assert handle.as_element() is None
    assert await dom_page.get_attribute("body", "data-published") is None


@pytest.mark.parametrize(
    "change",
    [
        "actor",
        "mention",
        "multiple",
        "intervening",
        "parent_id",
        "parent_body",
        "route",
        "draft",
    ],
)
async def test_reply_ownership_refuses_changed_identity_or_editor(dom_page, change):
    root = await setup_reply(dom_page)
    parent = await dom_page.evaluate_handle(
        PIN_PARENT_COMMENT_JS, {"post": root, "reference": PARENT}
    )
    if change == "draft":
        await dom_page.evaluate(
            "document.querySelector('#parent-block').insertAdjacentHTML('beforeend', '<div role=textbox contenteditable=true>Existing draft</div>')"
        )
        assert await dom_page.evaluate(OPEN_REPLY_EDITOR_JS, parent) == "draft_present"
        return
    assert await dom_page.evaluate(OPEN_REPLY_EDITOR_JS, parent) == "opened"
    await dom_page.evaluate(
        """change => {
      const reply = document.querySelector('#reply-block');
      if (change === 'actor') reply.querySelector('img').src = 'https://media.licdn.com/dms/image/v2/WRONG/company-logo/x';
      if (change === 'mention') reply.querySelector('strong').innerText = 'Someone Else';
      if (change === 'multiple') reply.after(reply.cloneNode(true));
      if (change === 'intervening') document.querySelector('#blank').append(document.querySelector('#boundary').firstElementChild.cloneNode(true));
      if (change === 'parent_id') document.querySelector('#parent-block').firstElementChild.id += 'x';
      if (change === 'parent_body') {const body = document.querySelector('#parent-block [componentkey^="CommentComponentReference_"]'); body.replaceWith(body.cloneNode(true));}
      if (change === 'route') history.pushState({}, '', '/feed/update/urn:li:activity:999/');
    }""",
        change,
    )
    handle = await dom_page.evaluate_handle(PIN_REPLY_CONTEXT_JS, parent)
    assert handle.as_element() is None
    assert await dom_page.get_attribute("body", "data-published") is None


@pytest.mark.parametrize(
    "change", ["actor", "parent_id", "parent_body", "intervening", "boundary", "route"]
)
async def test_dispatch_rechecks_parent_range_and_local_actor(dom_page, change):
    root = await setup_reply(dom_page)
    parent, scope = await open_reply(dom_page, root)
    assert scope.as_element() is not None
    assert await dom_page.evaluate(CLEAR_PREPARED_REPLY_JS, scope)
    editor = await scope.query_selector("[contenteditable=true]")
    await editor.fill("Reply body")
    await dom_page.evaluate(OWN_EDITOR_JS, {"editor": editor, "text": "Reply body"})
    await dom_page.evaluate(
        """change => {
      if (change === 'actor') document.querySelector('#reply-block img').src = 'https://media.licdn.com/dms/image/v2/WRONG/company-logo/x';
      if (change === 'parent_id') document.querySelector('#parent-block').firstElementChild.id += 'x';
      if (change === 'parent_body') document.querySelector('#parent-block [componentkey^="CommentComponentReference_"]').remove();
      if (change === 'intervening') document.querySelector('#blank').append(document.querySelector('#boundary').firstElementChild.cloneNode(true));
      if (change === 'boundary') document.querySelector('#boundary').remove();
      if (change === 'route') history.pushState({}, '', '/feed/update/urn:li:activity:999/');
    }""",
        change,
    )
    assert (
        await dom_page.evaluate(
            SUBMIT_EDITOR_JS, {"scope": scope, "text": "Reply body"}
        )
        != "submitted"
    )
    assert await dom_page.get_attribute("body", "data-published") is None


async def test_confirmation_excludes_other_parent_and_draft(dom_page):
    root = await setup_reply(dom_page)
    parent, scope = await open_reply(dom_page, root)
    assert await dom_page.evaluate(CLEAR_PREPARED_REPLY_JS, scope)
    editor = await scope.query_selector("[contenteditable=true]")
    await editor.fill("Exact reply")
    await dom_page.evaluate(
        "document.querySelector('#boundary [data-testid=expandable-text-box]').innerText='Exact reply'"
    )
    assert (
        await dom_page.evaluate(
            COUNT_TEXT_UNITS_JS, {"root": scope, "text": "Exact reply"}
        )
        == 0
    )
    await dom_page.evaluate(OWN_EDITOR_JS, {"editor": editor, "text": "Exact reply"})
    assert (
        await dom_page.evaluate(
            SUBMIT_EDITOR_JS, {"scope": scope, "text": "Exact reply"}
        )
        == "submitted"
    )
    assert (
        await dom_page.evaluate(
            COUNT_TEXT_UNITS_JS, {"root": scope, "text": "Exact reply"}
        )
        == 1
    )
    await dom_page.evaluate("document.querySelector('#boundary').remove()")
    assert (
        await dom_page.evaluate(
            COUNT_TEXT_UNITS_JS, {"root": scope, "text": "Exact reply"}
        )
        == -1
    )


@pytest.mark.parametrize("mention", [False, True])
async def test_owner_publishes_one_bound_reply_and_preserves_requested_mention(
    dom_page, monkeypatch, mention
):
    root = await setup_reply(dom_page)
    owner = await owner_for(dom_page, root, monkeypatch)
    result = await owner.reply_to_comment(
        dom_page.url,
        PARENT,
        "Useful detail\nThank you.",
        actor="/in/actor/",
        confirm_reply=True,
        mention_parent_author=mention,
    )
    assert result["status"] == "replied"
    assert result["acted"] is True and result["retry_safe"] is False
    assert result["parent_comment_reference"] == PARENT
    assert (
        await dom_page.locator("[data-receipt]").inner_text()
        == ("Comment Member\xa0" if mention else "") + "Useful detail\nThank you."
    )
    assert await dom_page.locator("[data-receipt]").count() == 1


async def test_owner_false_confirmation_never_types_or_submits(dom_page, monkeypatch):
    root = await setup_reply(dom_page)
    owner = await owner_for(dom_page, root, monkeypatch)
    result = await owner.reply_to_comment(
        dom_page.url, PARENT, "Do not publish", actor="/in/actor/"
    )
    assert result["status"] == "confirmation_required"
    assert result["retry_safe"] is True and result["acted"] is False
    assert (
        await dom_page.locator("#reply-block [contenteditable=true]").inner_text()
        == "Comment Member\xa0"
    )
    assert await dom_page.get_attribute("body", "data-published") is None


async def test_clearing_automatic_mention_refuses_changed_draft(dom_page):
    root = await setup_reply(dom_page)
    parent, scope = await open_reply(dom_page, root)
    await dom_page.evaluate(
        "document.querySelector('#reply-block [contenteditable=true]').append('Another draft')"
    )
    assert not await dom_page.evaluate(CLEAR_PREPARED_REPLY_JS, scope)
    assert (
        "Another draft"
        in await dom_page.locator("#reply-block [contenteditable=true]").inner_text()
    )


async def test_missing_parent_hydrates_without_retrying_reply_action(
    dom_page, monkeypatch
):
    root = await setup_reply(dom_page)
    owner = await owner_for(dom_page, root, monkeypatch)
    await dom_page.evaluate("""() => {
      const parent = document.querySelector('#parent-block');
      const column = parent.parentElement;
      parent.remove();
      setTimeout(() => column.insertBefore(parent, document.querySelector('#blank')), 600);
    }""")
    result = await owner.reply_to_comment(
        dom_page.url, PARENT, "Bounded wait", actor="/in/actor/"
    )
    assert result["status"] == "confirmation_required"
    assert await dom_page.locator("#reply-block").count() == 1
    assert await dom_page.get_attribute("body", "data-published") is None


async def test_submit_response_loss_is_not_retry_safe(dom_page, monkeypatch):
    root = await setup_reply(dom_page)
    owner = await owner_for(dom_page, root, monkeypatch)
    original = PostActions._submit_editor

    async def lost_response(self, scope, text):
        assert await original(self, scope, text) == "submitted"
        raise RuntimeError("Response lost after browser dispatch")

    monkeypatch.setattr(PostActions, "_submit_editor", lost_response)
    result = await owner.reply_to_comment(
        dom_page.url, PARENT, "One attempt only", actor="/in/actor/", confirm_reply=True
    )
    assert result["status"] == "reply_unconfirmed"
    assert result["retry_safe"] is False
    assert await dom_page.locator("[data-receipt]").count() == 1


async def test_preserved_parent_mention_loss_refuses_submission(dom_page, monkeypatch):
    root = await setup_reply(dom_page)
    owner = await owner_for(dom_page, root, monkeypatch)
    original = PostActions._submit_editor

    async def lose_mention(self, scope, text):
        await scope.evaluate("""scope => {
          const token = scope.querySelector('[data-type=mention]');
          token.replaceWith(document.createTextNode(token.innerText));
        }""")
        return await original(self, scope, text)

    monkeypatch.setattr(PostActions, "_submit_editor", lose_mention)
    result = await owner.reply_to_comment(
        dom_page.url,
        PARENT,
        "Keep token",
        actor="/in/actor/",
        confirm_reply=True,
        mention_parent_author=True,
    )
    assert result["status"] == "submit_unavailable"
    assert await dom_page.get_attribute("body", "data-published") is None


async def test_offsite_parent_pin_never_opens_reply(dom_page):
    await setup_reply(dom_page)
    await dom_page.goto("https://example.org/")
    handle = await dom_page.evaluate_handle(
        PIN_PARENT_COMMENT_JS, {"post": None, "reference": PARENT}
    )
    assert handle.as_element() is None


async def test_discovery_reports_only_rendered_nested_relationship(dom_page):
    root = await setup_reply(dom_page)
    nested = f"urn:li:comment:(activity:{POST_ID},98765)"
    await dom_page.evaluate(
        "html => document.querySelector('#parent-block').firstElementChild.insertAdjacentHTML('beforeend', html)",
        comment(nested, body="Nested response"),
    )
    rows = await dom_page.evaluate(READ_COMMENTS_JS, {"post": root, "limit": 20})
    response = next(row for row in rows if row["reference"] == nested)
    assert response["text"] == "Nested response"
    assert response["parent_reference"] == PARENT
    assert response["parent_relationship"] == "rendered_ancestor"
    original = next(row for row in rows if row["reference"] == PARENT)
    assert original["text"] == "Parent body"


async def test_exact_nested_parent_reply_opens_only_its_control(dom_page):
    root = await setup_reply(dom_page)
    nested = f"urn:li:comment:(activity:{POST_ID},98765)"
    await dom_page.evaluate(
        "html => document.querySelector('#parent-block').firstElementChild.insertAdjacentHTML('beforeend', html)",
        comment(nested, body="Nested response"),
    )
    await dom_page.evaluate("""() => {
      const buttons = document.querySelector('#parent-block').querySelectorAll('button');
      const open = buttons[0].onclick;
      buttons[0].onclick = () => document.body.setAttribute('data-wrong-parent', 'yes');
      buttons[1].onclick = open;
    }""")
    parent = await dom_page.evaluate_handle(
        PIN_PARENT_COMMENT_JS, {"post": root, "reference": nested}
    )
    assert parent.as_element() is not None
    assert await dom_page.evaluate(OPEN_REPLY_EDITOR_JS, parent) == "opened"
    scope = await dom_page.evaluate_handle(PIN_REPLY_CONTEXT_JS, parent)
    assert scope.as_element() is not None
    assert await dom_page.get_attribute("body", "data-wrong-parent") is None


async def test_discovery_owner_exposes_exact_reference_value_and_post_url(
    dom_page, monkeypatch
):
    root = await setup_reply(dom_page)
    owner = await owner_for(dom_page, root, monkeypatch)
    result = await owner.get_post_comments(dom_page.url, max_comments=1)
    assert result["sections"] == {"comments": "Parent body"}
    assert result["references"]["comments"][0]["value"] == PARENT
    assert result["references"]["comments"][0]["kind"] == "comment"
    assert (
        result["references"]["comments"][0]["url"]
        == f"/feed/update/urn:li:ugcPost:{POST_ID}/"
    )
    assert result["coverage"] == "currently rendered comments only"


async def test_company_author_name_trims_only_first_line_whitespace(dom_page):
    root = await setup_reply(dom_page)
    await dom_page.evaluate(r"""() => {
      const body = document.querySelector('#parent-block [componentkey^="CommentComponentReference_"]');
      const links = body.querySelectorAll('a');
      for (const link of links) link.href = '/company/example/posts';
      links[0].querySelector('img').alt = '';
      links[1].style.whiteSpace = 'pre-wrap';
      links[1].innerText = 'Example Company   \n100 followers';
    }""")
    rows = await dom_page.evaluate(READ_COMMENTS_JS, {"post": root, "limit": 1})
    assert rows[0]["author"] == {"url": "/company/example", "name": "Example Company"}


async def test_company_avatar_label_binds_undecorated_parent_name(dom_page):
    root = await setup_reply(dom_page)
    await dom_page.evaluate("""() => {
      const links = document.querySelector('#parent-block').querySelectorAll('a');
      for (const link of links) link.href = '/company/example/posts';
      links[0].querySelector('img').alt = 'View company: Example Company ';
      links[1].innerHTML = '<p>Example Company <span>Author</span></p><p>100 followers</p>';
    }""")
    rows = await dom_page.evaluate(READ_COMMENTS_JS, {"post": root, "limit": 1})
    assert rows[0]["author"] == {"url": "/company/example", "name": "Example Company"}
