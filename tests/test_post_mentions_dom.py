# ruff: noqa: F811
"""Rich mention text appends before the editor's trailing layout break."""

import pytest

from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.post_actions import (
    CHECK_MENTION_PREFIX_JS,
    PIN_EDITOR_JS,
    PostActions,
)
from linkedin_mcp_server.linkedin.post_mentions import PIN_AUTHOR_MENTION_JS
from linkedin_mcp_server.linkedin.session import PageSession
from test_post_actions_dom import dom_page  # noqa: F401
from test_post_identity_dom import AUTHOR, author_post, mention_picker

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]


async def trailing_mention(page):
    root = await author_post(page)
    editor = await root.query_selector('[role="textbox"]')
    await mention_picker(page, editor)
    await page.evaluate_handle(PIN_EDITOR_JS, {"scope": root})
    await page.evaluate("""() => {
      document.querySelector('[role=option] [role=button]').onclick = () => {
        document.querySelector('[contenteditable=true]').innerHTML =
          '<p><span data-type="mention" contenteditable="false"><strong>Post Author</strong></span><img alt=""><br></p>';
        document.querySelector('[role=listbox]').remove();
      };
    }""")
    return root, editor


async def test_owner_appends_after_rich_mention_before_trailing_break(dom_page):
    root, editor = await trailing_mention(dom_page)
    session = PageSession(dom_page)
    owner = PostActions(session, PageNavigator(session))
    assert (
        await owner._type_text(editor, "Useful insight\nThank you", author=AUTHOR)
        == "typed"
    )
    assert (await editor.inner_text()).replace(
        "\u00a0", " "
    ).strip() == "Post Author Useful insight\nThank you"
    assert len(await editor.query_selector_all('[data-type="mention"]')) == 1
    assert (
        await root.evaluate("root => root.__linkedinMcpPost.actor.path") == "/in/actor/"
    )
    assert await dom_page.get_attribute("body", "data-clicked") is None


@pytest.mark.parametrize("change", ["actor", "token", "draft", "route", "late_draft"])
async def test_trailing_break_does_not_relax_mention_or_draft_guards(
    dom_page, monkeypatch, change
):
    root, editor = await trailing_mention(dom_page)
    original = PageSession.run_on_linkedin

    async def intercept(self, program, *args, **kwargs):
        if program == CHECK_MENTION_PREFIX_JS and change == "late_draft":
            await editor.evaluate(
                "editor => editor.insertAdjacentText('beforeend', 'Human draft')"
            )
        result = await original(self, program, *args, **kwargs)
        if program == PIN_AUTHOR_MENTION_JS and result:
            await root.evaluate(
                """(root, change) => {
              const editor = root.querySelector('[contenteditable=true]');
              if (change === 'actor') root.__linkedinMcpPost.actorControl.querySelector('img').src='https://media.licdn.com/dms/image/v2/OTHER/profile-displayphoto/x';
              if (change === 'token') editor.querySelector('[data-type=mention]').setAttribute('data-type','plain');
              if (change === 'draft') editor.insertAdjacentText('beforeend','Human draft');
              if (change === 'route') history.pushState({}, '', '/feed/update/urn:li:activity:999/');
            }""",
                change,
            )
        return result

    monkeypatch.setattr(PageSession, "run_on_linkedin", intercept)
    session = PageSession(dom_page)
    result = await PostActions(session, PageNavigator(session))._type_text(
        editor, "Useful insight", author=AUTHOR
    )
    assert result != "typed"
    assert "Useful insight" not in await editor.inner_text()
    if change in ("draft", "late_draft"):
        assert "Human draft" in await editor.inner_text()
    assert await dom_page.get_attribute("body", "data-clicked") is None
