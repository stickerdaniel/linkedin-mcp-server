"""Why a fresh browser's first navigation stalled, or why its browser went.

On Windows runners the first ``Page.goto`` of a fresh browser has run out
Patchright's 30s default, in the identity gate against plain loopback and in
the product's own import validation while a row stages its session. Rows have
also seen Chromium exit while its Node driver lived on, and O4 rows have lost
the staged session between staging and the row. Their packets show the
timeout and the loss, never the phase either was reached in. This module
records that phase. It fixes nothing: no timeout, retry or verdict reads what
it writes.

**Observers are there before the browser is.** ``observe_navigation`` wraps
Patchright's own ``launch_persistent_context``, ``Page.goto``,
``BrowserContext.close`` and the driver's stop in the process that drives
the browser, for as long as it is entered, and listens on every context the
launch returns before anything navigates. A listener asks the browser
nothing: the driver dispatches request and response events whether or not
anyone listens (a subscription only decides whether it forwards them to this
process), and page lifecycle events always reach it. No CDP session, init
script, route, header or launch flag is added, so the browser says what it
said before (AGENTS.md, Browser Identity Rules).

**Write-through.** Every observed step replaces the file at once, so what was
obtained survives a navigation that never returns, a page that closed, a
cancelled task, and a staging subprocess the harness kills. ``outcome`` stays
``running`` in a file whose writer never finished.

**Absent stays absent.** A phase not observed is null. ``stalled_phase`` is
the first of request, response, commit and DOMContentLoaded that had not been
observed when the first failed navigation gave up, never one filled in from
the timeout. A browser root's exit code is not known here, because no observer
is its parent, and the record says so. The driver's is read from Patchright's
own handle on it, whose process is its parent, and only once that handle has
one.

**Two lifetimes, kept apart.** ``LifetimeSampler`` runs in the harness
process beside whichever process stages, and reads this process's descendants
only: the browser root on the profile (``--user-data-dir`` naming it, no
``--type=``) and the root's parent, the driver. Each is an identity (pid and
create time) and an interval (identified, last seen alive, first seen gone).
Its end is read against the close the driving process asked for: gone before
any was asked for is ``unexpected``, and a sampling interval that straddles
the request is ``ambiguous``.

**The session's lineage is counted, never copied.** ``record_cookie_lineage``
reads the cookie file and the profile's own cookie store at named points:
entry counts, LinkedIn cookie names, and whether a ``li_at`` carries the
staged value, by digest equality. The store is opened read-only and immutable,
so nothing it holds or locks changes for the browser that opens it next. No
cookie value, header, raw event payload, or URL query or fragment reaches a
file.

**Bounded.** At most ``MAX_NAVIGATIONS`` navigations, ``MAX_EVENTS`` lifecycle
events, ``MAX_PROCESSES`` processes and ``MAX_READINGS`` readings per file;
the sampler stops within ``STOP_SECONDS``. A failure of an observer is
counted in its file and never raised into the operation it watches.

The three files are in ``events.PUBLISHED_FILES``. Stdlib only at import:
``baseline_stage.py`` runs ``observe_navigation`` under the frozen baseline's
interpreter, which has neither psutil nor anything of the candidate.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

FIRST_NAVIGATION_FILE = "first-navigation.json"
BROWSER_LIFETIMES_FILE = "browser-lifetimes.json"
COOKIE_LINEAGE_FILE = "cookie-lineage.json"

#: What a navigation passes through, in order: its request sent, its response
#: headers, the commit of the main frame, and DOMContentLoaded, which is what
#: ``wait_until="domcontentloaded"`` waits for.
PHASES = ("request", "response", "commit", "domcontentloaded")

MAX_NAVIGATIONS = 8
MAX_EVENTS = 200
MAX_PROCESSES = 16
MAX_READINGS = 8
MAX_NAMES = 32
MAX_TEXT = 160
#: Pages and contexts one observation listens on.
MAX_TARGETS = 16
SAMPLE_SECONDS = 0.1
STOP_SECONDS = 2.0

USER_DATA_DIR_FLAG = "--user-data-dir="
CHILD_TYPE_FLAG = "--type="

#: Chromium's own error code, which is all of a failure's text that is kept.
_NET_ERROR = re.compile(r"net::ERR_[A-Z0-9_]+")

#: The domain the product stores LinkedIn's cookies under, and every
#: subdomain of it.
_LINKEDIN = "linkedin.com"

#: Where Chromium keeps a profile's cookies: ``Network`` since 96, the
#: profile directory itself before that.
_COOKIE_STORES = (("Default", "Network", "Cookies"), ("Default", "Cookies"))

_ROOT_EXIT_CODE = (
    "not observable: the browser root is the driver's child, and no observer "
    "is its parent"
)


def _text(value: Any) -> str | None:
    return value[:MAX_TEXT] if isinstance(value, str) else None


def sanitized_url(url: Any) -> str | None:
    """Scheme, host, port and path; never userinfo, query or fragment."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not parts.scheme:
        return None
    if host is None:
        # ``about:blank``, ``data:``: the rest of such a URL is content.
        return f"{parts.scheme}:"
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"{parts.scheme}://{netloc}{parts.path}"[:MAX_TEXT]


def _spelling(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(os.fspath(path))))


class _JsonFile:
    """One JSON document, replaced whole on every write, never raising."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.failures = 0
        self._lock = threading.Lock()
        self._writers: list[threading.Thread] = []
        self._writers_guard = threading.Lock()

    def write(self, document: Mapping[str, Any]) -> None:
        # Replaced on its own thread. The caller is the event loop a
        # navigation's deadline runs on, and a slow disc must not spend that
        # deadline. ``flush`` waits the writers out for a reader that needs the
        # file as this call left it.
        text = json.dumps(document, indent=2, sort_keys=True) + "\n"
        worker = threading.Thread(
            target=self._replace, args=(text,), name=f"evidence-{self.path.name}"
        )
        with self._writers_guard:
            self._writers.append(worker)
        worker.start()

    def flush(self, seconds: float = 5.0) -> None:
        deadline = time.monotonic() + seconds
        for worker in list(self._writers):
            worker.join(max(0.0, deadline - time.monotonic()))

    def read(self) -> dict[str, Any] | None:
        """The document as last written, once that write has landed."""
        self.flush()
        return read_document(self.path)

    def _replace(self, text: str) -> None:
        with self._lock:
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(text, encoding="utf-8")
                os.replace(temporary, self.path)
            except (OSError, TypeError, ValueError):
                self.failures += 1


def read_document(path: Path) -> dict[str, Any] | None:
    """A file this module wrote, or None when it is missing or unreadable."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


# --- The navigation, from inside the process that drives the browser ----------


def _outcome(exc: BaseException | None) -> tuple[str, str | None]:
    if exc is None:
        return "completed", None
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled", type(exc).__name__
    return "failed", type(exc).__name__


def _driver_process(owner: Any) -> Any:
    """Patchright's handle on its Node driver, or None where it has none.

    Private attributes, read and never written: a Patchright that moved them
    leaves the driver unknown rather than the observation broken.
    """
    impl = getattr(owner, "_impl_obj", owner)
    connection = getattr(impl, "_connection", None)
    transport = getattr(connection, "_transport", None)
    return getattr(transport, "_proc", None)


class NavigationRecorder:
    """What one process's browser did, from before its launch to its close."""

    def __init__(self, path: Path, *, label: str) -> None:
        self._file = _JsonFile(path)
        self._began_ns = time.monotonic_ns()
        self._lock = threading.RLock()
        self._targets: list[Any] = []
        self._listeners: list[tuple[Any, str, Callable[..., None]]] = []
        #: The navigation each page is on, by page.
        self._current: list[tuple[Any, dict[str, Any]]] = []
        self._driver: Any = None
        self.document: dict[str, Any] = {
            "label": label,
            "pid": os.getpid(),
            "began_wall": time.time(),
            "ended_ms": None,
            "outcome": "running",
            "error_type": None,
            #: The last step entered: setup, launch, navigation or close.
            "step": "setup",
            "launch": {"started_ms": None, "returned_ms": None, "error_type": None},
            "driver": {
                "pid": None,
                "exit_code": None,
                "exit_code_source": None,
                "stop_requested_ms": None,
                "stop_requested_wall": None,
            },
            "close_requested_ms": None,
            "close_requested_wall": None,
            "navigations": [],
            "first_failed_navigation": None,
            "stalled_phase": None,
            "lifecycle": [],
            "subresource_requests": 0,
            "dropped": {"navigations": 0, "events": 0, "targets": 0},
            "observer": {
                "installed": False,
                "unavailable": None,
                "attached": 0,
                "detached": 0,
                "detach_failures": 0,
                "errors": 0,
            },
            "write_failures": 0,
        }
        self.persist()

    def _ms(self) -> float:
        return round((time.monotonic_ns() - self._began_ns) / 1e6, 1)

    def persist(self, *, wait: bool = False) -> None:
        with self._lock:
            self.document["write_failures"] = self._file.failures
            self._file.write(self.document)
        # The last write of a finished observation, read back by whoever adds
        # to the record next: it has to be the file before that read.
        if wait:
            self._file.flush()

    def _guarded(self, handler: Callable[..., None]) -> Callable[..., None]:
        """A listener that can never raise into Patchright's dispatch."""

        def guarded(*args: Any) -> None:
            try:
                with self._lock:
                    handler(*args)
            except Exception:  # noqa: BLE001 - counted, never raised
                self.document["observer"]["errors"] += 1

        return guarded

    def _listen(self, target: Any, event: str, handler: Callable[..., None]) -> None:
        guarded = self._guarded(handler)
        target.on(event, guarded)
        self._listeners.append((target, event, guarded))
        self.document["observer"]["attached"] += 1

    def _watched(self, target: Any) -> bool:
        return any(known is target for known in self._targets)

    def _watch_context(self, context: Any) -> None:
        if self._watched(context):
            return
        if len(self._targets) >= MAX_TARGETS:
            self.document["dropped"]["targets"] += 1
            return
        self._targets.append(context)
        self._listen(context, "request", self._on_request)
        self._listen(context, "response", self._on_response)
        self._listen(context, "requestfailed", self._on_request_failed)
        self._listen(context, "page", self._watch_page)
        self._listen(context, "close", lambda _c: self._lifecycle("context-closed"))
        for page in list(context.pages):
            self._watch_page(page)

    def _watch_page(self, page: Any) -> None:
        if self._watched(page):
            return
        if len(self._targets) >= MAX_TARGETS:
            self.document["dropped"]["targets"] += 1
            return
        self._targets.append(page)
        self._listen(page, "framenavigated", lambda frame: self._on_commit(frame))
        self._listen(
            page, "domcontentloaded", lambda _p: self._phase(page, "domcontentloaded")
        )
        self._listen(page, "load", lambda _p: self._phase(page, "load"))
        self._listen(page, "crash", lambda _p: self._lifecycle("page-crashed"))
        self._listen(page, "close", lambda _p: self._lifecycle("page-closed"))

    def _lifecycle(self, name: str) -> None:
        events = self.document["lifecycle"]
        if len(events) >= MAX_EVENTS:
            self.document["dropped"]["events"] += 1
            return
        events.append({"event": name, "ms": self._ms()})
        self.persist()

    def _navigation_of(self, page: Any) -> dict[str, Any] | None:
        for known, navigation in reversed(self._current):
            if known is page:
                return navigation
        return None

    def _phase(self, page: Any, name: str, **fields: Any) -> None:
        navigation = self._navigation_of(page)
        if navigation is None:
            return
        phases = navigation["phases"]
        if phases.get(name) is not None:
            return
        phases[name] = self._ms()
        navigation.update(fields)
        self.persist()

    @staticmethod
    def _main_frame_navigation(request: Any) -> Any:
        """The page a main-frame navigation request belongs to, or None."""
        if not request.is_navigation_request():
            return None
        frame = request.frame
        if frame.parent_frame is not None:
            return None
        return frame.page

    def _on_request(self, request: Any) -> None:
        page = self._main_frame_navigation(request)
        if page is None:
            self.document["subresource_requests"] += 1
            return
        self._phase(page, "request")

    def _on_response(self, response: Any) -> None:
        page = self._main_frame_navigation(response.request)
        if page is not None:
            self._phase(page, "response", response_status=response.status)

    def _on_request_failed(self, request: Any) -> None:
        page = self._main_frame_navigation(request)
        navigation = None if page is None else self._navigation_of(page)
        if navigation is None or navigation["request_failed_ms"] is not None:
            return
        failure = request.failure if isinstance(request.failure, str) else ""
        matched = _NET_ERROR.search(failure)
        navigation["request_failed_ms"] = self._ms()
        navigation["failure"] = matched.group(0) if matched else "unrecognised"
        self.persist()

    def _on_commit(self, frame: Any) -> None:
        if frame.parent_frame is None:
            self._phase(frame.page, "commit")

    # The wrapped Patchright calls. Each is entered on the event loop, before
    # the call it wraps, and leaves that call's result and exception alone.

    def launching(self, browser_type: Any) -> None:
        self.document["step"] = "launch"
        self.document["launch"]["started_ms"] = self._ms()
        process = _driver_process(browser_type)
        if process is not None:
            self._driver = process
            self.document["driver"]["pid"] = getattr(process, "pid", None)
        self.persist()

    def launched(self, context: Any, exc: BaseException | None) -> None:
        launch = self.document["launch"]
        if exc is not None:
            launch["error_type"] = type(exc).__name__
        else:
            launch["returned_ms"] = self._ms()
            self._watch_context(context)
        self.persist()

    def navigating(self, page: Any, url: Any, options: Mapping[str, Any]) -> Any:
        self.document["step"] = "navigation"
        navigations = self.document["navigations"]
        if len(navigations) >= MAX_NAVIGATIONS:
            self.document["dropped"]["navigations"] += 1
            self.persist()
            return None
        # A page this observation has not seen yet, such as one opened through
        # a hidden target, is listened on now: still before it navigates.
        self._watch_context(page.context)
        self._watch_page(page)
        timeout = options.get("timeout")
        navigation: dict[str, Any] = {
            "url": sanitized_url(url),
            "wait_until": _text(options.get("wait_until")),
            "timeout_ms": timeout if isinstance(timeout, (int, float)) else None,
            "started_ms": self._ms(),
            "ended_ms": None,
            "outcome": "running",
            "phases": {name: None for name in (*PHASES, "load")},
            "response_status": None,
            "request_failed_ms": None,
            "failure": None,
        }
        navigations.append(navigation)
        self._current.append((page, navigation))
        self.persist()
        return navigation

    def navigated(self, navigation: Any, exc: BaseException | None) -> None:
        if navigation is None:
            return
        navigation["ended_ms"] = self._ms()
        outcome, error = _outcome(exc)
        navigation["outcome"] = "ok" if exc is None else error
        if outcome != "completed" and self.document["first_failed_navigation"] is None:
            self.document["first_failed_navigation"] = self.document[
                "navigations"
            ].index(navigation)
            # What had been observed when it gave up; a phase arriving after
            # does not explain the wait that ran out.
            phases = navigation["phases"]
            self.document["stalled_phase"] = next(
                (name for name in PHASES if phases[name] is None), None
            )
        self.persist()

    def closing(self, context: Any) -> None:
        if (
            not self._watched(context)
            or self.document["close_requested_ms"] is not None
        ):
            return
        self.document["step"] = "close"
        self.document["close_requested_ms"] = self._ms()
        self.document["close_requested_wall"] = time.time()
        self.persist()

    def stopping(self, playwright: Any) -> None:
        driver = self.document["driver"]
        if driver["stop_requested_ms"] is not None:
            return
        process = _driver_process(playwright)
        if self._driver is not None and process is not self._driver:
            return
        self.document["step"] = "close"
        driver["stop_requested_ms"] = self._ms()
        driver["stop_requested_wall"] = time.time()
        self.persist()

    def finish(self, exc: BaseException | None) -> None:
        with self._lock:
            outcome, error = _outcome(exc)
            self.document["outcome"] = outcome
            self.document["error_type"] = error
            self.document["ended_ms"] = self._ms()
            driver = self.document["driver"]
            code = getattr(self._driver, "returncode", None)
            if self._driver is None:
                driver["exit_code_source"] = "no driver was launched while observed"
            elif code is None:
                driver["exit_code_source"] = (
                    "unknown: the driver had not been seen to exit when the "
                    "observation ended"
                )
            else:
                driver["exit_code"] = code
                driver["exit_code_source"] = (
                    "Patchright's own handle on the driver, whose parent is the "
                    "observed process"
                )
            self.persist(wait=True)

    def detach(self) -> None:
        observer = self.document["observer"]
        for target, event, handler in self._listeners:
            try:
                target.remove_listener(event, handler)
                observer["detached"] += 1
            except Exception:  # noqa: BLE001 - a closed connection: counted
                observer["detach_failures"] += 1
        self._listeners.clear()
        self._targets.clear()
        self._current.clear()
        self._driver = None
        self.persist()


#: The recorders entered in this process, and Patchright's originals while
#: any is.
_ACTIVE: list[NavigationRecorder] = []
_ORIGINALS: dict[tuple[type, str], Any] = {}


def _each(
    call: Callable[[NavigationRecorder], Any],
    recorders: Iterable[NavigationRecorder] | None = None,
) -> list[tuple[NavigationRecorder, Any]]:
    results = []
    for recorder in list(_ACTIVE if recorders is None else recorders):
        try:
            results.append((recorder, call(recorder)))
        except Exception:  # noqa: BLE001 - counted, never raised
            recorder.document["observer"]["errors"] += 1
            results.append((recorder, None))
    return results


def _wrap_launch(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    async def launch_persistent_context(self: Any, *args: Any, **kwargs: Any) -> Any:
        entered = [recorder for recorder, _ in _each(lambda r: r.launching(self))]

        def ended(context: Any, exc: BaseException | None) -> None:
            _each(lambda r: r.launched(context, exc), entered)

        try:
            context = await original(self, *args, **kwargs)
        except BaseException as exc:
            ended(None, exc)
            raise
        ended(context, None)
        return context

    return launch_persistent_context


def _wrap_goto(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    async def goto(self: Any, url: Any, *args: Any, **kwargs: Any) -> Any:
        entered = _each(lambda r: r.navigating(self, url, kwargs))

        def ended(exc: BaseException | None) -> None:
            for recorder, navigation in entered:
                _each(lambda r: r.navigated(navigation, exc), [recorder])

        try:
            response = await original(self, url, *args, **kwargs)
        except BaseException as exc:
            ended(exc)
            raise
        ended(None)
        return response

    return goto


def _wrap_close(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    async def close(self: Any, *args: Any, **kwargs: Any) -> Any:
        _each(lambda recorder: recorder.closing(self))
        return await original(self, *args, **kwargs)

    return close


def _wrap_stop(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    async def __aexit__(self: Any, *args: Any) -> Any:
        _each(lambda recorder: recorder.stopping(self))
        return await original(self, *args)

    return __aexit__


def _install() -> str | None:
    """Wrap Patchright's calls; why it could not, or None.

    The driver's stop is the context manager's ``__aexit__``: ``start()``
    binds that to the instance as ``Playwright.stop``, so a wrapper on the
    ``Playwright`` class would never run. Bound when the driver starts, which
    is why only a driver started while observed has its stop recorded.
    """
    try:
        from patchright.async_api import BrowserContext, BrowserType, Page
        from patchright.async_api._context_manager import PlaywrightContextManager
    except ImportError as exc:
        return f"patchright is not importable: {type(exc).__name__}"
    targets = (
        (BrowserType, "launch_persistent_context", _wrap_launch),
        (Page, "goto", _wrap_goto),
        (BrowserContext, "close", _wrap_close),
        (PlaywrightContextManager, "__aexit__", _wrap_stop),
    )
    for cls, name, wrap in targets:
        original = cls.__dict__.get(name)
        if original is None:
            _uninstall()
            return f"patchright has no {cls.__name__}.{name}"
        _ORIGINALS[(cls, name)] = original
        setattr(cls, name, wrap(original))
    return None


def _uninstall() -> None:
    for (cls, name), original in _ORIGINALS.items():
        setattr(cls, name, original)
    _ORIGINALS.clear()


@contextlib.contextmanager
def observe_navigation(path: Path, *, label: str) -> Iterator[NavigationRecorder]:
    """Record every browser this process launches while entered, into *path*.

    The block's own exception, a cancellation included, leaves unchanged.
    """
    recorder = NavigationRecorder(path, label=label)
    if not _ACTIVE:
        unavailable = _install()
        recorder.document["observer"]["unavailable"] = unavailable
    recorder.document["observer"]["installed"] = bool(_ORIGINALS)
    _ACTIVE.append(recorder)
    recorder.persist()
    try:
        yield recorder
    except BaseException as exc:
        recorder.finish(exc)
        raise
    else:
        recorder.finish(None)
    finally:
        _ACTIVE.remove(recorder)
        if not _ACTIVE:
            _uninstall()
        recorder.detach()


def requested_ends(path: Path) -> dict[str, float | None] | None:
    """When the observed process asked its browser root and driver to end.

    By wall clock, which the sampler shares with any process on the machine.
    None when its record cannot be read, or when it saw no launch: a driver
    started before the observation has a stop it could not see, and an end
    read against a request nobody could record would be invented.
    """
    document = read_document(path) or {}
    launch = document.get("launch")
    if not isinstance(launch, dict) or launch.get("started_ms") is None:
        return None
    driver = document.get("driver")
    stop = driver.get("stop_requested_wall") if isinstance(driver, dict) else None
    close = document.get("close_requested_wall")
    asked = [t for t in (close, stop) if isinstance(t, (int, float))]
    return {
        "browser-root": min(asked) if asked else None,
        "driver": stop if isinstance(stop, (int, float)) else None,
    }


# --- The lifetimes, from the harness process ----------------------------------


class LifetimeSampler:
    """The browser root on *profile* and its driver, among this process's
    descendants, each as an identity and an interval."""

    def __init__(
        self,
        path: Path,
        *,
        profile: Path,
        label: str,
        interval: float = SAMPLE_SECONDS,
    ) -> None:
        self._file = _JsonFile(path)
        self._profile = _spelling(profile)
        self._interval = interval
        self._began_ns = time.monotonic_ns()
        self._began_wall = time.time()
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, name=f"lifetimes-{label}", daemon=True
        )
        #: Tracked lifetimes by (pid, create time), each the published record.
        self._tracked: dict[tuple[int, float], dict[str, Any]] = {}
        #: Descendants established as not the root: Chromium's own children.
        self._settled: set[tuple[int, float]] = set()
        self.document: dict[str, Any] = {
            "label": label,
            "began_wall": self._began_wall,
            "ended_ms": None,
            "outcome": "running",
            "error_type": None,
            "interval_seconds": interval,
            "samples": 0,
            "sample_failures": 0,
            "truncated_samples": 0,
            "sampler": "running",
            "stop_seconds": None,
            "stopped_within_bound": None,
            "requested_ends": None,
            "processes": [],
            "dropped_processes": 0,
            "write_failures": 0,
        }
        self._persist()

    def _ms(self) -> float:
        return round((time.monotonic_ns() - self._began_ns) / 1e6, 1)

    def _persist(self) -> None:
        self.document["write_failures"] = self._file.failures
        self._file.write(self.document)

    def start(self) -> LifetimeSampler:
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            import psutil
        except ImportError:
            with self._lock:
                self.document["sampler"] = "unavailable: psutil is not importable"
                self._persist()
            return
        try:
            me = psutil.Process()
            while True:
                self._sample(psutil, me)
                if self._halt.wait(self._interval):
                    break
            # The closing sample reads the ends, but on a loaded host one sample
            # can take longer than the whole stop bound, so it gets half of it.
            self._sample(psutil, me, deadline=time.monotonic() + STOP_SECONDS / 2)
        except Exception as exc:  # noqa: BLE001 - the sampler ends, the row does not
            with self._lock:
                self.document["sampler"] = f"failed: {type(exc).__name__}"
                self._persist()

    def _is_root(self, arguments: Iterable[str]) -> bool | None:
        """True for the root on the profile, False for any other Chromium
        process, None for a process that is not Chromium (yet)."""
        arguments = list(arguments)
        if any(argument.startswith(CHILD_TYPE_FLAG) for argument in arguments):
            return False
        for argument in arguments:
            if argument.startswith(USER_DATA_DIR_FLAG):
                named = argument[len(USER_DATA_DIR_FLAG) :].strip('"')
                return bool(named) and _spelling(named) == self._profile
        return None

    def _track(self, key: tuple[int, float], role: str, now: float) -> None:
        if key in self._tracked:
            return
        if len(self._tracked) >= MAX_PROCESSES:
            self.document["dropped_processes"] += 1
            return
        record = {
            "role": role,
            "pid": key[0],
            "create_time": key[1],
            "identified_ms": now,
            "last_alive_ms": now,
            "gone_ms": None,
            "ended": None,
            "exit_code": None,
            "exit_code_source": _ROOT_EXIT_CODE
            if role == "browser-root"
            else (
                "not observable here: the sampler is not the driver's parent; "
                "see the driving process's own record"
            ),
        }
        self._tracked[key] = record
        self.document["processes"].append(record)

    def _sample(self, psutil: Any, me: Any, *, deadline: float | None = None) -> None:
        try:
            children = me.children(recursive=True)
        except Exception:  # noqa: BLE001 - not knowing, asked again next time
            with self._lock:
                self.document["sample_failures"] += 1
            return
        now = self._ms()
        alive: set[tuple[int, float]] = set()
        changed = False
        truncated = False
        with self._lock:
            self.document["samples"] += 1
        # Process reads stay outside the lock, so a stop can record that it
        # gave up while one of them is still blocked.
        for child in children:
            if (
                time.monotonic() > deadline
                if deadline is not None
                else self._halt.is_set()
            ):
                truncated = True
                break
            try:
                key = (child.pid, child.create_time())
            except Exception:  # noqa: BLE001 - unreadable, not shown gone
                continue
            if key in self._tracked:
                try:
                    zombie = child.status() == psutil.STATUS_ZOMBIE
                except Exception:  # noqa: BLE001 - unreadable, asked again
                    alive.add(key)
                    continue
                if zombie:
                    # Read after the sample began, so unordered against a close
                    # asked for while this sample was still reading.
                    with self._lock:
                        self._tracked[key]["gone_ms"] = self._ms()
                        self._tracked[key]["order_uncertain"] = True
                    changed = True
                else:
                    alive.add(key)
                continue
            if key in self._settled:
                continue
            try:
                # Read again on every sample until it is settled: a child
                # forked by the driver shows the driver's command line
                # until it executes Chromium.
                root = self._is_root(child.cmdline())
            except Exception:  # noqa: BLE001 - read again next time
                continue
            if root is False:
                self._settled.add(key)
                continue
            if root is None:
                continue
            self._settled.add(key)
            self._track(key, "browser-root", now)
            alive.add(key)
            changed = True
            try:
                parent = child.parent()
                if parent is not None:
                    parent_key = (parent.pid, parent.create_time())
                    self._track(parent_key, "driver", now)
                    alive.add(parent_key)
            except Exception:  # noqa: BLE001 - the driver stays unidentified
                pass
        with self._lock:
            if truncated:
                # Cut short, so a process not reached is unknown, not gone.
                self.document["truncated_samples"] += 1
            for key, record in self._tracked.items():
                if key in alive:
                    record["last_alive_ms"] = now
                elif not truncated and record["gone_ms"] is None:
                    record["gone_ms"] = now
                    changed = True
            if changed:
                self._persist()

    def stop(
        self,
        *,
        exc: BaseException | None,
        requested: Mapping[str, float | None] | None,
    ) -> None:
        """End the sampler within ``STOP_SECONDS`` and read each end.

        *requested* is ``requested_ends`` of the driving process's record; None
        when it could not be read, which leaves every end unknown. Only the
        first call counts: what it read is not rewritten by a later one.
        """
        if self.document["ended_ms"] is not None:
            return
        try:
            began = time.monotonic()
            self._halt.set()
            if self._thread.is_alive():
                self._thread.join(STOP_SECONDS)
            stopped = not self._thread.is_alive()
            # A write can still hold the lock. Waiting without a bound would
            # spend the stop's own bound on it.
            if not self._lock.acquire(timeout=0 if stopped else STOP_SECONDS):
                self.document["stopped_within_bound"] = False
                self.document["stop_seconds"] = round(time.monotonic() - began, 3)
                self.document["ended_ms"] = self._ms()
                return
            try:
                outcome, error = _outcome(exc)
                self.document.update(
                    outcome=outcome,
                    error_type=error,
                    ended_ms=self._ms(),
                    stop_seconds=round(time.monotonic() - began, 3),
                    stopped_within_bound=stopped,
                    requested_ends=dict(requested) if requested is not None else None,
                )
                if self.document["sampler"] == "running":
                    self.document["sampler"] = "stopped" if stopped else "still running"
                for record in self._tracked.values():
                    record["ended"] = self._ended(record, requested)
                self._persist()
            finally:
                self._lock.release()
        except Exception:  # noqa: BLE001 - diagnostics never fail what they watch
            pass

    def _ended(
        self, record: Mapping[str, Any], requested: Mapping[str, float | None] | None
    ) -> str:
        if record["gone_ms"] is None:
            if self.document["truncated_samples"] or self.document["sample_failures"]:
                return "unknown"
            return "alive-at-stop"
        if requested is None:
            return "unknown"
        asked = requested.get(record["role"])
        if asked is None:
            return "unexpected"
        asked_ms = (asked - self._began_wall) * 1000
        if record.get("order_uncertain"):
            return "ambiguous"
        if record["gone_ms"] <= asked_ms:
            return "unexpected"
        if record["last_alive_ms"] >= asked_ms:
            return "after-close-request"
        return "ambiguous"


@contextlib.contextmanager
def observing(
    directory: Path, *, profile: Path, label: str, in_process: bool = True
) -> Iterator[Path]:
    """One launch's navigation and lifetimes, in *directory*, from before it.

    Yields where the navigation is recorded. *in_process* False is a launch
    another process drives, which records its own navigation there
    (``baseline_stage.py``); the lifetimes are read here either way, and each
    end against what that record says was asked for.
    """
    navigation = directory / FIRST_NAVIGATION_FILE
    try:
        lifetimes: LifetimeSampler | None = LifetimeSampler(
            directory / BROWSER_LIFETIMES_FILE, profile=profile, label=label
        )
        lifetimes.start()
    except Exception:  # noqa: BLE001 - no sampler, the launch still happens
        lifetimes = None
    failure: BaseException | None = None
    try:
        with (
            observe_navigation(navigation, label=label)
            if in_process
            else contextlib.nullcontext()
        ):
            yield navigation
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if lifetimes is not None:
            lifetimes.stop(exc=failure, requested=requested_ends(navigation))


# --- The session's lineage -----------------------------------------------------


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _linkedin(domain: Any) -> bool:
    return isinstance(domain, str) and (
        domain.lstrip(".") == _LINKEDIN or domain.endswith("." + _LINKEDIN)
    )


def _names(names: Iterable[Any]) -> list[str]:
    return sorted({name[:64] for name in names if isinstance(name, str)})[:MAX_NAMES]


def read_cookie_file(path: Path, expected_digest: str | None) -> dict[str, Any]:
    """Counts and names of a ``cookies.json``; a value only through its digest."""
    reading: dict[str, Any] = {
        "present": path.is_file(),
        "error": None,
        "entries": None,
        "linkedin_entries": None,
        "linkedin_names": [],
        "li_at_entries": None,
        "li_at_staged": None,
    }
    if not reading["present"]:
        return reading
    try:
        entries = json.loads(path.read_bytes())
        if not isinstance(entries, list):
            raise ValueError("not a list")
    except (OSError, ValueError) as exc:
        reading["error"] = type(exc).__name__
        return reading
    linkedin = [
        e for e in entries if isinstance(e, dict) and _linkedin(e.get("domain"))
    ]
    li_at = [e for e in entries if isinstance(e, dict) and e.get("name") == "li_at"]
    reading.update(
        entries=len(entries),
        linkedin_entries=len(linkedin),
        linkedin_names=_names(e.get("name") for e in linkedin),
        li_at_entries=len(li_at),
    )
    if expected_digest is not None:
        reading["li_at_staged"] = any(
            isinstance(e.get("value"), str) and _digest(e["value"]) == expected_digest
            for e in li_at
        )
    return reading


def read_cookie_store(profile: Path) -> dict[str, Any]:
    """Counts and names in the profile's own cookie store, never a value.

    Read-only and immutable: no lock is taken and no journal written, so the
    browser that opens the profile next finds it as it was. Immutable also
    means a journal the last browser left is not applied; its presence is
    reported, so a reading that may miss its tail says so.
    """
    reading: dict[str, Any] = {
        "store": None,
        "journal": [],
        "error": None,
        "rows": None,
        "linkedin_rows": None,
        "linkedin_names": [],
        "li_at_rows": None,
    }
    store = next(
        (
            profile.joinpath(*parts)
            for parts in _COOKIE_STORES
            if profile.joinpath(*parts).is_file()
        ),
        None,
    )
    if store is None:
        return reading
    reading["store"] = "/".join(store.relative_to(profile).parts)
    reading["journal"] = [
        suffix
        for suffix in ("-journal", "-wal")
        if store.with_name(store.name + suffix).exists()
    ]
    try:
        uri = f"{Path(os.path.abspath(store)).as_uri()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            rows = connection.execute(
                "SELECT host_key, name FROM cookies LIMIT 100000"
            ).fetchall()
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        reading["error"] = type(exc).__name__
        return reading
    linkedin = [name for host, name in rows if _linkedin(host)]
    reading.update(
        rows=len(rows),
        linkedin_rows=len(linkedin),
        linkedin_names=_names(linkedin),
        li_at_rows=sum(1 for name in linkedin if name == "li_at"),
    )
    return reading


def record_cookie_lineage(
    path: Path,
    *,
    point: str,
    profile: Path,
    cookie_file: Path,
    expected_digest: str | None,
) -> None:
    """Append one reading of the cookie file and the store, named *point*.

    Never raises: a reading that fails is recorded as failed, and a file that
    cannot be written leaves the row as it was.
    """
    try:
        document = read_document(path) or {"readings": [], "dropped": 0}
        readings = document.setdefault("readings", [])
        if len(readings) >= MAX_READINGS:
            document["dropped"] = int(document.get("dropped") or 0) + 1
        else:
            readings.append(
                {
                    "point": point,
                    "wall": time.time(),
                    "file": read_cookie_file(cookie_file, expected_digest),
                    "store": read_cookie_store(profile),
                }
            )
        file = _JsonFile(path)
        file.write(document)
        file.flush()
    except Exception:  # noqa: BLE001 - diagnostics never fail what they watch
        pass


def record_origin(
    path: Path, requests: Iterable[Any], decisions: Iterable[Any]
) -> None:
    """Add what the synthetic origin and its proxy saw to *path*'s record.

    The origin's own arrival times and paths, the cookie names each request
    sent and whether the origin accepted its session; the proxy's decisions,
    which carry no time. Nothing the origin keeps out of its own records.
    """
    try:
        # A driving process that wrote nothing leaves only this, which says so.
        document = read_document(path) or {"record": "absent"}
        requests, decisions = list(requests), list(decisions)
        document["origin"] = {
            "requests": [
                {
                    "wall": getattr(r, "t", None),
                    "host": _text(getattr(r, "host", None)),
                    "path": _text(urlsplit(getattr(r, "path", "")).path),
                    "cookie_names": _names(getattr(r, "cookie_names", ())),
                    "session_valid": getattr(r, "session_valid", None),
                }
                for r in requests[:MAX_EVENTS]
            ],
            "dropped_requests": max(0, len(requests) - MAX_EVENTS),
            "proxy_decisions": [
                {
                    "method": _text(getattr(d, "method", None)),
                    "host": _text(getattr(d, "host", None)),
                    "port": getattr(d, "port", None),
                    "forwarded": getattr(d, "forwarded", None),
                }
                for d in decisions[:MAX_EVENTS]
            ],
            "dropped_decisions": max(0, len(decisions) - MAX_EVENTS),
        }
        file = _JsonFile(path)
        file.write(document)
        file.flush()
    except Exception:  # noqa: BLE001 - diagnostics never fail what they watch
        pass
