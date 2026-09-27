"""The post-quit session starts only when every actor of the row is settled.

Driven through the real row entry, ``measure_host_quit_row``, with everything
that would launch, signal or touch an owner replaced: staging, the watcher
process, the host session, owner discovery, cleanup and the post-quit session
itself. What runs for real is the profile census over a modelled process
table, the gate, and the row's own continuation. Only the census and the
watcher's summary differ between cases, and the question is whether the
post-quit session was started.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import psutil
import pytest

from differential import harness
from differential.events import EventLog
from differential.harness import DaemonCleanup, PostQuit, measure_host_quit_row
from differential.session import LAST_VERSION_FILE, write_synthetic_cookie_file
from differential.signals import (
    COMPLETE,
    UNAVAILABLE,
    OracleOutcome,
    parse_strace,
)
from differential.test_row_judgement import _healthy
from linkedin_mcp_server.session_state import portable_cookie_path, write_source_state

ME = "harness-user"


class _Watcher:
    summary: dict = {}
    records: list = []

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def observed(self):
        return list(self.records)

    def stop(self):
        return self.summary


class _Actor:
    """A modelled owner or server: the handle the row took when it tied it.

    Killed, it stays a zombie, as a killed owner does until its parent reaps
    it; otherwise it has already left.
    """

    def __init__(self, pid: int):
        self.pid = pid
        self.kills = 0

    def kill(self):
        self.kills += 1

    def status(self):
        if self.kills:
            return psutil.STATUS_ZOMBIE
        raise psutil.NoSuchProcess(self.pid)

    def num_threads(self):
        # SIGKILL ended every thread: the dead leader is all that is left.
        return 1

    def wait(self, timeout=None):
        return None


class _Oracle:
    """No strace: the unit rows never attach anything to a modelled pid.

    ``stop`` returns ``outcome``, unavailable unless a test sets another.
    """

    available = False
    unavailable = "modelled"
    scope = None
    outcome = OracleOutcome(status=UNAVAILABLE, reasons=["modelled"])

    def __init__(self, directory, *, required=False):
        self.required = required

    def start(self, pids):
        raise AssertionError("the oracle is not available")

    def stop(self, *, confirmed_dead=()):
        return dataclasses.replace(self.outcome, required=self.required)


class _Row:
    """The row entry, and what the modelled row handed to its owner cleanup."""

    def __init__(self, run, owner, cleaned: list, log: EventLog):
        self._run = run
        self.owner = owner
        self.cleaned = cleaned
        self.log = log

    async def __call__(self, **row):
        return await self._run(**row)


def _process(pid: int, *, cmdline, exe=None, user=ME, status="running"):
    return SimpleNamespace(
        pid=pid, info={"cmdline": cmdline, "exe": exe, "status": status}, user=user
    )


@pytest.fixture
def row(tmp_path, monkeypatch, profile):
    """Run the row with the modelled census and watcher summary given."""
    directory, staged = profile
    healthy = _healthy(profile, daemon=True)
    account = harness.ActorAccount(directory)
    preservation = AsyncMock(return_value=PostQuit(valid=True))
    origin = SimpleNamespace(requests=[], accept_session=lambda _value: None)
    proxy = SimpleNamespace(url="http://127.0.0.1:9", decisions=[])
    owner = harness.OwnerIdentity(
        42, 1.0, "synthetic", str(account.auth_root), _Actor(42)
    )
    cleaned: list = []

    async def host(*args, **kwargs):
        origin.requests.extend(healthy.row_requests)
        started = kwargs.get("started")
        if started is not None:
            started(4242)
        await kwargs["after_call"]()
        return healthy.host

    def identify(*args, **kwargs):
        # The descriptor names pid 42 until something replaces it; once 42 is
        # killed, it is no owner any longer.
        if owner.process.kills:
            return None, "pid 42 is not running"
        return owner, None

    monkeypatch.setattr(harness, "claim_account", lambda _: account)
    monkeypatch.setattr(harness, "row_identity", lambda: {})
    monkeypatch.setattr(harness, "evidence_refusal", lambda *a, **k: None)
    monkeypatch.setattr(
        harness, "stage_signed_in_session", AsyncMock(return_value=staged)
    )
    monkeypatch.setattr(
        harness, "resolved_browser_executable", AsyncMock(return_value="/b/chrome")
    )
    monkeypatch.setattr(harness, "actor_environment", lambda *a, **k: {})
    monkeypatch.setattr(harness, "Watcher", _Watcher)
    monkeypatch.setattr(harness, "run_host_session", host)
    monkeypatch.setattr(harness, "identify_owner", identify)
    monkeypatch.setattr(harness, "SignalOracle", _Oracle)
    monkeypatch.setattr(_Oracle, "outcome", _Oracle.outcome)
    # A published descriptor, which is what the row looks for before reading.
    published = tmp_path / "descriptor.json"
    published.write_text("{}")
    monkeypatch.setattr(
        harness.daemon_descriptor, "descriptor_path", lambda _root: published
    )
    monkeypatch.setattr(
        harness.daemon_descriptor,
        "read",
        lambda _: SimpleNamespace(
            pid=42, instance_id="synthetic", protocol_version=2, log_path=""
        ),
    )

    def retire(_account, identified):
        cleaned.append(identified)
        return DaemonCleanup("dir", True, False, True, True)

    monkeypatch.setattr(harness, "retire_daemon_state", retire)
    monkeypatch.setattr(harness, "observe_preservation", preservation)
    monkeypatch.setattr(harness, "harness_user", lambda: ME)
    monkeypatch.setattr(harness, "process_user", lambda process: process.user)

    # A frozen runtime's identity, staging and browser, asked of no interpreter.
    monkeypatch.setattr(harness, "frozen_identity", lambda runtime: {})
    monkeypatch.setattr(harness, "frozen_refusal", lambda *a: None)
    monkeypatch.setattr(harness, "stage_frozen_session", lambda *a, **k: None)
    monkeypatch.setattr(harness, "bundled_executable", lambda runtime: "/b/chrome")

    async def run(*, processes, summary, observed=(), **row):
        monkeypatch.setattr(
            harness.psutil, "process_iter", lambda *a, **k: list(processes)
        )
        _Watcher.summary = {**(healthy.watcher or {}), **summary}
        _Watcher.records = list(observed)
        row.setdefault("daemon", True)
        row.setdefault("experiment", "K3")
        result = await measure_host_quit_row(
            profile=directory,
            # Modelled: the row reads only their request and decision logs.
            egress=cast(Any, (origin, proxy)),
            log=EventLog(tmp_path / "evidence", run="gate"),
            work_dir=tmp_path / "row",
            **row,
        )
        return result, preservation.await_count

    return _Row(run, owner, cleaned, EventLog(tmp_path / "evidence", run="gate"))


@pytest.fixture
def profile(tmp_path):
    directory = tmp_path / "auth" / "profile"
    directory.mkdir(parents=True)
    (directory / LAST_VERSION_FILE).write_text("153.0.8010.12")
    staged = write_synthetic_cookie_file(portable_cookie_path(directory))
    write_source_state(directory)
    return directory, staged


_OPEN_POSSIBLE_BROWSER = {
    "pid": 777,
    "possible_browser": True,
    "resolution": "open",
    "failures": ["cmdline: AccessDenied"],
}
_FINISHED_PS = {
    "pid": 778,
    "exe": "/bin/ps",
    "possible_browser": False,
    "resolution": "exited",
    "failures": ["cmdline: AccessDenied"],
}


async def test_a_settled_complete_census_starts_the_post_quit_session_once(row):
    result, calls = await row(
        processes=[
            _process(10, cmdline=["python", "server"]),
            _process(11, cmdline=None, user="root"),
        ],
        summary={"read_failures": [_FINISHED_PS], "relevant_read_failures": []},
    )
    assert calls == 1
    assert result.post_quit is not None and result.post_quit.valid is True


@pytest.mark.parametrize(
    ("processes", "summary", "reported"),
    [
        pytest.param(
            [_process(777, cmdline=None)],
            {"relevant_read_failures": [_OPEN_POSSIBLE_BROWSER]},
            "incomplete",
            id="denied-arguments-and-open-episode",
        ),
        pytest.param(
            [_process(777, cmdline=None)],
            {"relevant_read_failures": []},
            "incomplete",
            id="denied-arguments",
        ),
        pytest.param(
            [_process(777, cmdline=None, exe="/b/chrome")],
            {"relevant_read_failures": []},
            "incomplete",
            id="denied-arguments-browser-exe",
        ),
        pytest.param(
            [],
            {"relevant_read_failures": [_OPEN_POSSIBLE_BROWSER]},
            "unresolved possible browsers",
            id="open-episode",
        ),
    ],
)
async def test_an_unsettled_census_starts_nothing(row, processes, summary, reported):
    result, calls = await row(processes=processes, summary=summary)
    assert calls == 0
    assert result.post_quit is not None and result.post_quit.valid is None
    assert any(reported in failure for failure in result.post_quit.failures)
    assert any("post-quit not run" in failure for failure in result.failures)


def _retained(profile: str) -> dict:
    """The watcher's note of a known root whose later argument read failed."""
    return {
        "pid": 777,
        "start_identity": 6.0,
        "exe": "/b/chrome",
        "first": 1.0,
        "last": 2.0,
        "failures": ["cmdline: AccessDenied"],
        "possible_browser": False,
        "resolution": "a known browser root's earlier reading retained",
        "retained_profile": profile,
    }


def _retained_summary(key: str, retained_profile: str) -> dict:
    return {
        "read_failures": [_retained(retained_profile)],
        "relevant_read_failures": [],
        "observation_start": 0.0,
        "observation_end": time.time() + 60.0,
        "max_roots": {key: 1},
    }


async def test_a_retained_row_root_does_not_settle_a_census_that_cannot_read_it(
    row, profile
):
    # The watcher's retained reading is history; the launch waits on what the
    # census reads now, and it cannot read the browser's arguments.
    key = harness.ActorAccount(profile[0]).browser_key
    result, calls = await row(
        processes=[_process(777, cmdline=None, exe="/b/chrome")],
        summary=_retained_summary(key, key),
    )
    assert calls == 0
    assert result.post_quit is not None and result.post_quit.valid is None
    assert any("incomplete" in failure for failure in result.post_quit.failures)


async def test_an_empty_profile_permits_the_session_and_keeps_o1_open(
    row, profile, tmp_path
):
    # A root retained on another profile: nothing is on the row's profile now,
    # so the session may start, but the row's O1 stays unestablished.
    key = harness.ActorAccount(profile[0]).browser_key
    result, calls = await row(
        processes=[], summary=_retained_summary(key, str(tmp_path / "elsewhere"))
    )
    assert calls == 1
    assert result.post_quit is not None and result.post_quit.valid is True
    assert result.vector is not None and not result.vector.o1_single_browser
    assert any("possible browser" in failure for failure in result.failures)


def test_the_census_keeps_a_denied_reading_apart_from_an_empty_one(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(harness, "process_user", lambda process: process.user)
    account = harness.ActorAccount(tmp_path / "auth" / "profile")
    ours = [
        str(tmp_path / "browsers" / "chrome"),
        f"--user-data-dir={account.profile}",
    ]
    census = harness.profile_census(
        account,
        browser_dir=tmp_path / "browsers",
        process_iter=lambda *a, **k: [
            _process(1, cmdline=ours),
            _process(2, cmdline=None),
            _process(3, cmdline=None, user="root"),
            _process(4, cmdline=None, exe="/bin/ps"),
            _process(5, cmdline=["python"]),
        ],
        user=ME,
    )
    assert census.pids == [1]
    assert census.unresolved == [2]
    assert not census.complete


def test_other_users_and_known_non_browsers_leave_the_census_complete(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(harness, "process_user", lambda process: process.user)
    account = harness.ActorAccount(tmp_path / "auth" / "profile")
    census = harness.profile_census(
        account,
        browser_dir=tmp_path / "browsers",
        process_iter=lambda *a, **k: [
            _process(3, cmdline=None, user="root"),
            _process(4, cmdline=None, exe="/bin/ps"),
        ],
        user=ME,
    )
    assert census.complete and census.pids == []


@pytest.mark.parametrize(
    ("user", "unresolved"),
    [
        pytest.param("root", [7], id="another-user-but-own-user-unknown"),
        pytest.param(ME, [7], id="same-user"),
    ],
)
def test_an_unknown_harness_user_excludes_nobody_by_user(
    tmp_path, monkeypatch, user, unresolved
):
    monkeypatch.setattr(harness, "process_user", lambda process: process.user)
    monkeypatch.setattr(harness, "harness_user", lambda: None)
    account = harness.ActorAccount(tmp_path / "auth" / "profile")
    census = harness.profile_census(
        account,
        browser_exe="/b/chrome",
        process_iter=lambda *a, **k: [
            _process(7, cmdline=None, exe="/b/chrome", user=user)
        ],
    )
    assert census.unresolved == unresolved


def test_a_known_other_user_is_excluded_from_the_census(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "process_user", lambda process: process.user)
    account = harness.ActorAccount(tmp_path / "auth" / "profile")
    census = harness.profile_census(
        account,
        browser_exe="/b/chrome",
        process_iter=lambda *a, **k: [
            _process(7, cmdline=None, exe="/b/chrome", user="root")
        ],
        user=ME,
    )
    assert census.complete


@pytest.mark.parametrize(
    ("linux", "threads", "complete"),
    [
        pytest.param(True, 1, True, id="linux-leader-alone"),
        pytest.param(True, 2, False, id="linux-leader-with-live-threads"),
        pytest.param(True, None, False, id="linux-threads-unreadable"),
        pytest.param(False, 2, True, id="macos-or-windows"),
        pytest.param(False, None, True, id="macos-or-windows-threads-unreadable"),
    ],
)
def test_a_zombie_leaves_the_census_only_once_the_whole_process_exited(
    tmp_path, monkeypatch, linux, threads, complete
):
    monkeypatch.setattr(harness, "process_user", lambda process: process.user)
    monkeypatch.setattr(
        harness,
        "exited_zombie",
        functools.partial(
            harness.exited_zombie, linux=linux, threads_of=lambda _: threads
        ),
    )
    account = harness.ActorAccount(tmp_path / "auth" / "profile")
    census = harness.profile_census(
        account,
        process_iter=lambda *a, **k: [_process(6, cmdline=[], status="zombie")],
        user=ME,
    )
    assert census.complete is complete
    assert census.unresolved == ([] if complete else [6])


def _status(pid: int, want: str, seconds: float = 10.0) -> bool:
    """Wait, without reaping, until *pid* reports *want*."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == want:
                return True
        except psutil.NoSuchProcess:
            return False
        time.sleep(0.02)
    return False


# The leader ends with pthread_exit while a worker thread keeps the process,
# and the lock it took, alive. Python cannot end its main thread on its own
# (the interpreter waits for the others), so libc does it.
_ZOMBIE_LEADER = """
import ctypes, fcntl, os, sys, threading
lock = open(sys.argv[1], "w")
fcntl.flock(lock, fcntl.LOCK_EX)
try:
    pthread_exit = ctypes.CDLL(None).pthread_exit
except AttributeError:
    print("no pthread_exit", flush=True)
    sys.exit(0)
pthread_exit.argtypes = [ctypes.c_void_p]
started = threading.Event()
def worker():
    started.set()
    sys.stdin.buffer.read(1)
    os._exit(0)
threading.Thread(target=worker).start()
started.wait()
print("ready", flush=True)
pthread_exit(None)
"""


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="a zombie leader with live threads is Linux's; elsewhere a zombie "
    "has exited as a whole",
)
def test_a_real_zombie_leader_with_a_live_thread_stays_unresolved(tmp_path):
    import fcntl

    account = harness.ActorAccount(tmp_path / "auth" / "profile")
    lockfile = tmp_path / "lock"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _ZOMBIE_LEADER,
            str(lockfile),
            f"--user-data-dir={account.profile}",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        assert child.stdout is not None
        line = child.stdout.readline().decode().strip()
        if line == "no pthread_exit":
            pytest.skip("libc exports no pthread_exit to ctypes here")
        assert line == "ready"
        assert _status(child.pid, psutil.STATUS_ZOMBIE), "the leader never exited"
        with lockfile.open("w") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        census = harness.profile_census(account)
        assert child.pid in census.unresolved
        assert not census.complete
    finally:
        # Bounded however the body ended: release the worker, then force it.
        with contextlib.suppress(OSError, ValueError):
            assert child.stdin is not None
            child.stdin.write(b"x")
            child.stdin.close()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
        if child.stdout is not None:
            child.stdout.close()


@pytest.mark.skipif(os.name == "nt", reason="psutil reports no zombie on Windows")
def test_a_real_fully_exited_unreaped_child_leaves_the_census(tmp_path):
    account = harness.ActorAccount(tmp_path / "auth" / "profile")
    child = subprocess.Popen(
        [sys.executable, "-c", "pass", f"--user-data-dir={account.profile}"]
    )
    try:
        assert _status(child.pid, psutil.STATUS_ZOMBIE), "it never became a zombie"
        census = harness.profile_census(account)
        assert child.pid not in census.unresolved
        assert child.pid not in census.pids
    finally:
        child.wait(timeout=10)


# --- H-R6: the kill path, through the row entry ----------------------------------


def _guardian(principal: int, group: int) -> dict:
    return {
        "kind": "process.start",
        "actor": "guardian",
        "in_row": True,
        "pid": principal + 1,
        "ppid": principal,
        "cmdline": ["python", "-I", "-S", "-u", "/x/process_guardian.py", "5", "6"]
        + [str(group)],
    }


def _killed_events(row) -> list[dict]:
    return [r for r in row.log.records() if r["kind"] == "actor.killed"]


async def test_the_daemon_rows_kill_reaches_the_owner_and_is_reported(row):
    result, _ = await row(
        processes=[],
        summary={},
        observed=[_guardian(42, 42)],
        kill_actor=True,
        row="H-R6",
    )
    assert row.owner.process.kills == 1
    # A killed owner lingers as a zombie until its parent reaps it: dead.
    assert result.killed is not None
    assert result.killed["exit"] == "killed"
    if os.name != "nt":
        assert result.killed["guardian_owner_group"] == 42
    (event,) = _killed_events(row)
    assert (event["actor"], event["role"], event["pid"]) == ("harness", "owner", 42)


async def test_an_owner_nobody_replaced_is_still_handed_to_cleanup(row):
    # The frontend published no new owner after the kill: cleanup gets the
    # killed owner's handle, so it can settle that owner rather than meeting
    # its descriptor as one the row never identified.
    result, _ = await row(
        processes=[],
        summary={},
        observed=[_guardian(42, 0)],
        kill_actor=True,
        row="H-R6",
    )
    assert row.cleaned == [row.owner]
    assert result.owner is not None
    assert result.owner["replaced_after_kill"] is False
    assert result.owner["exit"]["how"] == "exited"


async def test_the_killed_zombie_found_again_is_no_successor(row, monkeypatch):
    # The stale descriptor still names the killed owner, unreaped, with its
    # own create time: identifying that lifetime again replaces nothing.
    monkeypatch.setattr(harness, "identify_owner", lambda *a, **k: (row.owner, None))
    result, _ = await row(
        processes=[],
        summary={},
        observed=[_guardian(42, 0)],
        kill_actor=True,
        row="H-R6",
    )
    assert result.owner is not None
    assert result.owner["replaced_after_kill"] is False


async def test_the_direct_rows_kill_reaches_the_associated_server(row, monkeypatch):
    server = _Actor(4242)
    monkeypatch.setattr(
        harness,
        "associate_server",
        lambda pid, observed: (server, 1.0) if pid == 4242 else (None, None),
    )
    result, _ = await row(
        processes=[],
        summary={},
        observed=[_guardian(4242, 0)],
        kill_actor=True,
        daemon=False,
        experiment="K1",
        row="H-R6",
    )
    assert server.kills == 1 and row.owner.process.kills == 0
    assert result.killed is not None and result.killed["exit"] == "killed"
    (event,) = _killed_events(row)
    assert (event["role"], event["pid"]) == ("frontend", 4242)


# --- O2 through the row entry ---------------------------------------------------


def _timeline(*records: dict, ends=(1.0, 2.0, 5.0, 6.0, 6.5, 7.0)) -> list[dict]:
    """Watcher records with the ready line and a summary that logs its samples."""
    return [
        {"kind": "watcher.ready", "t": 0.5, "baseline_pgids": [1]},
        *records,
        {
            "kind": "watcher.summary",
            "t": ends[-1],
            "sample_log": [[end - 0.01, end, 100000] for end in ends],
            "read_failures": [],
        },
    ]


def _started(pid, ppid, t, actor, *, in_row=True):
    return {
        "kind": "process.start",
        "t": t,
        "pid": pid,
        "ppid": ppid,
        "pgid": pid,
        "start_identity": t,
        "in_row": in_row,
        "actor": actor,
    }


async def test_a_signal_is_reported_as_a_signal_and_never_as_a_death(row, monkeypatch):
    # The traced owner sent a SIGTERM outside its launched set. That fails
    # O2, as a delivered signal: nothing observed a death.
    monkeypatch.setattr(
        _Oracle,
        "outcome",
        OracleOutcome(
            status=COMPLETE,
            traced=[42],
            attached_at=5.0,
            calls=parse_strace("42  6.0 kill(95, SIGTERM) = 0\n"),
        ),
    )
    result, _ = await row(
        processes=[],
        summary={},
        observed=_timeline(
            _started(42, 1, 1.0, "owner"), _started(95, 7, 2.0, "other", in_row=False)
        ),
    )
    assert result.vector is not None and result.vector.o2 == "violated"
    kinds = [r["kind"] for r in row.log.records()]
    assert "process.death_unattributed" not in kinds
    (violation,) = [r for r in row.log.records() if r["kind"] == "signal.violation"]
    assert "delivered SIGTERM" in violation["violation"]
    assert any(f.startswith("O2 violation") for f in result.failures)


class _Helpers:
    """Canaries that record their own teardown."""

    stopped = 0

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return []

    def outside_the_harness(self):
        return []

    def deaths(self):
        return []

    def stop(self):
        _Helpers.stopped += 1


async def test_a_helper_that_fails_to_stop_skips_no_other_teardown(row, monkeypatch):
    class Stuck(_Watcher):
        def stop(self):
            raise RuntimeError("the watcher would not stop")

    monkeypatch.setattr(harness, "Watcher", Stuck)
    monkeypatch.setattr(harness, "Canaries", _Helpers)
    monkeypatch.setattr(_Helpers, "stopped", 0)
    result, _ = await row(processes=[], summary={})
    assert _Helpers.stopped == 1
    assert any("the watcher could not be stopped" in f for f in result.failures)
    assert row.cleaned, "the owner's cleanup still ran"


async def test_a_watcher_that_never_started_still_has_its_helpers_ended(
    row, monkeypatch
):
    class Failing(_Watcher):
        def start(self):
            raise RuntimeError("no baseline")

    monkeypatch.setattr(harness, "Watcher", Failing)
    monkeypatch.setattr(harness, "Canaries", _Helpers)
    monkeypatch.setattr(_Helpers, "stopped", 0)
    with pytest.raises(RuntimeError, match="no baseline"):
        await row(processes=[], summary={})
    assert _Helpers.stopped == 1
