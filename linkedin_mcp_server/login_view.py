"""The login browser's page as images, and the user's input back into it.

The login stays with the login workflow: ``setup._run_login`` owns the browser
from launch to teardown. While a login that ``bootstrap`` started runs, it
publishes its open browser here with :func:`published`, and a sign-in view
borrows it. :meth:`LoginView.frame` returns JPEG screenshots of the page the
user signs in on, and :meth:`LoginView.apply` replays their clicks, keys and
text into it. Neither touches the runtime browser, the profile lease, rotation
or the session files.

A :class:`LoginAttempt` is one run of the shared login task. The bootstrap
coordinator creates it when it claims that task and starts the task through
:meth:`LoginAttempt.start`, which settles the attempt from the task's result.
A view therefore learns how the login ended even when no browser ever opened.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
import secrets
import time
from collections.abc import AsyncGenerator, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from patchright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)

#: The app-only tools that read and drive a published login browser. They reach
#: nothing else, so the browser serialization and the update notice leave them
#: out; one spelling here keeps those exemptions and the registration in step.
LOGIN_VIEW_TOOLS = frozenset(
    {"login_view_attach", "login_view_frame", "login_view_input", "login_view_release"}
)

Outcome = Literal["signed_in", "restricted", "failed", "cancelled", "repaired_by_peer"]
State = Literal[
    "signing_in", "signed_in", "restricted", "failed", "cancelled", "repaired_by_peer"
]

DEFAULT_BUDGET_S = 1.5
MAX_BUDGET_S = 3.0
MAX_EVENTS = 64
MAX_TEXT = 4096
#: Measured on the encoded form, which is what a tool result carries.
MAX_IMAGE_BASE64 = 768 * 1024

_QUALITY = 60
_FALLBACK_QUALITY = 40
_CAPTURE_SPACING_S = 0.1
#: How long one screenshot may take. Fixed rather than tied to a poll's budget:
#: a page that has just navigated on a loaded machine can take seconds to
#: render, and a capture cut off at every poll's deadline never finishes.
_CAPTURE_TIMEOUT_S = 10.0
#: The capture worker stops once no poll has waited for this long.
_IDLE_S = 3.0
_MAX_WHEEL = 5000.0
_MAX_REASON = 300

#: Keys a view may press by name. Everything else arrives as text, or as one
#: character with a modifier for a shortcut.
_NAMED_KEYS = frozenset(
    {
        "Enter",
        "Tab",
        "Backspace",
        "Delete",
        "Escape",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "Home",
        "End",
        "PageUp",
        "PageDown",
    }
)


class LoginBrowser(Protocol):
    """What :func:`published` reads from the login's ``BrowserManager``."""

    @property
    def page(self) -> Page: ...

    @property
    def context(self) -> BrowserContext: ...


class Click(BaseModel):
    """A click at a point given as fractions of the frame, clamped to it."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["click"]
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)
    button: Literal["left", "middle", "right"] = "left"


class Wheel(BaseModel):
    """A vertical scroll of *dy* CSS pixels over a point of the frame."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["wheel"]
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)
    dy: float = Field(allow_inf_nan=False)


class Key(BaseModel):
    """A named key, or one character with modifiers for a shortcut.

    ``Meta`` presses Control on Linux and Command on macOS, so a shortcut a Mac
    user types does what they meant in the login browser.
    """

    model_config = ConfigDict(extra="forbid")
    type: Literal["key"]
    key: str = Field(min_length=1, max_length=16)
    modifiers: list[Literal["Alt", "Control", "Meta", "Shift"]] = Field(
        default_factory=list, max_length=4
    )

    @field_validator("key")
    @classmethod
    def _a_key_the_page_can_press(cls, key: str) -> str:
        if key in _NAMED_KEYS or len(key) == 1:
            return key
        raise ValueError("not a supported key")


class TypeText(BaseModel):
    """Characters typed one by one, as key events."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["type"]
    text: str = Field(min_length=1, max_length=MAX_TEXT)


class Paste(BaseModel):
    """Text inserted at once, the way a paste lands, for any UTF-8 text."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["paste"]
    text: str = Field(min_length=1, max_length=MAX_TEXT)


InputEvent = Annotated[
    Click | Wheel | Key | TypeText | Paste, Field(discriminator="type")
]


@dataclass(frozen=True, slots=True)
class Frame:
    """One screenshot of the viewed page.

    *geometry* is the page's CSS viewport as ``WIDTHxHEIGHT``, read from the
    image itself, and input has to name it: a click is placed by the frame the
    user saw, so a page that resized since then refuses it.
    """

    digest: str
    page_id: str
    geometry: str
    jpeg: bytes


FrameStatus = Literal["image", "unchanged", "transient", "too_large", "closed"]


@dataclass(frozen=True, slots=True)
class FrameResult:
    status: FrameStatus
    frame: Frame | None = None


InputStatus = Literal[
    "applied", "stale", "refused", "closed", "interrupted", "too_long"
]


@dataclass(frozen=True, slots=True)
class InputResult:
    status: InputStatus
    applied: int = 0


@dataclass(frozen=True, slots=True)
class _Capture:
    frame: Frame | None
    failure: Literal["transient", "too_large"] | None = None


class LoginView:
    """The pages of one published login browser, as a view sees them.

    Every page gets a stable id in the order it opened, the opener first. The
    viewed page is the newest one still open, so a sign-in popup shows while it
    is open and the opener returns when it closes.

    One capture worker runs while anybody polls for a frame and shares each
    screenshot with every waiter; each caller compares it against its own
    previous frame. A capture may outlast the poll that started it, and the
    next poll gets its result. The worker stops once no poll has waited for
    :data:`_IDLE_S`. Input batches run one at a time, in arrival order.
    """

    def __init__(self, context: BrowserContext, opener: Page) -> None:
        self._context = context
        self._pages: list[tuple[str, Page]] = [("p1", opener)]
        self._opened = 1
        self._latest: _Capture | None = None
        self._captures = 0
        self._pulse = asyncio.Event()
        self._waiting = 0
        self._last_watched = 0.0
        self._worker: asyncio.Task[None] | None = None
        self._input = asyncio.Lock()
        self._closed = False
        context.on("page", self._page_opened)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """End every read and write into the login browser, at once.

        Synchronous, because the login calls it from inside its own browser
        context right before teardown, and it must not wait for a capture the
        closing browser may never answer.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._context.remove_listener("page", self._page_opened)
        except Exception:
            logger.debug("Could not stop watching the login pages", exc_info=True)
        if self._worker is not None:
            self._worker.cancel()
        self._wake()

    async def frame(self, after: str | None, budget_s: float) -> FrameResult:
        """The viewed page once it differs from the frame digested *after*.

        Waits up to *budget_s* (capped at :data:`MAX_BUDGET_S`) and then answers
        ``unchanged``. ``after=None`` asks for the current image, changed or not.
        A capture that fails while the page navigates or closes answers
        ``transient`` rather than failing anything.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(max(budget_s, 0.0), MAX_BUDGET_S)
        seen = self._captures
        self._waiting += 1
        self._last_watched = loop.time()
        try:
            while True:
                if self._closed:
                    return FrameResult("closed")
                latest = self._latest
                if latest is not None:
                    if latest.frame is not None and latest.frame.digest != after:
                        return FrameResult("image", latest.frame)
                    if latest.failure is not None and self._captures > seen:
                        return FrameResult(latest.failure)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return FrameResult("unchanged")
                pulse = self._pulse
                self._ensure_worker()
                try:
                    await asyncio.wait_for(pulse.wait(), remaining)
                except TimeoutError:
                    return FrameResult("unchanged")
        finally:
            self._waiting -= 1
            self._last_watched = loop.time()

    async def apply(
        self,
        page_id: str,
        geometry: str,
        events: Sequence[InputEvent],
        *,
        still_allowed: Callable[[], bool],
    ) -> InputResult:
        """Replay *events* into page *page_id*, in order, after earlier batches.

        *page_id* and *geometry* name the frame the user acted on. Input for a
        page that is no longer viewed, or one whose size changed, is refused as
        ``stale`` and nothing reaches the page. *still_allowed* is asked once
        the batch holds the input lock and again before every event, because a
        grant can be replaced while a batch waits, while the page's size is
        read, or between two events. A batch stops at the first event whose
        grant or page is gone (``refused`` or ``stale``), or that navigation or
        teardown cut short, reports how many events landed, and is never
        replayed.

        The size is read from the page itself, not from the last screenshot,
        because a page can resize between the frame the user saw and the batch.
        """
        if len(events) > MAX_EVENTS or _text_length(events) > MAX_TEXT:
            return InputResult("too_long")
        async with self._input:
            if not still_allowed():
                return InputResult("refused")
            if self._closed:
                return InputResult("closed")
            viewed = self._viewed()
            if viewed is None or viewed[0] != page_id:
                return InputResult("stale")
            page = viewed[1]
            if await _live_geometry(page) != geometry:
                return InputResult("stale")
            width, height = _dimensions(geometry)
            applied = 0
            try:
                for event in events:
                    # Every check again before each event, the first one
                    # included: the size read and each dispatch await the
                    # browser, and in that time the grant can be replaced or
                    # an event can open a popup the user now sees instead.
                    if not still_allowed():
                        return InputResult("refused", applied)
                    if self._closed:
                        return InputResult("closed", applied)
                    viewed = self._viewed()
                    if viewed is None or viewed[0] != page_id:
                        return InputResult("stale", applied)
                    await _dispatch(page, event, width, height)
                    applied += 1
            except Exception:
                logger.debug("Login view input stopped", exc_info=True)
                return InputResult("interrupted", applied)
            return InputResult("applied", applied)

    def _page_opened(self, page: Page) -> None:
        if self._closed:
            return
        self._opened += 1
        self._pages.append((f"p{self._opened}", page))
        self._wake()

    def _viewed(self) -> tuple[str, Page] | None:
        self._pages = [(pid, page) for pid, page in self._pages if not page.is_closed()]
        return self._pages[-1] if self._pages else None

    def _wake(self) -> None:
        pulse, self._pulse = self._pulse, asyncio.Event()
        pulse.set()

    def _ensure_worker(self) -> None:
        if self._worker is None and not self._closed:
            self._worker = asyncio.create_task(
                self._capture_while_watched(), name="linkedin-login-view-capture"
            )

    def _watched(self, now: float) -> bool:
        return self._waiting > 0 or now - self._last_watched < _IDLE_S

    async def _capture_while_watched(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while self._watched(loop.time()) and not self._closed:
                self._latest = await self._capture(_CAPTURE_TIMEOUT_S)
                self._captures += 1
                self._wake()
                await asyncio.sleep(_CAPTURE_SPACING_S)
        finally:
            # A frame kept past the last waiter would be handed to the next one
            # as current, however much the page changed in between.
            self._worker = None
            self._latest = None

    async def _capture(self, timeout_s: float) -> _Capture:
        viewed = self._viewed()
        if viewed is None:
            return _Capture(None, "transient")
        page_id, page = viewed
        try:
            jpeg = await _screenshot(page, _QUALITY, timeout_s)
            if _base64_length(jpeg) > MAX_IMAGE_BASE64:
                jpeg = await _screenshot(page, _FALLBACK_QUALITY, timeout_s)
        except Exception:
            logger.debug("Login view capture failed", exc_info=True)
            return _Capture(None, "transient")
        if _base64_length(jpeg) > MAX_IMAGE_BASE64:
            return _Capture(None, "too_large")
        size = _jpeg_size(jpeg)
        if size is None:
            return _Capture(None, "transient")
        geometry = f"{size[0]}x{size[1]}"
        # The page id is part of the digest: a switch to another page that
        # looks the same is still a new frame, and the viewer needs its id.
        digest = hashlib.sha256(page_id.encode() + b"\0" + jpeg).hexdigest()[:32]
        return _Capture(Frame(digest, page_id, geometry, jpeg))


#: The attempt the login task belongs to, set only in that task's context. A
#: login without one (``--login`` at a terminal) publishes nothing.
_THIS_LOGIN: contextvars.ContextVar[LoginAttempt | None] = contextvars.ContextVar(
    "linkedin_login_attempt", default=None
)


class LoginAttempt:
    """One run of the shared login task, as a sign-in view follows it.

    :attr:`state` is ``signing_in`` until the task ends, then the outcome the
    task ended with. It never comes from page text. :attr:`view` is the
    published browser while the login holds it open, else ``None``.
    """

    def __init__(self) -> None:
        self.id = secrets.token_urlsafe(9)
        self.outcome: Outcome | None = None
        self.reason: str | None = None
        self.ended_at: float | None = None
        self._view: LoginView | None = None
        self._view_ended = False
        self._opened_a_browser = False
        self._pulse = asyncio.Event()

    @property
    def state(self) -> State:
        return self.outcome or "signing_in"

    @property
    def view(self) -> LoginView | None:
        return self._view

    def start(
        self, login: Coroutine[Any, Any, None], *, name: str
    ) -> asyncio.Task[None]:
        """Run *login* as this attempt's task and settle the attempt from it."""
        context = contextvars.copy_context()
        context.run(_THIS_LOGIN.set, self)
        task = asyncio.create_task(login, name=name, context=context)
        task.add_done_callback(self._settle)
        return task

    def close_view(self) -> None:
        """End the view for good; the login itself keeps running."""
        self._view_ended = True
        self._hide()

    async def wait_for_change(self, timeout_s: float) -> None:
        """Return when the view is published or hidden, or the attempt ends."""
        try:
            await asyncio.wait_for(self._pulse.wait(), max(timeout_s, 0.0))
        except TimeoutError:
            pass

    def _show(self, view: LoginView) -> None:
        self._opened_a_browser = True
        if self._view_ended or self.outcome is not None:
            view.close()
            return
        self._view = view
        self._wake()

    def _hide(self) -> None:
        view, self._view = self._view, None
        if view is not None:
            view.close()
            self._wake()

    def _settle(self, task: asyncio.Task[None]) -> None:
        self._hide()
        if task.cancelled():
            self.outcome = "cancelled"
        elif (error := task.exception()) is None:
            # A login that never opened a browser and still succeeded found
            # another client's fresh session in place.
            self.outcome = "signed_in" if self._opened_a_browser else "repaired_by_peer"
        else:
            from linkedin_mcp_server.core.exceptions import AccountRestrictedError
            from linkedin_mcp_server.core.proxy_errors import redact_proxy_credentials

            if isinstance(error, AccountRestrictedError):
                self.outcome = "restricted"
            else:
                self.outcome = "failed"
                self.reason = redact_proxy_credentials(str(error))[:_MAX_REASON]
        self.ended_at = time.monotonic()
        self._wake()

    def _wake(self) -> None:
        pulse, self._pulse = self._pulse, asyncio.Event()
        pulse.set()


@asynccontextmanager
async def published(browser: LoginBrowser) -> AsyncGenerator[None]:
    """Let the sign-in view of the login running in this task see *browser*.

    Used inside the ``async with`` that keeps the login browser open, so the
    view is closed before that browser's teardown begins. Does nothing, and
    reads nothing from *browser*, for a login no attempt belongs to.
    """
    attempt = _THIS_LOGIN.get()
    if attempt is None:
        yield
        return
    attempt._show(LoginView(browser.context, browser.page))
    try:
        yield
    finally:
        attempt._hide()


async def _screenshot(page: Page, quality: int, timeout_s: float) -> bytes:
    # `scale="css"` makes the image exactly the CSS viewport, which is what the
    # geometry and the click coordinates are measured in. `caret="initial"`
    # keeps the text cursor visible, so the user sees which field has focus.
    return await page.screenshot(
        type="jpeg",
        quality=quality,
        scale="css",
        caret="initial",
        timeout=timeout_s * 1000,
    )


async def _live_geometry(page: Page) -> str | None:
    """The page's CSS viewport now, as ``WIDTHxHEIGHT``, or ``None`` mid-navigation.

    Read in patchright's isolated world, which the page's own scripts never see.
    """
    try:
        width, height = await page.evaluate("[window.innerWidth, window.innerHeight]")
    except Exception:
        logger.debug("Could not read the login page's viewport", exc_info=True)
        return None
    return f"{width}x{height}"


async def _dispatch(page: Page, event: InputEvent, width: int, height: int) -> None:
    match event:
        case Click():
            await page.mouse.click(
                _at(event.x, width), _at(event.y, height), button=event.button
            )
        case Wheel():
            await page.mouse.move(_at(event.x, width), _at(event.y, height))
            await page.mouse.wheel(0, min(max(event.dy, -_MAX_WHEEL), _MAX_WHEEL))
        case Key():
            names = ["ControlOrMeta" if m == "Meta" else m for m in event.modifiers]
            await page.keyboard.press("+".join([*names, event.key]))
        case TypeText():
            await page.keyboard.type(event.text)
        case Paste():
            await page.keyboard.insert_text(event.text)


def _at(fraction: float, size: int) -> float:
    return min(max(fraction, 0.0), 1.0) * max(size - 1, 0)


def _text_length(events: Sequence[InputEvent]) -> int:
    return sum(len(e.text) for e in events if isinstance(e, (TypeText, Paste)))


def _dimensions(geometry: str) -> tuple[int, int]:
    width, height = geometry.split("x")
    return int(width), int(height)


def _base64_length(data: bytes) -> int:
    return 4 * ((len(data) + 2) // 3)


#: Start-of-frame markers, which carry the image size. C4, C8 and CC share the
#: range but are other segments.
_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a JPEG's start-of-frame segment."""
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in _SOF_MARKERS:
            if i + 9 > len(data):
                return None
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return width, height
        i += 2 + int.from_bytes(data[i + 2 : i + 4], "big")
    return None
