"""Browser-DOM tests for the Docker login viewer's page, ``scripts/login-viewer.html``.

The page runs in headless chromium against a stand-in for noVNC's
``core/rfb.js`` that records what would go to the VNC server and, like
noVNC's keyboard handler, takes every key that reaches the screen and cancels
the browser's default for it. So each case is a claim about the page and
never about noVNC or x11vnc; the image itself was checked by hand against the
real ones. Every request other than the two files is aborted. Skipped
automatically when chromium is not installed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from patchright.async_api import async_playwright

#: Keep every test that launches Chromium on one worker; see
#: ``test_root_content_dom.py``.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

PAGE = Path(__file__).parents[1] / "scripts" / "login-viewer.html"
ORIGIN = "http://127.0.0.1:6080"
URL = f"{ORIGIN}/login.html#token=abc_123-XYZ"
#: The modifier that makes the browser paste on this platform.
PASTE = "Meta" if sys.platform == "darwin" else "Control"

RFB_STAND_IN = """
export default class RFB extends EventTarget {
    constructor(target, url) {
        super();
        window.viewer = { url, scaleViewport: null, sent: [], forwarded: [] };
        const canvas = document.createElement('canvas');
        canvas.tabIndex = -1;
        target.append(canvas);
        for (const type of ['keydown', 'keyup']) {
            canvas.addEventListener(type, e => {
                window.viewer.forwarded.push(`${type} ${e.code}`);
                e.preventDefault();
                e.stopPropagation();
            });
        }
        canvas.focus();
        setTimeout(() => this.dispatchEvent(new Event('connect')));
    }
    set scaleViewport(value) { window.viewer.scaleViewport = value; }
    sendKey(keysym, code, down) { window.viewer.sent.push([keysym, code, down ?? null]); }
}
"""


@pytest.fixture
async def viewer():
    """The page at the address ``viewer_url`` prints, connected, or a skip."""
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        page.set_default_timeout(3000)

        async def serve(route: Any) -> None:
            path = route.request.url.removeprefix(ORIGIN).split("#")[0]
            if path == "/login.html":
                await route.fulfill(content_type="text/html", body=PAGE.read_text())
            elif path == "/core/rfb.js":
                await route.fulfill(content_type="text/javascript", body=RFB_STAND_IN)
            else:
                await route.abort()

        try:
            await page.route("**/*", serve)
            await page.goto(URL)
            await page.wait_for_function(
                "document.getElementById('status').textContent.startsWith('Connected')"
            )
            yield page
        finally:
            await browser.close()


async def copy(page: Any, text: str) -> None:
    """Put text on the browser's clipboard from a field outside the screen."""
    await page.evaluate(
        """text => {
            const field = document.createElement('textarea');
            field.id = 'source';
            field.value = text;
            document.body.append(field);
            field.select();
        }""",
        text,
    )
    await page.keyboard.press(f"{PASTE}+KeyC")
    await page.evaluate("document.getElementById('source').remove()")
    await page.focus("canvas")


async def sent(page: Any) -> list[list[Any]]:
    await page.wait_for_timeout(100)
    return await page.evaluate("window.viewer.sent", isolated_context=False)


async def test_the_page_connects_to_the_token_websocket_and_scales(viewer):
    state = await viewer.evaluate("window.viewer", isolated_context=False)

    assert state["url"] == "ws://127.0.0.1:6080/websockify?token=abc_123-XYZ"
    assert state["scaleViewport"] is True


async def test_a_paste_is_typed_once_the_modifier_is_released(viewer):
    await copy(viewer, "Pa$$ 1!\tx")

    await viewer.keyboard.down(PASTE)
    await viewer.keyboard.press("KeyV")
    assert await sent(viewer) == []

    await viewer.keyboard.up(PASTE)

    assert await sent(viewer) == [[ord(char), None, None] for char in "Pa$$ 1!"] + [
        [0xFF09, None, None],
        [ord("x"), None, None],
    ]
    forwarded = await viewer.evaluate("window.viewer.forwarded", isolated_context=False)
    assert "keydown KeyV" not in forwarded
    assert "keyup KeyV" not in forwarded


async def test_text_with_a_character_the_display_cannot_type_is_not_typed(viewer):
    await copy(viewer, "pässword")

    await viewer.keyboard.press(f"{PASTE}+KeyV")

    assert await sent(viewer) == []
    status = await viewer.text_content("#status")
    assert status is not None and status.startswith("Only plain ASCII")


async def test_a_plain_v_still_reaches_the_screen(viewer):
    await viewer.keyboard.press("KeyV")

    forwarded = await viewer.evaluate("window.viewer.forwarded", isolated_context=False)
    assert forwarded == ["keydown KeyV", "keyup KeyV"]
    assert await sent(viewer) == []
