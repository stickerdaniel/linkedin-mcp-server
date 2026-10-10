# ruff: noqa: F811
"""Publication receipts require a new rendered comment by the requested actor."""

import pytest

from linkedin_mcp_server.linkedin.post_actions import (
    COUNT_TEXT_UNITS_JS,
    CLEAR_EDITOR_JS,
    PostActions,
)
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from test_post_actions_dom import POST_ID, ENGLISH, _pinned, plain_post, dom_page  # noqa: F401
from test_post_replies_dom import setup_reply, open_reply
from test_post_actions_dom import _typed

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]


@pytest.mark.parametrize(
    "change", ["none", "actor", "route", "root", "toggle", "identity"]
)
async def test_reaction_confirmation_preserves_dispatch_identity(
    dom_page, monkeypatch, change
):
    from linkedin_mcp_server.linkedin import post_actions

    await dom_page.goto(
        f"https://www.linkedin.com/feed/update/urn:li:ugcPost:{POST_ID}/"
    )
    root = await _pinned(dom_page, plain_post(ENGLISH))
    await root.evaluate(
        """(root, change) => {
      const pin = root.__linkedinMcpPost;
      pin.toggle.onclick = () => {
        pin.toggle.setAttribute('aria-pressed', 'true');
        document.body.setAttribute('data-dispatch-count', '1');
        if (change === 'actor') pin.actorControl.querySelector('img').src = 'https://media.licdn.com/dms/image/v2/OTHER/profile-displayphoto/x';
        if (change === 'route') history.pushState({}, '', '/feed/');
        if (change === 'root') root.replaceWith(root.cloneNode(true));
        if (change === 'toggle') pin.toggle.replaceWith(pin.toggle.cloneNode(true));
        if (change === 'identity') root.setAttribute('data-urn', 'urn:li:ugcPost:999');
      };
    }""",
        change,
    )
    monkeypatch.setattr(post_actions, "_CONFIRM_TIMEOUT", 10)
    monkeypatch.setattr(post_actions, "_CONFIRM_POLL", 0.001)
    session = PageSession(dom_page)
    result = await PostActions(session, PageNavigator(session))._react_default(
        root, dom_page.url, POST_ID, "like"
    )
    assert result["status"] == ("reacted" if change == "none" else "react_unconfirmed")
    assert result["acted"] is (change == "none")
    assert result["retry_safe"] is False
    assert await dom_page.get_attribute("body", "data-dispatch-count") == "1"


@pytest.mark.parametrize("change", ["text", "actor"])
async def test_cleanup_preserves_changed_text_or_identity(dom_page, change):
    root, editor = await _typed(dom_page, plain_post(ENGLISH), "Server draft")
    if change == "text":
        await editor.fill("Human draft")
    else:
        await root.evaluate("root => root.__linkedinMcpPost.actorControl.remove()")
    expected = await editor.inner_text()
    assert await dom_page.evaluate(CLEAR_EDITOR_JS, {"editor": editor}) is False
    assert await editor.inner_text() == expected


def receipt(text, *, member="actor", reference=None):
    urn = reference or f"urn:li:comment:(activity:{POST_ID},98765)"
    return (
        f'<div id="replaceableComment_{urn}" componentkey="replaceableComment_{urn}">'
        f'<div componentkey="CommentComponentReference_{urn}">'
        f'<a href="/in/{member}/"><img src="https://media.licdn.com/dms/image/v2/PERSON/profile-displayphoto/x">Actor</a>'
        f'<p data-testid="expandable-text-box">{text}</p></div></div>'
    )


@pytest.mark.parametrize("reply", [False, True])
@pytest.mark.parametrize(
    "variant", ["unattributed", "other_actor", "invalid_reference", "draft"]
)
async def test_matching_text_without_an_owned_receipt_never_confirms(
    dom_page, reply, variant
):
    if reply:
        root = await setup_reply(dom_page)
        _parent, scope = await open_reply(dom_page, root)
    else:
        scope = await _pinned(dom_page, plain_post(ENGLISH))
    html = {
        "unattributed": "<div><p>Exact submitted text</p></div>",
        "other_actor": receipt("Exact submitted text", member="someone-else"),
        "invalid_reference": receipt(
            "Exact submitted text", reference="urn:li:comment:not-an-id"
        ),
        "draft": '<div contenteditable="true">'
        + receipt("Exact submitted text")
        + "</div>",
    }[variant]
    if reply:
        await dom_page.locator("#reply-block").evaluate(
            "(node, html) => node.insertAdjacentHTML('afterend', html)", html
        )
    else:
        await scope.evaluate(
            "(node, html) => node.insertAdjacentHTML('beforeend', html)", html
        )
    assert (
        await dom_page.evaluate(
            COUNT_TEXT_UNITS_JS, {"root": scope, "text": "Exact submitted text"}
        )
        == 0
    )


@pytest.mark.parametrize("reply", [False, True])
async def test_a_new_exact_actor_receipt_confirms_and_an_existing_urn_cannot(
    dom_page, reply
):
    if reply:
        root = await setup_reply(dom_page)
        _parent, scope = await open_reply(dom_page, root)
        target = dom_page.locator("#reply-block")
        insert = "(node, html) => node.insertAdjacentHTML('afterend', html)"
    else:
        scope = await _pinned(dom_page, plain_post(ENGLISH))
        target = scope
        insert = "(node, html) => node.insertAdjacentHTML('beforeend', html)"
    await target.evaluate(insert, receipt("Earlier body"))
    assert (
        await dom_page.evaluate(
            COUNT_TEXT_UNITS_JS,
            {"root": scope, "text": "Exact submitted text", "captureBaseline": True},
        )
        == 0
    )
    await dom_page.locator(
        f'[componentkey="CommentComponentReference_urn:li:comment:(activity:{POST_ID},98765)"] [data-testid="expandable-text-box"]'
    ).evaluate("node => node.innerText = 'Exact submitted text'")
    assert (
        await dom_page.evaluate(
            COUNT_TEXT_UNITS_JS, {"root": scope, "text": "Exact submitted text"}
        )
        == 0
    )
    await target.evaluate(
        insert,
        receipt(
            "Exact submitted text",
            reference=f"urn:li:comment:(activity:{POST_ID},98766)",
        ),
    )
    assert (
        await dom_page.evaluate(
            COUNT_TEXT_UNITS_JS, {"root": scope, "text": "Exact submitted text"}
        )
        == 1
    )
