# tests/test_post_composer_dom.py
"""Browser-DOM tests for the post composer script in ``linkedin/post_publisher.py``.

The unit suite fakes the page, so ``_COMPOSER_JS`` never executes there. These
tests run the real script against synthetic markup in headless chromium.

The script finds the composer, the Post button and the dismiss button by
structure alone, so each case is built in three "locales" with identical
structure: English words, German words, and empty strings (which carry no verb
to match). Every case asserts the same answer for all three in one assertion
that names the locales. A decision that differs between them read a word, which
the AGENTS.md LinkedIn Page Rules forbid.

The fixture is a claim about the algorithm, not about LinkedIn: the live
composer was inspected on 2026-10-28 and has one ``<dialog>`` whose icon buttons
carry an ``aria-label`` while the Post button carries none.

Skipped automatically when chromium is not installed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from patchright.async_api import Page, async_playwright

from linkedin_mcp_server.linkedin.post_publisher import _COMPOSER_JS

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

#: Read through the DOM, which both worlds share: patchright evaluates in an
#: isolated world, where a `window` variable set by a page handler is invisible.
_CLICKED = "document.body.dataset.clicked ?? null"

#: (dismiss, emoji, media, post) words per locale. Empty strings are the
#: control: an attribute that is present but says nothing.
_LOCALES = {
    "english": ("Dismiss", "Show Emoji Picker", "Media", "Post"),
    "german": ("Schließen", "Emoji anzeigen", "Medien", "Posten"),
    "opaque": ("", "", "", "·"),
}


def _dialog(
    words: tuple[str, str, str, str],
    *,
    post_buttons: int = 1,
    post_disabled: bool = False,
    hidden: bool = False,
    editor: bool = True,
) -> str:
    dismiss, emoji, media, post = words
    post_markup = "".join(
        f'<button id="post{n}" {"disabled" if post_disabled else ""} '
        f"onclick=\"document.body.dataset.clicked = 'post{n}'\">{post}</button>"
        for n in range(post_buttons)
    )
    editor_markup = (
        '<div contenteditable="true" role="textbox" id="editor">Draft</div>'
        if editor
        else ""
    )
    return (
        f"<dialog open {'style="display:none"' if hidden else ''}>"
        f'<button id="dismiss" aria-label="{dismiss}" '
        "onclick=\"document.body.dataset.clicked = 'dismiss'\"></button>"
        f"{editor_markup}"
        f'<button id="emoji" aria-label="{emoji}" '
        "onclick=\"document.body.dataset.clicked = 'emoji'\"></button>"
        f'<button id="media" aria-label="{media}" '
        "onclick=\"document.body.dataset.clicked = 'media'\"></button>"
        f"{post_markup}</dialog>"
    )


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    """Real chromium page, or skip when no browser is installed.

    Only launch and setup are guarded by the skip, so a failure inside a test
    is never turned into one.
    """
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            context = await browser.new_context()
            page = await context.new_page()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield page
        finally:
            await browser.close()


async def _read(page: Page, html: str, mode: str = "read") -> dict[str, Any]:
    await page.set_content(f"<body>{html}</body>")
    return await page.evaluate(_COMPOSER_JS, mode)


async def _every_locale(page: Page, mode: str = "read", **kwargs: Any) -> dict:
    answers = {
        name: await _read(page, _dialog(words, **kwargs), mode)
        for name, words in _LOCALES.items()
    }
    assert len({repr(sorted(a.items())) for a in answers.values()}) == 1, answers
    return next(iter(answers.values()))


async def test_the_one_post_button_is_found_in_every_locale(page):
    state = await _every_locale(page)

    assert state["dialogs"] == 1 and state["editors"] == 1
    assert state["postButtons"] == 1 and state["postEnabled"] is True
    assert state["text"] == "Draft"


async def test_a_disabled_post_button_is_not_enabled(page):
    state = await _every_locale(page, post_disabled=True)

    assert state["postButtons"] == 1 and state["postEnabled"] is False


async def test_two_unlabelled_buttons_are_ambiguous_and_never_clicked(page):
    state = await _every_locale(page, mode="post", post_buttons=2)

    assert state["postButtons"] == 2 and state["postEnabled"] is False
    assert state["result"] == "unavailable"
    assert await page.evaluate(_CLICKED) is None


async def test_post_mode_clicks_only_the_post_button(page):
    for words in _LOCALES.values():
        await _read(page, _dialog(words), "post")
        assert await page.evaluate(_CLICKED) == "post0"


async def test_dismiss_mode_clicks_the_first_button(page):
    for words in _LOCALES.values():
        await _read(page, _dialog(words), "dismiss")
        assert await page.evaluate(_CLICKED) == "dismiss"


async def test_a_hidden_dialog_is_not_the_composer(page):
    state = await _every_locale(page, hidden=True)

    assert state["dialogs"] == 0 and state["editors"] == 0


async def test_a_dialog_without_an_editor_is_not_the_composer(page):
    state = await _every_locale(page, editor=False)

    assert state["dialogs"] == 0


async def test_two_composers_fail_closed(page):
    html = _dialog(_LOCALES["english"]) + _dialog(_LOCALES["german"])

    state = await _read(page, html, "post")

    assert state["dialogs"] == 2 and state["editors"] == 0
    assert state["result"] is None
    assert await page.evaluate(_CLICKED) is None
