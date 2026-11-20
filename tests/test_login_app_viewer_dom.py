"""Browser-DOM tests for the sign-in view's page, ``login_app/viewer.html``.

The page runs in headless Chromium. All but the last case replace the MCP
Apps SDK with a stand-in ``App`` that records every ``callServerTool`` call and
lets the test answer it, so each case is a claim about the page and never
about a host. The last case loads the page exactly as the server serves it and
checks that the inlined SDK hands the page its ``App``. Skipped automatically
when Chromium is not installed.
"""

from __future__ import annotations

import json
import sys
from importlib import resources
from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.login_app import viewer_html

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

ORIGIN = "https://view.invalid"
#: The modifier that makes the browser paste on this platform.
PASTE = "Meta" if sys.platform == "darwin" else "Control"
#: A valid 1x1 PNG, so the image has a size to click on.
PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="

SDK_STAND_IN = """
class App {
  constructor() { window.bridge = this; this.calls = []; }
  connect() { return new Promise((resolve) => { this.resolveConnect = resolve; }); }
  getHostContext() { return { theme: 'light' }; }
  callServerTool(params) {
    return new Promise((resolve, reject) => this.calls.push({ params, resolve, reject }));
  }
}
globalThis.linkedinLoginSdk = { App };
"""


def shown(open_id: str = "O1", attempt_id: str = "A1") -> dict[str, Any]:
    """An open_login result that shows the sign-in, as the host hands it over."""
    return {
        "content": [{"type": "text", "text": "The LinkedIn sign-in is shown."}],
        "structuredContent": {
            "view": "shown",
            "attempt_id": attempt_id,
            "open_id": open_id,
        },
    }


def attached(
    grant: str = "G1", open_id: str = "O1", attempt_id: str = "A1"
) -> dict[str, Any]:
    """A login_view_attach result carrying *grant* in its _meta."""
    return {
        "content": [{"type": "text", "text": json.dumps({"access": "granted"})}],
        "_meta": {
            "linkedin.dev/login-view": {
                "grant": grant,
                "attempt_id": attempt_id,
                "open_id": open_id,
            }
        },
    }


def _template() -> str:
    return (
        resources.files("linkedin_mcp_server") / "login_app" / "viewer.html"
    ).read_text(encoding="utf-8")


@pytest.fixture
async def browser_page():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        page.set_default_timeout(3000)
        try:
            yield page
        finally:
            await browser.close()


async def _serve(page: Any, html: str, *, host: str | None = None) -> None:
    """Serve *html* as the view, optionally inside a *host* page that frames it."""

    async def serve(route: Any) -> None:
        url = route.request.url
        if url.startswith(f"{ORIGIN}/view.html"):
            await route.fulfill(content_type="text/html", body=html)
        elif host is not None and url.startswith(f"{ORIGIN}/host.html"):
            await route.fulfill(content_type="text/html", body=host)
        else:
            await route.abort()

    await page.route("**/*", serve)
    await page.goto(f"{ORIGIN}/{'view' if host is None else 'host'}.html")


@pytest.fixture
async def viewer(browser_page: Any) -> Any:
    """The page with the SDK stand-in, loaded and not yet connected."""
    html = _template().replace(
        "<!--EXT_APPS_SDK-->", f'<script type="module">{SDK_STAND_IN}</script>', 1
    )
    await _serve(browser_page, html)
    await browser_page.wait_for_function("window.bridge !== undefined")
    return browser_page


async def calls(page: Any, name: str) -> list[dict[str, Any]]:
    return await page.evaluate(
        "name => bridge.calls.filter(c => c.params.name === name).map(c => c.params.arguments)",
        name,
        isolated_context=False,
    )


async def answer(page: Any, name: str, index: int, result: dict[str, Any]) -> None:
    await page.evaluate(
        """([name, index, result]) =>
            bridge.calls.filter(c => c.params.name === name)[index].resolve(result)""",
        [name, index, result],
        isolated_context=False,
    )


async def wait_for_calls(page: Any, name: str, count: int) -> None:
    await page.wait_for_function(
        "([name, count]) => bridge.calls.filter(c => c.params.name === name).length >= count",
        arg=[name, count],
    )


def frame(**reply: Any) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": json.dumps(reply)}]
    if reply.get("frame") == "image":
        content.append({"type": "image", "data": PIXEL, "mimeType": "image/png"})
    return {"content": content}


LIVE = frame(
    access="full",
    state="signing_in",
    frame="image",
    hash="h1",
    page_id="p1",
    geometry="800x600",
)


async def connect(page: Any) -> None:
    await page.evaluate("bridge.resolveConnect()", isolated_context=False)


async def deliver(page: Any, result: dict[str, Any]) -> None:
    """Hand *result* to the page as the result of the model's open_login call."""
    await page.evaluate(
        "result => bridge.ontoolresult(result)", result, isolated_context=False
    )


async def show_the_sign_in(page: Any) -> None:
    await deliver(page, shown())


async def fail(page: Any, name: str, index: int) -> None:
    """Fail call *index* of tool *name* the way a lost transport does."""
    await page.evaluate(
        """([name, index]) => bridge.calls.filter(c => c.params.name === name)[index]
            .reject(new Error('transport closed'))""",
        [name, index],
        isolated_context=False,
    )


async def live(page: Any) -> None:
    """Connected, attached with grant G1, and showing the first frame."""
    await connect(page)
    await show_the_sign_in(page)
    await wait_for_calls(page, "login_view_attach", 1)
    await answer(page, "login_view_attach", 0, attached())
    await wait_for_calls(page, "login_view_frame", 1)
    await answer(page, "login_view_frame", 0, LIVE)
    await page.wait_for_function(
        "document.getElementById('text').textContent.startsWith('Live.')"
    )


@pytest.mark.parametrize(
    "shown_first", [True, False], ids=["result first", "connection first"]
)
async def test_the_grant_comes_from_attach_once_both_exist(
    viewer: Any, shown_first: bool
):
    first, second = (
        (show_the_sign_in, connect) if shown_first else (connect, show_the_sign_in)
    )

    await first(viewer)
    await viewer.wait_for_timeout(150)
    assert await calls(viewer, "login_view_attach") == []

    await second(viewer)
    await wait_for_calls(viewer, "login_view_attach", 1)
    assert await calls(viewer, "login_view_frame") == []
    await answer(viewer, "login_view_attach", 0, attached())
    await wait_for_calls(viewer, "login_view_frame", 1)
    await viewer.wait_for_timeout(150)

    assert await calls(viewer, "login_view_attach") == [
        {"attempt_id": "A1", "open_id": "O1"}
    ]
    assert await calls(viewer, "login_view_frame") == [
        {"grant": "G1", "attempt_id": "A1", "after": "", "budget_s": 1.5}
    ]


async def test_a_word_only_result_ends_without_attaching(viewer: Any):
    await connect(viewer)
    await deliver(
        viewer,
        {"content": [{"type": "text", "text": "A LinkedIn session is in place now."}]},
    )

    await viewer.wait_for_function(
        "document.getElementById('text').textContent.startsWith('A LinkedIn session')"
    )
    await viewer.wait_for_timeout(150)
    assert await calls(viewer, "login_view_attach") == []
    assert await calls(viewer, "login_view_frame") == []


async def test_a_duplicate_result_while_attaching_attaches_once(viewer: Any):
    await connect(viewer)
    await show_the_sign_in(viewer)
    await wait_for_calls(viewer, "login_view_attach", 1)

    await show_the_sign_in(viewer)
    await viewer.wait_for_timeout(200)
    assert len(await calls(viewer, "login_view_attach")) == 1

    await answer(viewer, "login_view_attach", 0, attached())
    await wait_for_calls(viewer, "login_view_frame", 1)
    assert len(await calls(viewer, "login_view_attach")) == 1


async def test_an_attach_answered_after_teardown_starts_nothing(viewer: Any):
    await connect(viewer)
    await show_the_sign_in(viewer)
    await wait_for_calls(viewer, "login_view_attach", 1)

    await viewer.evaluate("bridge.onteardown({}, {})", isolated_context=False)
    await answer(viewer, "login_view_attach", 0, attached())
    await viewer.wait_for_timeout(300)

    assert await calls(viewer, "login_view_frame") == []


async def test_an_attach_for_a_superseded_result_is_never_used(viewer: Any):
    await connect(viewer)
    await show_the_sign_in(viewer)
    await wait_for_calls(viewer, "login_view_attach", 1)

    # A newer open_login result arrives while the first attach is pending.
    await deliver(viewer, shown(open_id="O2"))
    await wait_for_calls(viewer, "login_view_attach", 2)
    await answer(viewer, "login_view_attach", 0, attached(grant="G-old"))
    await viewer.wait_for_timeout(200)
    assert await calls(viewer, "login_view_frame") == []

    await answer(viewer, "login_view_attach", 1, attached(grant="G2", open_id="O2"))
    await wait_for_calls(viewer, "login_view_frame", 1)
    grants = {call["grant"] for call in await calls(viewer, "login_view_frame")}
    assert grants == {"G2"}
    assert (await calls(viewer, "login_view_attach"))[1]["open_id"] == "O2"


@pytest.mark.parametrize("access", ["replaced", "none", "unavailable"])
async def test_a_refused_attach_ends_with_the_way_out_and_no_retry(
    viewer: Any, access: str
):
    await connect(viewer)
    await show_the_sign_in(viewer)
    await wait_for_calls(viewer, "login_view_attach", 1)

    await answer(
        viewer,
        "login_view_attach",
        0,
        {"content": [{"type": "text", "text": json.dumps({"access": access})}]},
    )
    await viewer.wait_for_function("document.body.className.includes('ended')")
    await viewer.wait_for_timeout(1200)

    assert len(await calls(viewer, "login_view_attach")) == 1
    assert await calls(viewer, "login_view_frame") == []
    assert await viewer.locator("#fallback").is_visible()


async def test_an_attach_without_its_grant_ends_as_a_delivery_problem(viewer: Any):
    await connect(viewer)
    await show_the_sign_in(viewer)
    await wait_for_calls(viewer, "login_view_attach", 1)

    await answer(
        viewer,
        "login_view_attach",
        0,
        {"content": [{"type": "text", "text": json.dumps({"access": "granted"})}]},
    )
    await viewer.wait_for_function(
        "document.getElementById('text').textContent.startsWith('The chat did not hand')"
    )
    await viewer.wait_for_timeout(1200)

    assert len(await calls(viewer, "login_view_attach")) == 1
    assert await calls(viewer, "login_view_frame") == []
    assert await viewer.locator("#fallback").is_visible()


async def test_a_lost_attach_is_retried_three_times_then_ends(viewer: Any):
    await connect(viewer)
    await show_the_sign_in(viewer)
    for index in range(4):
        await wait_for_calls(viewer, "login_view_attach", index + 1)
        await fail(viewer, "login_view_attach", index)

    await viewer.wait_for_function(
        "document.getElementById('text').textContent.startsWith('The chat could not reach')"
    )
    await viewer.wait_for_timeout(2000)
    assert len(await calls(viewer, "login_view_attach")) == 4
    assert await calls(viewer, "login_view_frame") == []
    assert await viewer.locator("#fallback").is_visible()


async def test_teardown_during_an_attach_retry_wait_attaches_no_more(viewer: Any):
    await connect(viewer)
    await show_the_sign_in(viewer)
    await wait_for_calls(viewer, "login_view_attach", 1)
    await fail(viewer, "login_view_attach", 0)
    await viewer.wait_for_function(
        "document.getElementById('text').textContent.includes('Retrying')"
    )

    await viewer.evaluate("bridge.onteardown({}, {})", isolated_context=False)
    await viewer.wait_for_timeout(1200)

    assert len(await calls(viewer, "login_view_attach")) == 1
    assert await calls(viewer, "login_view_frame") == []


async def test_an_unreachable_sign_in_ends_the_view_and_polls_no_more(viewer: Any):
    await live(viewer)
    await wait_for_calls(viewer, "login_view_frame", 2)

    await answer(viewer, "login_view_frame", 1, frame(access="unavailable"))
    await viewer.wait_for_function("document.body.className.includes('ended')")
    await viewer.click("#help")
    await viewer.click("#help")
    await viewer.wait_for_timeout(600)

    assert "no longer reachable" in await viewer.locator("#text").inner_text()
    assert await viewer.locator("#screen").is_hidden()
    assert await viewer.locator("#fallback").is_visible()
    assert len(await calls(viewer, "login_view_frame")) == 2
    assert await calls(viewer, "login_view_input") == []


async def test_an_input_of_unknown_outcome_never_claims_it_was_lost(viewer: Any):
    await live(viewer)

    await viewer.click("#screen")
    await wait_for_calls(viewer, "login_view_input", 1)
    unknown = (
        "The page may have received part of that input; check it before typing again."
    )
    await answer(
        viewer,
        "login_view_input",
        0,
        {"isError": True, "content": [{"type": "text", "text": unknown}]},
    )
    await viewer.wait_for_function(
        "document.getElementById('text').textContent.startsWith('The page may')"
    )
    await viewer.wait_for_timeout(300)

    assert await viewer.locator("#text").inner_text() == unknown
    assert len(await calls(viewer, "login_view_input")) == 1


async def test_a_paste_is_sent_once_and_its_shortcut_is_not(viewer: Any):
    await live(viewer)
    await viewer.evaluate(
        """text => {
            const field = document.createElement('textarea');
            field.id = 'source';
            field.value = text;
            document.body.append(field);
            field.select();
        }""",
        "pässwörd ✓ 🔑",
    )
    await viewer.keyboard.press(f"{PASTE}+KeyC")
    await viewer.evaluate("document.getElementById('source').remove()")

    await viewer.click("#screen")
    await wait_for_calls(viewer, "login_view_input", 1)
    await answer(
        viewer, "login_view_input", 0, frame(access="full", input="applied", applied=1)
    )
    await viewer.keyboard.press(f"{PASTE}+KeyV")
    await wait_for_calls(viewer, "login_view_input", 2)
    await viewer.wait_for_timeout(150)

    sent = await calls(viewer, "login_view_input")
    assert [call["events"] for call in sent[1:]] == [
        [{"type": "paste", "text": "pässwörd ✓ 🔑"}]
    ]


async def test_input_waits_for_the_batch_in_flight_and_keeps_its_order(viewer: Any):
    await live(viewer)

    await viewer.click("#screen")
    await wait_for_calls(viewer, "login_view_input", 1)
    await viewer.keyboard.type("ab")
    await viewer.keyboard.press("Enter")
    await viewer.wait_for_timeout(150)
    assert len(await calls(viewer, "login_view_input")) == 1

    await answer(
        viewer, "login_view_input", 0, frame(access="full", input="applied", applied=1)
    )
    await wait_for_calls(viewer, "login_view_input", 2)

    first, second = await calls(viewer, "login_view_input")
    assert [event["type"] for event in first["events"]] == ["click"]
    assert first["page_id"] == "p1" and first["geometry"] == "800x600"
    assert second["events"] == [
        {"type": "type", "text": "ab"},
        {"type": "key", "key": "Enter", "modifiers": []},
    ]


async def test_input_queued_for_a_page_is_never_sent_to_the_page_that_replaced_it(
    viewer: Any,
):
    await live(viewer)
    await wait_for_calls(viewer, "login_view_frame", 2)

    # A click goes out on p1; text typed meanwhile, while p1 is still shown,
    # waits behind it. Then a popup p2 replaces p1 before the click returns.
    await viewer.click("#screen")
    await wait_for_calls(viewer, "login_view_input", 1)
    await viewer.keyboard.type("hunter2")
    popup = frame(
        access="full",
        state="signing_in",
        frame="image",
        hash="h2",
        page_id="p2",
        geometry="420x360",
    )
    await answer(viewer, "login_view_frame", 1, popup)
    await wait_for_calls(viewer, "login_view_frame", 3)
    await answer(
        viewer, "login_view_input", 0, frame(access="full", input="applied", applied=1)
    )
    await viewer.wait_for_function(
        "document.getElementById('text').textContent.startsWith('The page changed')"
    )

    # Input typed on p2 goes to p2.
    await viewer.keyboard.type("x")
    await wait_for_calls(viewer, "login_view_input", 2)

    first, second = await calls(viewer, "login_view_input")
    assert (first["page_id"], first["geometry"]) == ("p1", "800x600")
    assert (second["page_id"], second["geometry"]) == ("p2", "420x360")
    assert second["events"] == [{"type": "type", "text": "x"}]


async def test_teardown_stops_polling_and_drops_queued_input(viewer: Any):
    await live(viewer)
    await wait_for_calls(viewer, "login_view_frame", 2)
    # A click is in flight and text waits behind it when the host tears down.
    await viewer.click("#screen")
    await wait_for_calls(viewer, "login_view_input", 1)
    await viewer.keyboard.type("hunter2")

    await viewer.evaluate("bridge.onteardown({}, {})", isolated_context=False)
    await answer(
        viewer, "login_view_input", 0, frame(access="full", input="applied", applied=1)
    )
    await answer(
        viewer,
        "login_view_frame",
        1,
        frame(access="full", state="signing_in", frame="unchanged"),
    )
    await viewer.wait_for_timeout(300)

    assert len(await calls(viewer, "login_view_frame")) == 2
    sent = await calls(viewer, "login_view_input")
    assert [[e["type"] for e in call["events"]] for call in sent] == [["click"]]


async def focused(page: Any) -> str:
    return await page.evaluate("document.activeElement?.id ?? ''")


async def sent_keys(page: Any) -> list[str]:
    return [
        event["key"]
        for call in await calls(page, "login_view_input")
        for event in call["events"]
        if event["type"] == "key"
    ]


async def test_tab_reaches_the_help_before_the_page_image(viewer: Any):
    await live(viewer)
    # The help sits below the image on screen, so it must lead in the DOM.
    help_box = await viewer.locator("#help").bounding_box()
    screen_box = await viewer.locator("#screen").bounding_box()
    assert help_box is not None and screen_box is not None
    assert help_box["y"] >= screen_box["y"] + screen_box["height"]

    await viewer.keyboard.press("Tab")

    assert await focused(viewer) == "help"


async def test_escape_twice_leaves_the_page_for_the_help(viewer: Any):
    await live(viewer)
    await viewer.focus("#screen")
    assert await focused(viewer) == "screen"
    assert (
        await viewer.locator("#screen").get_attribute("aria-describedby")
        == "screen-keys"
    )
    assert "Escape twice" in await viewer.locator("#screen-keys").text_content()

    await viewer.keyboard.press("Escape")
    await viewer.keyboard.press("Escape")
    await wait_for_calls(viewer, "login_view_input", 1)
    assert await focused(viewer) == "help"

    await viewer.keyboard.press("Enter")
    assert await viewer.locator("#fallback").is_visible()

    # Let the first batch finish, then send a sentinel: whatever the queue held
    # behind the first Escape goes out before it.
    await answer(
        viewer, "login_view_input", 0, frame(access="full", input="applied", applied=1)
    )
    await viewer.focus("#screen")
    await viewer.keyboard.type("z")
    await wait_for_calls(viewer, "login_view_input", 2)
    sent = [
        event
        for call in await calls(viewer, "login_view_input")
        for event in call["events"]
    ]
    assert sent == [
        {"type": "key", "key": "Escape", "modifiers": []},
        {"type": "type", "text": "z"},
    ]


async def test_one_escape_and_tab_still_reach_the_page(viewer: Any):
    await live(viewer)
    await viewer.focus("#screen")

    await viewer.keyboard.press("Escape")
    await wait_for_calls(viewer, "login_view_input", 1)
    assert await focused(viewer) == "screen"
    await answer(
        viewer, "login_view_input", 0, frame(access="full", input="applied", applied=1)
    )
    await viewer.wait_for_timeout(1100)
    await viewer.keyboard.press("Tab")
    await wait_for_calls(viewer, "login_view_input", 2)

    assert await focused(viewer) == "screen"
    assert await sent_keys(viewer) == ["Escape", "Tab"]


async def test_the_way_out_of_the_chat_is_reachable_while_live_and_after_expiry(
    viewer: Any,
):
    await live(viewer)
    fallback = viewer.locator("#fallback")
    assert await fallback.is_hidden()

    await viewer.click("#help")
    assert await fallback.is_visible()
    assert "--login --login-viewer" in await fallback.inner_text()
    # The next live frame keeps it open.
    await wait_for_calls(viewer, "login_view_frame", 2)
    await answer(
        viewer,
        "login_view_frame",
        1,
        frame(access="full", state="signing_in", frame="unchanged"),
    )
    await wait_for_calls(viewer, "login_view_frame", 3)
    assert await fallback.is_visible()
    await viewer.click("#help")
    assert await fallback.is_hidden()

    await answer(viewer, "login_view_frame", 2, frame(access="expired"))
    await viewer.wait_for_function(
        "document.getElementById('text').textContent.includes('expired')"
    )
    assert await viewer.locator("#help").is_visible()
    assert await fallback.is_visible()
    await viewer.click("#help")
    assert await fallback.is_hidden()
    await viewer.click("#help")
    assert await fallback.is_visible()


async def test_a_signed_in_login_leaves_one_line_and_no_image(viewer: Any):
    await live(viewer)
    await wait_for_calls(viewer, "login_view_frame", 2)

    await answer(
        viewer,
        "login_view_frame",
        1,
        frame(access="status", state="signed_in", frame="none"),
    )
    await viewer.wait_for_function(
        "document.getElementById('text').textContent === 'Signed in to LinkedIn. You can close this.'"
    )
    await viewer.wait_for_timeout(150)

    assert await viewer.locator("#screen").is_hidden()
    assert await viewer.locator("#fallback").is_hidden()
    assert len(await calls(viewer, "login_view_frame")) == 2


async def test_an_error_result_shows_its_text_and_the_way_out(viewer: Any):
    await connect(viewer)
    await viewer.evaluate(
        """() => bridge.ontoolresult({ isError: true,
            content: [{ type: 'text', text: 'Mount a host directory and try again.' }] })""",
        isolated_context=False,
    )

    await viewer.wait_for_function(
        "document.getElementById('text').textContent === 'Mount a host directory and try again.'"
    )
    assert await viewer.locator("#fallback").is_visible()
    assert "--login --login-viewer" in await viewer.locator("#fallback").inner_text()
    assert await calls(viewer, "login_view_frame") == []


async def test_the_inlined_sdk_hands_the_page_its_app(browser_page: Any):
    # Framed, as a host frames it, so the SDK's handshake goes to a parent
    # that never answers and the page waits for its host.
    errors: list[str] = []
    browser_page.on("pageerror", lambda error: errors.append(str(error)))

    await _serve(browser_page, viewer_html(), host='<iframe src="/view.html"></iframe>')
    view = browser_page.frames[1]
    await view.wait_for_function(
        "typeof globalThis.linkedinLoginSdk?.App === 'function'"
    )
    await browser_page.wait_for_timeout(200)

    assert errors == []
    assert (
        await view.locator("#text").inner_text() == "Connecting to the sign-in browser."
    )
