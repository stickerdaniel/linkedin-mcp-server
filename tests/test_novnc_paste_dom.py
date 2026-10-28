"""Browser-DOM tests for the login viewer's paste add-on, ``scripts/novnc-paste.js``.

These cases load the add-on as the module `vnc.html` loads, in headless
chromium, against stand-ins for the three noVNC modules it imports: a connected
RFB that records the keys it is asked to send, noVNC's keyboard, which presses a
key that reaches it on the remote side, and keysym tables for plain ASCII.
A synthetic origin serves every file and any other request is refused, so
nothing leaves the browser.

Each Ctrl+V types its text once: from the browser's paste event, or, when none
comes in time, from ``navigator.clipboard.readText()``, stubbed here. Synthetic
key events have no default action, so a paste event fires only where a case
dispatches one. Skipped automatically when chromium is not installed; run
locally after ``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from patchright.async_api import Page, Route, async_playwright

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' wall-clock
#: timers.
#: Without that distribution mode the group mark is inert.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

_ADD_ON = Path(__file__).resolve().parents[1] / "scripts" / "novnc-paste.js"
_ORIGIN = "https://viewer.invalid"

# Shared through the DOM, which the page and patchright's isolated evaluation
# world both see: the text the stand-in RFB typed, each key pressed or released
# on the remote side with how much text was typed by then, how often the
# clipboard was read, and what that read returns, whether it is refused, and
# whether it is held; and whether the keyboard holds Control back as noVNC does
# on Windows.
_VIEWER = """<!DOCTYPE html>
<html><head>
<script type="module">
  import UI from "./app/ui.js";
  const state = document.documentElement.dataset;
  Object.assign(state, { typed: "", keys: "[]", reads: "0", clipboard: "", refused: "", held: "", altgr: "" });
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: {
      // A held read answers, with the clipboard as it was, once the hold ends,
      // as a read waiting on a permission prompt does.
      readText() {
        state.reads = String(Number(state.reads) + 1);
        if (state.refused) {
          return Promise.reject(new DOMException("Read permission denied.", "NotAllowedError"));
        }
        const text = state.clipboard;
        return new Promise(resolve => {
          const answer = () => (state.held ? setTimeout(answer, 10) : resolve(text));
          answer();
        });
      },
    },
  });
  const record = (code, down) => {
    const keys = JSON.parse(state.keys);
    keys.push([code, down, state.typed.length]);
    state.keys = JSON.stringify(keys);
  };
  // noVNC's keyboard: a key that reaches it is pressed on the remote side. On
  // Windows a ControlLeft press waits 100 ms in case AltGr follows.
  document.addEventListener("keydown", event => {
    if (event.code === "ControlLeft" && state.altgr) setTimeout(() => record(event.code, true), 100);
    else record(event.code, true);
  });
  const newRfb = () => Object.assign(new EventTarget(), {
    viewOnly: false,
    // A press and release, as the add-on sends each character, or one of them.
    sendKey(keysym, code, down) {
      if (down === undefined) {
        state.typed += keysym === 0xff0d ? "\\n" : keysym === 0xff09 ? "\\t" : String.fromCodePoint(keysym);
      } else {
        record(code, down);
      }
    },
  });
  UI.connected = true;
  UI.rfb = newRfb();
  document.addEventListener("viewer-test-connection", event => {
    if (event.detail === "disconnect") {
      UI.connected = false;
      UI.rfb.dispatchEvent(new Event("disconnect"));
    } else {
      if (event.detail === "replace") UI.rfb = newRfb();
      UI.connected = true;
    }
  });
</script>
<script type="module" src="paste.js"></script>
</head><body></body></html>
"""

_MODULES = {
    "/app/ui.js": "export default { connected: false, rfb: undefined };",
    "/core/input/keysym.js": """export default {
  XK_Return: 0xff0d, XK_Tab: 0xff09,
  XK_Shift_L: 0xffe1, XK_Shift_R: 0xffe2, XK_Control_L: 0xffe3, XK_Control_R: 0xffe4,
  XK_Super_L: 0xffeb, XK_Super_R: 0xffec,
};""",
    # Latin-1 keysyms are the code points themselves.
    "/core/input/keysymdef.js": "export default { lookup: cp => cp };",
}

_CONTROL = """() => document.body.dispatchEvent(new KeyboardEvent("keydown", {
  key: "Control", code: "ControlLeft", ctrlKey: true, bubbles: true, cancelable: true,
}))"""

_CTRL_V = """() => document.body.dispatchEvent(new KeyboardEvent("keydown", {
  key: "v", code: "KeyV", ctrlKey: true, bubbles: true, cancelable: true,
}))"""

_PASTE = """text => {
  const clipboardData = new DataTransfer();
  clipboardData.setData("text/plain", text);
  document.body.dispatchEvent(new ClipboardEvent("paste", {
    clipboardData, bubbles: true, cancelable: true,
  }));
}"""

# While the add-on types, it keeps every key from the page; once it is done, a
# key goes through again.
_IDLE = """() => document.body.dispatchEvent(new KeyboardEvent("keyup", {
  key: "Shift", code: "ShiftLeft", bubbles: true, cancelable: true,
}))"""

#: Well past the add-on's 300 ms wait for a paste event, so a fallback that
#: would read the clipboard has done so.
_SETTLE_MS = 1000


@pytest.fixture
async def viewer():
    files = {
        "/vnc.html": ("text/html", _VIEWER),
        "/paste.js": ("text/javascript", _ADD_ON.read_text(encoding="utf-8")),
        **{path: ("text/javascript", body) for path, body in _MODULES.items()},
    }

    async def serve(route: Route) -> None:
        url = urlsplit(route.request.url)
        found = files.get(url.path) if url.netloc == "viewer.invalid" else None
        if found is None:
            await route.abort()
            return
        content_type, body = found
        await route.fulfill(content_type=content_type, body=body)

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            await page.route("**/*", serve)
            await page.goto(f"{_ORIGIN}/vnc.html")
            yield page
        finally:
            await browser.close()


async def _state(page: Page, name: str) -> str:
    return await page.evaluate(f"document.documentElement.dataset.{name}")


async def _set_clipboard(
    page: Page, text: str, *, refused: bool = False, held: bool = False
) -> None:
    await page.evaluate(
        """([text, refused, held]) => Object.assign(document.documentElement.dataset, {
          clipboard: text, refused: refused ? "1" : "", held: held ? "1" : "",
        })""",
        [text, refused, held],
    )


async def _settle(page: Page) -> None:
    await page.wait_for_timeout(_SETTLE_MS)
    await page.wait_for_function(_IDLE)


async def test_a_paste_right_after_a_fallback_is_typed(viewer):
    """An email typed by the fallback, then a password pasted within seconds."""
    await _set_clipboard(viewer, "user@example.com")
    await viewer.evaluate(_CTRL_V)  # no paste event: the clipboard is read
    await viewer.wait_for_function(
        "document.documentElement.dataset.typed === 'user@example.com'"
    )
    await viewer.wait_for_function(_IDLE)

    await _set_clipboard(viewer, "correct horse")
    await viewer.evaluate(_CTRL_V)
    await viewer.evaluate(_PASTE, "correct horse")
    await _settle(viewer)

    assert await _state(viewer, "typed") == "user@example.comcorrect horse"
    # The second Ctrl+V's own paste event cancelled its fallback.
    assert await _state(viewer, "reads") == "1"


@pytest.mark.parametrize("next_ctrl_v", [True, False], ids=["next", "same"])
async def test_a_refused_clipboard_read_holds_back_no_paste(viewer, next_ctrl_v):
    """A refused read typed nothing, so the paste event after it still types.

    Either the next Ctrl+V's, or this Ctrl+V's own when it comes late.
    """
    await _set_clipboard(viewer, "", refused=True)
    await viewer.evaluate(_CTRL_V)
    await viewer.wait_for_function("document.documentElement.dataset.reads === '1'")

    if next_ctrl_v:
        await viewer.evaluate(_CTRL_V)
    await viewer.evaluate(_PASTE, "correct horse")
    await _settle(viewer)

    assert await _state(viewer, "typed") == "correct horse"


async def test_a_read_that_answers_after_the_next_ctrl_v_is_not_typed(viewer):
    """A fallback still reading when Ctrl+V comes again gives way to it.

    Typed late, its text would land wherever the person had moved on to.
    """
    await _set_clipboard(viewer, "user@example.com", held=True)
    await viewer.evaluate(_CTRL_V)  # no paste event: the read starts and waits
    await viewer.wait_for_function("document.documentElement.dataset.reads === '1'")

    await viewer.evaluate(_CTRL_V)
    await viewer.evaluate(_PASTE, "correct horse")
    await _settle(viewer)
    await _set_clipboard(viewer, "correct horse")  # the first read answers now
    await _settle(viewer)

    assert await _state(viewer, "typed") == "correct horse"


async def test_a_late_paste_event_after_the_fallback_is_not_typed_again(viewer):
    await _set_clipboard(viewer, "correct horse")
    await viewer.evaluate(_CTRL_V)
    await viewer.wait_for_function(
        "document.documentElement.dataset.typed === 'correct horse'"
    )
    await viewer.wait_for_function(_IDLE)

    # The same Ctrl+V's paste event, arriving after its fallback typed the text.
    await viewer.evaluate(_PASTE, "correct horse")
    await _settle(viewer)

    assert await _state(viewer, "typed") == "correct horse"


async def test_a_paste_with_no_ctrl_v_before_it_is_typed(viewer):
    """The browser's own Paste menu, after a Ctrl+V whose paste event came."""
    await viewer.evaluate(_CTRL_V)
    await viewer.evaluate(_PASTE, "user@example.com")
    await viewer.wait_for_function(_IDLE)

    await viewer.evaluate(_PASTE, "correct horse")
    await _settle(viewer)

    assert await _state(viewer, "typed") == "user@example.comcorrect horse"
    assert await _state(viewer, "reads") == "0"


async def test_pastes_received_while_typing_are_queued_in_order(viewer):
    """Browser-menu pastes arrive even while key events are blocked."""
    await viewer.clock.install()
    first = "a" * 100
    await viewer.evaluate(_CTRL_V)
    await viewer.evaluate(_PASTE, first)
    assert await _state(viewer, "typed") == "a"

    await viewer.evaluate(_PASTE, "second\r\nline\n")
    await viewer.evaluate(_PASTE, "third")
    await viewer.clock.run_for(5000)

    assert await _state(viewer, "typed") == first + "second\nlinethird"
    assert await _state(viewer, "reads") == "0"


@pytest.mark.parametrize("connection", ["disconnect", "replace"])
@pytest.mark.parametrize("paste_immediately", [False, True])
async def test_a_connection_change_drops_pending_pastes(
    viewer, connection, paste_immediately
):
    """Neither active nor queued text may reach the next connection."""
    await viewer.clock.install()
    await viewer.evaluate(_PASTE, "a" * 100)
    await viewer.evaluate(_PASTE, "queued text")
    await viewer.clock.run_for(40)

    # Read the boundary in the same page task, before another timer can type.
    already_typed = await viewer.evaluate(
        """connection => {
          const alreadyTyped = document.documentElement.dataset.typed;
          document.dispatchEvent(new CustomEvent("viewer-test-connection", {detail: connection}));
          if (connection === "disconnect") {
            // Reconnect before the pending 20 ms sleep ends, even with the
            // same RFB: checking only the current connected flag misses it.
            document.dispatchEvent(new CustomEvent("viewer-test-connection", {detail: "reconnect"}));
          }
          return alreadyTyped;
        }""",
        connection,
    )
    if paste_immediately:
        await viewer.evaluate(_PASTE, "fresh text")
    await viewer.clock.run_for(5000)

    assert await _state(viewer, "typed") == already_typed + (
        "fresh text" if paste_immediately else ""
    )
    if not paste_immediately:
        await viewer.evaluate(_PASTE, "fresh text")
        await viewer.clock.run_for(1000)
        assert await _state(viewer, "typed") == already_typed + "fresh text"


@pytest.mark.parametrize("connection", ["disconnect", "replace"])
@pytest.mark.parametrize("read_started", [False, True])
async def test_a_connection_change_cancels_a_clipboard_attempt(
    viewer, connection, read_started
):
    """A pending shortcut must not read or deliver text to another connection."""
    await viewer.clock.install()
    await _set_clipboard(viewer, "stale clipboard", held=True)
    await viewer.evaluate(_CTRL_V)
    if read_started:
        await viewer.clock.run_for(400)
        assert await _state(viewer, "reads") == "1"

    await viewer.evaluate(
        """connection => {
          document.dispatchEvent(new CustomEvent("viewer-test-connection", {detail: connection}));
          if (connection === "disconnect") {
            document.dispatchEvent(new CustomEvent("viewer-test-connection", {detail: "reconnect"}));
          }
        }""",
        connection,
    )
    await viewer.clock.run_for(1000)
    assert await _state(viewer, "reads") == ("1" if read_started else "0")

    await _set_clipboard(viewer, "fresh text")
    await viewer.clock.run_for(1000)
    assert await _state(viewer, "typed") == ""

    # The old shortcut's paste event can arrive after the connection changed too.
    await viewer.evaluate(_PASTE, "stale paste event")
    await viewer.clock.run_for(1000)
    assert await _state(viewer, "typed") == ""

    await viewer.evaluate(_CTRL_V)
    await viewer.evaluate(_PASTE, "fresh text")
    await viewer.clock.run_for(1000)
    assert await _state(viewer, "typed") == "fresh text"


@pytest.mark.parametrize("connection", ["disconnect", "replace"])
async def test_a_fresh_menu_paste_survives_after_a_cancelled_attempt_expires(
    viewer, connection
):
    """Late read results stay cancelled without swallowing a later menu paste."""
    await viewer.clock.install()
    await _set_clipboard(viewer, "stale clipboard", held=True)
    await viewer.evaluate(_CTRL_V)
    await viewer.clock.run_for(400)
    assert await _state(viewer, "reads") == "1"

    await viewer.evaluate(
        """connection => {
          document.dispatchEvent(new CustomEvent("viewer-test-connection", {detail: connection}));
          if (connection === "disconnect") {
            document.dispatchEvent(new CustomEvent("viewer-test-connection", {detail: "reconnect"}));
          }
        }""",
        connection,
    )
    await viewer.clock.run_for(6000)
    await viewer.evaluate(_PASTE, "fresh menu paste")
    await viewer.clock.run_for(1000)
    assert await _state(viewer, "typed") == "fresh menu paste"

    await _set_clipboard(viewer, "irrelevant")
    await viewer.clock.run_for(1000)
    assert await _state(viewer, "typed") == "fresh menu paste"


async def test_control_goes_up_before_the_first_character(viewer):
    """Control reached noVNC on its own, so the remote side holds it down.

    Typed under it, the text would be a row of shortcuts. The V never reaches
    noVNC at all.
    """
    await viewer.evaluate(_CONTROL)
    await viewer.evaluate(_CTRL_V)
    await viewer.evaluate(_PASTE, "correct horse")
    await _settle(viewer)

    assert await _state(viewer, "typed") == "correct horse"
    assert json.loads(await _state(viewer, "keys")) == [
        ["ControlLeft", True, 0],
        ["ControlLeft", False, 0],
    ]


async def test_control_held_back_for_altgr_goes_up_before_the_first_character(
    viewer,
):
    """noVNC on Windows sends a ControlLeft press 100 ms late, in case AltGr
    follows. A release sent before it would leave Control down for the text."""
    await viewer.evaluate("document.documentElement.dataset.altgr = '1'")
    await viewer.evaluate(_CONTROL)
    await viewer.evaluate(_CTRL_V)
    await viewer.evaluate(_PASTE, "correct horse")
    await _settle(viewer)

    assert await _state(viewer, "typed") == "correct horse"
    assert json.loads(await _state(viewer, "keys")) == [
        ["ControlLeft", True, 0],
        ["ControlLeft", False, 0],
    ]


async def test_a_modifier_released_before_the_paste_is_not_released_again(viewer):
    """Only what is still down goes up: a browser-menu paste after Control was
    let go sends no key at all."""
    await viewer.evaluate(_CONTROL)
    await viewer.evaluate(
        """() => document.body.dispatchEvent(new KeyboardEvent("keyup", {
          key: "Control", code: "ControlLeft", bubbles: true, cancelable: true,
        }))"""
    )
    await viewer.evaluate(_PASTE, "correct horse")
    await _settle(viewer)

    assert await _state(viewer, "typed") == "correct horse"
    assert json.loads(await _state(viewer, "keys")) == [["ControlLeft", True, 0]]


async def test_a_control_character_is_skipped(viewer):
    await viewer.evaluate(_CTRL_V)
    await viewer.evaluate(_PASTE, "correct\x07horse")
    await _settle(viewer)

    assert await _state(viewer, "typed") == "correcthorse"


async def test_a_long_paste_types_its_first_thousand_characters(viewer):
    # Each character waits 20 ms, so the clock is the page's own, run forward.
    await viewer.clock.install()
    text = "".join(chr(ord("a") + i % 26) for i in range(1500))
    await viewer.evaluate(_CTRL_V)
    await viewer.evaluate(_PASTE, text)
    await viewer.clock.run_for(60_000)

    assert await _state(viewer, "typed") == text[:1000]
