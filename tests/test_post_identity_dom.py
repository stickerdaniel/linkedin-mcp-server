# ruff: noqa: F811
"""Synthetic rendered identity fixtures; no LinkedIn network or session."""

from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.navigation import PageNavigator

import pytest

from linkedin_mcp_server.linkedin.post_actions import (
    CLICK_REACT_TOGGLE_JS,
    OWN_EDITOR_JS,
    PIN_POST_ROOT_JS,
    PIN_EDITOR_JS,
    POST_ACTION_SIGNALS_JS,
    SUBMIT_EDITOR_JS,
    PostActions,
)
from linkedin_mcp_server.linkedin.post_actors import (
    OPEN_ACTOR_PICKER_JS,
    PIN_ACTOR_JS,
    READ_ACTOR_IDENTITY_JS,
    SAVE_ACTOR_JS,
    SELECT_ACTOR_JS,
)
from linkedin_mcp_server.linkedin.post_mentions import (
    PIN_AUTHOR_MENTION_JS,
    READ_POST_AUTHOR_JS,
    SELECT_AUTHOR_MENTION_JS,
)
from test_post_actions_dom import (
    ENGLISH,
    POST_ID,
    _pinned,
    dom_page,  # noqa: F401 -- shared real Chromium fixture
    plain_post,
)

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]
ACTOR = {"path": "/in/actor/", "name": "Example Member", "avatar": "PERSON"}
COMPANY = {"path": "/company/example/", "name": "Example Company", "avatar": "COMPANY"}
AUTHOR = {"path": "/company/author", "name": "Post Author", "avatar": "AUTHOR"}


def avatar(key, kind="company-logo"):
    return f'<img width="30" height="30" src="https://media.licdn.com/dms/image/v2/{key}/{kind}_100/x">'


async def routed_page(page, path, body):
    await page.route(
        "**/*",
        lambda route: route.fulfill(status=200, content_type="text/html", body=body),
    )
    await page.goto("https://www.linkedin.com" + path)


@pytest.mark.parametrize(
    "actor,heading,kind",
    [(ACTOR, "h2", "profile-displayphoto"), (COMPANY, "h1", "company-logo")],
)
async def test_identity_binds_exact_page_heading_and_image(
    dom_page, actor, heading, kind
):
    await routed_page(
        dom_page,
        actor["path"],
        f'<html lang="en"><main><section>{avatar(actor["avatar"], kind)}<{heading}>{actor["name"]}</{heading}></section><h2>About</h2></main></html>',
    )
    assert await dom_page.evaluate(READ_ACTOR_IDENTITY_JS, actor["path"]) == actor
    assert await dom_page.evaluate(READ_ACTOR_IDENTITY_JS, "/in/someone-else/") is None


async def test_actor_identity_refuses_ambiguous_photo(dom_page):
    await routed_page(
        dom_page,
        ACTOR["path"],
        f"<main><section><h2>Example Member</h2>{avatar('PERSON', 'profile-displayphoto')}{avatar('OTHER', 'profile-displayphoto')}</section></main>",
    )
    assert await dom_page.evaluate(READ_ACTOR_IDENTITY_JS, ACTOR["path"]) is None


def picker_body():
    return f"""<section id="picker" hidden><h2>Comment, react, and repost as</h2><div role="radiogroup">
      <div role="radio" aria-label="Select Example Member" aria-checked="true" data-key="PERSON">{avatar("PERSON")}</div>
      <div role="radio" aria-label="Select Example Company" aria-checked="false" data-key="COMPANY">{avatar("COMPANY")}</div>
      </div><button id="cancel">Cancel</button><button id="save">Save</button></section>"""


async def actor_picker(page):
    root = await _pinned(page, plain_post(ENGLISH))
    await page.evaluate(
        """html => {
      document.body.insertAdjacentHTML('beforeend', html);
      const picker = document.querySelector('#picker');
      const switcher = document.querySelector('[aria-label="Switch to different account"]');
      switcher.onclick = () => { picker.hidden = false; };
      for (const radio of picker.querySelectorAll('[role=radio]')) radio.onclick = () => {
        for (const option of picker.querySelectorAll('[role=radio]')) option.setAttribute('aria-checked', String(option === radio));
      };
      document.querySelector('#cancel').onclick = () => { picker.hidden = true; };
      document.querySelector('#save').onclick = () => {
        switcher.querySelector('img').src = picker.querySelector('[aria-checked=true] img').src;
        picker.hidden = true;
      };
    }""",
        picker_body(),
    )
    return root


@pytest.mark.parametrize(
    "actor,expected", [(ACTOR, "selected"), (COMPANY, "save_required")]
)
async def test_selects_requested_person_or_company_and_verifies_result(
    dom_page, actor, expected
):
    root = await actor_picker(dom_page)
    assert await dom_page.evaluate(OPEN_ACTOR_PICKER_JS, root)
    assert await dom_page.evaluate(SELECT_ACTOR_JS, {"actor": actor}) == expected
    if expected == "save_required":
        assert await dom_page.evaluate(SAVE_ACTOR_JS, {"actor": actor})
    assert await dom_page.evaluate(PIN_ACTOR_JS, {"root": root, "actor": actor})
    assert await dom_page.evaluate(CLICK_REACT_TOGGLE_JS, {"root": root}) == "clicked"
    assert await dom_page.get_attribute("body", "data-clicked") == "post-react"


@pytest.mark.parametrize("failure", ["wrong_avatar", "duplicate", "unavailable"])
async def test_unverified_company_never_clicks_reaction(dom_page, failure):
    root = await actor_picker(dom_page)
    assert await dom_page.evaluate(OPEN_ACTOR_PICKER_JS, root)
    await dom_page.evaluate(
        """failure => {
      const option = document.querySelector('[data-key=COMPANY]');
      if (failure === 'duplicate') option.after(option.cloneNode(true));
      if (failure === 'unavailable') option.remove();
      if (failure === 'wrong_avatar') option.querySelector('img').src = 'https://media.licdn.com/dms/image/v2/OTHER/company-logo/x';
    }""",
        failure,
    )
    assert await dom_page.evaluate(SELECT_ACTOR_JS, {"actor": COMPANY}) == "unavailable"
    assert (
        await dom_page.evaluate(CLICK_REACT_TOGGLE_JS, {"root": root})
        == "actor_changed"
    )
    assert await dom_page.get_attribute("body", "data-clicked") is None


@pytest.mark.parametrize("change", ["avatar", "control"])
async def test_actor_change_between_verification_and_dispatch_refuses(dom_page, change):
    root = await _pinned(dom_page, plain_post(ENGLISH))
    await root.evaluate(
        """(root, change) => {
      const control = root.querySelector('[aria-label="Switch to different account"]');
      if (change === 'avatar') control.querySelector('img').src = 'https://media.licdn.com/dms/image/v2/OTHER/company-logo/x';
      else control.replaceWith(control.cloneNode(true));
    }""",
        change,
    )
    assert (
        await dom_page.evaluate(CLICK_REACT_TOGGLE_JS, {"root": root})
        == "actor_changed"
    )
    assert await dom_page.get_attribute("body", "data-clicked") is None


async def author_post(page):
    root = await _pinned(page, plain_post(ENGLISH))
    await root.evaluate(
        """(root, html) => root.insertAdjacentHTML('afterbegin', html)""",
        f'<div><a href="https://www.linkedin.com/company/author/">{avatar("AUTHOR")}</a><a href="https://www.linkedin.com/company/author/">Post Author</a></div>',
    )
    return root


async def mention_picker(page, editor, *, duplicate=False, wrong_avatar=False):
    options = f'<div role="option"><div role="button"><p>Post Author</p>{avatar("OTHER" if wrong_avatar else "AUTHOR")}</div></div>'
    if duplicate:
        options += options
    await page.evaluate(
        """html => {
      document.body.insertAdjacentHTML('beforeend', html);
      for (const option of document.querySelectorAll('[role=option] [role=button]')) option.onclick = () => {
        const editor = document.querySelector('[contenteditable=true]');
        editor.innerHTML = '<span data-type="mention" contenteditable="false"><strong>Post Author</strong></span>';
        document.querySelector('[role=listbox]').remove();
      };
    }""",
        f'<div role="listbox" aria-label="Mention suggestions">{options}</div>',
    )
    await editor.focus()


async def test_author_mention_is_real_token_and_survives_dispatch(dom_page):
    root = await author_post(dom_page)
    assert await dom_page.evaluate(READ_POST_AUTHOR_JS, root) == AUTHOR
    editor = await root.query_selector('[role="textbox"]')
    await mention_picker(dom_page, editor)
    assert (
        await dom_page.evaluate(
            SELECT_AUTHOR_MENTION_JS, {"editor": editor, "author": AUTHOR}
        )
        == "selected"
    )
    assert await dom_page.evaluate(
        PIN_AUTHOR_MENTION_JS, {"editor": editor, "author": AUTHOR}
    )
    await dom_page.keyboard.type(" Thank you")
    await dom_page.evaluate(
        OWN_EDITOR_JS, {"editor": editor, "text": "Post Author Thank you"}
    )
    assert (
        await dom_page.evaluate(
            SUBMIT_EDITOR_JS, {"scope": root, "text": "Post Author Thank you"}
        )
        == "submitted"
    )
    assert await dom_page.get_attribute("body", "data-clicked") == "comment-submit"


@pytest.mark.parametrize("failure", ["wrong_avatar", "duplicate"])
async def test_mention_refuses_ambiguous_or_wrong_entity(dom_page, failure):
    root = await author_post(dom_page)
    editor = await root.query_selector('[role="textbox"]')
    await mention_picker(
        dom_page,
        editor,
        duplicate=failure == "duplicate",
        wrong_avatar=failure == "wrong_avatar",
    )
    assert (
        await dom_page.evaluate(
            SELECT_AUTHOR_MENTION_JS, {"editor": editor, "author": AUTHOR}
        )
        == "unavailable"
    )
    assert await dom_page.get_attribute("body", "data-clicked") is None


async def test_plain_text_replacement_of_mention_cannot_publish(dom_page):
    root = await author_post(dom_page)
    editor = await root.query_selector('[role="textbox"]')
    await mention_picker(dom_page, editor)
    await dom_page.evaluate(
        SELECT_AUTHOR_MENTION_JS, {"editor": editor, "author": AUTHOR}
    )
    assert await dom_page.evaluate(
        PIN_AUTHOR_MENTION_JS, {"editor": editor, "author": AUTHOR}
    )
    await dom_page.keyboard.type(" Thank you")
    await dom_page.evaluate(
        OWN_EDITOR_JS, {"editor": editor, "text": "Post Author Thank you"}
    )
    await editor.evaluate("editor => { editor.textContent = 'Post Author Thank you'; }")
    assert (
        await dom_page.evaluate(
            SUBMIT_EDITOR_JS, {"scope": root, "text": "Post Author Thank you"}
        )
        == "mention_changed"
    )
    assert await dom_page.get_attribute("body", "data-clicked") is None


async def test_owner_typing_preserves_rich_mention_and_newline(dom_page):
    root = await author_post(dom_page)
    editor = await root.query_selector('[role="textbox"]')
    await mention_picker(dom_page, editor)
    await dom_page.evaluate_handle(PIN_EDITOR_JS, {"scope": root})
    actions = PostActions(PageSession(dom_page), PageNavigator(PageSession(dom_page)))
    assert (
        await actions._type_text(editor, "Thank you\nUseful", author=AUTHOR) == "typed"
    )
    assert (await editor.inner_text()).replace(
        "\u00a0", " "
    ) == "Post Author Thank you\nUseful"
    assert len(await editor.query_selector_all('[data-type="mention"]')) == 1


async def test_sdui_detail_route_binds_only_exact_target_row(dom_page):
    body = """<html lang="en"><main><section data-sdui-screen="com.linkedin.sdui.flagshipnav.feed.UpdateDetail"><div role="listitem" id="target">
    <div><button aria-label="Reaction button state: no reaction">Like</button><button aria-label="Open reactions menu" aria-expanded="false">Open</button><button aria-label="Comment">Comment</button><button aria-label="Repost" aria-expanded="false">Repost</button></div>
    <div role="textbox" contenteditable="true"> </div></div></section></main></html>"""
    await routed_page(dom_page, f"/feed/update/urn:li:activity:{POST_ID}/", body)
    root = await dom_page.evaluate_handle(PIN_POST_ROOT_JS, POST_ID)
    assert await root.evaluate("root => root.id") == "target"
    signals = await dom_page.evaluate(POST_ACTION_SIGNALS_JS, POST_ID)
    assert signals["reactPressedPresent"] and signals["reactPressed"] is False
    assert (await dom_page.evaluate(POST_ACTION_SIGNALS_JS, "999"))["hasRoot"] is False


@pytest.mark.parametrize(
    "program",
    [
        OPEN_ACTOR_PICKER_JS,
        CLICK_REACT_TOGGLE_JS,
        SUBMIT_EDITOR_JS,
        SELECT_AUTHOR_MENTION_JS,
    ],
)
async def test_offsite_document_refuses_before_any_script_runs(dom_page, program):
    from linkedin_mcp_server.core.exceptions import OffLinkedInLandingError

    await dom_page.goto("https://portal.example/")
    with pytest.raises(OffLinkedInLandingError):
        await PageSession(dom_page).run_on_linkedin(program, {})
    assert await dom_page.get_attribute("body", "data-clicked") is None
    assert (
        await dom_page.evaluate_handle(PIN_POST_ROOT_JS, POST_ID)
    ).as_element() is None


async def test_company_admin_redirect_uses_exact_rendered_vanity_links(dom_page):
    body = f'<main><a href="https://www.linkedin.com/company/example/posts">{avatar("COMPANY")}</a><a href="https://www.linkedin.com/company/example/posts">Example Company<br>1,000 followers</a></main>'
    await routed_page(dom_page, "/company/123/admin/dashboard/", body)
    assert await dom_page.evaluate(READ_ACTOR_IDENTITY_JS, COMPANY["path"]) == COMPANY
    assert (
        await dom_page.evaluate(READ_ACTOR_IDENTITY_JS, "/company/unrelated/") is None
    )


async def test_actor_changed_before_typing_leaves_empty_editor(dom_page):
    root = await _pinned(dom_page, plain_post(ENGLISH))
    await dom_page.evaluate_handle(PIN_EDITOR_JS, {"scope": root})
    editor = await root.query_selector('[role="textbox"]')
    await root.evaluate("root => root.__linkedinMcpPost.actorControl.remove()")
    assert (
        await PostActions(
            PageSession(dom_page), PageNavigator(PageSession(dom_page))
        )._type_text(editor, "private draft")
        == "not_owned"
    )
    assert await editor.inner_text() == ""


async def test_sibling_comment_confirms_only_in_pinned_detail_column(dom_page):
    from linkedin_mcp_server.linkedin.post_actions import COUNT_TEXT_UNITS_JS

    body = """<html lang="en"><main data-sdui-screen="com.linkedin.sdui.flagshipnav.feed.UpdateDetail"><div data-component-type="LazyColumn" data-testid="synthetic-commentList-detail"><div role="listitem" id="target"><div><button aria-label="Reaction button state: no reaction">Like</button><button aria-expanded="false">Open</button><button>Comment</button><button aria-expanded="false">Repost</button></div><div role="textbox" contenteditable="true"></div></div><div role="listitem" id="comments"></div></div><aside id="unrelated"></aside></main></html>"""
    await routed_page(dom_page, f"/feed/update/urn:li:activity:{POST_ID}/", body)
    root = await dom_page.evaluate_handle(PIN_POST_ROOT_JS, POST_ID)
    await dom_page.locator("#unrelated").evaluate(
        "node => { node.innerHTML='<p>Exact comment</p>'; }"
    )
    assert (
        await dom_page.evaluate(
            COUNT_TEXT_UNITS_JS, {"root": root, "text": "Exact comment"}
        )
        == 0
    )
    await dom_page.locator("#comments").evaluate(
        "node => { node.innerHTML='<p>Exact comment</p>'; }"
    )
    assert (
        await dom_page.evaluate(
            COUNT_TEXT_UNITS_JS, {"root": root, "text": "Exact comment"}
        )
        == 1
    )
    await dom_page.evaluate("history.replaceState({}, '', '/feed/')")
    assert (
        await dom_page.evaluate(
            COUNT_TEXT_UNITS_JS, {"root": root, "text": "Exact comment"}
        )
        == -1
    )


@pytest.mark.parametrize("change", ["same_post", "wrong_post", "wrong_route"])
async def test_actor_save_remount_reacquires_only_original_target(dom_page, change):
    from linkedin_mcp_server.linkedin.post_actions import (
        CLICK_REACT_TOGGLE_JS,
        PostActions,
    )
    from linkedin_mcp_server.linkedin.navigation import PageNavigator

    root = await actor_picker(dom_page)
    await dom_page.evaluate(
        """change => {
      const save = document.querySelector('#save');
      const original = save.onclick;
      save.onclick = () => {
        original();
        const root = document.querySelector('[data-urn]');
        const copy = root.cloneNode(true);
        if (change === 'wrong_post') {
          for (const node of [copy, ...copy.querySelectorAll('*')]) {
            for (const attr of [...node.attributes]) {
              if (attr.value.includes('7506667649444237313')) node.setAttribute(attr.name, attr.value.replaceAll('7506667649444237313', '999'));
            }
          }
        }
        root.replaceWith(copy);
        if (change === 'wrong_route') history.replaceState({}, '', '/feed/changed/');
      };
    }""",
        change,
    )
    session = PageSession(dom_page)
    selected = await PostActions(session, PageNavigator(session))._select_actor(
        root, COMPANY
    )
    if change == "same_post":
        assert selected is not None
        assert (
            await session.run_on_linkedin(CLICK_REACT_TOGGLE_JS, {"root": selected})
            == "clicked"
        )
        assert await dom_page.get_attribute("body", "data-clicked") == "post-react"
    else:
        assert selected is None
        assert await dom_page.get_attribute("body", "data-clicked") is None
