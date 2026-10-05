"""The first-navigation evidence names the phase, and keeps the failure.

Every browser here is a real Chromium on a disposable empty profile, against
a loopback origin this module serves, never LinkedIn and never the synthetic
origin's CA. Each stalled or lost case still fails exactly as it would
unobserved, and the evidence says where it stood. The harness cases run the
real row entry with its staging made to fail before any row record exists.

The browser cases skip where no Chromium is installed, as the other
``browser_contract`` tests do; CI's full suite installs one.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import psutil
import pytest
from patchright.async_api import Error as PlaywrightError
from patchright.async_api import TimeoutError as PlaywrightTimeoutError
from patchright.async_api import async_playwright

from differential import harness
from differential.baseline import REPO_ROOT, BaselineRefused, Runtime
from differential.events import EventLog, publish
from differential.first_navigation import (
    BROWSER_LIFETIMES_FILE,
    COOKIE_LINEAGE_FILE,
    FIRST_NAVIGATION_FILE,
    STOP_SECONDS,
    LifetimeSampler,
    observe_navigation,
    read_document,
    record_cookie_lineage,
    requested_ends,
)
from differential.session import StagingError


def _browser_case(test: Callable[..., Any]) -> Callable[..., Any]:
    """A case that launches Chromium: on the worker every such case shares."""
    return pytest.mark.xdist_group("browser_runtime")(
        pytest.mark.browser_contract(test)
    )


#: A value that must never reach an artefact.
SENTINEL = "sentinel-7b0c1f9e4d2a"

_DOCUMENT = b"<!doctype html><title>ok</title><p>ok"


class _Origin:
    """A loopback origin that answers each path its own way, or stalls.

    ``/`` answers at once; ``/black-hole`` says nothing for *hold* seconds;
    ``/headers`` sends a 200 promising a body and then stalls; ``/blocking``
    answers a document whose parser-blocking script, ``/stalled.js``, never
    comes. One request per connection. Every stall ends when it is closed.
    """

    def __init__(self, hold: float = 60.0) -> None:
        self.hold = hold
        self._release = threading.Event()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def __enter__(self) -> _Origin:
        return self

    def __exit__(self, *exc: object) -> None:
        self._release.set()
        self._listener.close()

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(
                target=self._serve, args=(connection,), daemon=True
            ).start()

    def _serve(self, connection: socket.socket) -> None:
        with connection:
            try:
                head = b""
                while b"\r\n\r\n" not in head:
                    chunk = connection.recv(4096)
                    if not chunk:
                        return
                    head += chunk
                path = head.split(b" ", 2)[1].split(b"?", 1)[0].decode()
                self._answer(connection, path)
            except OSError:
                return

    def _send(self, connection: socket.socket, body: bytes, length: int) -> None:
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
            + f"Content-Length: {length}\r\n\r\n".encode()
            + body
        )

    def _answer(self, connection: socket.socket, path: str) -> None:
        if path == "/black-hole":
            if not self._release.wait(self.hold):
                self._send(connection, _DOCUMENT, len(_DOCUMENT))
        elif path == "/headers":
            self._send(connection, b"", 4096)
            self._release.wait(self.hold)
        elif path == "/blocking":
            body = b'<!doctype html><script src="/stalled.js"></script><p>after'
            self._send(connection, body, len(body))
        elif path == "/stalled.js":
            self._release.wait(self.hold)
        else:
            self._send(connection, _DOCUMENT, len(_DOCUMENT))


@contextlib.asynccontextmanager
async def _browser(profile: Path) -> AsyncIterator[Any]:
    """A driver and a persistent context on *profile*, both ended after."""
    playwright = await async_playwright().start()
    try:
        try:
            context = await playwright.chromium.launch_persistent_context(
                profile, channel="chromium", headless=True
            )
        except Exception as exc:  # noqa: BLE001 - a missing browser
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield context
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(context.close(), 10)
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(playwright.stop(), 10)


def _record(path: Path) -> dict[str, Any]:
    document = read_document(path)
    assert document is not None, f"{path.name} was not written"
    return document


async def _until(condition: Callable[[], Any], seconds: float, what: str) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        await asyncio.sleep(0.05)
    pytest.fail(f"{what} within {seconds}s")


# --- Which phase a stalled first navigation reached ----------------------------


@_browser_case
async def test_a_black_hole_origin_is_a_navigation_stalled_before_its_response(
    tmp_path,
):
    """The Windows stall's shape: a first goto running out Patchright's own
    30s default against an origin that answers only after 35s."""
    path = tmp_path / "evidence" / FIRST_NAVIGATION_FILE
    with _Origin(hold=35) as origin:
        with observe_navigation(path, label="black-hole"):
            async with _browser(tmp_path / "profile") as context:
                with pytest.raises(PlaywrightTimeoutError):
                    await context.pages[0].goto(
                        origin.url("/black-hole"), wait_until="domcontentloaded"
                    )
    record = _record(path)
    (navigation,) = record["navigations"]
    assert navigation["outcome"] == "TimeoutError"
    assert navigation["timeout_ms"] is None, "the default bound, not one of ours"
    assert navigation["ended_ms"] - navigation["started_ms"] >= 29_000
    assert navigation["phases"]["request"] is not None
    assert navigation["phases"]["response"] is None
    assert record["first_failed_navigation"] == 0
    assert record["stalled_phase"] == "response"


@_browser_case
async def test_headers_and_a_commit_then_a_stalled_document_stall_before_domcontentloaded(
    tmp_path,
):
    path = tmp_path / "evidence" / FIRST_NAVIGATION_FILE
    with _Origin() as origin:
        with observe_navigation(path, label="headers"):
            async with _browser(tmp_path / "profile") as context:
                with pytest.raises(PlaywrightTimeoutError):
                    await context.pages[0].goto(
                        origin.url("/headers"),
                        wait_until="domcontentloaded",
                        timeout=5_000,
                    )
    record = _record(path)
    (navigation,) = record["navigations"]
    phases = navigation["phases"]
    assert navigation["outcome"] == "TimeoutError"
    assert navigation["response_status"] == 200
    assert phases["request"] is not None
    assert phases["response"] is not None
    assert phases["commit"] is not None
    assert phases["domcontentloaded"] is None
    assert record["stalled_phase"] == "domcontentloaded"


@_browser_case
async def test_a_parser_blocking_script_leaves_domcontentloaded_missing(tmp_path):
    """The whole document arrived; its parser waits on a script that never
    does. Also where each lifetime's exit code comes from, or why none does."""
    evidence = tmp_path / "evidence"
    profile = tmp_path / "profile"
    path = evidence / FIRST_NAVIGATION_FILE
    lifetimes = LifetimeSampler(
        evidence / BROWSER_LIFETIMES_FILE, profile=profile, label="blocking"
    ).start()
    try:
        with _Origin() as origin:
            with observe_navigation(path, label="blocking"):
                async with _browser(profile) as context:
                    with pytest.raises(PlaywrightTimeoutError):
                        await context.pages[0].goto(
                            origin.url("/blocking"),
                            wait_until="domcontentloaded",
                            timeout=5_000,
                        )
    finally:
        lifetimes.stop(exc=None, requested=requested_ends(path))
    record = _record(path)
    (navigation,) = record["navigations"]
    assert navigation["phases"]["commit"] is not None
    assert navigation["phases"]["domcontentloaded"] is None
    assert record["stalled_phase"] == "domcontentloaded"
    assert record["subresource_requests"] >= 1, "the script it waits on was asked for"
    # The driver stopped inside the observation, and its parent read its code.
    assert record["driver"]["exit_code"] == 0
    processes = {
        p["role"]: p for p in _record(evidence / BROWSER_LIFETIMES_FILE)["processes"]
    }
    root = processes["browser-root"]
    assert root["exit_code"] is None
    assert "no observer is its parent" in root["exit_code_source"]
    assert root["pid"] != record["driver"]["pid"]
    assert processes["driver"]["pid"] == record["driver"]["pid"]
    assert root["ended"] in ("after-close-request", "ambiguous")


# --- A browser lost under a living driver ---------------------------------------


@_browser_case
async def test_a_browser_killed_under_a_living_driver_is_an_unexpected_end(tmp_path):
    evidence = tmp_path / "evidence"
    profile = tmp_path / "profile"
    path = evidence / FIRST_NAVIGATION_FILE
    lifetimes_path = evidence / BROWSER_LIFETIMES_FILE
    lifetimes = LifetimeSampler(lifetimes_path, profile=profile, label="killed").start()
    try:
        with _Origin() as origin:
            with observe_navigation(path, label="killed") as recorder:
                async with _browser(profile) as context:
                    page = context.pages[0]

                    def root() -> dict[str, Any] | None:
                        document = read_document(lifetimes_path) or {}
                        return next(
                            (
                                p
                                for p in document.get("processes", [])
                                if p["role"] == "browser-root"
                            ),
                            None,
                        )

                    found = await _until(
                        root, 15, "the browser root was not identified"
                    )
                    process = psutil.Process(found["pid"])
                    assert process.create_time() == pytest.approx(
                        found["create_time"], abs=0.01
                    )
                    process.kill()
                    await _until(
                        lambda: any(
                            event["event"] == "context-closed"
                            for event in recorder.document["lifecycle"]
                        ),
                        15,
                        "the context did not report its browser gone",
                    )
                    with pytest.raises(PlaywrightError):
                        await page.goto(origin.url("/"), timeout=10_000)
                    # Read while the driver still lives and nothing asked
                    # either of them to end.
                    lifetimes.stop(exc=None, requested=requested_ends(path))
    finally:
        # Already stopped on the way through; this only covers a failure.
        lifetimes.stop(exc=None, requested=requested_ends(path))
    record = _record(path)
    (navigation,) = record["navigations"]
    assert navigation["outcome"] != "ok"
    assert record["stalled_phase"] == "request"
    # The teardown's close came after, and the lifetimes were read before it.
    assert record["close_requested_ms"] > navigation["ended_ms"]
    processes = {p["role"]: p for p in _record(lifetimes_path)["processes"]}
    assert processes["browser-root"]["gone_ms"] is not None
    assert processes["browser-root"]["ended"] == "unexpected"
    assert processes["browser-root"]["exit_code"] is None
    assert processes["driver"]["ended"] == "alive-at-stop"


# --- Observers that leave ---------------------------------------------------------


@_browser_case
async def test_observers_detach_and_the_sampler_stops_within_its_bound(tmp_path):
    evidence = tmp_path / "evidence"
    profile = tmp_path / "profile"
    path = evidence / FIRST_NAVIGATION_FILE
    label = f"detach-{os.getpid()}"
    lifetimes = LifetimeSampler(
        evidence / BROWSER_LIFETIMES_FILE, profile=profile, label=label
    ).start()
    with _Origin() as origin:
        async with contextlib.AsyncExitStack() as stack:
            with observe_navigation(path, label="detach"):
                context = await stack.enter_async_context(_browser(profile))
                await context.pages[0].goto(origin.url("/"))
            lifetimes.stop(exc=None, requested=requested_ends(path))
            observed = path.read_bytes()
            # The same browser goes on: another page, another navigation, a
            # second context of its own. None of it reaches the record.
            page = await context.new_page()
            await page.goto(origin.url("/"))
            await context.pages[0].goto(origin.url("/?again"))
            await page.close()
            assert path.read_bytes() == observed
    record = _record(path)
    (navigation,) = record["navigations"]
    assert navigation["outcome"] == "ok"
    assert record["observer"]["detached"] == record["observer"]["attached"] > 0
    assert record["observer"]["detach_failures"] == 0
    assert record["observer"]["errors"] == 0
    sampled = _record(evidence / BROWSER_LIFETIMES_FILE)
    assert sampled["stopped_within_bound"] is True
    assert sampled["stop_seconds"] <= STOP_SECONDS
    assert not any(
        thread.name == f"lifetimes-{label}" for thread in threading.enumerate()
    )


# --- A staging process that never finished ----------------------------------------

_STAGING_SCRIPT = """
import asyncio
import pathlib
import sys

# What the frozen baseline's interpreter has: no psutil, only ``tests``.
sys.modules["psutil"] = None
sys.path[0] = {tests!r}

from differential.first_navigation import observe_navigation
from patchright.async_api import async_playwright


async def main():
    playwright = await async_playwright().start()
    context = await playwright.chromium.launch_persistent_context(
        sys.argv[2], channel="chromium", headless=True
    )
    await context.pages[0].goto(sys.argv[3], timeout=120_000)


with observe_navigation(pathlib.Path(sys.argv[1]), label="killed-staging"):
    asyncio.run(main())
"""


@_browser_case
async def test_a_killed_staging_process_leaves_what_it_had_observed(tmp_path):
    """The frozen staging's case: its interpreter records, and the harness's
    timeout kills it mid-navigation. What it had written stays."""
    path = tmp_path / "evidence" / FIRST_NAVIGATION_FILE
    script = tmp_path / "stage.py"
    script.write_text(
        _STAGING_SCRIPT.format(tests=str(Path(__file__).resolve().parents[1]))
    )
    with _Origin() as origin:
        child = subprocess.Popen(
            [
                sys.executable,
                str(script),
                str(path),
                str(tmp_path / "profile"),
                origin.url("/black-hole"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:

            def requested() -> bool:
                document = read_document(path) or {}
                navigations = document.get("navigations") or []
                return bool(navigations and navigations[0]["phases"]["request"])

            deadline = time.monotonic() + 30
            while not requested():
                if child.poll() is not None:
                    pytest.skip(f"the staging process ended first: {child.stderr}")
                if time.monotonic() > deadline:
                    pytest.fail("the staging process never sent its request")
                await asyncio.sleep(0.05)
        finally:
            tree = [psutil.Process(child.pid)]
            with contextlib.suppress(psutil.Error):
                tree += tree[0].children(recursive=True)
            for process in tree:
                with contextlib.suppress(psutil.Error):
                    process.kill()
            psutil.wait_procs(tree, timeout=10)
            child.wait(10)
    record = _record(path)
    assert record["label"] == "killed-staging"
    assert record["outcome"] == "running", "the writer never finished"
    assert record["step"] == "navigation"
    assert record["observer"]["installed"] is True
    (navigation,) = record["navigations"]
    assert navigation["outcome"] == "running"
    assert navigation["phases"]["request"] is not None
    assert navigation["phases"]["response"] is None


# --- The session's lineage, and what never reaches an artefact --------------------


def _cookie(name: str, value: str, domain: str = ".linkedin.com") -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "expires": time.time() + 86_400,
        "httpOnly": True,
        "secure": True,
        "sameSite": "None",
    }


def _store_bytes(profile: Path) -> bytes:
    for parts in (("Default", "Network", "Cookies"), ("Default", "Cookies")):
        candidate = profile.joinpath(*parts)
        if candidate.is_file():
            return candidate.read_bytes()
    pytest.fail("the browser left no cookie store")


@_browser_case
async def test_a_zero_cookie_export_is_counted_beside_the_store_it_left(tmp_path):
    profile = tmp_path / "profile"
    async with _browser(profile) as context:
        await context.add_cookies(
            [_cookie("li_at", SENTINEL), _cookie("JSESSIONID", "ajax:1")]
        )
    staged_file = tmp_path / "cookies.json"
    staged_file.write_text(json.dumps([_cookie("li_at", SENTINEL)]))
    lineage = tmp_path / "evidence" / COOKIE_LINEAGE_FILE
    staged_digest = hashlib.sha256(SENTINEL.encode()).hexdigest()
    store = _store_bytes(profile)

    record_cookie_lineage(
        lineage,
        point="after-staging",
        profile=profile,
        cookie_file=staged_file,
        expected_digest=staged_digest,
    )
    # The row's browser exported nothing over the staged file.
    staged_file.write_text("[]")
    record_cookie_lineage(
        lineage,
        point="after-row",
        profile=profile,
        cookie_file=staged_file,
        expected_digest=staged_digest,
    )

    staging, row = _record(lineage)["readings"]
    assert staging["point"] == "after-staging"
    assert staging["file"]["li_at_staged"] is True
    assert staging["store"]["linkedin_names"] == ["JSESSIONID", "li_at"]
    assert row["point"] == "after-row"
    assert row["file"]["entries"] == 0
    assert row["file"]["li_at_staged"] is False
    assert row["store"]["li_at_rows"] == 1, "the store kept what the export lost"
    # Read, never changed: the export and the store are as they were.
    assert staged_file.read_text() == "[]"
    assert _store_bytes(profile) == store


@_browser_case
async def test_no_artefact_carries_a_secret(tmp_path):
    """The sentinel is in the URL's query, in a cookie the browser sends, in
    the cookie file and in the store, and in no file the evidence holds."""
    evidence = tmp_path / "evidence"
    profile = tmp_path / "profile"
    path = evidence / FIRST_NAVIGATION_FILE
    lifetimes = LifetimeSampler(
        evidence / BROWSER_LIFETIMES_FILE, profile=profile, label="secret"
    ).start()
    try:
        with _Origin() as origin:
            with observe_navigation(path, label="secret"):
                async with _browser(profile) as context:
                    await context.add_cookies(
                        [
                            _cookie("li_at", SENTINEL),
                            {
                                **_cookie("token", SENTINEL),
                                "domain": "127.0.0.1",
                                "secure": False,
                                "sameSite": "Lax",
                            },
                        ]
                    )
                    await context.pages[0].goto(
                        origin.url(f"/?token={SENTINEL}#{SENTINEL}")
                    )
    finally:
        lifetimes.stop(exc=None, requested=requested_ends(path))
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps([_cookie("li_at", SENTINEL)]))
    record_cookie_lineage(
        evidence / COOKIE_LINEAGE_FILE,
        point="after-staging",
        profile=profile,
        cookie_file=cookie_file,
        expected_digest=hashlib.sha256(SENTINEL.encode()).hexdigest(),
    )
    (navigation,) = _record(path)["navigations"]
    assert navigation["url"] == origin.url("/")
    written = sorted(p.name for p in evidence.rglob("*") if p.is_file())
    assert written == sorted(
        [BROWSER_LIFETIMES_FILE, COOKIE_LINEAGE_FILE, FIRST_NAVIGATION_FILE]
    )
    for artefact in evidence.rglob("*"):
        if artefact.is_file():
            assert SENTINEL.encode() not in artefact.read_bytes(), artefact.name


# --- Staging that fails before the row has a record --------------------------------


def _row_fakes(monkeypatch, tmp_path) -> tuple[Any, Any, EventLog]:
    account = harness.ActorAccount(tmp_path / "auth" / "profile")
    account.profile.mkdir(parents=True)
    monkeypatch.setattr(harness, "claim_account", lambda _: account)
    monkeypatch.setattr(harness, "row_identity", lambda: {})
    monkeypatch.setattr(harness, "evidence_refusal", lambda *a, **k: None)
    origin = SimpleNamespace(requests=[], accept_session=lambda _value: None)
    proxy = SimpleNamespace(decisions=[], url="http://127.0.0.1:9")
    return account, (origin, proxy), EventLog(tmp_path / "evidence", run="staging")


async def _row(account, egress, log: EventLog, **row: Any) -> None:
    await harness.measure_host_quit_row(
        profile=account.profile,
        experiment="K3",
        daemon=True,
        egress=cast(Any, egress),
        log=log,
        work_dir=log.directory / "rows" / "K3",
        **row,
    )


async def test_a_staging_failure_before_the_row_record_is_published(
    monkeypatch, tmp_path
):
    account, egress, log = _row_fakes(monkeypatch, tmp_path)

    async def stage(*args: Any, **kwargs: Any) -> None:
        raise StagingError("the product's own import validation rejected it")

    monkeypatch.setattr(harness, "stage_signed_in_session", stage)
    with pytest.raises(StagingError, match="rejected it"):
        await _row(account, egress, log)

    row = log.directory / "rows" / "K3"
    assert not (row / "failures.json").exists(), "no row record was made"
    navigation = _record(row / FIRST_NAVIGATION_FILE)
    assert navigation["label"] == "candidate-staging"
    assert (navigation["outcome"], navigation["error_type"]) == (
        "failed",
        "StagingError",
    )
    assert navigation["step"] == "setup", "it failed before any browser launched"
    assert navigation["origin"]["requests"] == []
    lifetimes = _record(row / BROWSER_LIFETIMES_FILE)
    assert (lifetimes["outcome"], lifetimes["error_type"]) == ("failed", "StagingError")

    out = tmp_path / "published"
    publish(log.directory, str(out), "staging")
    published = out / "staging" / "rows" / "K3"
    assert (published / FIRST_NAVIGATION_FILE).read_bytes() == (
        row / FIRST_NAVIGATION_FILE
    ).read_bytes()
    assert (published / BROWSER_LIFETIMES_FILE).is_file()


async def test_a_cancelled_staging_is_recorded_as_cancelled(monkeypatch, tmp_path):
    account, egress, log = _row_fakes(monkeypatch, tmp_path)
    entered = asyncio.Event()

    async def stage(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(harness, "stage_signed_in_session", stage)
    task = asyncio.create_task(_row(account, egress, log))
    await asyncio.wait_for(entered.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = log.directory / "rows" / "K3"
    navigation = _record(row / FIRST_NAVIGATION_FILE)
    assert (navigation["outcome"], navigation["error_type"]) == (
        "cancelled",
        "CancelledError",
    )
    assert _record(row / BROWSER_LIFETIMES_FILE)["outcome"] == "cancelled"


async def test_a_frozen_staging_failure_is_recorded_by_the_staging_interpreter(
    monkeypatch, tmp_path
):
    """The real staging script in a subprocess of its own, told a profile
    other than the row's, so it refuses before any browser exists. Its record
    is its own; the harness adds the lifetimes and the origin beside it."""
    account, egress, log = _row_fakes(monkeypatch, tmp_path)
    elsewhere = tmp_path / "elsewhere" / "profile"
    monkeypatch.setattr(harness, "frozen_identity", lambda runtime: {})
    monkeypatch.setattr(harness, "frozen_refusal", lambda *a: None)
    monkeypatch.setattr(
        harness,
        "actor_environment",
        lambda *a, **k: {**os.environ, "USER_DATA_DIR": str(elsewhere)},
    )
    runtime = Runtime(sys.executable, REPO_ROOT, tmp_path / "browsers", pinned="0" * 40)
    with pytest.raises(BaselineRefused, match="StagingError"):
        await _row(account, egress, log, runtime=runtime)

    row = log.directory / "rows" / "K3"
    navigation = _record(row / FIRST_NAVIGATION_FILE)
    assert navigation["label"] == "frozen-staging"
    assert navigation["pid"] != os.getpid()
    assert (navigation["outcome"], navigation["error_type"]) == (
        "failed",
        "StagingError",
    )
    assert navigation["step"] == "setup"
    assert "origin" in navigation
    lifetimes = _record(row / BROWSER_LIFETIMES_FILE)
    assert (lifetimes["outcome"], lifetimes["error_type"]) == (
        "failed",
        "BaselineRefused",
    )
    assert lifetimes["label"] == "frozen-staging"


class _SlowChild:
    """A descendant whose every read takes *delay*, as on a loaded host."""

    def __init__(self, pid: int, delay: float) -> None:
        self.pid = pid
        self._delay = delay

    def create_time(self) -> float:
        time.sleep(self._delay)
        return 1.0

    def status(self) -> str:
        return psutil.STATUS_RUNNING

    def cmdline(self) -> list[str]:
        return ["not-chromium"]


def test_a_slow_process_table_cannot_hold_the_stop_past_its_bound(
    tmp_path, monkeypatch
):
    # One full sample here takes six seconds, three times the stop bound.
    slow = [_SlowChild(pid, 0.3) for pid in range(100_000, 100_020)]
    monkeypatch.setattr(psutil.Process, "children", lambda self, recursive: slow)
    label = f"slow-{os.getpid()}"
    lifetimes = LifetimeSampler(
        tmp_path / BROWSER_LIFETIMES_FILE, profile=tmp_path / "profile", label=label
    ).start()
    time.sleep(0.5)
    lifetimes.stop(exc=None, requested=None)
    sampled = _record(tmp_path / BROWSER_LIFETIMES_FILE)
    assert sampled["stopped_within_bound"] is True
    assert sampled["stop_seconds"] <= STOP_SECONDS
    assert sampled["truncated_samples"] >= 1


def test_a_cut_short_sample_marks_nothing_gone(tmp_path):
    lifetimes = LifetimeSampler(
        tmp_path / BROWSER_LIFETIMES_FILE, profile=tmp_path / "profile", label="cut"
    )
    lifetimes._track((4242, 1.0), "browser-root", 0.0)
    me = SimpleNamespace(children=lambda recursive: [_SlowChild(4242, 0.0)])
    # The deadline has passed before the first child is read, so the tracked
    # root is simply not reached: unknown, not gone.
    lifetimes._sample(psutil, me, deadline=time.monotonic() - 1)
    (record,) = lifetimes.document["processes"]
    assert record["gone_ms"] is None
    assert lifetimes.document["truncated_samples"] == 1


def test_the_closing_sample_still_reads_after_a_stop_request(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    root = SimpleNamespace(
        pid=4343,
        create_time=lambda: 1.0,
        status=lambda: psutil.STATUS_RUNNING,
        cmdline=lambda: ["chromium", f"--user-data-dir={profile}"],
        parent=lambda: None,
    )
    monkeypatch.setattr(psutil.Process, "children", lambda self, recursive: [root])
    lifetimes = LifetimeSampler(
        tmp_path / BROWSER_LIFETIMES_FILE, profile=profile, label="closing"
    )
    # Stopped before the first sample: the routine one gives way at once, and
    # only the closing sample is left to see the browser.
    lifetimes._halt.set()
    lifetimes._run()
    (record,) = lifetimes.document["processes"]
    assert (record["role"], record["pid"]) == ("browser-root", 4343)
