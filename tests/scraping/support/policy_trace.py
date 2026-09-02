"""Strict deterministic browser doubles for scraping policy traces."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Coroutine, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

import asyncio
import inspect
import re


_MISSING = object()
_REAL_ASYNCIO_SLEEP = asyncio.sleep


class TraceRecorder:
    """Record declared semantic operations and reject every unknown operation."""

    def __init__(self, scenario: str, allowed: Iterable[str]):
        self.scenario = scenario
        self.allowed = frozenset(allowed)
        self.events: list[dict[str, Any]] = []
        self._call: str | None = None
        self._section: str | None = None
        self.pending_response_reads = 0
        self._callbacks: list[Callable[..., Any]] = []

    def callback_id(self, callback: Callable[..., Any]) -> str:
        for index, known in enumerate(self._callbacks, start=1):
            if known is callback:
                return f"callback-{index}"
        self._callbacks.append(callback)
        return f"callback-{len(self._callbacks)}"

    @contextmanager
    def context(self, call: str, section: str | None = None):
        previous = self._call, self._section
        self._call, self._section = call, section
        try:
            yield
        finally:
            self._call, self._section = previous

    def record(self, kind: str, **values: Any) -> None:
        if kind not in self.allowed:
            raise AssertionError(
                f"{self.scenario}: undeclared operation {kind!r}; "
                f"allowed={sorted(self.allowed)!r}"
            )
        event: dict[str, Any] = {"seq": len(self.events) + 1, "kind": kind}
        if self._call is not None:
            event["call"] = self._call
        if self._section is not None:
            event["section"] = self._section
        event.update(values)
        self.events.append(event)

    def trace(self, call: dict[str, Any], result: Any = None) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": 1,
            "scenario": self.scenario,
            "call": call,
            "events": self.events,
        }
        if result is not None:
            value["result"] = result
        return value

    def assert_no_pending_reads(self) -> None:
        assert self.pending_response_reads == 0, (
            f"{self.scenario}: {self.pending_response_reads} response body read(s) pending"
        )


class FakeClock:
    """A monotonic clock whose sleeps advance logical time immediately."""

    def __init__(self, recorder: TraceRecorder, start: float = 1000.0):
        self.recorder = recorder
        self.now = start
        self.reason = "sleep"

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.recorder.record("sleep", reason=self.reason, seconds=seconds)
        self.now += seconds
        await _REAL_ASYNCIO_SLEEP(0)

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass(slots=True)
class _Outcome:
    value: Any = _MISSING
    error: BaseException | None = None


class Script:
    """A finite queue of scripted outcomes."""

    def __init__(self, values: Iterable[Any] = ()):
        self.values: deque[_Outcome] = deque()
        for value in values:
            if isinstance(value, BaseException):
                self.values.append(_Outcome(error=value))
            else:
                self.values.append(_Outcome(value=value))

    def take(self, operation: str, scenario: str, default: Any = _MISSING) -> Any:
        if not self.values:
            if default is not _MISSING:
                return default
            raise AssertionError(f"{scenario}: no scripted result for {operation}")
        outcome = self.values.popleft()
        if outcome.error is not None:
            raise outcome.error
        value = outcome.value
        return value() if callable(value) else value


class ScriptedLocator:
    """The strict fluent Locator subset used by the extractor."""

    def __init__(self, page: ScriptedPage, semantic_id: str):
        self.page = page
        self.semantic_id = semantic_id

    @property
    def first(self) -> ScriptedLocator:
        return self.page._derived_locator(self.semantic_id, "first")

    @property
    def last(self) -> ScriptedLocator:
        return self.page._derived_locator(self.semantic_id, "last")

    def nth(self, index: int) -> ScriptedLocator:
        return self.page._derived_locator(self.semantic_id, f"nth:{index}")

    def locator(self, selector: str) -> ScriptedLocator:
        return self.page._locator_for(self.semantic_id, selector)

    def filter(self, *, has_text: Any = None, **kwargs: Any) -> ScriptedLocator:
        if kwargs:
            raise AssertionError(
                f"{self.page.recorder.scenario}: unsupported locator filter {kwargs!r}"
            )
        text = has_text.pattern if isinstance(has_text, re.Pattern) else has_text
        return self.page._derived_locator(self.semantic_id, f"filter:{text}")

    async def _operation(self, name: str, **values: Any) -> Any:
        operation = f"locator.{name}"
        self.page.recorder.record(operation, locator=self.semantic_id, **values)
        return self.page._take(f"{self.semantic_id}.{name}", default=_MISSING)

    async def count(self) -> int:
        return int(await self._operation("count"))

    async def is_visible(self) -> bool:
        return bool(await self._operation("is_visible"))

    async def inner_text(self) -> str:
        return str(await self._operation("inner_text"))

    async def wait_for(
        self, *, state: str = "visible", timeout: int | None = None
    ) -> None:
        await self._operation("wait_for", state=state, timeout_ms=timeout)

    async def scroll_into_view_if_needed(self, *, timeout: int | None = None) -> None:
        await self._operation("scroll_into_view", timeout_ms=timeout)

    async def click(self, *, timeout: int | None = None) -> None:
        await self._operation("click", timeout_ms=timeout)

    async def fill(self, value: str, *, timeout: int | None = None) -> None:
        await self._operation("fill", value=value, timeout_ms=timeout)

    async def focus(self) -> None:
        await self._operation("focus")


class _Mouse:
    def __init__(self, page: ScriptedPage):
        self.page = page

    async def move(self, x: int, y: int) -> None:
        self.page.recorder.record("mouse.move", x=x, y=y)

    async def wheel(self, delta_x: int, delta_y: int) -> None:
        self.page.recorder.record("mouse.wheel", delta_x=delta_x, delta_y=delta_y)
        self.page._take("mouse.wheel", default=None)
        await _REAL_ASYNCIO_SLEEP(0)


class _Keyboard:
    def __init__(self, page: ScriptedPage):
        self.page = page

    async def press(self, key: str) -> None:
        self.page.recorder.record("keyboard.press", key=key)
        self.page._take("keyboard.press", default=None)

    async def type(self, text: str, *, delay: int | None = None) -> None:
        self.page.recorder.record("keyboard.type", text=text, delay_ms=delay)
        self.page._take("keyboard.type", default=None)


class ScriptedPage:
    """Strict Page subset with semantic JavaScript dispatch and exact listeners."""

    def __init__(
        self,
        recorder: TraceRecorder,
        *,
        url: str = "about:blank",
        viewport_size: dict[str, int] | None = None,
    ):
        self.recorder = recorder
        self.url = url
        self.main_frame = SimpleNamespace(url=url)
        self.viewport_size = viewport_size or {"width": 1280, "height": 720}
        self.mouse = _Mouse(self)
        self.keyboard = _Keyboard(self)
        self.time_origin = 1000.0
        self.scripts: dict[str, Script] = {}
        self.locator_ids: dict[tuple[str | None, str], str] = {}
        self.derived_ids: dict[tuple[str, str], str] = {}
        self.listeners: dict[str, list[Callable[..., Any]]] = defaultdict(list)
        self.goto_landings: deque[str] = deque()

    def script(self, operation: str, *values: Any) -> ScriptedPage:
        self.scripts[operation] = Script(values)
        return self

    def declare_locator(
        self, selector: str, semantic_id: str, *, parent: str | None = None
    ) -> ScriptedLocator:
        self.locator_ids[(parent, selector)] = semantic_id
        return ScriptedLocator(self, semantic_id)

    def declare_derived(
        self, parent: str, operation: str, semantic_id: str
    ) -> ScriptedLocator:
        self.derived_ids[(parent, operation)] = semantic_id
        return ScriptedLocator(self, semantic_id)

    def _take(self, operation: str, default: Any = _MISSING) -> Any:
        script = self.scripts.get(operation)
        if script is None:
            if default is not _MISSING:
                return default
            raise AssertionError(
                f"{self.recorder.scenario}: unknown scripted operation {operation!r}"
            )
        return script.take(operation, self.recorder.scenario, default)

    def _locator_for(self, parent: str | None, selector: str) -> ScriptedLocator:
        semantic_id = self.locator_ids.get((parent, selector))
        if semantic_id is None:
            raise AssertionError(
                f"{self.recorder.scenario}: undeclared locator parent={parent!r} "
                f"selector={selector!r}"
            )
        self.recorder.record(
            "locator.create", locator=semantic_id, parent=parent, selector=selector
        )
        return ScriptedLocator(self, semantic_id)

    def _derived_locator(self, parent: str, operation: str) -> ScriptedLocator:
        semantic_id = self.derived_ids.get((parent, operation))
        if semantic_id is None:
            raise AssertionError(
                f"{self.recorder.scenario}: undeclared locator derivation "
                f"{parent!r}.{operation}"
            )
        self.recorder.record(
            "locator.derive", locator=semantic_id, parent=parent, operation=operation
        )
        return ScriptedLocator(self, semantic_id)

    def locator(self, selector: str) -> ScriptedLocator:
        return self._locator_for(None, selector)

    def on(self, event: str, callback: Callable[..., Any]) -> None:
        if callback in self.listeners[event]:
            raise AssertionError(
                f"{self.recorder.scenario}: callback already registered for {event}"
            )
        self.listeners[event].append(callback)
        self.recorder.record(
            "listener.add", event=event, callback_id=self.recorder.callback_id(callback)
        )

    def remove_listener(self, event: str, callback: Callable[..., Any]) -> None:
        if callback not in self.listeners[event]:
            raise AssertionError(
                f"{self.recorder.scenario}: callback identity was not registered "
                f"for {event}"
            )
        self.listeners[event].remove(callback)
        self.recorder.record(
            "listener.remove",
            event=event,
            callback_id=self.recorder.callback_id(callback),
        )

    async def goto(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        timeout: int = 30000,
    ) -> None:
        landing = self.goto_landings.popleft() if self.goto_landings else url
        self.url = landing
        self.main_frame.url = landing
        self.time_origin += 1.0
        self.recorder.record(
            "navigate",
            requested_url=url,
            landed_url=landing,
            wait_until=wait_until,
            timeout_ms=timeout,
        )
        for callback in tuple(self.listeners["framenavigated"]):
            callback(self.main_frame)
        self._take("goto", default=None)

    async def title(self) -> str:
        self.recorder.record("title")
        return str(self._take("title", default="LinkedIn"))

    async def wait_for_selector(
        self,
        selector: str,
        *,
        state: str = "visible",
        timeout: int | None = None,
    ) -> None:
        operation = semantic_selector_id(selector)
        self.recorder.record(
            "wait_for_selector",
            operation=operation,
            state=state,
            timeout_ms=timeout,
        )
        self._take(f"wait_for_selector:{operation}", default=None)

    async def wait_for_function(
        self,
        expression: str,
        *,
        arg: Any = None,
        timeout: int | None = None,
    ) -> None:
        operation = semantic_program_id(expression)
        self.recorder.record(
            "wait_for_function",
            operation=operation,
            program_digest=program_digest(expression),
            arg=arg,
            timeout_ms=timeout,
        )
        self._take(f"wait_for_function:{operation}", default=None)

    async def wait_for_load_state(
        self, state: str = "load", *, timeout: int | None = None
    ) -> None:
        self.recorder.record("wait_for_load_state", state=state, timeout_ms=timeout)
        self._take("wait_for_load_state", default=None)

    async def evaluate(self, expression: str, arg: Any = _MISSING) -> Any:
        operation = semantic_program_id(expression)
        values: dict[str, Any] = {
            "operation": operation,
            "program_digest": program_digest(expression),
        }
        if arg is not _MISSING:
            values["arg"] = arg
        self.recorder.record("evaluate", **values)
        if operation == "document_origin":
            return self.time_origin
        return self._take(f"evaluate:{operation}")

    def emit(self, event: str, value: Any) -> None:
        callbacks = tuple(self.listeners[event])
        if not callbacks:
            raise AssertionError(
                f"{self.recorder.scenario}: no listener registered for {event}"
            )
        self.recorder.record("listener.emit", event=event, count=len(callbacks))
        for callback in callbacks:
            callback(value)

    def assert_clean(self) -> None:
        remaining = {
            name: len(values) for name, values in self.listeners.items() if values
        }
        assert not remaining, f"{self.recorder.scenario}: listeners remain: {remaining}"
        self.recorder.assert_no_pending_reads()


class ScriptedResponse:
    """A strict response body source with pending-read accounting."""

    def __init__(
        self,
        recorder: TraceRecorder,
        url: str,
        body: bytes | BaseException,
        *,
        release: asyncio.Event | None = None,
    ):
        self.recorder = recorder
        self.url = url
        self._body = body
        self._release = release

    async def body(self) -> bytes:
        self.recorder.pending_response_reads += 1
        self.recorder.record("response.body.start", url=self.url)
        try:
            if self._release is not None:
                await self._release.wait()
            if isinstance(self._body, BaseException):
                raise self._body
            return self._body
        finally:
            self.recorder.pending_response_reads -= 1
            self.recorder.record("response.body.finish", url=self.url)


def semantic_selector_id(selector: str) -> str:
    selectors = {
        "main": "main",
        "[role='menu']": "profile_more_menu",
        "main li label[aria-label]": "conversation_rows",
    }
    if selector in selectors:
        return selectors[selector]
    if "dialog" in selector and "textarea" in selector:
        return "dialog_textarea"
    if "dialog" in selector:
        return "dialog"
    raise AssertionError(f"unrecognized selector program: {selector!r}")


def program_digest(program: str) -> str:
    """Fingerprint a JavaScript program, ignoring only whitespace layout.

    The operation name alone is matched from a single marker substring, so a
    program can be rewritten around that marker and keep its label. Relocating
    a program between modules changes its indentation and nothing else, which
    is why the fingerprint collapses whitespace before hashing.
    """

    compact = " ".join(program.split())
    return sha256(compact.encode("utf-8")).hexdigest()[:12]


def semantic_program_id(program: str) -> str:
    """Map current JavaScript programs to stable operation names."""

    compact = " ".join(program.split())
    checks = (
        ("performance.timeOrigin", "document_origin"),
        ("MAX_HEADING_CONTAINERS", "root_content"),
        ("SIDEBAR_SECTIONS", "sidebar_profiles"),
        ("showAllUrls", "sidebar_profiles"),
        ("hasInvite", "connection_action_signals"),
        ("expanded === 'false'", "open_more_button"),
        ("hasIncomingActionRow", "incoming_accept"),
        ('main a[href*="/messaging/compose/"]', "profile_urn"),
        ("return {ids: ids, scoped", "job_ids"),
        ("querySelectorAll(selector)", "message_compose_href"),
        ("const heading = document.querySelector('main h1')", "profile_display_name"),
        ("pickerInput", "select_message_recipient"),
        ("targetValues", "compose_matches_recipient"),
        ("el.focus()", "focus_message_composer"),
        ('button[data-control-name="send"]', "click_send_button"),
        ("main li label[aria-label]", "conversation_thread_refs"),
        ("isScrollable", "scroll_main_region"),
        ("jobs-search-pagination__page-state", "job_total_pages"),
        ("artdeco-pagination__pages", "saved_job_total_pages"),
        ("document.body?.innerText", "body_marker"),
        ("(document.querySelector('main') || document.body).innerText", "page_text"),
        ("main.innerText.length > 200", "main_text_200"),
        ("main.innerText.length > 100", "main_text_100"),
        ("minimumLength", "main_text_minimum"),
        ('a[href*="/in/"]', "company_people_ready"),
        ("text.startsWith('Load more')", "profile_details_ready"),
        ("bodyText.includes", "message_text_visible"),
        ("premium/", "premium_dialog_text"),
        ('main a[href*="/in/"]', "sidebar_expanded_profiles"),
    )
    for marker, operation in checks:
        if marker in compact:
            return operation
    raise AssertionError(f"unrecognized evaluate program: {compact[:180]!r}")


def bind_effective(
    function: Callable[..., Any], *args: Any, **kwargs: Any
) -> dict[str, Any]:
    """Return a helper call with production defaults applied."""

    bound = inspect.signature(function).bind(*args, **kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)


async def completed(value: Any = None) -> Any:
    return value


AsyncBoundary = Callable[..., Coroutine[Any, Any, Any]]
