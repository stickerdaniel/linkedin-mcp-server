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
    observing,
    record_origin,
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
    # The last write started before this read has landed: writers replace the
    # file on their own thread, so reading at once can see the one before.
    _flush_evidence()
    document = read_document(path)
    assert document is not None, f"{path.name} was not written"
    return document


def _flush_evidence() -> None:
    from differential.first_navigation import flush_evidence

    flush_evidence()


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
    assert record["first_failed_navigation"] == 0
    # Read when the goto gave up. The phases themselves are not: the recorder
    # listens until the page closes, so a stall that delays the close records
    # the answer at 35 s as well.
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


# --- A relaunch inside one observation --------------------------------------------


@_browser_case
async def test_a_relaunch_keeps_the_first_launch_and_reads_its_own_ends(tmp_path):
    """Staging's relaunch after a stall: two browsers on one profile, observed
    by the harness's own ``observing``. The first launch keeps the top level
    and its stalled navigation; the second is appended, and its browser lost
    before its own close reads as unexpected, not as ended by the first's."""
    evidence = tmp_path / "evidence"
    profile = tmp_path / "profile"

    def roots() -> list[dict[str, Any]]:
        document = read_document(evidence / BROWSER_LIFETIMES_FILE) or {}
        found = [
            p for p in document.get("processes", []) if p["role"] == "browser-root"
        ]
        return sorted(found, key=lambda p: p["identified_ms"])

    with _Origin() as origin:
        with observing(evidence, profile=profile, label="relaunch") as path:
            async with _browser(profile) as context:
                with pytest.raises(PlaywrightTimeoutError):
                    await context.pages[0].goto(
                        origin.url("/black-hole"), timeout=1_000
                    )
                await _until(roots, 15, "the first browser root was not identified")
            async with _browser(profile) as context:
                page = context.pages[0]
                await page.goto(origin.url("/"))
                second = await _until(
                    lambda: len(roots()) == 2 and roots()[1],
                    15,
                    "the second browser root was not identified",
                )
                root = psutil.Process(second["pid"])
                # The driver reaps its browser. Held stopped across the kill,
                # the sampler can only find the root as a zombie, which is the
                # read the CI runs raced into.
                reaper = root.parent()
                reaper.suspend()
                try:
                    root.kill()
                    await _until(
                        lambda: roots()[1]["gone_ms"] is not None,
                        15,
                        "the second browser root was not seen gone",
                    )
                finally:
                    reaper.resume()
    record = _record(path)
    assert [n["launch"] for n in record["navigations"]] == [1, 2]
    assert (record["first_failed_navigation"], record["stalled_phase"]) == (
        0,
        "response",
    )
    (relaunch,) = record["relaunches"]
    assert record["close_requested_ms"] < relaunch["launch"]["started_ms"]
    assert relaunch["close_requested_ms"] > relaunch["launch"]["started_ms"]
    assert record["driver"]["pid"] != relaunch["driver"]["pid"]
    first, second = (root["ended"] for root in roots())
    assert first in ("after-close-request", "ambiguous")
    assert second == "unexpected"


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
            _flush_evidence()
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
    lifetimes.stop(exc=None, requested={})
    assert record["ended"] == "unknown"


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


def _listed(pid: int, status: Any) -> SimpleNamespace:
    """A descendant as ``children`` lists it, with its create time cached."""
    return SimpleNamespace(pid=pid, create_time=lambda: 1.0, status=status)


def _reaped(pid: int) -> Any:
    def status() -> str:
        raise psutil.NoSuchProcess(pid)

    return status


def test_a_root_reaped_after_the_listing_is_gone_not_unreadable(tmp_path):
    lifetimes = LifetimeSampler(
        tmp_path / BROWSER_LIFETIMES_FILE, profile=tmp_path / "profile", label="reaped"
    )
    lifetimes._track((4242, 1.0), "browser-root", 0.0)
    lifetimes._track((4141, 1.0), "driver", 0.0)
    # The driver reaped the killed root between the listing and the read.
    listed = [_listed(4242, _reaped(4242)), _listed(4141, lambda: "running")]
    me = SimpleNamespace(children=lambda recursive: listed)
    lifetimes._sample(psutil, me)
    lifetimes.stop(exc=None, requested={})
    ends = {
        p["role"]: p["ended"]
        for p in _record(tmp_path / BROWSER_LIFETIMES_FILE)["processes"]
    }
    assert ends == {"browser-root": "unexpected", "driver": "alive-at-stop"}


def _unreadable(pid: int) -> Any:
    def status() -> str:
        raise psutil.AccessDenied(pid)

    return status


_RUNNING = SimpleNamespace(
    children=lambda recursive: [_listed(4141, lambda: "running")]
)
_UNREADABLE = SimpleNamespace(
    children=lambda recursive: [_listed(4141, _unreadable(4141))]
)


@pytest.mark.parametrize(
    ("first", "last", "ended"),
    [
        # A routine sample the stop request cut short, then the closing one.
        ((_RUNNING, time.monotonic() - 1), (_RUNNING, None), "alive-at-stop"),
        # An earlier sample that could not read the driver, then one that did.
        ((_UNREADABLE, None), (_RUNNING, None), "alive-at-stop"),
        # The driver seen alive, then a closing sample that could not read it.
        ((_RUNNING, None), (_UNREADABLE, None), "unknown"),
    ],
    ids=["cut-short-then-read", "unreadable-then-read", "read-then-unreadable"],
)
def test_only_the_closing_sample_says_whether_a_process_is_alive(
    tmp_path, first, last, ended
):
    lifetimes = LifetimeSampler(
        tmp_path / BROWSER_LIFETIMES_FILE, profile=tmp_path / "profile", label="last"
    )
    lifetimes._track((4141, 1.0), "driver", 0.0)
    for me, deadline in (first, last):
        lifetimes._sample(psutil, me, deadline=deadline)
    lifetimes.stop(exc=None, requested={})
    (driver,) = _record(tmp_path / BROWSER_LIFETIMES_FILE)["processes"]
    assert driver["ended"] == ended


def test_the_origin_record_drops_the_query(tmp_path):
    from types import SimpleNamespace

    from differential.events import publish

    path = tmp_path / FIRST_NAVIGATION_FILE
    record_origin(
        path,
        [SimpleNamespace(t=1.0, host="localhost", path="/feed/?token=query-secret")],
        [],
    )
    assert _record(path)["origin"]["requests"][0]["path"] == "/feed/"
    out = tmp_path / "published"
    publish(tmp_path, str(out), "run")
    assert b"query-secret" not in (out / "run" / FIRST_NAVIGATION_FILE).read_bytes()


def test_a_stop_does_not_wait_out_a_read_past_its_bound(tmp_path, monkeypatch):
    # One read holds the sampler's lock for far longer than the stop bound.
    # The stop ends at the bound and reports that it did, instead of waiting
    # the read out and calling that on time.
    holding = threading.Event()
    release = threading.Event()

    class Blocking:
        pid = 7

        def create_time(self) -> float:
            holding.set()
            release.wait()
            return 1.0

        def status(self) -> str:
            return psutil.STATUS_RUNNING

        def cmdline(self) -> list[str]:
            return ["not-chromium"]

    monkeypatch.setattr(
        psutil.Process, "children", lambda self, recursive: [Blocking()]
    )
    lifetimes = LifetimeSampler(
        tmp_path / BROWSER_LIFETIMES_FILE, profile=tmp_path / "profile", label="held"
    )
    # A driver the last finished sample saw alive, which may die while the
    # next one is blocked.
    lifetimes._track((8, 1.0), "driver", 0.0)
    driver = SimpleNamespace(children=lambda recursive: [_listed(8, lambda: "running")])
    lifetimes._sample(psutil, driver)
    lifetimes.start()
    assert holding.wait(5), "the sampler never reached the blocking read"
    stopper = threading.Thread(target=lambda: lifetimes.stop(exc=None, requested=None))
    stopper.start()
    stopper.join(STOP_SECONDS * 2)
    assert not stopper.is_alive(), "the stop waited out the blocked read"
    release.set()
    stopper.join(5)
    assert lifetimes.document["stopped_within_bound"] is False
    (record,) = lifetimes.document["processes"]
    assert record["ended"] == "unknown"


def test_an_unreadable_process_is_not_recorded_as_gone(tmp_path):
    lifetimes = LifetimeSampler(
        tmp_path / BROWSER_LIFETIMES_FILE, profile=tmp_path / "profile", label="unread"
    )
    lifetimes._track((5151, 1.0), "browser-root", 0.0)

    class Unreadable:
        pid = 5151

        def create_time(self) -> float:
            return 1.0

        def status(self) -> str:
            raise psutil.AccessDenied(self.pid)

    lifetimes._sample(
        psutil, SimpleNamespace(children=lambda recursive: [Unreadable()])
    )
    (record,) = lifetimes.document["processes"]
    # The one promise: an unreadable sample never invents a time of death, so
    # a later sample that can read the process may still find it alive.
    assert record["gone_ms"] is None


def test_a_close_after_the_machine_slept_is_not_unexpected(tmp_path, monkeypatch):
    """The monotonic clock stops while the machine sleeps; the wall clock, which
    the close request is stamped with, does not."""
    lifetimes = LifetimeSampler(
        tmp_path / BROWSER_LIFETIMES_FILE, profile=tmp_path / "profile", label="slept"
    )
    lifetimes._track((4242, 1.0), "browser-root", 0.0)
    alive = SimpleNamespace(
        children=lambda recursive: [_listed(4242, lambda: "running")]
    )
    gone = SimpleNamespace(children=lambda recursive: [])
    lifetimes._sample(psutil, alive)
    awake = time.time
    monkeypatch.setattr(time, "time", lambda: awake() + 370)
    asked = time.time()
    lifetimes._sample(psutil, alive)
    lifetimes._sample(psutil, gone)
    lifetimes.stop(exc=None, requested={"browser-root": asked})
    (root,) = _record(tmp_path / BROWSER_LIFETIMES_FILE)["processes"]
    assert root["ended"] == "after-close-request"


def test_a_wall_clock_set_back_leaves_the_order_open(tmp_path, monkeypatch):
    lifetimes = LifetimeSampler(
        tmp_path / BROWSER_LIFETIMES_FILE, profile=tmp_path / "profile", label="back"
    )
    lifetimes._track((4242, 1.0), "browser-root", 0.0)
    alive = SimpleNamespace(
        children=lambda recursive: [_listed(4242, lambda: "running")]
    )
    gone = SimpleNamespace(children=lambda recursive: [])
    asked = time.time()
    # Seen alive after the close was asked for, then gone once the clock is set
    # back past the request.
    lifetimes._sample(psutil, alive)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 60)
    lifetimes._sample(psutil, gone)
    lifetimes.stop(exc=None, requested={"browser-root": asked})
    (root,) = _record(tmp_path / BROWSER_LIFETIMES_FILE)["processes"]
    assert root["ended"] == "ambiguous"


@pytest.mark.parametrize(
    ("while_reading", "ended"),
    [
        # Nothing happened between the sample's start and the read.
        (None, "unexpected"),
        # The close was asked for after the sample began, before the zombie
        # was read: the death may have followed it.
        ("close", "ambiguous"),
        # The clock was set back after the close, before the zombie was read.
        ("clock-back", "ambiguous"),
    ],
)
def test_a_zombie_is_ordered_by_when_it_was_read(
    tmp_path, monkeypatch, while_reading, ended
):
    """A root its driver has not reaped yet is gone. Its end is ordered by when
    the sampler read it, never by when that sample began."""
    lifetimes = LifetimeSampler(
        tmp_path / BROWSER_LIFETIMES_FILE, profile=tmp_path / "profile", label="order"
    )
    lifetimes._track((6161, 1.0), "browser-root", 0.0)
    asked: dict[str, float] = {}
    real = time.time

    def zombie() -> str:
        if while_reading is not None and not asked:
            asked["browser-root"] = real()
            time.sleep(0.02)
        if while_reading == "clock-back":
            monkeypatch.setattr(time, "time", lambda: real() - 60)
        return psutil.STATUS_ZOMBIE

    lifetimes._sample(
        psutil, SimpleNamespace(children=lambda recursive: [_listed(6161, zombie)])
    )
    (record,) = lifetimes.document["processes"]
    first_gone = record["gone_ms"]
    lifetimes._sample(
        psutil, SimpleNamespace(children=lambda recursive: [_listed(6161, zombie)])
    )
    assert record["gone_ms"] == first_gone, "only the first read is its end"
    asked.setdefault("browser-root", real() + 5)
    lifetimes.stop(exc=None, requested=asked)
    assert record["ended"] == ended


def test_a_sampler_that_cannot_start_does_not_stop_the_launch(tmp_path, monkeypatch):
    real_start = threading.Thread.start

    def start(self) -> None:
        if self.name.startswith("lifetimes-"):
            raise RuntimeError("can't start new thread")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    entered = []
    with observing(tmp_path, profile=tmp_path / "profile", label="unstarted"):
        entered.append(True)
    assert entered == [True]
    document = read_document(tmp_path / BROWSER_LIFETIMES_FILE)
    assert document is None or document["samples"] == 0
