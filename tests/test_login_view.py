"""The sign-in view's core: what a viewer sees of the login browser, and what reaches it.

The browser cases drive real headless Chromium on a synthetic page served from
a fake origin, so the field values are what Chromium itself produced. The rest
use a stand-in page whose screenshots and input are recorded, for the timing
and failure paths a real page cannot be made to produce on demand.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from patchright.async_api import Error as PlaywrightError
from patchright.async_api import async_playwright

from linkedin_mcp_server import login_view
from linkedin_mcp_server.login_view import (
    Click,
    Key,
    LoginAttempt,
    LoginView,
    Paste,
    TypeText,
)

ORIGIN = "https://login.invalid"

FORM = """<!doctype html>
<html><body style="margin:0">
<input id="email" style="position:absolute;left:0;top:0;width:300px;height:40px">
<input id="password" type="password" style="position:absolute;left:0;top:60px;width:300px;height:40px">
<button id="popup" style="position:absolute;left:0;top:120px;width:300px;height:40px"
  onclick="window.open('/popup', 'signin', 'width=420,height=360')">Open</button>
</body></html>"""

POPUP = """<!doctype html>
<html><body style="margin:0;background:#e0f0ff">
<input id="code" style="position:absolute;left:0;top:0;width:300px;height:40px">
</body></html>"""


def jpeg(width: int, height: int, tag: bytes = b"") -> bytes:
    """The smallest byte string the view reads as a JPEG of that size."""
    comment = b"\xff\xfe" + (len(tag) + 2).to_bytes(2, "big") + tag
    sof = (
        b"\xff\xc0"
        + (17).to_bytes(2, "big")
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03"
        + b"\x00" * 9
    )
    return b"\xff\xd8" + comment + sof + b"\xff\xd9"


class FakePage:
    """A page whose screenshots are scripted and whose input is recorded.

    Its viewport is the size of its first scripted screenshot unless
    *viewport* is set; :attr:`on_evaluate` runs while that size is read. A screenshot slower than the timeout it is given fails
    the way patchright's does, after that timeout.
    """

    def __init__(self, shots: list[bytes | Exception] | None = None) -> None:
        self.shots = shots or [jpeg(800, 600)]
        self.taken = 0
        self.closed = False
        self.received: list[tuple[str, Any]] = []
        self.gate: asyncio.Event | None = None
        self.render_s = 0.0
        #: Runs while the page reports its size, to act in the middle of that read.
        self.on_evaluate: Callable[[], Awaitable[None]] | None = None
        #: Runs after a click lands, as a page reacting to it would.
        self.on_click: Callable[[], None] | None = None
        first = next(shot for shot in self.shots if isinstance(shot, bytes))
        size = login_view._jpeg_size(first)
        assert size is not None
        self.viewport = size
        page = self

        class Mouse:
            async def click(self, x: float, y: float, *, button: str) -> None:
                page.received.append(("click", (x, y, button)))
                if page.on_click is not None:
                    page.on_click()

            async def move(self, x: float, y: float) -> None:
                page.received.append(("move", (x, y)))

            async def wheel(self, dx: float, dy: float) -> None:
                page.received.append(("wheel", (dx, dy)))

        class Keyboard:
            async def press(self, key: str) -> None:
                page.received.append(("press", key))

            async def type(self, text: str) -> None:
                page.received.append(("type", text))

            async def insert_text(self, text: str) -> None:
                page.received.append(("insert_text", text))

        self.mouse = Mouse()
        self.keyboard = Keyboard()

    def is_closed(self) -> bool:
        return self.closed

    async def evaluate(self, _expression: str) -> list[int]:
        if self.on_evaluate is not None:
            await self.on_evaluate()
        return list(self.viewport)

    async def screenshot(self, *, timeout: float, **_options: object) -> bytes:
        if self.gate is not None:
            await self.gate.wait()
        if self.render_s > timeout / 1000:
            await asyncio.sleep(timeout / 1000)
            raise PlaywrightError(f"Timeout {timeout}ms exceeded.")
        await asyncio.sleep(self.render_s)
        shot = self.shots[min(self.taken, len(self.shots) - 1)]
        self.taken += 1
        if isinstance(shot, Exception):
            raise shot
        return shot


class FakeContext:
    def __init__(self) -> None:
        self.listeners: list[Any] = []

    def on(self, _event: str, listener: Any) -> None:
        self.listeners.append(listener)

    def remove_listener(self, _event: str, listener: Any) -> None:
        self.listeners.remove(listener)

    def open(self, page: FakePage) -> None:
        for listener in list(self.listeners):
            listener(page)


def fake_view(page: FakePage | None = None) -> tuple[LoginView, FakePage, FakeContext]:
    page = page or FakePage()
    context = FakeContext()
    return LoginView(cast(Any, context), cast(Any, page)), page, context


def always() -> bool:
    return True


class TestFrames:
    async def test_a_new_viewer_gets_the_current_image_even_if_unchanged(self):
        view, _page, _ = fake_view()
        first = await view.frame(None, 1)
        assert first.status == "image" and first.frame is not None

        again = await view.frame(None, 1)

        assert again.status == "image"
        assert again.frame is not None and again.frame.digest == first.frame.digest
        view.close()

    async def test_an_unchanged_page_answers_unchanged_once_the_budget_runs_out(self):
        view, _page, _ = fake_view()
        first = await view.frame(None, 1)
        assert first.frame is not None

        result = await view.frame(first.frame.digest, 0.3)

        assert result.status == "unchanged" and result.frame is None
        view.close()

    async def test_a_change_returns_before_the_budget(self):
        page = FakePage(
            [jpeg(800, 600, b"a"), jpeg(800, 600, b"a"), jpeg(800, 600, b"b")]
        )
        view, _, _ = fake_view(page)
        first = await view.frame(None, 1)
        assert first.frame is not None

        started = asyncio.get_running_loop().time()
        changed = await view.frame(first.frame.digest, 3)

        assert changed.status == "image"
        assert changed.frame is not None and changed.frame.digest != first.frame.digest
        assert asyncio.get_running_loop().time() - started < 2
        view.close()

    async def test_a_failed_capture_is_transient_and_the_next_one_recovers(self):
        page = FakePage(
            [
                PlaywrightError("Target page, context or browser has been closed"),
                jpeg(640, 480),
            ]
        )
        view, _, _ = fake_view(page)

        assert (await view.frame(None, 1)).status == "transient"
        recovered = await view.frame(None, 1)

        assert recovered.status == "image"
        assert recovered.frame is not None and recovered.frame.geometry == "640x480"
        view.close()

    async def test_a_frame_too_large_even_at_lower_quality_is_dropped(self):
        huge = jpeg(800, 600, b"x" * 60000) * 14
        view, page, _ = fake_view(FakePage([huge]))

        result = await view.frame(None, 1)

        assert result.status == "too_large" and result.frame is None
        assert page.taken == 2  # re-taken once at the lower quality
        view.close()

    async def test_a_capture_slower_than_a_poll_reaches_the_next_poll(self):
        # A page that has just navigated on a loaded host renders for longer
        # than one poll waits. The capture keeps going, and a later poll for
        # the current image gets it.
        page = FakePage()
        page.render_s = 1.2
        view, _, _ = fake_view(page)

        first = await view.frame(None, 0.5)
        polls = 1
        result = first
        while result.status != "image" and polls < 8:
            result = await view.frame(None, 0.5)
            polls += 1

        assert first.status == "unchanged"
        assert result.status == "image"
        assert result.frame is not None and result.frame.geometry == "800x600"
        view.close()

    async def test_nobody_polling_stops_the_captures(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(login_view, "_IDLE_S", 0.3)
        view, page, _ = fake_view()
        assert (await view.frame(None, 1)).status == "image"

        await asyncio.sleep(0.8)
        taken = page.taken
        await asyncio.sleep(0.5)

        assert page.taken == taken
        view.close()

    async def test_switching_to_a_page_that_looks_the_same_is_a_new_frame(self):
        same = jpeg(800, 600, b"same")
        view, _, context = fake_view(FakePage([same]))
        opener = await view.frame(None, 1)
        assert opener.frame is not None and opener.frame.page_id == "p1"
        popup = FakePage([same])

        context.open(popup)
        switched = await view.frame(opener.frame.digest, 2)
        assert switched.status == "image"
        assert switched.frame is not None and switched.frame.page_id == "p2"

        popup.closed = True
        back = await view.frame(switched.frame.digest, 2)
        assert back.status == "image"
        assert back.frame is not None and back.frame.page_id == "p1"
        view.close()

    async def test_closing_the_view_ends_a_waiting_frame_request(self):
        page = FakePage()
        page.gate = asyncio.Event()
        view, _, _ = fake_view(page)
        waiting = asyncio.create_task(view.frame(None, 3))
        await asyncio.sleep(0.05)

        view.close()

        assert (await asyncio.wait_for(waiting, 1)).status == "closed"


class TestInput:
    async def _ready(self, view: LoginView) -> tuple[str, str]:
        result = await view.frame(None, 1)
        assert result.frame is not None
        return result.frame.page_id, result.frame.geometry

    async def test_events_land_in_order_with_coordinates_from_the_frame(self):
        view, page, _ = fake_view(FakePage([jpeg(801, 601)]))
        page_id, geometry = await self._ready(view)

        result = await view.apply(
            page_id,
            geometry,
            [
                Click(type="click", x=0.5, y=2.0),
                TypeText(type="type", text="dä"),
                Key(type="key", key="a", modifiers=["Meta"]),
                Paste(type="paste", text="pässwörd ✓ 🔑"),
            ],
            still_allowed=always,
        )

        assert result.status == "applied" and result.applied == 4
        assert page.received == [
            ("click", (400.0, 600.0, "left")),
            ("type", "dä"),
            ("press", "ControlOrMeta+a"),
            ("insert_text", "pässwörd ✓ 🔑"),
        ]
        view.close()

    async def test_input_for_a_changed_geometry_reaches_nothing(self):
        view, page, _ = fake_view(FakePage([jpeg(800, 600)]))
        page_id, _geometry = await self._ready(view)

        result = await view.apply(
            page_id, "1024x768", [TypeText(type="type", text="x")], still_allowed=always
        )

        assert result.status == "stale"
        assert page.received == []
        view.close()

    async def test_input_for_a_page_resized_since_its_frame_reaches_nothing(self):
        view, page, _ = fake_view(FakePage([jpeg(800, 600)]))
        page_id, geometry = await self._ready(view)
        page.viewport = (400, 300)

        result = await view.apply(
            page_id, geometry, [Click(type="click", x=0.5, y=0.5)], still_allowed=always
        )

        assert result.status == "stale"
        assert page.received == []
        view.close()

    async def test_a_popup_opened_while_the_size_is_read_gets_no_input(self):
        view, page, context = fake_view(FakePage([jpeg(800, 600)]))
        page_id, geometry = await self._ready(view)
        popup = FakePage([jpeg(420, 360)])

        async def popup_opens() -> None:
            context.open(popup)

        page.on_evaluate = popup_opens

        result = await view.apply(
            page_id,
            geometry,
            [TypeText(type="type", text="secret")],
            still_allowed=always,
        )

        assert result.status == "stale"
        assert page.received == []
        assert popup.received == []
        view.close()

    async def test_a_grant_replaced_while_the_size_is_read_sends_nothing(self):
        view, page, _ = fake_view()
        page_id, geometry = await self._ready(view)
        grant = {"live": True}

        async def replaced_meanwhile() -> None:
            grant["live"] = False

        page.on_evaluate = replaced_meanwhile

        result = await view.apply(
            page_id,
            geometry,
            [TypeText(type="type", text="secret")],
            still_allowed=lambda: grant["live"],
        )

        assert (result.status, result.applied) == ("refused", 0)
        assert page.received == []
        view.close()

    async def test_a_grant_replaced_mid_batch_stops_the_rest(self):
        view, page, _ = fake_view()
        page_id, geometry = await self._ready(view)

        result = await view.apply(
            page_id,
            geometry,
            [
                Click(type="click", x=0.5, y=0.5),
                Paste(type="paste", text="secret"),
            ],
            # Replaced while the click was dispatched.
            still_allowed=lambda: page.received == [],
        )

        assert (result.status, result.applied) == ("refused", 1)
        assert [kind for kind, _ in page.received] == ["click"]
        view.close()

    async def test_a_popup_the_first_event_opens_gets_none_of_the_rest(self):
        view, page, context = fake_view(FakePage([jpeg(800, 600)]))
        page_id, geometry = await self._ready(view)
        popup = FakePage([jpeg(420, 360)])
        page.on_click = lambda: context.open(popup)

        result = await view.apply(
            page_id,
            geometry,
            [
                Click(type="click", x=0.5, y=0.5),
                Paste(type="paste", text="secret"),
            ],
            still_allowed=always,
        )

        assert (result.status, result.applied) == ("stale", 1)
        assert [kind for kind, _ in page.received] == ["click"]
        assert popup.received == []
        view.close()

    async def test_more_text_than_one_batch_carries_is_refused_whole(self):
        view, page, _ = fake_view()
        page_id, geometry = await self._ready(view)

        result = await view.apply(
            page_id,
            geometry,
            [
                Paste(type="paste", text="a" * 3000),
                Paste(type="paste", text="b" * 2000),
            ],
            still_allowed=always,
        )

        assert result.status == "too_long"
        assert page.received == []
        view.close()


class TestAttempt:
    async def test_the_attempt_settles_from_the_task_result(self):
        async def fails() -> None:
            raise RuntimeError("the login browser could not start")

        async def succeeds_without_a_browser() -> None:
            return None

        async def never() -> None:
            await asyncio.Event().wait()

        failed, peer, cancelled = LoginAttempt(), LoginAttempt(), LoginAttempt()
        tasks = [
            failed.start(fails(), name="t1"),
            peer.start(succeeds_without_a_browser(), name="t2"),
            cancelled.start(never(), name="t3"),
        ]
        await asyncio.sleep(0)
        tasks[2].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        assert (failed.state, failed.reason) == (
            "failed",
            "the login browser could not start",
        )
        assert peer.state == "repaired_by_peer"
        assert cancelled.state == "cancelled"

    async def test_a_login_publishes_its_browser_only_while_it_holds_it(self):
        page, context = FakePage(), FakeContext()
        browser = SimpleNamespace(page=page, context=context)
        inside = asyncio.Event()
        leave = asyncio.Event()

        async def login() -> None:
            async with login_view.published(cast(Any, browser)):
                inside.set()
                await leave.wait()

        attempt = LoginAttempt()
        task = attempt.start(login(), name="login")
        await inside.wait()
        assert attempt.view is not None and attempt.state == "signing_in"

        leave.set()
        await task

        assert attempt.view is None
        assert attempt.state == "signed_in"
        assert context.listeners == []

    async def test_a_login_without_an_attempt_publishes_nothing(self):
        # `--login` at a terminal: nothing about the browser is read.
        async with login_view.published(cast(Any, object())):
            pass


async def test_the_login_closes_the_view_before_its_browser_tears_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A view polling while the login browser closes gets nothing from it."""
    import linkedin_mcp_server.setup as setup

    attempt = LoginAttempt()
    seen_at_teardown: list[object] = []

    class Manager:
        async def __aenter__(self) -> Any:
            return SimpleNamespace(page=FakePage(), context=FakeContext())

        async def __aexit__(self, *_args: object) -> None:
            seen_at_teardown.append(attempt.view)

    async def navigation_failed(*_args: object, **_kwargs: object) -> None:
        raise ValueError("navigation failed")

    monkeypatch.setattr(setup, "goto_reporting_proxy_errors", navigation_failed)

    async def login() -> None:
        await setup._run_login(
            cast(setup.BrowserManager, Manager()),
            tmp_path / "profile",
            SimpleNamespace(),
            0,
        )

    task = attempt.start(login(), name="login")
    with pytest.raises(ValueError):
        await task

    assert seen_at_teardown == [None]
    assert attempt.state == "failed"


# ---------------------------------------------------------------------------
# Real Chromium
# ---------------------------------------------------------------------------


@pytest.fixture
async def chromium():
    """A page on the synthetic form and its context, or a skip."""
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            context = await browser.new_context(viewport={"width": 800, "height": 600})
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")

        async def serve(route: Any) -> None:
            path = route.request.url.removeprefix(ORIGIN)
            body = POPUP if path.startswith("/popup") else FORM
            await route.fulfill(content_type="text/html", body=body)

        try:
            await context.route("**/*", serve)
            page = await context.new_page()
            page.set_default_timeout(5000)
            await page.goto(f"{ORIGIN}/login")
            yield context, page
        finally:
            await browser.close()


async def _frame_of(view: LoginView, page_id: str) -> tuple[str, str]:
    for _ in range(20):
        result = await view.frame(None, 1)
        if result.frame is not None and result.frame.page_id == page_id:
            return result.frame.page_id, result.frame.geometry
        await asyncio.sleep(0.1)
    raise AssertionError(f"no frame of {page_id}")


def _centre_of(box: dict[str, float], geometry: str) -> tuple[float, float]:
    width, height = (int(n) for n in geometry.split("x"))
    return (
        (box["x"] + box["width"] / 2) / (width - 1),
        (box["y"] + box["height"] / 2) / (height - 1),
    )


@pytest.mark.browser_dom
@pytest.mark.xdist_group("browser_runtime")
class TestInChromium:
    async def test_typed_and_pasted_text_arrive_exactly(self, chromium):
        context, page = chromium
        view = LoginView(context, page)
        page_id, geometry = await _frame_of(view, "p1")
        assert geometry == "800x600"
        long_paste = "pässwörd ✓ 🔑 " * 100  # 1400 characters

        email_box = await page.locator("#email").bounding_box()
        password_box = await page.locator("#password").bounding_box()
        assert email_box is not None and password_box is not None
        ex, ey = _centre_of(email_box, geometry)
        px, py = _centre_of(password_box, geometry)
        result = await view.apply(
            page_id,
            geometry,
            [
                Click(type="click", x=ex, y=ey),
                TypeText(type="type", text="dä@x.y"),
                Click(type="click", x=px, y=py),
                Paste(type="paste", text=long_paste),
            ],
            still_allowed=always,
        )

        assert result.status == "applied"
        assert await page.locator("#email").input_value() == "dä@x.y"
        assert await page.locator("#password").input_value() == long_paste
        view.close()

    async def test_concurrent_batches_never_interleave(self, chromium):
        context, page = chromium
        view = LoginView(context, page)
        page_id, geometry = await _frame_of(view, "p1")
        await page.locator("#email").focus()

        await asyncio.gather(
            view.apply(
                page_id,
                geometry,
                [TypeText(type="type", text="aaaaaaaa")],
                still_allowed=always,
            ),
            view.apply(
                page_id,
                geometry,
                [TypeText(type="type", text="bbbbbbbb")],
                still_allowed=always,
            ),
        )

        assert await page.locator("#email").input_value() in {
            "aaaaaaaabbbbbbbb",
            "bbbbbbbbaaaaaaaa",
        }
        view.close()

    async def test_a_resize_after_the_frame_refuses_the_input(self, chromium):
        context, page = chromium
        view = LoginView(context, page)
        page_id, geometry = await _frame_of(view, "p1")
        assert geometry == "800x600"
        await page.evaluate(
            "window.clicks = []; addEventListener('click', e => clicks.push([e.clientX, e.clientY]))"
        )

        await page.set_viewport_size({"width": 400, "height": 300})
        result = await view.apply(
            page_id, geometry, [Click(type="click", x=0.5, y=0.5)], still_allowed=always
        )

        assert result.status == "stale"
        assert await page.evaluate("window.clicks") == []
        view.close()

    async def test_a_popup_is_viewed_while_open_and_stale_input_reaches_nothing(
        self, chromium
    ):
        context, page = chromium
        view = LoginView(context, page)
        opener_id, opener_geometry = await _frame_of(view, "p1")

        async with page.expect_popup() as popup_info:
            await page.locator("#popup").click()
        popup = await popup_info.value
        await popup.wait_for_load_state()
        popup_id, popup_geometry = await _frame_of(view, "p2")

        # The opener is no longer the viewed page, so input for it is refused.
        await page.locator("#email").focus()
        stale = await view.apply(
            opener_id,
            opener_geometry,
            [TypeText(type="type", text="x")],
            still_allowed=always,
        )
        assert stale.status == "stale"
        assert await page.locator("#email").input_value() == ""

        await popup.locator("#code").focus()
        typed = await view.apply(
            popup_id,
            popup_geometry,
            [TypeText(type="type", text="123456")],
            still_allowed=always,
        )
        assert typed.status == "applied"
        assert await popup.locator("#code").input_value() == "123456"

        await popup.close()
        assert await _frame_of(view, "p1") == (opener_id, opener_geometry)
        view.close()
