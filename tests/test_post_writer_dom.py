"""Synthetic composer DOM contracts; every browser request is intercepted."""

import os
from html import escape
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin.post_writer import PostWriter
from linkedin_mcp_server.linkedin.session import PageSession


pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]


@pytest.fixture
async def dom_page():
    """Launch an isolated browser against intercepted synthetic pages only."""
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(
                headless=True,
                channel="chromium",
                executable_path=os.getenv("POST_TEST_BROWSER"),
            )
            page = await browser.new_page()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            await page.route(
                "https://www.linkedin.com/in/test-author/",
                lambda route: route.fulfill(
                    body="<main><h1>Synthetic Author</h1></main>",
                    content_type="text/html",
                ),
            )
            await page.goto("https://www.linkedin.com/in/test-author/")
            yield page
        finally:
            await browser.close()


@pytest.mark.browser_dom
@pytest.mark.parametrize(
    "locale, action, audience, delayed, empty_html, text",
    [
        ("en", "Post", "Anyone", False, "", "An approved post"),
        ("fr", "Publier", "Tout le monde", False, "", "An approved post"),
        ("en", "Post", "Anyone", True, "", "An approved post"),
        ("en", "Post", "Anyone", False, "<p><br></p>", "An approved post"),
        ("en", "Post", "Anyone", False, "<p> &nbsp; </p>", "An approved post"),
        (
            "en",
            "Post",
            "Anyone",
            False,
            "",
            "First paragraph.\n\nSecond paragraph with café, 🎵 and <tag> & plain text.",
        ),
        ("en", "Post", "Anyone", False, "", "<tag> & plain text"),
    ],
)
async def test_real_composer_pinned_actions_and_verified_post(
    dom_page, locale, action, audience, delayed, empty_html, text
):
    page = dom_page
    author = "https://www.linkedin.com/in/test-author/"

    async def route(request):
        if "/feed/update/" in request.request.url:
            html = f'<meta charset="utf-8"><main><article><header><a href="{author}">Author</a></header><p style="white-space:pre-wrap">{escape(text)}</p></article></main>'
        else:
            html = f'''<html lang="{locale}"><dialog open>
            <a href="{author}">Author</a><button>{audience}</button>
            <div role="textbox" contenteditable="true">{empty_html}</div>
            <button onclick="location.href='/feed/update/urn:li:activity:123/'">{action}</button>
            </dialog></html>'''
        if delayed and "/feed/update/" not in request.request.url:
            import json

            dialog_html = html.split("<dialog", 1)[1].split("</dialog>", 1)[0]
            html = f'<html lang="{locale}"><body><script>setTimeout(() => document.body.insertAdjacentHTML("beforeend", {json.dumps("<dialog" + dialog_html + "</dialog>")}), 150)</script></body></html>'
        await request.fulfill(body=html, content_type="text/html")

    await page.route("**/*", route)
    nav = MagicMock()
    nav._navigate_to_page = page.goto
    session = PageSession(page)
    writer = PostWriter(session, nav)
    draft = writer.prepare(text, author)["draft_id"]
    result = await writer.publish(draft, text, confirmed=True)
    assert result["status"] == "published", (
        result,
        page.url,
        await page.locator("body").inner_text(),
    )
    assert (await page.locator("main").inner_text()).endswith(text)


@pytest.mark.parametrize("raw", ["First\r\nSecond", "First\rSecond"])
async def test_cr_line_breaks_publish_the_exact_returned_preview(dom_page, raw):
    page = dom_page
    author = "https://www.linkedin.com/in/test-author/"
    expected = "First\nSecond"

    async def route(request):
        if "/feed/update/" in request.request.url:
            html = f'<main><article><header><a href="{author}">Author</a></header><p style="white-space:pre-wrap">{expected}</p></article></main>'
        else:
            html = f'''<html lang="en"><dialog open><a href="{author}">Author</a>
                <button>Anyone</button><div role="textbox" contenteditable="true"></div>
                <button onclick="location.href='/feed/update/urn:li:activity:123/'">Post</button>
                </dialog></html>'''
        await request.fulfill(body=html, content_type="text/html")

    await page.route("**/*", route)
    nav = MagicMock()
    nav._navigate_to_page = page.goto
    writer = PostWriter(PageSession(page), nav)
    preview = writer.prepare(raw, author)
    assert preview["text"] == expected

    result = await writer.publish(preview["draft_id"], preview["text"], confirmed=True)

    assert result["status"] == "published"
    assert await page.locator("[contenteditable]").count() == 0
    assert (await page.locator("main").inner_text()).endswith(expected)


@pytest.mark.browser_dom
async def test_off_domain_at_mutation_seam_cannot_receive_post(dom_page, monkeypatch):
    page = dom_page
    author = "https://www.linkedin.com/in/test-author/"
    html = f'''<html lang="en"><dialog open><a href="{author}">Author</a>
    <button>Anyone</button><div role="textbox" contenteditable="true"></div>
    <button onclick="document.documentElement.dataset.clicked=1">Post</button></dialog></html>'''
    await page.route(
        "**/*", lambda route: route.fulfill(body=html, content_type="text/html")
    )
    from linkedin_mcp_server.core.destination import linkedin_element

    @asynccontextmanager
    async def redirect_then_check(locator):
        await page.goto("https://example.test/")
        async with linkedin_element(locator) as handle:
            yield handle

    monkeypatch.setattr(
        "linkedin_mcp_server.linkedin.post_writer.linkedin_element",
        redirect_then_check,
    )
    nav = MagicMock()
    nav._navigate_to_page = page.goto
    writer = PostWriter(PageSession(page), nav)
    draft = writer.prepare("Must never be typed", author)["draft_id"]
    result = await writer.publish(draft, "Must never be typed", confirmed=True)
    assert result["status"] == "outcome_unknown"
    assert await page.locator("div").inner_text() == ""
    assert await page.locator("html").get_attribute("data-clicked") is None


async def test_unlinked_actor_variant_is_refused_without_typing_or_posting(dom_page):
    """A display name alone cannot verify a personal publishing identity."""
    page = dom_page
    html = """<html lang="en"><dialog open>
        <div role="button" aria-expanded="false">Example Author</div>
        <div role="button" aria-expanded="false">Post to Anyone</div>
        <div role="button" aria-expanded="false">Comments: Anyone</div>
        <div role="textbox" contenteditable="true"><p><br></p></div>
        <button onclick="document.documentElement.dataset.clicked=1">Post</button>
        </dialog></html>"""
    await page.route(
        "**/*", lambda route: route.fulfill(body=html, content_type="text/html")
    )
    nav = MagicMock()
    nav._navigate_to_page = page.goto
    writer = PostWriter(PageSession(page), nav)
    draft_id = writer.prepare(
        "Do not enter this text", "https://www.linkedin.com/in/test-author/"
    )["draft_id"]
    with pytest.raises(ValueError, match="Unsupported composer.*author"):
        await writer.publish(draft_id, "Do not enter this text", confirmed=True)
    assert not writer._state.drafts[draft_id].attempted
    assert (await page.locator('[role="textbox"]').inner_text()).strip() == ""
    assert await page.locator("html").get_attribute("data-clicked") is None


@pytest.mark.parametrize(
    "existing", ["editor", "dialog", "attachment", "selected_file", "inline_attachment"]
)
async def test_existing_operator_work_survives_publication_request(dom_page, existing):
    page = dom_page
    markup = {
        "editor": '<div role="textbox" contenteditable="true">An operator draft</div>',
        "dialog": '<div role="dialog"><p>An operator dialog</p></div>',
        "attachment": '<dialog open><img alt="An operator attachment" src="data:image/png;base64,"/></dialog>',
        "selected_file": '<input type="file" style="display:none">',
        "inline_attachment": '<div role="textbox" contenteditable="true"><a href="/in/test-author/"><img width="32" height="32" alt="Operator image" src="data:image/png;base64,"></a></div>',
    }[existing]
    await page.set_content(f"<main>{markup}</main>")
    if existing == "selected_file":
        await page.locator('input[type="file"]').set_input_files(
            {"name": "synthetic.txt", "mimeType": "text/plain", "buffer": b"synthetic"}
        )
    before = await page.locator("main").inner_html()
    original_url = page.url
    nav = MagicMock(_navigate_to_page=MagicMock())
    writer = PostWriter(PageSession(page), nav)
    draft_id = writer.prepare(
        "New approved text", "https://www.linkedin.com/in/test-author/"
    )["draft_id"]
    with pytest.raises(ValueError, match="Existing browser work"):
        await writer.publish(draft_id, "New approved text", confirmed=True)
    nav._navigate_to_page.assert_not_called()
    assert not writer._state.drafts[draft_id].attempted
    assert page.url == original_url
    assert await page.locator("main").inner_html() == before
    if existing == "selected_file":
        assert (
            await page.locator("input").evaluate("el => el.files[0].name")
            == "synthetic.txt"
        )


@pytest.mark.parametrize("seam", ["after_preflight", "focus_restore", "focus_redirect"])
async def test_operator_text_arriving_before_insertion_is_never_replaced(
    dom_page, monkeypatch, seam
):
    page = dom_page
    approved = "Exact approved publication"
    operator = "An independently restored operator draft"
    author = "https://www.linkedin.com/in/test-author/"
    focus = (
        f"onfocus=\"this.innerText='{operator}'\"" if seam == "focus_restore" else ""
    )
    if seam == "focus_redirect":
        focus = "onfocus=\"document.getElementById('operator-field').focus()\""

    async def route(request):
        if "/feed/update/" in request.request.url:
            html = f'<main><article><header><a href="{author}">Author</a></header><p>{approved}</p></article></main>'
        else:
            html = f'''<html lang="en"><dialog open><a href="{author}">Author</a>
                <button>Anyone</button><div role="textbox" contenteditable="true" {focus}></div>
                <button onclick="location.href='/feed/update/urn:li:activity:123/'">Post</button>
                </dialog><input id="operator-field" value="Independent input"></html>'''
        await request.fulfill(body=html, content_type="text/html")

    await page.route("**/*", route)
    if seam == "after_preflight":
        from linkedin_mcp_server.core.destination import linkedin_element

        class InterleavedButton:
            def __init__(self, handle):
                self.handle = handle
                self.interleaved = False

            async def evaluate(self, script, args):
                result = await self.handle.evaluate(script, args)
                if not self.interleaved and not args["click"]:
                    self.interleaved = True
                    await page.locator('[role="textbox"]').evaluate(
                        "(editor, text) => { editor.innerText = text; }", operator
                    )
                return result

        @asynccontextmanager
        async def interleave_after_check(locator):
            async with linkedin_element(locator) as handle:
                if await handle.evaluate("element => element.tagName === 'BUTTON'"):
                    yield InterleavedButton(handle)
                else:
                    yield handle

        monkeypatch.setattr(
            "linkedin_mcp_server.linkedin.post_writer.linkedin_element",
            interleave_after_check,
        )
    nav = MagicMock()
    nav._navigate_to_page = page.goto
    writer = PostWriter(PageSession(page), nav)
    draft_id = writer.prepare(approved, author)["draft_id"]
    result = await writer.publish(draft_id, approved, confirmed=True)
    assert result["status"] == "not_submitted"
    assert result["published"] is False
    assert result["retry_allowed"] is False
    expected = "" if seam == "focus_redirect" else operator
    assert await page.locator('[role="textbox"]').inner_text() == expected
    if seam == "focus_redirect":
        assert (
            await page.locator("#operator-field").input_value() == "Independent input"
        )
    with pytest.raises(ValueError, match="Attempt already"):
        await writer.publish(draft_id, approved, confirmed=True)


@pytest.mark.parametrize("position", ["inside_editor", "after_editor"])
@pytest.mark.parametrize("seam", ["navigation_restore", "focus_restore"])
async def test_linked_operator_images_are_preserved_without_typing_or_clicking(
    dom_page, position, seam
):
    """An author-profile href does not make an image in the draft an avatar."""
    page = dom_page
    author = "https://www.linkedin.com/in/test-author/"
    approved = "Approved text must not replace the operator image"
    image = f'<a href="{author}"><img id="operator-image" width="32" height="32" alt="Operator image" src="data:image/png;base64,"></a>'

    async def route(request):
        if "/feed/update/" in request.request.url:
            html = f'<main><article><header><a href="{author}">Author</a></header><p>{approved}</p></article></main>'
        else:
            html = f'''<html lang="en"><dialog open><header><a href="{author}">Author</a></header>
                <button>Anyone</button><div role="textbox" contenteditable="true"></div>
                <button onclick="document.documentElement.dataset.clicked=1">Post</button>
                </dialog><script>
                const editor = document.querySelector('[contenteditable]');
                editor.addEventListener('input', () => document.documentElement.dataset.typed=1);
                </script></html>'''
        await request.fulfill(body=html, content_type="text/html")

    await page.route("**/*", route)

    async def navigate_and_restore(url):
        await page.goto(url)
        await page.locator("[contenteditable]").evaluate(
            """(editor, {position, image, seam}) => {
                const restore = () => editor.insertAdjacentHTML(
                    position === 'inside_editor' ? 'afterbegin' : 'afterend', image);
                if (seam === 'focus_restore')
                    editor.addEventListener('focus', restore, {once: true});
                else restore();
            }""",
            {"position": position, "image": image, "seam": seam},
        )

    nav = MagicMock()
    nav._navigate_to_page = navigate_and_restore
    writer = PostWriter(PageSession(page), nav)
    draft_id = writer.prepare(approved, author)["draft_id"]
    if seam == "navigation_restore":
        with pytest.raises(ValueError, match="unsupported_attachment"):
            await writer.publish(draft_id, approved, confirmed=True)
    else:
        result = await writer.publish(draft_id, approved, confirmed=True)
        assert result["status"] == "not_submitted"
        assert result["reason"] == "unsupported_attachment"
    assert await page.locator("#operator-image").count() == 1
    assert await page.locator("[contenteditable]").inner_text() == ""
    assert await page.locator("html").get_attribute("data-typed") is None
    assert await page.locator("html").get_attribute("data-clicked") is None


async def test_author_avatar_before_the_editor_does_not_block_a_plain_text_post(
    dom_page,
):
    page = dom_page
    author = "https://www.linkedin.com/in/test-author/"
    text = "An approved plain-text post"

    async def route(request):
        if "/feed/update/" in request.request.url:
            html = f'<main><article><header><a href="{author}">Author</a></header><p>{text}</p></article></main>'
        else:
            html = f'''<html lang="en"><dialog open><header><a href="{author}">
                <img width="32" height="32" alt="Author avatar" src="data:image/png;base64,">Author
                </a></header><button>Anyone</button><div role="textbox" contenteditable="true"></div>
                <button onclick="location.href='/feed/update/urn:li:activity:123/'">Post</button></dialog></html>'''
        await request.fulfill(body=html, content_type="text/html")

    await page.route("**/*", route)
    nav = MagicMock()
    nav._navigate_to_page = page.goto
    writer = PostWriter(PageSession(page), nav)
    draft_id = writer.prepare(text, author)["draft_id"]
    result = await writer.publish(draft_id, text, confirmed=True)
    assert result["status"] == "published"


@pytest.mark.parametrize(
    "media",
    [
        '<img width="32" height="32" src="data:image/png;base64,">',
        "<video></video>",
        '<a download href="/synthetic.pdf"> </a>',
        '<div role="radiogroup"><input type="radio"></div>',
    ],
)
async def test_media_only_inline_editor_stops_author_lookup(dom_page, media):
    page = dom_page
    await page.set_content(
        f'<main><div role="textbox" contenteditable="true">{media}</div></main>'
    )
    before = await page.locator("main").inner_html()
    resolve = AsyncMock(return_value="https://www.linkedin.com/in/test-author/")
    nav = MagicMock()
    writer = PostWriter(PageSession(page), nav, resolve_author=resolve)
    with pytest.raises(ValueError, match="Existing browser work"):
        await writer.prepare_post("A new preview")
    resolve.assert_not_awaited()
    nav._navigate_to_page.assert_not_called()
    assert await page.locator("main").inner_html() == before


async def test_feed_media_and_empty_inline_placeholder_allow_author_lookup(dom_page):
    page = dom_page
    await page.set_content("""<main>
        <article><img width="32" height="32" src="data:image/png;base64,"><video></video></article>
        <div role="textbox" contenteditable="true"><p><br></p></div></main>""")
    before = await page.locator("main").inner_html()
    resolve = AsyncMock(return_value="https://www.linkedin.com/in/test-author/")
    nav = MagicMock()
    writer = PostWriter(PageSession(page), nav, resolve_author=resolve)
    result = await writer.prepare_post("A new preview")
    assert result["status"] == "preview"
    assert result["text"] == "A new preview"
    resolve.assert_awaited_once_with()
    nav._navigate_to_page.assert_not_called()
    assert await page.locator("main").inner_html() == before
