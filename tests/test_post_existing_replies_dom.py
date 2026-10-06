# ruff: noqa: F811
"""Existing flat reply rows are unchanged context, never inferred parent links."""

import pytest

from linkedin_mcp_server.linkedin.post_actions import (
    COUNT_TEXT_UNITS_JS,
    OWN_EDITOR_JS,
    SUBMIT_EDITOR_JS,
)
from linkedin_mcp_server.linkedin.post_comments import (
    CLEAR_PREPARED_REPLY_JS,
    OPEN_REPLY_EDITOR_JS,
    PIN_PARENT_COMMENT_JS,
    PIN_REPLY_CONTEXT_JS,
)
from test_post_actions_dom import dom_page  # noqa: F401
from test_post_replies_dom import PARENT, POST_ID, comment, setup_reply

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]
OLD = f"urn:li:comment:(activity:{POST_ID},778899)"


async def existing_reply(page, *, existing_container=True):
    root = await setup_reply(page)
    await page.locator("#blank").evaluate(
        "(node, html) => node.insertAdjacentHTML('beforebegin', html)",
        f'<div id="old-reply">{comment(OLD, body="Existing reply")}</div>',
    )
    if existing_container:
        await page.evaluate("""() => {
          const button = document.querySelector('#parent-block button');
          const open = button.onclick;
          const slot = document.createElement('div'); slot.id = 'reply-slot';
          document.querySelector('#boundary').before(slot);
          button.onclick = () => {
            open();
            const block = document.querySelector('#reply-block');
            slot.append(...block.childNodes); block.remove(); slot.id = 'reply-block';
          };
        }""")
    parent = await page.evaluate_handle(
        PIN_PARENT_COMMENT_JS, {"post": root, "reference": PARENT}
    )
    assert parent.as_element() is not None
    assert await page.evaluate(OPEN_REPLY_EDITOR_JS, parent) == "opened"
    return parent


@pytest.mark.parametrize("existing_container", [True, False])
async def test_unchanged_existing_reply_allows_exact_parent_submission(
    dom_page, existing_container
):
    parent = await existing_reply(dom_page, existing_container=existing_container)
    scope = await dom_page.evaluate_handle(PIN_REPLY_CONTEXT_JS, parent)
    assert scope.as_element() is not None
    assert await dom_page.evaluate(CLEAR_PREPARED_REPLY_JS, scope)
    assert (
        await dom_page.evaluate(
            COUNT_TEXT_UNITS_JS,
            {"root": scope, "text": "Existing reply", "captureBaseline": True},
        )
        == 0
    )
    assert (
        await dom_page.evaluate(
            COUNT_TEXT_UNITS_JS, {"root": scope, "text": "Existing reply"}
        )
        == 0
    )
    editor = await scope.query_selector('[contenteditable="true"]')
    await editor.fill("Another reply")
    await dom_page.evaluate(OWN_EDITOR_JS, {"editor": editor, "text": "Another reply"})
    await scope.evaluate("""node => {
      node.querySelector('button').onclick = () => document.body.dataset.submitted='yes';
    }""")
    assert (
        await dom_page.evaluate(
            SUBMIT_EDITOR_JS, {"scope": scope, "text": "Another reply"}
        )
        == "submitted"
    )
    assert await dom_page.get_attribute("body", "data-submitted") == "yes"


CHANGES = [
    "added",
    "removed",
    "component_removed",
    "replaced",
    "reordered",
    "urn",
    "component_key",
    "body_node",
    "body_text",
    "author",
    "avatar",
]


async def change_existing(page, change):
    await page.evaluate(
        """change => {
      const row = document.querySelector('#old-reply');
      const node = row.firstElementChild;
      const body = node.firstElementChild;
      if (change === 'added') row.after(row.cloneNode(true));
      if (change === 'removed') row.remove();
      if (change === 'component_removed') node.remove();
      if (change === 'replaced') node.replaceWith(node.cloneNode(true));
      if (change === 'reordered') document.querySelector('#reply-block').after(row);
      if (change === 'urn') node.id += '0';
      if (change === 'component_key') node.setAttribute('componentkey', node.id + '0');
      if (change === 'body_node') body.replaceWith(body.cloneNode(true));
      if (change === 'body_text') body.querySelector('[data-testid=expandable-text-box]').innerText='Changed';
      if (change === 'author') body.querySelector('a').href='/in/different-author/';
      if (change === 'avatar') body.querySelector('img').src='https://media.licdn.com/dms/image/v2/OTHER/profile-displayphoto/x';
    }""",
        change,
    )


@pytest.mark.parametrize("phase", ["pin", "dispatch"])
@pytest.mark.parametrize("change", CHANGES)
async def test_existing_reply_changes_refuse_before_publication(
    dom_page, phase, change
):
    parent = await existing_reply(dom_page)
    if phase == "pin":
        await change_existing(dom_page, change)
        scope = await dom_page.evaluate_handle(PIN_REPLY_CONTEXT_JS, parent)
        assert scope.as_element() is None
    else:
        scope = await dom_page.evaluate_handle(PIN_REPLY_CONTEXT_JS, parent)
        assert scope.as_element() is not None
        assert await dom_page.evaluate(CLEAR_PREPARED_REPLY_JS, scope)
        editor = await scope.query_selector('[contenteditable="true"]')
        await editor.fill("Another reply")
        await dom_page.evaluate(
            OWN_EDITOR_JS, {"editor": editor, "text": "Another reply"}
        )
        await change_existing(dom_page, change)
        assert (
            await dom_page.evaluate(
                SUBMIT_EDITOR_JS, {"scope": scope, "text": "Another reply"}
            )
            != "submitted"
        )
    assert await dom_page.get_attribute("body", "data-published") is None
