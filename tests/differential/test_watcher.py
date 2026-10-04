"""The watcher finds a second browser on one profile, and only that.

The logic runs on synthetic samples and a modelled process table first. The
process cases run the real watcher against stand-in "browsers": plain Python
processes whose command line carries ``--user-data-dir=``, which is all the
watcher reads, including one that only execs into that command line after
three seconds. So they measure the sampling on this platform's process table
without a browser, and a watcher that cannot see a process fails here rather
than passing O1 in a native row by never seeing anything.

A process whose metadata cannot be read is on record either way, and makes the
census uncertain unless it is established as unrelated for the lifetime read.
On macOS the non-browser case is exercised with the real setuid ``/bin/ps``.
"""

from __future__ import annotations

import errno
import io
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, cast

import pytest

import psutil

from differential import harness, watcher
from differential.events import EventLog, read_jsonl
from differential.harness import watcher_failures
from differential.watcher import (
    ProcessRecord,
    Sampler,
    Tracker,
    browser_roots,
    canonical_user_data_dir,
    classify,
    duration_stats,
    invoked_module,
    record,
    user_data_dir,
)

WATCHER = Path(__file__).with_name("watcher.py")
PROFILE = "/tmp/differential-profile"


def _browser(pid: int, ppid: int, *extra: str, profile: str = PROFILE, start=1.0):
    return record(
        pid,
        ppid,
        start,
        "/opt/chrome",
        ["chrome", f"--user-data-dir={profile}", *extra],
    )


def test_only_a_browser_root_names_its_profile():
    assert user_data_dir(["chrome", f"--user-data-dir={PROFILE}"]) == (
        canonical_user_data_dir(PROFILE)
    )
    assert (
        user_data_dir(["chrome", "--type=renderer", f"--user-data-dir={PROFILE}"])
        is None
    )
    assert user_data_dir(["node", "run-driver"]) is None


def test_one_browser_with_its_children_is_one_root():
    sample = {
        10: record(10, 1, 1.0, None, ["python", "-m", "linkedin_mcp_server"]),
        11: _browser(11, 10),
        12: _browser(12, 11, "--type=renderer"),
        13: _browser(13, 11, "--type=gpu-process"),
    }
    assert browser_roots(sample) == {canonical_user_data_dir(PROFILE): (11,)}


def test_two_browsers_on_one_profile_are_two_roots():
    sample = {11: _browser(11, 1), 21: _browser(21, 2)}
    assert browser_roots(sample) == {canonical_user_data_dir(PROFILE): (11, 21)}


def test_two_profiles_are_counted_apart():
    sample = {11: _browser(11, 1), 21: _browser(21, 2, profile="/tmp/other")}
    roots = browser_roots(sample)
    assert roots[canonical_user_data_dir(PROFILE)] == (11,)
    assert roots[canonical_user_data_dir("/tmp/other")] == (21,)


def test_the_tracker_records_the_sample_with_two_roots():
    tracker = Tracker()
    tracker.observe({11: _browser(11, 1)}, t=1.0)
    tracker.observe({11: _browser(11, 1), 21: _browser(21, 2)}, t=2.0)
    tracker.observe({21: _browser(21, 2)}, t=3.0)

    key = canonical_user_data_dir(PROFILE)
    assert tracker.max_roots == {key: 2}
    assert tracker.violations == [{"t": 2.0, "profile": key, "pids": [11, 21]}]


def test_one_browser_after_another_is_not_a_violation():
    tracker = Tracker()
    tracker.observe({11: _browser(11, 1)}, t=1.0)
    tracker.observe({}, t=2.0)
    tracker.observe({21: _browser(21, 2)}, t=3.0)
    assert tracker.max_roots == {canonical_user_data_dir(PROFILE): 1}
    assert tracker.violations == []


def test_starts_and_exits_are_keyed_by_create_time():
    tracker = Tracker()
    baseline = {5: record(5, 1, 1.0, None, ["background"])}
    assert [kind for _, kind, _ in tracker.observe(baseline, t=0.0)] == []

    server = record(7, 5, 2.0, None, ["python", "-m", "linkedin_mcp_server"])
    events = tracker.observe({**baseline, 7: server}, t=1.0)
    assert [(actor, kind, fields["pid"]) for actor, kind, fields in events] == [
        ("frontend", "process.start", 7)
    ]

    # Same pid, new create time: the old process exited and another started.
    events = tracker.observe(
        {**baseline, 7: record(7, 5, 9.0, None, ["something", "else"])}, t=2.0
    )
    assert [(kind, fields["start_identity"]) for _, kind, fields in events] == [
        ("process.exit", 2.0),
        ("process.start", 9.0),
    ]

    events = tracker.observe(baseline, t=3.0)
    assert [(kind, fields["pid"]) for _, kind, fields in events] == [
        ("process.exit", 7)
    ]


@pytest.mark.parametrize(
    ("cmdline", "actor"),
    [
        (["python", "-P", "-m", "linkedin_mcp_server.daemon_owner"], "owner"),
        (["python", "-I", "/x/linkedin_mcp_server/process_guardian.py"], "guardian"),
        (["node", "cli.js", "run-driver"], "driver"),
        (["python", "-m", "linkedin_mcp_server"], "frontend"),
        (["chrome", "--type=renderer", "--user-data-dir=/p"], "browser"),
        (["bash"], "other"),
    ],
)
def test_actors_are_named_from_the_command_line(cmdline, actor):
    assert classify(ProcessRecord(1, 0, 0.0, None, tuple(cmdline))) == actor


def _stand_in_browser(profile: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            f"--user-data-dir={profile}",
        ]
    )


def _start_watcher(tmp_path: Path, *, deadline: float = 60) -> subprocess.Popen:
    out, stop = tmp_path / "watcher.jsonl", tmp_path / "watcher.stop"
    watcher = subprocess.Popen(
        [
            sys.executable,
            str(WATCHER),
            "--out",
            str(out),
            "--stop",
            str(stop),
            "--run",
            "unit",
            "--experiment",
            "K0",
            "--row",
            "watcher-unit",
            "--platform",
            "test",
            "--root-pid",
            str(os.getpid()),
            "--deadline",
            str(deadline),
            # Judged as the harness judges: without it every unreadable
            # process, such as a parallel test's held /bin/ps, could be one.
            "--browser-dir",
            str(tmp_path / "ms-playwright"),
        ]
    )
    limit = time.monotonic() + 15
    while not any(r["kind"] == "watcher.ready" for r in read_jsonl(out)):
        assert time.monotonic() < limit, "the watcher never took its baseline"
        time.sleep(0.05)
    return watcher


def _stop_watcher(tmp_path: Path, watcher: subprocess.Popen) -> list[dict]:
    # Long enough for the exits to land in a sample before it stops.
    time.sleep(0.5)
    (tmp_path / "watcher.stop").touch()
    watcher.wait(timeout=15)
    return read_jsonl(tmp_path / "watcher.jsonl")


def _run_watcher(tmp_path: Path, profile: Path, browsers: int) -> list[dict]:
    watcher = _start_watcher(tmp_path)
    started: list[subprocess.Popen[bytes]] = []
    try:
        for _ in range(browsers):
            started.append(_stand_in_browser(profile))
        key = canonical_user_data_dir(str(profile))
        limit = time.monotonic() + 15
        while time.monotonic() < limit:
            if any(
                r["kind"] == "browser.roots"
                and len(r["roots"].get(key, [])) == browsers
                for r in read_jsonl(tmp_path / "watcher.jsonl")
            ):
                break
            time.sleep(0.05)
    finally:
        for process in started:
            process.kill()
            process.wait(timeout=10)
    return _stop_watcher(tmp_path, watcher)


def test_the_watcher_process_reports_two_browsers_on_one_profile(tmp_path):
    profile = tmp_path / "profile"
    records = _run_watcher(tmp_path, profile, browsers=2)
    (summary,) = [r for r in records if r["kind"] == "watcher.summary"]
    key = canonical_user_data_dir(str(profile))
    assert summary["max_roots"].get(key) == 2
    assert [v for v in summary["violations"] if v["profile"] == key]
    assert summary["stopped_by"] == "stop file"
    assert all(
        {"t", "run", "experiment", "row", "platform", "actor", "kind"} <= set(r)
        for r in records
    )


def test_the_watcher_process_sees_one_browser_and_its_exit(tmp_path):
    profile = tmp_path / "profile"
    records = _run_watcher(tmp_path, profile, browsers=1)
    (summary,) = [r for r in records if r["kind"] == "watcher.summary"]
    key = canonical_user_data_dir(str(profile))
    assert summary["max_roots"].get(key) == 1
    # Only this profile's: the watcher sees the whole machine, and a parallel
    # worker may be running the two-browser case beside this one.
    assert [v for v in summary["violations"] if v["profile"] == key] == []
    ours = f"--user-data-dir={profile}"
    parents = {
        r["pid"]: r["ppid"]
        for r in records
        if r["kind"] == "process.start"
        and r["actor"] == "browser"
        and ours in r["cmdline"]
    }
    # Roots only: on Windows a venv's python.exe is a launcher that runs the
    # interpreter as its child with the same command line, so the one stand-in
    # is two processes there.
    roots = {pid for pid, ppid in parents.items() if ppid not in parents}
    assert len(roots) == 1
    assert set(parents) <= {r["pid"] for r in records if r["kind"] == "process.exit"}
    assert os.getpid() not in parents


@pytest.mark.skipif(
    sys.platform != "win32", reason="POSIX lets no unprivileged process run ahead"
)
def test_on_windows_the_watcher_runs_ahead_of_the_row(tmp_path):
    watcher = _start_watcher(tmp_path)
    try:
        (ready,) = [
            r
            for r in read_jsonl(tmp_path / "watcher.jsonl")
            if r["kind"] == "watcher.ready"
        ]
        # Read from outside, as the scheduler has it: the pid launched here is
        # a venv launcher, and the watcher is the interpreter it started.
        running = psutil.Process(ready["pid"]).nice()
    finally:
        records = _stop_watcher(tmp_path, watcher)
    # Windows-only in psutil, so absent from the stubs ty reads on POSIX.
    assert running == psutil.HIGH_PRIORITY_CLASS  # ty: ignore[unresolved-attribute]
    (summary,) = [r for r in records if r["kind"] == "watcher.summary"]
    assert summary["priority"] == "HIGH_PRIORITY_CLASS"
    assert "priority_error" not in summary


_DELAYED_EXEC = """
import os
import sys
import time

time.sleep(3)
profile = os.environ["STAND_IN_PROFILE"]
os.execv(sys.executable, [sys.executable, sys.argv[1], "--user-data-dir=" + profile])
"""

_AFTER_EXEC = "import time\ntime.sleep(3)\n"


def test_a_process_that_execs_into_a_browser_late_is_still_seen(tmp_path):
    # Nothing in the first three seconds names the profile on the command
    # line; the environment carries it, and only the exec puts it there.
    profile = tmp_path / "profile"
    before, after = tmp_path / "before_exec.py", tmp_path / "after_exec.py"
    before.write_text(_DELAYED_EXEC)
    after.write_text(_AFTER_EXEC)
    watcher = _start_watcher(tmp_path)
    try:
        stand_in = subprocess.Popen(
            [sys.executable, str(before), str(after)],
            env={**os.environ, "STAND_IN_PROFILE": str(profile)},
        )
        try:
            stand_in.wait(timeout=30)
        finally:
            if stand_in.poll() is None:
                stand_in.kill()
    finally:
        records = _stop_watcher(tmp_path, watcher)
    (summary,) = [r for r in records if r["kind"] == "watcher.summary"]
    key = canonical_user_data_dir(str(profile))
    assert summary["max_roots"].get(key) == 1, summary["max_roots"]
    # On Windows the exec is a new process whose parent is gone before it is
    # sampled, so it is not tied to the row and its arguments are withheld;
    # the profile it names is published either way.
    assert any(
        r["kind"] in ("process.update", "process.start")
        and r["actor"] == "browser"
        and (f"--user-data-dir={profile}" in r["cmdline"] or r.get("profile") == key)
        for r in records
    )


def test_a_watcher_that_stops_early_cannot_carry_o1(tmp_path):
    watcher = _start_watcher(tmp_path, deadline=1)
    watcher.wait(timeout=15)
    ended = time.time()
    records = read_jsonl(tmp_path / "watcher.jsonl")
    (summary,) = [r for r in records if r["kind"] == "watcher.summary"]
    assert summary["stopped_by"] == "deadline"
    failures = watcher_failures(
        summary, actors_began=summary["observation_start"], actors_ended=ended + 5
    )
    assert any("stopped by 'deadline'" in failure for failure in failures)
    assert any("ended before" in failure for failure in failures)


def test_a_watcher_stopped_on_request_after_the_actors_covers_them(tmp_path):
    watcher = _start_watcher(tmp_path)
    began = time.time()
    time.sleep(0.3)
    ended = time.time()
    records = _stop_watcher(tmp_path, watcher)
    (summary,) = [r for r in records if r["kind"] == "watcher.summary"]
    assert summary["observation_start"] <= began
    assert summary["observation_end"] >= ended
    # Coverage and the requested stop, not the gap: this test shares the
    # machine with every other xdist worker and launches no browser, so a slow
    # sample here says nothing about O1. The rows keep the 1.0s bound, and the
    # summary tests further down hold that it rejects a gap.
    assert (
        watcher_failures(
            summary, actors_began=began, actors_ended=ended, max_gap=math.inf
        )
        == []
    )
    # The evidence states what sampling cost on this machine.
    assert summary["sample_seconds_mean"] > 0
    assert summary["sample_seconds_p95"] >= summary["sample_seconds_mean"] * 0.5
    assert summary["first_sample_cached"] > 0
    assert summary["reads_per_sample_max"] >= 1
    # Every sample is logged, and each event's time is a logged sample's end.
    log = summary["sample_log"]
    assert len(log) == summary["samples"]
    assert all(began <= ended for began, ended, _ in log)
    ends = {ended for _, ended, _ in log}
    assert all(r["t"] in ends for r in records if r["kind"].startswith("process."))
    (ready,) = [r for r in records if r["kind"] == "watcher.ready"]
    assert ready["baseline_pgids"] or os.name == "nt"
    # The largest gap comes with what it went to, on either side of a sample.
    largest = summary["largest_gap"]
    assert largest["seconds"] == summary["max_gap_seconds"]
    # Named here, not from the watcher's own lists: a phase or step dropped
    # from both the record and its list must still fail.
    assert set(largest["outside_sampling"]["steps"]) == {
        "tracker",
        "enqueue",
        "write",
        "flush",
        "sleep",
        "wakeup_delay",
        "stop_check",
    }
    # The file-system calls made off the sampling path, for the gap and the run.
    assert set(largest["file_io"]) == {"write", "flush", "stop_check"}
    assert set(summary["file_io"]) == {"write", "flush", "stop_check", "queue"}
    assert summary["file_io"]["stop_check"]["count"] >= 1
    assert set(largest["sample"]["phases"]) == {
        "last_pid",
        "enumeration",
        "reads",
        "canonicalization",
        "bookkeeping",
    }
    if sys.platform.startswith("linux"):
        assert all(isinstance(last, int) for _, _, last in log)


BROWSER_DIR = "/opt/ms-playwright"
BROWSER_EXE = f"{BROWSER_DIR}/chromium-1/chrome-linux/chrome"


def _field(entry, name):
    value = entry[name]
    if isinstance(value, BaseException):
        raise value
    return value


class _FakeProcess:
    """A modelled psutil.Process. Any field may be an exception to raise."""

    def __init__(self, table, pid):
        self.pid = pid
        if pid not in table:
            raise psutil.NoSuchProcess(pid)
        self._entry = table[pid]
        opening = self._entry.get("open")
        if isinstance(opening, BaseException):
            raise opening

    def create_time(self):
        if "start_seconds" in self._entry:
            # A modelled slow read, as ``cmdline_seconds``.
            self._entry["clock"]["now"] += self._entry["start_seconds"]
        return _field(self._entry, "start")

    def ppid(self):
        return _field(self._entry, "ppid")

    def exe(self):
        return _field({"exe": self._entry.get("exe", "/usr/bin/python3")}, "exe")

    def cmdline(self):
        self._entry["cmdline_reads"] = self._entry.get("cmdline_reads", 0) + 1
        if "cmdline_seconds" in self._entry:
            # A modelled slow read: the sampler's timer moves on by that much.
            self._entry["clock"]["now"] += self._entry["cmdline_seconds"]
        return _field(self._entry, "cmdline")

    def environ(self):
        self._entry.setdefault("environ_reads", 0)
        self._entry["environ_reads"] += 1
        return _field({"environ": self._entry.get("environ", {})}, "environ")


#: The harness's user in the model; a table entry may name another in "user".
HARNESS = "harness-user"


def _user_of(process) -> object:
    return process._entry.get("user", HARNESS)


def _sampler(
    table,
    *,
    root=1,
    browser_exe=BROWSER_EXE,
    no_exec=False,
    user_of=_user_of,
    timer=time.perf_counter,
    last_pid_of=lambda: None,
    pids=None,
    clock=time.time,
    cpu=time.process_time,
    pgid_of=None,
):
    return Sampler(
        root,
        own_pid=999,
        pids=pids or (lambda: list(table)),
        open_process=lambda pid: _FakeProcess(table, pid),
        user_of=user_of,
        timer=timer,
        clock=clock,
        cpu=cpu,
        last_pid_of=last_pid_of,
        user=HARNESS,
        browser_exe=browser_exe,
        browser_dir=BROWSER_DIR,
        # A POSIX process table unless a test models Windows: the same on
        # every host the suite runs on.
        no_exec=no_exec,
        # pid 0 is the System Idle Process wherever the table models Windows.
        idle_pid=0 if no_exec else None,
        # The modelled group, never the real one of a real pid.
        pgid_of=pgid_of
        or (lambda pid: _field({"pgid": (table.get(pid) or {}).get("pgid")}, "pgid")),
        # Markers are read wherever a guardian drains by them: POSIX.
        read_markers=not no_exec,
    )


def _summary(sampler: Sampler, tracker: Tracker) -> dict:
    return {
        "stopped_by": "stop file",
        "observation_start": 0.0,
        "observation_end": 1000.0,
        "max_gap_seconds": 0.1,
        "max_roots": dict(tracker.max_roots),
        "read_failures": sampler.read_failures,
        "relevant_read_failures": sampler.relevant_read_failures,
    }


def _judged(sampler: Sampler, tracker: Tracker) -> list[str]:
    return watcher_failures(
        _summary(sampler, tracker), actors_began=1.0, actors_ended=999.0
    )


def _observe(sampler: Sampler, tracker: Tracker, t: float):
    return tracker.observe(sampler.sample(), t)


def _chrome(profile: str) -> list[str]:
    return [BROWSER_EXE, f"--user-data-dir={profile}"]


def _row_table() -> dict[int, dict[str, Any]]:
    return {
        1: {"start": 1.0, "ppid": 0, "cmdline": ["pytest"]},
        50: {"start": 1.0, "ppid": 0, "cmdline": ["system-service"]},
    }


@pytest.mark.parametrize(
    "denied",
    [
        pytest.param({"start": psutil.AccessDenied(2)}, id="create-time"),
        pytest.param({"open": psutil.AccessDenied(2)}, id="open"),
        pytest.param({"start": OSError("denied")}, id="create-time-oserror"),
    ],
)
def test_a_known_actor_whose_identity_cannot_be_read_stays_unknown(denied):
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = {"start": 2.0, "ppid": 1, "cmdline": ["python", "pre-exec"]}
    _observe(sampler, tracker, 1.0)
    table[2].update(denied)
    events = _observe(sampler, tracker, 2.0)
    # Still present, not an exit: a denied read is not a disappearance.
    assert not [e for e in events if e[1] == "process.exit" and e[2]["pid"] == 2]
    (episode,) = sampler.relevant_read_failures
    assert episode["pid"] == 2 and episode["possible_browser"]
    assert any(
        "anything but a possible browser" in f for f in _judged(sampler, tracker)
    )


def test_a_vanished_actor_is_an_exit_not_an_unknown():
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = {"start": 2.0, "ppid": 1, "cmdline": ["python", "server"]}
    _observe(sampler, tracker, 1.0)
    table[2]["open"] = psutil.NoSuchProcess(2)
    events = _observe(sampler, tracker, 2.0)
    assert [e[1] for e in events if e[2].get("pid") == 2] == ["process.exit"]
    assert sampler.read_failures == []


def test_two_roots_one_unreadable_fails_the_row_judgement():
    # The e1cb model: an actor becomes a browser on the row's profile and then
    # cannot be identified, while a second browser on the profile stays
    # readable. The readable one alone reads as one root; the unknown one must
    # keep O1 from being established.
    profile = "/tmp/e1cb-profile"
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = {"start": 2.0, "ppid": 1, "cmdline": ["python", "pre-exec"]}
    _observe(sampler, tracker, 1.0)
    table[2].update(start=psutil.AccessDenied(2), cmdline=_chrome(profile))
    table[3] = {
        "start": 3.0,
        "ppid": 1,
        "exe": BROWSER_EXE,
        "cmdline": _chrome(profile),
    }
    _observe(sampler, tracker, 2.0)
    assert _judged(sampler, tracker)


def test_two_readable_roots_are_counted_as_two():
    profile = "/tmp/e1cb-profile"
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    for pid in (2, 3):
        table[pid] = {
            "start": 2.0,
            "ppid": 1,
            "exe": BROWSER_EXE,
            "cmdline": _chrome(profile),
        }
    _observe(sampler, tracker, 1.0)
    assert tracker.max_roots[canonical_user_data_dir(profile)] == 2
    assert _judged(sampler, tracker) == []


def test_a_first_sample_harness_descendant_that_execs_into_a_browser_is_seen():
    # A staging leftover, already running when the watcher starts.
    profile = "/tmp/e1cb-profile"
    table = _row_table()
    table[2] = {"start": 0.5, "ppid": 1, "cmdline": ["leftover"]}
    sampler, tracker = _sampler(table), Tracker()
    first = sampler.sample()
    tracker.observe(first, 0.0)
    assert first[2].in_row
    table[2]["cmdline"] = _chrome(profile)
    _observe(sampler, tracker, 1.0)
    assert tracker.max_roots[canonical_user_data_dir(profile)] == 1


def test_a_first_sample_unrelated_process_is_read_once():
    table = _row_table()
    reads = {"n": 0}
    original = table[50]

    class Counting(dict):
        def __getitem__(self, key):
            if key == "cmdline":
                reads["n"] += 1
            return super().__getitem__(key)

    table[50] = Counting(original)
    sampler = _sampler(table)
    for _ in range(5):
        sampler.sample()
    assert reads["n"] == 1


def test_an_unreadable_actor_whose_exe_is_not_the_browser_is_only_evidence():
    # The macOS setuid /bin/ps, modelled: its executable reads, its arguments
    # do not, and it is not the row's browser.
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = {
        "start": 2.0,
        "ppid": 1,
        "exe": "/bin/ps",
        "cmdline": psutil.AccessDenied(2),
    }
    _observe(sampler, tracker, 1.0)
    table.pop(2)
    _observe(sampler, tracker, 2.0)
    (episode,) = sampler.read_failures
    assert episode["exe"] == "/bin/ps" and episode["resolution"] == "exited"
    assert episode["failures"] == ["cmdline: AccessDenied"]
    assert not episode["possible_browser"]
    assert _judged(sampler, tracker) == []


@pytest.mark.parametrize(
    "exe",
    [
        pytest.param(BROWSER_EXE, id="the-browser-exe"),
        pytest.param(
            f"{BROWSER_DIR}/chromium-1/chrome-linux/chrome_crashpad",
            id="under-browser-dir",
        ),
        pytest.param(psutil.AccessDenied(2), id="unreadable-exe"),
    ],
)
def test_an_unreadable_actor_that_could_be_the_browser_counts(exe):
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = {"start": 2.0, "ppid": 1, "exe": exe, "cmdline": psutil.AccessDenied(2)}
    _observe(sampler, tracker, 1.0)
    assert [e["pid"] for e in sampler.relevant_read_failures] == [2]
    assert _judged(sampler, tracker)


def test_the_resolved_browser_executable_counts_even_outside_the_browsers_dir():
    # A browser the product resolved somewhere else, such as a system install.
    elsewhere = "/Applications/Browser.app/Contents/MacOS/Browser"
    table = _row_table()
    sampler = _sampler(table, browser_exe=elsewhere)
    tracker = Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = {
        "start": 2.0,
        "ppid": 1,
        "exe": elsewhere,
        "cmdline": psutil.AccessDenied(2),
    }
    _observe(sampler, tracker, 1.0)
    assert [e["pid"] for e in sampler.relevant_read_failures] == [2]


def test_a_never_readable_process_that_becomes_readable_stays_uncertain():
    # Its first observation failed, so nothing says what it was meanwhile;
    # reading it later, or its exit, cannot show that no overlap happened.
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = {"start": 2.0, "ppid": 1, "exe": "/usr/bin/python3", "cmdline": ["py"]}
    table[2]["start"] = psutil.AccessDenied(2)
    _observe(sampler, tracker, 1.0)
    table[2]["start"] = 2.0
    _observe(sampler, tracker, 2.0)
    table.pop(2)
    _observe(sampler, tracker, 3.0)
    (episode,) = sampler.relevant_read_failures
    assert episode["pid"] == 2 and episode["resolution"] in ("readable", "exited")
    assert _judged(sampler, tracker)


def _unattributed(user: object | None) -> dict[str, Any]:
    """A process whose executable and arguments cannot be read, nor its user
    when *user* is None."""
    return {
        "start": 2.0,
        "ppid": 50,
        "exe": psutil.AccessDenied(2),
        "cmdline": psutil.AccessDenied(2),
        "user": user,
    }


def test_a_later_reading_of_the_same_lifetime_that_shows_another_user_resolves_it():
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = _unattributed(None)
    _observe(sampler, tracker, 1.0)
    assert [e["pid"] for e in sampler.relevant_read_failures] == [2]
    table[2]["user"] = "root"
    _observe(sampler, tracker, 2.0)
    (episode,) = sampler.read_failures
    assert episode["start_identity"] == 2.0
    assert not episode["possible_browser"]
    assert episode["resolved_by"] == "another user"
    assert _judged(sampler, tracker) == []


def test_another_users_later_process_at_the_pid_resolves_nothing():
    # The e1ce model: the first record has no create time, or a different one,
    # so nothing ties the later process to what ran there while unreadable.
    for first, later_start in (({"open": psutil.AccessDenied(2)}, 2.0), ({}, 5.0)):
        table = _row_table()
        sampler, tracker = _sampler(table), Tracker()
        _observe(sampler, tracker, 0.0)
        table[2] = {**_unattributed(None), **first}
        _observe(sampler, tracker, 1.0)
        table.pop(2)
        _observe(sampler, tracker, 2.0)
        table[2] = {"start": later_start, "ppid": 50, "cmdline": ["d"], "user": "root"}
        _observe(sampler, tracker, 3.0)
        assert [e["pid"] for e in sampler.relevant_read_failures] == [2]
        assert _judged(sampler, tracker)


@pytest.mark.parametrize(
    "unverified",
    [
        pytest.param({"open": psutil.AccessDenied(50)}, id="open-denied"),
        pytest.param({"start": psutil.AccessDenied(50)}, id="create-time-denied"),
        pytest.param({"open": OSError("synthetic")}, id="open-oserror"),
    ],
)
def test_a_cached_exclusion_does_not_cover_an_unverified_process_at_its_pid(
    unverified,
):
    # pid 50 is established unrelated at the first sample. Whatever runs there
    # once its identity cannot be read is not covered by that.
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    _observe(sampler, tracker, 1.0)
    assert sampler.read_failures == []
    table[50] = {
        "start": 3.0,
        "ppid": 1,
        "exe": BROWSER_EXE,
        "cmdline": _chrome("/tmp/e1ce-profile"),
        **unverified,
    }
    _observe(sampler, tracker, 2.0)
    (episode,) = sampler.relevant_read_failures
    assert episode["pid"] == 50 and episode["start_identity"] is None
    assert _judged(sampler, tracker)


def test_a_cached_exclusion_holds_while_the_create_time_matches():
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[50]["cmdline"] = psutil.AccessDenied(50)
    _observe(sampler, tracker, 1.0)
    assert sampler.read_failures == []
    assert _judged(sampler, tracker) == []


def _baseline_table() -> dict[int, dict[str, Any]]:
    """pid 1 is init and pid 10 the harness, which started at 5.0."""
    return {
        1: {"start": 0.0, "ppid": 0, "cmdline": ["init"]},
        10: {"start": 5.0, "ppid": 1, "cmdline": ["pytest"]},
    }


@pytest.mark.parametrize(
    "ancestry",
    [
        pytest.param(
            {
                59: {"start": 5.2, "ppid": 10, "open": psutil.NoSuchProcess(59)},
                60: {"start": 5.5, "ppid": 59, "cmdline": ["leftover"]},
            },
            id="parent-vanished",
        ),
        pytest.param(
            {
                59: {"start": 5.2, "ppid": 10, "open": psutil.AccessDenied(59)},
                60: {"start": 5.5, "ppid": 59, "cmdline": ["leftover"]},
            },
            id="parent-unopenable",
        ),
        pytest.param(
            {60: {"start": 5.5, "ppid": 1, "cmdline": ["leftover"]}},
            id="adopted-by-init-after-the-harness-began",
        ),
        pytest.param(
            {
                # Itself settled, but born after 60: not 60's parent.
                59: {"start": 5.8, "ppid": 0, "cmdline": ["younger"]},
                60: {"start": 5.5, "ppid": 59, "cmdline": ["leftover"]},
            },
            id="younger-process-at-the-parent-pid",
        ),
        pytest.param(
            {60: {"start": 5.5, "ppid": psutil.AccessDenied(60), "cmdline": ["x"]}},
            id="parent-unreadable",
        ),
    ],
)
def test_a_baseline_process_with_an_open_ancestry_is_watched(ancestry):
    # The e1ce model: at the first sample it is not a known harness
    # descendant, but nothing shows it is not one, so it is read again on
    # every sample and counted once it becomes a browser beside the row's own.
    profile = "/tmp/e1ce-profile"
    table = {**_baseline_table(), **ancestry}
    sampler, tracker = _sampler(table, root=10), Tracker()
    _observe(sampler, tracker, 0.0)
    table.pop(59, None)
    table[60] = {
        "start": 5.5,
        "ppid": 1,
        "exe": BROWSER_EXE,
        "cmdline": _chrome(profile),
    }
    table[70] = {
        "start": 6.0,
        "ppid": 10,
        "exe": BROWSER_EXE,
        "cmdline": _chrome(profile),
    }
    _observe(sampler, tracker, 1.0)
    assert tracker.max_roots[canonical_user_data_dir(profile)] == 2


@pytest.mark.parametrize(
    "ancestry",
    [
        pytest.param({60: {"start": 5.5, "ppid": 0, "cmdline": ["d"]}}, id="pid-0"),
        pytest.param(
            {
                59: {"start": 3.0, "ppid": 0, "cmdline": ["launcher"]},
                60: {"start": 5.5, "ppid": 59, "cmdline": ["d"]},
            },
            id="through-a-parent-of-pid-0",
        ),
    ],
)
def test_a_baseline_process_with_a_complete_ancestry_is_read_once(ancestry):
    assert _reads_over_five_samples({**_baseline_table(), **ancestry}) == 1


def _reads_over_five_samples(
    table: dict[int, dict[str, Any]], *, no_exec: bool = False
) -> int:
    """How often pid 60's command line is read over five samples."""
    reads = {"n": 0}

    class Counting(dict):
        def __getitem__(self, key):
            if key == "cmdline":
                reads["n"] += 1
            return super().__getitem__(key)

    table[60] = Counting(table[60])
    sampler = _sampler(table, root=10, no_exec=no_exec)
    for _ in range(5):
        sampler.sample()
    return reads["n"]


@pytest.mark.parametrize(
    "start",
    [
        pytest.param(4.0, id="reads-older-than-the-harness"),
        pytest.param(5.5, id="reads-younger-than-the-harness"),
    ],
)
def test_a_child_of_pid_1_is_watched_whatever_its_create_time_says(start):
    # pid 1 adopts the harness's orphans too, and a wall-clock create time
    # cannot show which of its children were born before the harness.
    table = {
        **_baseline_table(),
        60: {"start": start, "ppid": 1, "cmdline": ["service"]},
    }
    assert _reads_over_five_samples(table) == 5


def _dead_parent_service() -> dict[int, dict[str, Any]]:
    """A service whose parent pid names a process that is gone, as Windows
    keeps it for every orphan. Fully readable, and no browser."""
    return {
        **_baseline_table(),
        60: {
            "start": 5.5,
            "ppid": 59,
            "exe": "C:/Windows/System32/svchost.exe",
            "cmdline": ["svchost.exe", "-k", "netsvcs"],
        },
    }


def test_on_windows_a_fully_read_non_browser_is_read_once():
    # Windows has no exec: this image is the process's for its lifetime.
    assert _reads_over_five_samples(_dead_parent_service(), no_exec=True) == 1


def test_on_posix_the_same_process_stays_watched():
    # It could exec into the browser later, and its ancestry is open.
    assert _reads_over_five_samples(_dead_parent_service(), no_exec=False) == 5


@pytest.mark.parametrize(
    "change",
    [
        # Its arguments unread but its executable no browser: settled, see
        # test_on_windows_a_non_browser_image_is_settled_by_one_read.
        pytest.param({"exe": psutil.AccessDenied(60)}, id="executable-unread"),
        pytest.param({"exe": ""}, id="executable-empty"),
        pytest.param({"exe": BROWSER_EXE}, id="the-browser"),
        pytest.param(
            {"exe": f"{BROWSER_DIR}/chromium-1/chrome-linux/chrome_crashpad"},
            id="under-the-browsers-dir",
        ),
        pytest.param(
            {"cmdline": ["svchost.exe", "--user-data-dir=/tmp/any-profile"]},
            id="names-a-profile",
        ),
    ],
)
def test_on_windows_an_image_not_read_in_full_or_a_browser_stays_watched(change):
    table = _dead_parent_service()
    table[60] = {**table[60], **change}
    sampler = _sampler(table, root=10, no_exec=True)
    samples = [sampler.sample() for _ in range(5)]
    # Not established unrelated: in every sample, where O1 counts it.
    assert sampler.stats()["first_sample_watched"] == 1
    assert all(60 in sample for sample in samples)


def test_on_windows_a_browser_started_later_is_a_new_process_and_is_counted():
    # The first-sample service is cached; the browser that joins the row is a
    # new process, read in full as new, beside the row's own.
    profile = "/tmp/e1dd-profile"
    table = _dead_parent_service()
    sampler, tracker = _sampler(table, root=10, no_exec=True), Tracker()
    _observe(sampler, tracker, 0.0)
    assert sampler.first_sample_cached >= 1
    table[61] = {
        "start": 6.0,
        "ppid": 59,
        "exe": BROWSER_EXE,
        "cmdline": _chrome(profile),
    }
    table[70] = {
        "start": 6.0,
        "ppid": 10,
        "exe": BROWSER_EXE,
        "cmdline": _chrome(profile),
    }
    _observe(sampler, tracker, 1.0)
    assert tracker.max_roots[canonical_user_data_dir(profile)] == 2


@pytest.mark.parametrize(
    "start",
    [
        pytest.param(5.5, id="e1ce-c-ordinary-order"),
        # The e1dc and e1dd models: born after the harness, but its wall-clock
        # create time reads older, as after a backward clock step.
        pytest.param(4.9, id="e1dc-e1dd-calendar-reads-older"),
    ],
)
def test_on_posix_a_first_sample_process_with_an_open_ancestry_is_still_caught(start):
    profile = "/tmp/e1dd-profile"
    table = {
        **_baseline_table(),
        60: {"start": start, "ppid": 59, "cmdline": ["python", "pre-exec"]},
    }
    sampler, tracker = _sampler(table, root=10), Tracker()
    _observe(sampler, tracker, 0.0)
    assert sampler.first_sample_watched == 1
    table[60] = {
        "start": start,
        "ppid": 1,
        "exe": BROWSER_EXE,
        "cmdline": _chrome(profile),
    }
    table[70] = {
        "start": 6.0,
        "ppid": 10,
        "exe": BROWSER_EXE,
        "cmdline": _chrome(profile),
    }
    _observe(sampler, tracker, 1.0)
    assert tracker.max_roots[canonical_user_data_dir(profile)] == 2


@pytest.mark.parametrize("no_exec", [False, True])
def test_the_summary_states_what_sampling_cost(no_exec):
    table = {
        **_baseline_table(),
        60: {"start": 5.5, "ppid": 0, "cmdline": ["settled"]},
        61: {"start": 5.5, "ppid": 59, "cmdline": ["python", "open"]},
    }
    sampler = _sampler(table, root=10, no_exec=no_exec)
    for _ in range(3):
        sampler.sample()
    stats = sampler.stats()
    assert stats["no_exec"] is no_exec
    if no_exec:
        # init and 60 by ancestry, 61 by its image; the harness is the row's.
        assert (stats["first_sample_cached"], stats["first_sample_watched"]) == (3, 0)
        # The harness, read in full once, is carried after that.
        assert sampler.reads_per_sample == [4, 0, 0]
        assert sampler.carried_per_sample == [0, 1, 1]
    else:
        # init and 60 by ancestry; 61 is watched.
        assert (stats["first_sample_cached"], stats["first_sample_watched"]) == (2, 1)
        # Every process in the first sample, then only the harness and 61.
        assert sampler.reads_per_sample == [4, 2, 2]
    assert duration_stats([0.01, 0.02, 0.03]) == {
        "sample_seconds_mean": 0.02,
        "sample_seconds_p95": 0.03,
    }


def _watched_baseline_beside_a_browser(
    exe: Any,
) -> tuple[Sampler, Tracker]:
    """A baseline orphan born after the harness, so watched, whose reads fail
    while a readable browser runs on the row's profile beside it."""
    table: dict[int, dict[str, Any]] = {
        **_baseline_table(),
        60: {"start": 5.5, "ppid": 1, "exe": "/usr/bin/python3", "cmdline": ["x"]},
    }
    sampler, tracker = _sampler(table, root=10), Tracker()
    _observe(sampler, tracker, 0.0)
    table[60]["cmdline"] = psutil.AccessDenied(60)
    table[60]["exe"] = exe
    table[70] = {"start": 6.0, "ppid": 10, "cmdline": _chrome("/tmp/row")}
    for step in range(1, 6):
        _observe(sampler, tracker, float(step))
    return sampler, tracker


@pytest.mark.parametrize(
    "exe",
    [
        pytest.param(psutil.AccessDenied(60), id="exe-denied"),
        pytest.param("", id="exe-empty"),
    ],
)
def test_a_watched_baseline_process_that_cannot_be_seen_keeps_o1_open(exe):
    # Watched means read again, not discounted: with neither its executable
    # nor its arguments visible it may be the second browser on the profile.
    sampler, tracker = _watched_baseline_beside_a_browser(exe)
    assert [e["pid"] for e in sampler.relevant_read_failures] == [60]
    assert _judged(sampler, tracker)


def test_a_watched_baseline_process_with_a_readable_other_exe_is_evidence_only():
    sampler, tracker = _watched_baseline_beside_a_browser("/usr/bin/python3")
    assert sampler.relevant_read_failures == []
    assert _judged(sampler, tracker) == []


def test_a_cached_exclusion_tied_to_the_harness_later_is_withdrawn():
    # pid 51's parent recorded at the first sample turns out to be a row
    # actor: from then on 51 is read again like any actor.
    profile = "/tmp/e1ce-profile"
    table = {
        **_row_table(),
        52: {"start": 0.5, "ppid": 0, "cmdline": ["parent"]},
        51: {"start": 0.8, "ppid": 52, "cmdline": ["child"]},
    }
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[52] = {"start": 3.0, "ppid": 1, "cmdline": ["row-actor"]}
    _observe(sampler, tracker, 1.0)
    table[51]["cmdline"] = _chrome(profile)
    _observe(sampler, tracker, 2.0)
    assert tracker.max_roots.get(canonical_user_data_dir(profile)) == 1


@pytest.mark.parametrize(
    "unreadable",
    [
        pytest.param({"open": psutil.AccessDenied(2)}, id="open"),
        pytest.param({"start": psutil.AccessDenied(2)}, id="create-time"),
        pytest.param({"cmdline": psutil.AccessDenied(2)}, id="arguments"),
    ],
)
def test_another_users_unreadable_process_does_not_count(unreadable):
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = {
        "start": 2.0,
        "ppid": 50,
        "exe": BROWSER_EXE,
        "cmdline": ["daemon"],
        "user": "root",
        **unreadable,
    }
    if "open" in unreadable:
        # Opening failed, so nothing can say whose it is: that stays uncertain.
        _observe(sampler, tracker, 1.0)
        assert sampler.relevant_read_failures
        return
    _observe(sampler, tracker, 1.0)
    assert sampler.read_failures == []
    assert _judged(sampler, tracker) == []


def test_an_unknown_harness_user_excludes_nobody_by_user():
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    sampler.user = None
    _observe(sampler, tracker, 0.0)
    table[2] = {
        "start": 2.0,
        "ppid": 50,
        "exe": BROWSER_EXE,
        "cmdline": psutil.AccessDenied(2),
        "user": "root",
    }
    _observe(sampler, tracker, 1.0)
    assert [e["pid"] for e in sampler.relevant_read_failures] == [2]


def test_an_unrelated_process_that_cannot_be_read_is_not_recorded():
    table = _row_table()
    sampler = _sampler(table)
    sampler.sample()
    table[3] = {"start": 2.0, "ppid": 50, "cmdline": psutil.AccessDenied(3)}
    for _ in range(5):
        sampler.sample()
    assert sampler.read_failures == []


_ON_MACOS = pytest.mark.skipif(
    sys.platform != "darwin", reason="setuid /bin/ps is macOS's"
)


def _real_sampler(tmp_path: Path) -> Sampler:
    browsers = tmp_path / "ms-playwright"
    browsers.mkdir()
    return Sampler(os.getpid(), browser_dir=str(browsers))


@_ON_MACOS
def test_a_real_setuid_ps_held_alive_is_recorded_but_not_counted(tmp_path):
    # What the product runs on macOS to read process ancestry. psutil cannot
    # read the arguments of a setuid-root process; its executable it can. Its
    # output fills a pipe nobody reads, so it is provably alive while the
    # sampler reads it; no race with a short-lived ps is involved.
    sampler = _real_sampler(tmp_path)
    sampler.sample()
    columns = ["-o", "command="] * 16
    ps = subprocess.Popen(["/bin/ps", "-A", "-ww", *columns], stdout=subprocess.PIPE)
    try:
        began = time.monotonic()
        while time.monotonic() - began < 1.0:
            sampler.sample()
            time.sleep(0.05)
        assert ps.poll() is None, "ps finished early; its output fit the pipe"
    finally:
        # Bounded however the body ended: stop it, release the pipe, reap it.
        if ps.poll() is None:
            ps.kill()
        assert ps.stdout is not None
        ps.stdout.close()
        ps.wait(timeout=10)
    sampler.sample()
    ours = [e for e in sampler.read_failures if e["pid"] == ps.pid]
    assert ours, "the sampler read the held ps and recorded nothing"
    assert ours[0]["exe"] == "/bin/ps"
    assert any(f.startswith("cmdline") for f in ours[0]["failures"])
    assert not ours[0]["possible_browser"]
    assert sampler.relevant_read_failures == []


# The macOS runner's framework Python, as the E1d packets recorded it: the
# venv's stub re-executes this, which becomes both exe and argv[0].
FRAMEWORK_PYTHON = (
    "/Library/Frameworks/Python.framework/Versions/3.13/Resources/"
    "Python.app/Contents/MacOS/Python"
)
BASELINE_VENV_PYTHON = "/tmp/differential-baseline/checkout/.venv/bin/python"


def test_a_server_process_records_the_venv_it_was_launched_as_and_nothing_else():
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = {
        "start": 2.0,
        "ppid": 1,
        "exe": FRAMEWORK_PYTHON,
        "cmdline": [FRAMEWORK_PYTHON, "-m", "linkedin_mcp_server"],
        "environ": {
            "__PYVENV_LAUNCHER__": BASELINE_VENV_PYTHON,
            "PROXY_PASSWORD": "synthetic-secret",
        },
    }
    events = _observe(sampler, tracker, 1.0)
    (start,) = [e[2] for e in events if e[1] == "process.start" and e[2]["pid"] == 2]
    assert start["launcher"] == BASELINE_VENV_PYTHON
    assert "synthetic-secret" not in repr(events)
    for tick in (2.0, 3.0):
        _observe(sampler, tracker, tick)
    # Fixed at exec, so read once for as long as the command line holds.
    assert table[2]["environ_reads"] == 1


@pytest.mark.parametrize(
    ("ppid", "cmdline"),
    [
        pytest.param(1, ["node", "run-driver"], id="row-non-server"),
        pytest.param(
            1,
            [FRAMEWORK_PYTHON, "helper.py", "--directory=/tmp/linkedin_mcp_server-x"],
            id="row-argument-merely-naming-the-module",
        ),
        pytest.param(
            1,
            [FRAMEWORK_PYTHON, "-c", "import runpy", "-m", "linkedin_mcp_server"],
            id="row-module-after-dash-c",
        ),
        pytest.param(
            50,
            [FRAMEWORK_PYTHON, "-m", "linkedin_mcp_server"],
            id="unrelated-actual-server",
        ),
        pytest.param(
            50,
            [FRAMEWORK_PYTHON, "-P", "-m", "linkedin_mcp_server.daemon_owner"],
            id="unrelated-actual-owner",
        ),
    ],
)
def test_only_a_row_server_or_owner_is_asked_for_its_environment(ppid, cmdline):
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = {
        "start": 2.0,
        "ppid": ppid,
        "cmdline": cmdline,
        "environ": {"__PYVENV_LAUNCHER__": BASELINE_VENV_PYTHON},
    }
    for tick in (1.0, 2.0):
        _observe(sampler, tracker, tick)
    assert "environ_reads" not in table[2]
    assert sampler.sample()[2].launcher is None


def test_a_row_owner_records_its_launcher():
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = {
        "start": 2.0,
        "ppid": 1,
        "cmdline": [FRAMEWORK_PYTHON, "-P", "-m", "linkedin_mcp_server.daemon_owner"],
        "environ": {"__PYVENV_LAUNCHER__": BASELINE_VENV_PYTHON},
    }
    _observe(sampler, tracker, 1.0)
    assert sampler.sample()[2].launcher == BASELINE_VENV_PYTHON


def test_known_limit_a_same_command_reexec_keeps_the_first_launcher():
    """A documented boundary, not a guarantee (review e1da, E1DA-03).

    The launcher is a cached initial-image observation for this PID, create
    time and command line; same-command re-exec is outside this identity
    oracle. A re-exec into another venv's framework stub keeps the pid, the
    create time and the command line, so the first value stays.
    """
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = {
        "start": 2.0,
        "ppid": 1,
        "cmdline": [FRAMEWORK_PYTHON, "-m", "linkedin_mcp_server"],
        "environ": {"__PYVENV_LAUNCHER__": BASELINE_VENV_PYTHON},
    }
    _observe(sampler, tracker, 1.0)
    table[2]["environ"] = {"__PYVENV_LAUNCHER__": "/candidate/.venv/bin/python"}
    _observe(sampler, tracker, 2.0)
    assert sampler.sample()[2].launcher == BASELINE_VENV_PYTHON
    assert table[2]["environ_reads"] == 1


#: Argument shapes after the interpreter, each with whether CPython runs the
#: module named ``M`` for it (review e1db, E1DB-01). ``M`` stands for a module.
GRAMMAR = [
    pytest.param(["-m", "M"], True, id="m-separate"),
    pytest.param(["-mM"], True, id="m-attached"),
    pytest.param(["-Bm", "M"], True, id="m-clustered"),
    pytest.param(["-BmM"], True, id="m-clustered-attached"),
    pytest.param(["-PIm", "M"], True, id="m-clustered-after-flags"),
    pytest.param(["-I", "-P", "-m", "M"], True, id="flags-then-m"),
    pytest.param(["-BW", "ignore", "-m", "M"], True, id="W-clustered"),
    pytest.param(["-W", "ignore", "-m", "M"], True, id="W-separate"),
    pytest.param(["-Wignore", "-m", "M"], True, id="W-attached"),
    pytest.param(["-X", "dev", "-u", "-m", "M"], True, id="X-separate"),
    pytest.param(["-Xdev", "-m", "M"], True, id="X-attached"),
    pytest.param(
        ["--check-hash-based-pycs", "always", "-m", "M"], True, id="hash-check-always"
    ),
    pytest.param(
        ["--check-hash-based-pycs", "never", "-m", "M"], True, id="hash-check-never"
    ),
    pytest.param(
        ["--check-hash-based-pycs", "default", "-m", "M"],
        True,
        id="hash-check-default",
    ),
    pytest.param(
        ["--check-hash-based-pycs", "invalid-mode", "-m", "M"],
        False,
        id="hash-check-invalid-mode",
    ),
    pytest.param(
        ["--check-hash-based-pycs", "-m", "M"], False, id="hash-check-mode-missing"
    ),
    pytest.param(["--check-hash-based-pycs"], False, id="hash-check-alone"),
    pytest.param(
        ["--check-hash-based-pycs=always", "-m", "M"], False, id="long-with-equals"
    ),
    pytest.param(["-cprint(1)", "-m", "M"], False, id="c-attached"),
    pytest.param(["-Bcprint(1)", "-m", "M"], False, id="c-clustered"),
    pytest.param(["-c", "print(1)", "-m", "M"], False, id="c-separate"),
    pytest.param(["--", "-m", "M"], False, id="double-dash"),
    pytest.param(["-", "-m", "M"], False, id="stdin"),
    pytest.param(["script.py", "-m", "M"], False, id="script"),
    pytest.param(["-V", "-m", "M"], False, id="V"),
    pytest.param(["-h", "-m", "M"], False, id="h"),
    pytest.param(["--version", "-m", "M"], False, id="version"),
    pytest.param(["--unknown", "-m", "M"], False, id="unknown-long"),
    pytest.param(["-Z", "-m", "M"], False, id="unknown-short"),
    pytest.param(["-m"], False, id="m-without-module"),
]


def _shape(arguments: list[str], module: str) -> list[str]:
    return [
        module if argument == "M" else argument.replace("mM", "m" + module)
        for argument in arguments
    ]


@pytest.mark.parametrize(("arguments", "runs"), GRAMMAR)
def test_the_module_is_read_as_the_interpreter_reads_its_options(arguments, runs):
    module = "linkedin_mcp_server.daemon_owner"
    cmdline = ["python", *_shape(arguments, module)]
    assert invoked_module(cmdline) == (module if runs else None)


@pytest.mark.parametrize(("arguments", "runs"), GRAMMAR)
def test_the_grammar_is_the_interpreters_own(tmp_path, arguments, runs):
    """What this interpreter actually does with each shape, on a harmless module.

    The module only prints a marker. Under ``-I`` or ``-P`` the working
    directory is not on the path, so "no module named" also shows the
    interpreter tried to run it; either way ``-m`` took effect.
    """
    module = "e1db_harmless_probe"
    (tmp_path / f"{module}.py").write_text("print('MODULE-RAN')\n")
    shape = _shape(arguments, module)
    result = subprocess.run(
        [sys.executable, *shape],
        cwd=tmp_path,
        env={k: v for k, v in os.environ.items() if not k.startswith("PYTHON")},
        input="",
        capture_output=True,
        text=True,
        timeout=30,
    )
    tried = "MODULE-RAN" in result.stdout or f"No module named {module}" in (
        result.stderr
    )
    assert tried is runs, (shape, result.returncode, result.stderr[-300:])
    assert (invoked_module([sys.executable, *shape]) == module) is runs


def test_a_malformed_command_line_names_no_module():
    assert invoked_module(["python"]) is None
    assert invoked_module([]) is None
    assert invoked_module(["python", "--directory=/x/linkedin_mcp_server"]) is None


@pytest.mark.parametrize(("arguments", "runs"), GRAMMAR)
def test_only_a_real_server_invocation_is_asked_for_its_launcher(arguments, runs):
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = {
        "start": 2.0,
        "ppid": 1,
        "cmdline": [FRAMEWORK_PYTHON, *_shape(arguments, "linkedin_mcp_server")],
        "environ": {"__PYVENV_LAUNCHER__": BASELINE_VENV_PYTHON},
    }
    _observe(sampler, tracker, 1.0)
    assert ("environ_reads" in table[2]) is runs


def test_an_unreadable_environment_leaves_no_launcher():
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = {
        "start": 2.0,
        "ppid": 1,
        "cmdline": [FRAMEWORK_PYTHON, "-m", "linkedin_mcp_server"],
        "environ": psutil.AccessDenied(2),
    }
    _observe(sampler, tracker, 1.0)
    assert sampler.sample()[2].launcher is None


def test_actors_descend_from_the_root_and_are_re_read_every_sample():
    table: dict[int, dict[str, Any]] = {
        1: {"start": 1.0, "ppid": 0, "cmdline": ["pytest"]}
    }
    sampler = _sampler(table)
    sampler.sample()
    table[2] = {"start": 2.0, "ppid": 1, "cmdline": ["python", "server"]}
    table[3] = {"start": 2.0, "ppid": 2, "cmdline": ["node", "run-driver"]}
    first = sampler.sample()
    assert first[1].in_row and first[2].in_row and first[3].in_row
    # An exec long after first sight still lands.
    table[3]["cmdline"] = ["chrome", "--user-data-dir=/tmp/row"]
    for _ in range(100):
        later = sampler.sample()
    assert later[3].profile == canonical_user_data_dir("/tmp/row")


def test_the_tracker_reports_an_exec_as_an_update():
    tracker = Tracker()
    tracker.observe({}, t=0.0)
    tracker.observe({7: record(7, 1, 2.0, None, ["python", "stand-in"])}, t=1.0)
    events = tracker.observe(
        {7: record(7, 1, 2.0, None, ["chrome", f"--user-data-dir={PROFILE}"])}, t=2.0
    )
    assert [(actor, kind) for actor, kind, _ in events][:1] == [
        ("browser", "process.update")
    ]


def test_published_events_withhold_the_arguments_of_other_processes():
    # watcher.jsonl is uploaded as CI evidence; a stranger's arguments may hold
    # a credential, while the row's own actors are needed whole.
    secret = "--token=not-for-the-artifact"
    own = ["python", "-m", "linkedin_mcp_server"]
    tracker = Tracker()
    tracker.observe({}, t=0.0)
    events = tracker.observe(
        {
            7: record(7, 1, 2.0, "/usr/bin/tool", ["tool", secret]),
            8: record(8, 1, 2.0, "/usr/bin/python3", own, in_row=True),
            9: _browser(9, 1, start=2.0),
        },
        t=1.0,
    )
    fields = {f["pid"]: f for _, kind, f in events if kind == "process.start"}
    assert secret not in str(events)
    assert fields[7]["cmdline"] == []
    assert fields[8]["cmdline"] == own
    assert fields[9]["profile"] == canonical_user_data_dir(PROFILE)


def test_a_watcher_that_never_takes_its_baseline_is_stopped(tmp_path, monkeypatch):
    # Detached from the row, it would otherwise sample until its own deadline
    # after the row had already failed.
    silent = tmp_path / "silent_watcher.py"
    silent.write_text("import time\ntime.sleep(120)\n")
    monkeypatch.setattr(harness, "WATCHER_SCRIPT", silent)
    watcher = harness.Watcher(
        tmp_path / "w", EventLog(tmp_path / "log", run="r"), experiment="K1", row="R"
    )
    with pytest.raises(RuntimeError, match="baseline"):
        watcher.start(ready_seconds=0.5)
    assert watcher._process is not None
    assert watcher._process.poll() is not None


def test_the_harness_watcher_trusts_images_only_where_there_is_no_exec(tmp_path):
    # The real watcher, started the way a row starts it: the image rule is on
    # exactly on Windows, and its summary says which it applied.
    watcher = harness.Watcher(
        tmp_path / "w",
        EventLog(tmp_path / "log", run="r"),
        experiment="K1",
        row="R",
        browser_dir=tmp_path / "ms-playwright",
    )
    watcher.start()
    summary = watcher.stop()
    assert summary is not None
    assert summary["no_exec"] is (sys.platform == "win32")
    assert "clock_offset_at_start" not in summary


# --- What a lifetime costs ---------------------------------------------------------


def _row_actor_table() -> dict[int, dict[str, Any]]:
    """The harness (10) and a server it started after the first sample (70)."""
    return {
        **_baseline_table(),
        70: {"start": 6.0, "ppid": 10, "cmdline": ["python", "-m", "server"]},
    }


@pytest.mark.parametrize(("no_exec", "reads"), [(True, 1), (False, 5)])
def test_a_row_actor_is_read_once_per_lifetime_only_where_nothing_can_exec(
    no_exec, reads
):
    table = _baseline_table()
    sampler = _sampler(table, root=10, no_exec=no_exec)
    sampler.sample()
    table.update({70: _row_actor_table()[70]})
    samples = [sampler.sample() for _ in range(5)]
    assert table[70]["cmdline_reads"] == reads
    # Carried or read, it is the same record in every sample.
    assert {sample[70] for sample in samples} == {samples[0][70]}
    assert samples[0][70].in_row


def test_on_windows_a_failed_read_is_read_again_until_it_is_whole():
    table = _baseline_table()
    sampler = _sampler(table, root=10, no_exec=True)
    sampler.sample()
    table[70] = {**_row_actor_table()[70], "cmdline": psutil.AccessDenied(70)}
    sampler.sample()
    sampler.sample()
    assert table[70]["cmdline_reads"] == 2
    table[70]["cmdline"] = ["python", "-m", "server"]
    for _ in range(3):
        sample = sampler.sample()
    assert table[70]["cmdline_reads"] == 3
    assert sample[70].cmdline == ("python", "-m", "server")


def test_a_lifetime_found_gone_is_not_read_again_but_its_pids_next_one_is():
    table = _baseline_table()
    sampler = _sampler(table, root=10)
    sampler.sample()
    # Still listed and still identified, but gone when its arguments are read,
    # as psutil reports a Windows process whose handle outlives it.
    table[70] = {**_row_actor_table()[70], "cmdline": psutil.NoSuchProcess(70)}
    samples = [sampler.sample() for _ in range(4)]
    assert table[70]["cmdline_reads"] == 1
    assert all(70 not in sample for sample in samples)
    assert sampler.stats()["vanished_reads"] == 1
    table[70] = {"start": 7.0, "ppid": 10, "cmdline": ["python", "-m", "server"]}
    assert 70 in sampler.sample()


def test_a_lifetimes_user_is_read_once():
    asked: list[int] = []

    def user_of(process):
        asked.append(process.pid)
        return _user_of(process)

    table = _baseline_table()
    sampler = _sampler(table, root=10, user_of=user_of)
    sampler.sample()
    # Unreadable arguments on the browser's executable: judged every sample.
    table[70] = {
        "start": 6.0,
        "ppid": 59,
        "exe": BROWSER_EXE,
        "cmdline": psutil.AccessDenied(70),
    }
    for _ in range(4):
        sampler.sample()
    assert asked.count(70) == 1
    assert sampler.relevant_read_failures


def test_a_slow_sample_names_its_largest_timed_read():
    clock = {"now": 0.0}
    table = _baseline_table()
    sampler = _sampler(table, root=10, timer=lambda: clock["now"])
    sampler.sample()
    table[70] = {
        **_row_actor_table()[70],
        "exe": "C:/Windows/System32/cmd.exe",
        "cmdline_seconds": 1.2,
        "clock": clock,
    }
    sampler.sample()
    table[70]["cmdline_seconds"] = 0.0
    sampler.sample()
    stats = sampler.stats()
    assert stats["slow_sample_count"] == 1
    (slow,) = stats["slow_samples"]
    assert slow["seconds"] == 1.2
    assert (slow["slowest"]["kind"], slow["slowest"]["pid"]) == ("cmdline", 70)
    assert slow["slowest"]["exe"] == "C:/Windows/System32/cmd.exe"
    assert stats["slowest_read"]["kind"] == "cmdline"


def test_a_summary_without_a_gap_breakdown_names_the_slowest_samples():
    # Written before the watcher kept ``largest_gap``.
    slow = {
        "seconds": 1.3,
        "reads": 55,
        "slowest": {"kind": "cmdline", "pid": 70, "seconds": 1.2, "exe": "x.exe"},
    }
    failures = watcher_failures(
        {
            "stopped_by": "stop file",
            "observation_start": 0.0,
            "observation_end": 10.0,
            "max_gap_seconds": 1.38,
            "slow_samples": [slow],
            # The gap is the sample itself: 0.08s waiting, 1.3s reading.
            "sample_log": [[4.0, 4.004, None], [4.084, 5.384, None]],
        },
        actors_began=1.0,
        actors_ended=9.0,
    )
    (failure,) = failures
    assert "1.38s" in failure and "'cmdline'" in failure and "x.exe" in failure
    assert "the run's slowest samples" in failure


def test_a_summary_without_a_gap_breakdown_names_time_outside_sampling():
    # Run 36327966208, K0 on windows-latest: the only slow sample was the
    # baseline, which no gap is measured across, and the widest gap was the
    # watcher not running between two samples of 4ms.
    baseline = {
        "seconds": 0.2928,
        "reads": 138,
        "slowest": {
            "kind": "ppid",
            "pid": 1608,
            "seconds": 0.0027,
            "exe": "C:\\Windows\\System32\\svchost.exe",
        },
    }
    failures = watcher_failures(
        {
            "stopped_by": "stop file",
            "observation_start": 0.2928,
            "observation_end": 10.0,
            "max_gap_seconds": 1.1702,
            "slow_samples": [baseline],
            "priority": "NORMAL_PRIORITY_CLASS",
            "sample_log": [
                [0.0, 0.2928, None],
                [0.3430, 0.3468, None],
                [1.5128, 1.5170, None],
                [1.5674, 1.5712, None],
            ],
        },
        actors_began=1.0,
        actors_ended=9.0,
    )
    (failure,) = failures
    assert "1.1660s of it passed between two samples" in failure
    assert "0.0042s in the sample that closed it" in failure
    assert "NORMAL_PRIORITY_CLASS" in failure
    assert "svchost" not in failure


# --- What a gap went to -----------------------------------------------------------

#: The modelled stall: over the gap budget on its own.
_STALL = 1.2
_INTERVAL = 0.05


def _timed_run(
    monkeypatch, stalls: dict[int, dict[str, float]], *, samples: int = 8
) -> tuple[dict[str, Any], Sampler]:
    """Run the watcher's loop on a modelled table and clock, where sample *n*
    begins by arming ``stalls[n]``: each named site takes the clock forward by
    its seconds the next time it runs. Return the summary the harness judges.

    Every site is on the loop's own thread. The writer and the stop-file
    thread never move the modelled clock, so the loop may take a few samples
    past *samples* before the stop thread sees the request.
    """
    clock = {"now": 100.0, "cpu": 0.0}
    armed: dict[str, float] = {}
    table: dict[int, dict[str, Any]] = {
        **_row_actor_table(),
        80: {"start": 6.5, "ppid": 10, "exe": BROWSER_EXE, "cmdline": _chrome(PROFILE)},
    }
    began = {"n": 0}

    def stall(site: str) -> None:
        seconds = armed.pop(site, 0.0)
        clock["now"] += seconds
        if site == "bookkeeping":
            # Computing, not waiting: the CPU time moves with it.
            clock["cpu"] += seconds

    def last_pid() -> None:
        began["n"] += 1
        armed.update(stalls.get(began["n"], {}))
        # A process that starts in every sample, so every sample has events.
        table[100 + began["n"]] = {"start": 7.0, "ppid": 10, "cmdline": ["helper"]}
        if "canonicalization" in armed:
            # A profile is resolved when its root is first seen, not again.
            table[90] = {
                "start": 7.0,
                "ppid": 10,
                "exe": BROWSER_EXE,
                "cmdline": _chrome(f"{PROFILE}-late"),
            }
        table[70]["cmdline_seconds"] = armed.pop("cmdline", 0.0)
        stall("last_pid")

    def pids() -> list[int]:
        stall("enumeration")
        return list(table)

    def pgid(pid: int) -> int:
        if pid == 70:
            stall("pgid")
        return pid

    canonical = watcher.canonical_user_data_dir

    def slow_canonical(value: str) -> str:
        stall("canonicalization")
        return canonical(value)

    # The last bookkeeping of a sample, after its last read.
    track = Sampler._track

    def slow_track(self, sample, verdicts) -> None:
        stall("bookkeeping")
        track(self, sample, verdicts)

    monkeypatch.setattr(watcher, "canonical_user_data_dir", slow_canonical)
    monkeypatch.setattr(Sampler, "_track", slow_track)
    table[70]["clock"] = clock

    def now() -> float:
        return clock["now"]

    sampler = _sampler(
        table,
        root=10,
        timer=now,
        clock=now,
        cpu=lambda: clock["cpu"],
        last_pid_of=last_pid,
        pids=pids,
        pgid_of=pgid,
    )
    tracker = Tracker()
    observe_ = tracker.observe

    def slow_observe(sample, t):
        stall("tracker")
        return observe_(sample, t)

    monkeypatch.setattr(tracker, "observe", slow_observe)
    # The loop's own steps around the writer and the stop-file thread.
    put = watcher.EventWriter.put

    def slow_put(self, events, t) -> None:
        stall("enqueue")
        put(self, events, t)

    requested = watcher.StopWatch.requested

    def slow_requested(self) -> bool:
        stall("stop_check")
        return requested(self)

    monkeypatch.setattr(watcher.EventWriter, "put", slow_put)
    monkeypatch.setattr(watcher.StopWatch, "requested", slow_requested)

    def stop_requested() -> bool:
        return began["n"] >= samples

    def sleep(seconds: float) -> None:
        clock["now"] += seconds + armed.pop("wakeup_delay", 0.0)
        # Lets the stop-file thread run.
        time.sleep(0)

    loop = watcher.observe(
        sampler,
        tracker,
        io.StringIO(),
        stop_requested,
        base={},
        interval=_INTERVAL,
        deadline=1000.0,
        timer=now,
        monotonic=now,
        wall=now,
        sleep=sleep,
        cpu=lambda: clock["cpu"],
        stop_poll=0.001,
    )
    return {**loop, **sampler.stats(), "priority": "HIGH_PRIORITY_CLASS"}, sampler


def _gap_failure(summary: dict[str, Any]) -> str:
    (failure,) = watcher_failures(
        summary,
        actors_began=summary["observation_start"],
        actors_ended=summary["observation_end"],
    )
    return failure


def _largest_part(failure: str) -> tuple[str, float]:
    found = re.search(r"largest part was (.+?) at ([0-9.]+)s[;,]", failure)
    assert found, failure
    return found[1], float(found[2])


@pytest.mark.parametrize(
    ("site", "named"),
    [
        ("last_pid", "reading the kernel's last pid in the sample"),
        ("enumeration", "enumerating pids in the sample"),
        ("cmdline", "process reads in the sample"),
        ("pgid", "process reads in the sample"),
        ("canonicalization", "canonicalizing paths in the sample"),
        ("bookkeeping", "classification and bookkeeping in the sample"),
        ("tracker", "turning the previous sample into events outside sampling"),
        ("enqueue", "handing events to the writer thread outside sampling"),
        ("wakeup_delay", "waking late from sleep outside sampling"),
        ("stop_check", "checking for a stop request outside sampling"),
    ],
)
def test_a_gap_names_the_phase_its_time_went_to(monkeypatch, site, named):
    summary, _ = _timed_run(monkeypatch, {4: {site: _STALL}})
    # The sleep after a sample shrinks by whatever the sample's own loop took.
    assert _STALL <= summary["max_gap_seconds"] <= _STALL + _INTERVAL
    failure = _gap_failure(summary)
    name, seconds = _largest_part(failure)
    assert name == named, failure
    assert seconds == pytest.approx(_STALL, abs=0.01)
    if name == "process reads in the sample":
        assert f"most of it {site} reads" in failure
        assert f"the longest {_STALL:.4f}s of pid 70" in failure
    assert "outside sampling" in failure
    assert "slowest process read in that sample was" in failure
    assert "waited" not in failure
    # The phases of the sample that closed it add up to its duration.
    sample = summary["largest_gap"]["sample"]
    assert sum(sample["phases"].values()) == pytest.approx(sample["seconds"])


def test_reads_split_across_kinds_compete_as_one_phase(monkeypatch):
    """0.8s of reads split between two kinds outweigh 0.6s of bookkeeping."""
    summary, _ = _timed_run(
        monkeypatch, {4: {"cmdline": 0.4, "pgid": 0.4, "bookkeeping": 0.6}}
    )
    name, seconds = _largest_part(_gap_failure(summary))
    assert name == "process reads in the sample"
    assert seconds == pytest.approx(0.8, abs=0.01)


def test_a_late_wakeup_says_how_long_the_sleep_asked_for(monkeypatch):
    summary, _ = _timed_run(monkeypatch, {4: {"wakeup_delay": _STALL}})
    assert f"after asking for {_INTERVAL:.4f}s" in _gap_failure(summary)


def test_a_gap_names_the_sample_that_closed_it_not_the_runs_slowest(monkeypatch):
    summary, sampler = _timed_run(
        monkeypatch,
        {
            # The run's slowest sample, closing a gap under the budget.
            3: {"cmdline": 0.9},
            # The largest gap: a late wakeup, then a sample spent computing.
            5: {"wakeup_delay": 0.5},
            6: {"bookkeeping": 0.6},
        },
    )
    (slowest, *_) = sorted(
        sampler.slow_samples, key=lambda entry: entry["seconds"], reverse=True
    )
    assert (slowest["slowest"]["kind"], slowest["seconds"]) == ("cmdline", 0.9)
    largest = summary["largest_gap"]
    assert largest["seconds"] == pytest.approx(0.05 + 0.5 + 0.6)
    # The sample that ended the gap: the sixth sample's end.
    assert largest["t"] == summary["sample_log"][5][1]
    failure = _gap_failure(summary)
    name, seconds = _largest_part(failure)
    assert name == "classification and bookkeeping in the sample"
    assert seconds == pytest.approx(0.6)
    assert "0.5500s of it passed between two samples, outside sampling" in failure
    assert "0.6000s in the sample that closed it" in failure
    assert "0.6000s in the sample; the slowest process read" in failure
    assert "0.9000" not in failure


def test_a_gap_names_the_file_system_calls_it_overlapped():
    # Run 37000909990's 1.23s stop-file check, had it run off the loop and
    # the gap been over budget for another reason.
    failure = _gap_failure(
        {
            "stopped_by": "stop file",
            "observation_start": 0.0,
            "observation_end": 10.0,
            "max_gap_seconds": 1.284,
            "largest_gap": {
                "seconds": 1.284,
                "outside_sampling": {"seconds": 1.2796, "steps": {"tracker": 1.27}},
                "in_sample": {"seconds": 0.0043},
                "sample": {},
                "file_io": {
                    "write": {"count": 2, "seconds": 0.0011, "in_progress_seconds": 0},
                    "flush": {"count": 2, "seconds": 0.0004, "in_progress_seconds": 0},
                    "stop_check": {
                        "count": 0,
                        "seconds": 0.0,
                        "in_progress_seconds": 1.2331,
                    },
                },
            },
        }
    )
    assert _largest_part(failure)[0] == (
        "turning the previous sample into events outside sampling"
    )
    assert (
        "off the sampling path, calls that ended in that gap (whole durations): "
        "the event writer's writes took "
        "0.0011s over 2 calls, its flushes took 0.0004s over 2 calls, the "
        "stop-file checks took 0.0000s over 0 calls and one was still running "
        "after 1.2331s"
    ) in failure


def test_on_windows_the_idle_process_does_not_hold_sampling():
    """psutil answers pid 0's create time on Windows with a query of the whole
    process table, modelled here as a stall over the gap budget every time."""
    clock = {"now": 100.0}
    table: dict[int, dict[str, Any]] = {
        **_row_actor_table(),
        80: {"start": 6.5, "ppid": 10, "exe": BROWSER_EXE, "cmdline": _chrome(PROFILE)},
        0: {
            "start": 0.0,
            "ppid": 0,
            "exe": "",
            "cmdline": [],
            "user": "NT AUTHORITY\\SYSTEM",
            "start_seconds": _STALL,
            "clock": clock,
        },
    }
    samples = {"n": 0}
    seen = threading.Event()

    def pids() -> list[int]:
        samples["n"] += 1
        return list(table)

    def now() -> float:
        return clock["now"]

    def stop_requested() -> bool:
        if samples["n"] < 8:
            return False
        seen.set()
        return True

    def sleep(seconds: float) -> None:
        clock["now"] += seconds
        # The stop-file thread runs in real time, however late it is
        # scheduled, while modelled time costs the loop nothing. Held here
        # until that thread has seen the request, the loop does not spin
        # through samples meanwhile.
        if samples["n"] >= 8:
            seen.wait(10)

    sampler = _sampler(
        table, root=10, no_exec=True, timer=now, clock=now, cpu=lambda: 0.0, pids=pids
    )
    tracker = Tracker()
    loop = watcher.observe(
        sampler,
        tracker,
        io.StringIO(),
        stop_requested,
        base={},
        interval=_INTERVAL,
        # Out of reach, so the stop always comes from the stop-file thread,
        # as the row judgement requires.
        deadline=math.inf,
        timer=now,
        monotonic=now,
        wall=now,
        sleep=sleep,
        cpu=lambda: 0.0,
        stop_poll=0.001,
    )
    summary: dict[str, Any] = {**loop, **sampler.stats()}
    assert summary["max_gap_seconds"] < harness.MAX_WATCHER_GAP_SECONDS
    assert (
        watcher_failures(
            summary,
            actors_began=summary["observation_start"],
            actors_ended=summary["observation_end"],
        )
        == []
    )
    # Still watching the row: its browser is the profile's one root.
    assert tracker.max_roots == {canonical_user_data_dir(PROFILE): 1}


# --- File-system calls stay off the sampling path ----------------------------------

#: What one modelled file-system call blocks for: over the gap budget on its
#: own, as the stop-file check that took 1.2331s on windows-latest (run
#: 37000909990).
_BLOCKED = 1.5
#: How long a real-time run samples before its stop is requested.
_RUN = 2.0


class _SlowFile(io.StringIO):
    """An event file whose every write and flush first calls *hook* with the
    call's name and its index among the calls of that name."""

    def __init__(self, hook: Callable[[str, int], None]) -> None:
        super().__init__()
        self._hook = hook
        self.calls = {"write": 0, "flush": 0}

    def _call(self, name: str) -> None:
        index = self.calls[name]
        self.calls[name] += 1
        self._hook(name, index)

    def write(self, text: str) -> int:
        self._call("write")
        return super().write(text)

    def flush(self) -> None:
        self._call("flush")
        super().flush()


def _real_time_run(
    out: IO[str],
    stop_requested: Callable[[], bool],
    *,
    tracker: Tracker | None = None,
    queue_bound: int = watcher.WRITE_QUEUE_BATCHES,
    deadline: float = 20.0,
) -> dict[str, Any]:
    """The loop in real time on a modelled table where a process starts in
    every sample, so every sample has events to write, the first one a
    browser's roots."""
    table: dict[int, dict[str, Any]] = {
        **_row_actor_table(),
        80: {"start": 6.5, "ppid": 10, "exe": BROWSER_EXE, "cmdline": _chrome(PROFILE)},
    }
    count = {"samples": 0}

    def pids() -> list[int]:
        count["samples"] += 1
        table[100 + count["samples"]] = {"start": 7.0, "ppid": 10, "cmdline": ["x"]}
        return list(table)

    return watcher.observe(
        _sampler(table, root=10, pids=pids),
        tracker or Tracker(),
        out,
        stop_requested,
        base={},
        interval=_INTERVAL,
        deadline=deadline,
        queue_bound=queue_bound,
    )


def _recording(monkeypatch, tracker: Tracker) -> list[tuple[float, str, Any]]:
    """Every event *tracker* produces, in order, with the ready line where
    the loop adds it: after the first sample's events."""
    produced: list[tuple[float, str, Any]] = []
    observe_ = tracker.observe

    def recording(sample, t):
        events = observe_(sample, t)
        produced.extend((t, kind, fields.get("pid")) for _, kind, fields in events)
        if tracker.samples == 1:
            produced.append((t, "watcher.ready", os.getpid()))
        return events

    monkeypatch.setattr(tracker, "observe", recording)
    return produced


def _written(out: io.StringIO) -> list[tuple[float, str, Any]]:
    return [
        (r["t"], r["kind"], r.get("pid"))
        for r in map(json.loads, out.getvalue().splitlines())
    ]


@pytest.mark.parametrize("call", ["stop_check", "write", "flush"])
def test_a_blocked_file_system_call_does_not_hold_sampling(call):
    began = time.monotonic()

    def block(name: str, index: int) -> None:
        # Every call of its kind after the first, whichever thread makes it:
        # the first write carries the ready line.
        if name == call and index >= 1:
            time.sleep(_BLOCKED)

    checks = {"n": 0}
    requested: list[float] = []

    def stop_requested() -> bool:
        block("stop_check", checks["n"])
        checks["n"] += 1
        if time.monotonic() - began < _RUN:
            return False
        requested.append(time.time())
        return True

    summary = _real_time_run(_SlowFile(block), stop_requested)
    # One more sample began once the request was seen.
    assert summary["stopped_by"] == "stop file"
    assert summary["sample_log"][-1][0] >= requested[0]
    # Sampling went on through the blocked call, and the row would pass.
    assert summary["observation_end"] - summary["observation_start"] >= _BLOCKED
    assert summary["max_gap_seconds"] < harness.MAX_WATCHER_GAP_SECONDS
    assert (
        watcher_failures(
            summary,
            actors_began=summary["observation_start"],
            actors_ended=summary["observation_end"],
        )
        == []
    )
    # The slow file system is still on record.
    assert summary["file_io"][call]["max_seconds"] >= _BLOCKED


def test_events_reach_the_file_in_the_order_the_samples_produced_them(monkeypatch):
    tracker = Tracker()
    produced = _recording(monkeypatch, tracker)
    # Every write is slow, so samples pile up behind it and leave together.
    out = _SlowFile(lambda name, _: time.sleep(0.2) if name == "write" else None)
    began = time.monotonic()
    _real_time_run(out, lambda: time.monotonic() - began >= 1.0, tracker=tracker)
    # The ready line follows the first sample's events, as the harness reads it.
    assert [kind for _, kind, _ in produced[:2]] == ["browser.roots", "watcher.ready"]
    assert _written(out) == produced
    assert out.calls["write"] < len({t for t, _, _ in produced})


def test_a_writer_a_whole_queue_behind_holds_the_loop_and_loses_nothing(
    monkeypatch,
):
    tracker = Tracker()
    produced = _recording(monkeypatch, tracker)
    out = _SlowFile(lambda name, _: time.sleep(0.5) if name == "write" else None)
    began = time.monotonic()
    summary = _real_time_run(
        out, lambda: time.monotonic() - began >= 1.5, tracker=tracker, queue_bound=2
    )
    assert _written(out) == produced
    held = summary["file_io"]["queue"]
    assert held["bound"] == 2
    assert held["full_waits"] >= 1 and held["full_wait_seconds"] > 0
    # The wait is the loop's, and the gap it widened names it.
    assert summary["largest_gap"]["outside_sampling"]["steps"]["enqueue"] >= 0.1


@pytest.mark.parametrize("when", ["while sampling", "at shutdown"])
def test_a_failed_write_is_raised_not_swallowed(when):
    began = time.monotonic()
    stopping = threading.Event()

    def fail(name: str, index: int) -> None:
        if name != "write" or index == 0:
            return
        if when == "at shutdown" and index == 1:
            # Held until the stop is requested, so the last sample's events
            # wait behind it and only the drain at the end fails.
            stopping.wait(10)
            time.sleep(0.5)
            return
        raise OSError(errno.ENOSPC, "No space left on device")

    def stop_requested() -> bool:
        if when == "while sampling":
            return False
        if time.monotonic() - began >= 0.5:
            stopping.set()
        return stopping.is_set()

    with pytest.raises(RuntimeError, match="event writer failed") as raised:
        _real_time_run(_SlowFile(fail), stop_requested, deadline=10.0)
    assert isinstance(raised.value.__cause__, OSError)
    # Raised by the next sample handed over, not once the deadline ran out.
    assert time.monotonic() - began < 5.0


def test_a_failed_stop_file_check_is_raised_not_swallowed():
    def stop_requested() -> bool:
        raise PermissionError(errno.EACCES, "Access is denied")

    began = time.monotonic()
    with pytest.raises(RuntimeError, match="stop-file check failed") as raised:
        _real_time_run(io.StringIO(), stop_requested, deadline=10.0)
    assert isinstance(raised.value.__cause__, PermissionError)
    assert time.monotonic() - began < 5.0


class _BlockedWrites(io.TextIOBase):
    """The real event file, whose every write blocks first."""

    def __init__(self, out: IO[str], seconds: float) -> None:
        self._out, self._seconds = out, seconds

    def write(self, s: str) -> int:
        time.sleep(self._seconds)
        return self._out.write(s)

    def flush(self) -> None:
        self._out.flush()


def test_a_stop_request_publishes_the_whole_summary_behind_a_slow_writer(
    tmp_path, monkeypatch
):
    class SlowWriter(watcher.EventWriter):
        def __init__(self, out, *args, **kwargs) -> None:
            super().__init__(
                cast(IO[str], _BlockedWrites(out, _BLOCKED)), *args, **kwargs
            )

    monkeypatch.setattr(watcher, "EventWriter", SlowWriter)
    out, stop, profile = (
        tmp_path / "watcher.jsonl",
        tmp_path / "watcher.stop",
        tmp_path / "profile",
    )
    exits: list[int] = []
    arguments = [
        *("--out", str(out), "--stop", str(stop), "--run", "unit"),
        *("--experiment", "K0", "--row", "watcher-unit", "--platform", "test"),
        *("--root-pid", str(os.getpid()), "--deadline", "60"),
        *("--browser-dir", str(tmp_path / "ms-playwright")),
    ]
    # The real loop on the real process table, in this process.
    run = threading.Thread(target=lambda: exits.append(watcher.main(arguments)))
    run.start()
    browser: subprocess.Popen[bytes] | None = None
    try:
        limit = time.monotonic() + 30
        while not any(r["kind"] == "watcher.ready" for r in read_jsonl(out)):
            assert run.is_alive() and time.monotonic() < limit
            time.sleep(0.05)
        browser = _stand_in_browser(profile)
        # Sampled by now, its start waits behind a blocked write.
        time.sleep(0.5)
        requested = time.time()
        stop.touch()
        run.join(timeout=60)
    finally:
        stop.touch()
        run.join(timeout=60)
        if browser is not None:
            browser.kill()
            browser.wait(timeout=10)
    assert not run.is_alive() and exits == [0]
    *events, summary = read_jsonl(out)
    assert summary["kind"] == "watcher.summary"
    assert summary["stopped_by"] == "stop file"
    assert summary["observation_end"] >= requested
    assert summary["file_io"]["write"]["max_seconds"] >= _BLOCKED
    # Everything sampled is in the file, ahead of the summary.
    ours = f"--user-data-dir={profile}"
    assert any(r["kind"] == "process.start" and ours in r["cmdline"] for r in events)
    ends = {ended for _, ended, _ in summary["sample_log"]}
    assert all(r["t"] in ends for r in events)


# --- A lifetime's paths are resolved once ------------------------------------------

#: What one modelled path resolution takes: on Windows, ``realpath`` opens the
#: file, and has waited over a second for a busy filesystem.
_RESOLVE = 0.3
PYTHON_EXE = "/usr/bin/python3"


def _modelled_paths(monkeypatch, clock: dict[str, float], links: dict[str, str]):
    """Replace path resolution with a slow model of a filesystem whose
    symlinks are *links*; return the paths resolved, one list per sample."""
    resolved: list[list[str]] = []

    def slow_real(path: str) -> str:
        clock["now"] += _RESOLVE
        resolved[-1].append(path)
        return links.get(path, path)

    monkeypatch.setattr(watcher, "_real", slow_real)
    return resolved


def _modelled_sampler(table, clock: dict[str, float], no_exec: bool) -> Sampler:
    return _sampler(table, root=10, no_exec=no_exec, timer=lambda: clock["now"])


@pytest.mark.parametrize("no_exec", [True, False], ids=["windows", "posix"])
def test_a_lifetimes_paths_are_resolved_when_first_seen_not_every_sample(
    monkeypatch, no_exec
):
    clock = {"now": 0.0}
    resolved = _modelled_paths(monkeypatch, clock, {})
    table = _baseline_table()
    sampler = _modelled_sampler(table, clock, no_exec)
    breakdowns: list[dict[str, Any]] = []
    for n in range(1, 7):
        if n == 2:
            table[80] = {
                "start": 6.5,
                "ppid": 10,
                "exe": BROWSER_EXE,
                "cmdline": _chrome(PROFILE),
            }
        if n >= 2:
            # A new process in every sample, which is not a browser.
            table[100 + n] = {"start": 7.0, "ppid": 10, "cmdline": ["helper"]}
        resolved.append([])
        sampler.sample()
        assert sampler.breakdown is not None
        breakdowns.append(sampler.breakdown)
    if no_exec:
        # The first sample settles pid 1 by its image, which compares it with
        # the browser's paths; nothing new after that is compared.
        expected = [
            [BROWSER_DIR, BROWSER_EXE, PYTHON_EXE],
            [PROFILE],
            *[[]] * 4,
        ]
    else:
        # The browser's own paths once in the run, then every new lifetime's
        # executable once, when its marker is read.
        expected = [
            [],
            [BROWSER_DIR, BROWSER_EXE, BROWSER_EXE, PROFILE, PYTHON_EXE],
            *[[PYTHON_EXE]] * 4,
        ]
    assert [sorted(paths) for paths in resolved] == expected
    # Each resolution is charged to the sample that made it, and only it.
    for paths, breakdown in zip(resolved, breakdowns):
        assert breakdown["canonicalization"]["count"] == len(paths)
        assert breakdown["phases"]["canonicalization"] == pytest.approx(
            _RESOLVE * len(paths)
        )


def _link_table() -> dict[int, dict[str, Any]]:
    return {
        **_baseline_table(),
        80: {
            "start": 6.5,
            "ppid": 10,
            "exe": BROWSER_EXE,
            "cmdline": _chrome("/tmp/route"),
        },
    }


@pytest.mark.parametrize("no_exec", [True, False], ids=["windows", "posix"])
def test_a_recycled_pid_has_its_profile_resolved_again(monkeypatch, no_exec):
    clock = {"now": 0.0}
    links = {"/tmp/route": "/real/first"}
    resolved = _modelled_paths(monkeypatch, clock, links)
    table = _link_table()
    sampler = _modelled_sampler(table, clock, no_exec)
    resolved.append([])
    assert sampler.sample()[80].profile == "/real/first"
    # Another process at the same pid, on a path that now leads elsewhere.
    table[80]["start"] = 6.6
    links["/tmp/route"] = "/real/second"
    resolved.append([])
    assert sampler.sample()[80].profile == "/real/second"
    assert "/tmp/route" in resolved[1]


@pytest.mark.parametrize("no_exec", [True, False], ids=["windows", "posix"])
def test_a_lifetime_that_left_a_sample_is_resolved_again(monkeypatch, no_exec):
    # Linux reads a create time in clock ticks, so a recycled pid can show
    # the one its predecessor had. A sample that saw the pid gone has ended
    # that lifetime, and what was resolved for it goes with it.
    clock = {"now": 0.0}
    links = {"/tmp/route": "/real/first"}
    resolved = _modelled_paths(monkeypatch, clock, links)
    table = _link_table()
    sampler, tracker = _modelled_sampler(table, clock, no_exec), Tracker()
    resolved.append([])
    _observe(sampler, tracker, 0.0)
    entry = table.pop(80)
    resolved.append([])
    _observe(sampler, tracker, 1.0)
    table[80] = entry
    links["/tmp/route"] = "/real/second"
    resolved.append([])
    sample = sampler.sample()
    events = tracker.observe(sample, 2.0)
    assert [e[1] for e in events if e[2].get("pid") == 80] == ["process.start"]
    assert sample[80].profile == "/real/second"
    assert "/tmp/route" in resolved[2]


def test_a_lifetime_that_execs_onto_another_profile_is_resolved_again():
    # POSIX: the same pid and create time, a new command line.
    table = _link_table()
    sampler, tracker = _sampler(table, root=10), Tracker()
    _observe(sampler, tracker, 0.0)
    table[80]["cmdline"] = _chrome("/tmp/other-profile")
    _observe(sampler, tracker, 1.0)
    assert tracker.max_roots == {
        canonical_user_data_dir("/tmp/route"): 1,
        canonical_user_data_dir("/tmp/other-profile"): 1,
    }


def _symlinked_profile(tmp_path: Path) -> tuple[str, str]:
    """One profile directory and a second route to it, as macOS reaches its
    temporary directory through ``/var`` and ``/private/var``."""
    real = tmp_path / "real"
    (real / "profile").mkdir(parents=True)
    route = tmp_path / "route"
    try:
        route.symlink_to(real, target_is_directory=True)
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"cannot create a symlink here: {exc}")
    return str(real / "profile"), str(route / "profile")


@pytest.mark.parametrize("no_exec", [True, False], ids=["windows", "posix"])
@pytest.mark.parametrize("second_route", [False, True], ids=["same", "symlink"])
def test_a_second_root_on_a_resolved_profile_is_still_a_violation(
    tmp_path, no_exec, second_route
):
    profile, route = _symlinked_profile(tmp_path)
    table = {
        **_baseline_table(),
        80: {"start": 6.5, "ppid": 10, "exe": BROWSER_EXE, "cmdline": _chrome(profile)},
    }
    sampler, tracker = _sampler(table, root=10, no_exec=no_exec), Tracker()
    _observe(sampler, tracker, 0.0)
    # The first root's profile is resolved by now; the second comes later.
    table[81] = {
        "start": 6.6,
        "ppid": 10,
        "exe": BROWSER_EXE,
        "cmdline": _chrome(route if second_route else profile),
    }
    for t in (1.0, 2.0, 3.0):
        _observe(sampler, tracker, t)
    key = canonical_user_data_dir(profile)
    assert tracker.max_roots == {key: 2}
    assert tracker.violations == [
        {"t": t, "profile": key, "pids": [80, 81]} for t in (1.0, 2.0, 3.0)
    ]


# --- What O2 reads from the samples ----------------------------------------------


def test_a_sample_records_when_it_began_and_the_kernels_last_pid():
    table = _baseline_table()
    table[1]["pgid"] = 1
    last = iter([700, 705])
    sampler = _sampler(table, root=10, last_pid_of=lambda: next(last))
    sampler.sample()
    assert sampler.last_pid_at_begin == 700 and sampler.baseline_pgids == [1]
    began = sampler.began_at
    sampler.sample()
    assert sampler.last_pid_at_begin == 705
    assert began is not None and sampler.began_at is not None
    assert sampler.began_at >= began


def test_a_settled_process_that_joins_a_group_is_reported():
    # 50 is settled unrelated at the first sample; its group is still read.
    table = {**_baseline_table(), 50: {"start": 1.0, "ppid": 0, "cmdline": ["svc"]}}
    table[50]["pgid"] = 50
    sampler, tracker = _sampler(table, root=10), Tracker()
    _observe(sampler, tracker, 0.0)
    assert sampler.first_sample_cached >= 1
    table[50]["pgid"] = 30
    (update,) = [e for e in _observe(sampler, tracker, 1.0) if e[1] == "process.update"]
    assert (update[2]["pid"], update[2]["pgid"]) == (50, 30)
    assert update[2]["cmdline"] == []  # withheld: it is still no actor


# --- Windows: an image that cannot be the browser is read once ---------------------

LSAISO = "C:/Windows/System32/LsaIso.exe"


def _slow_unreadable_arguments(exe: str, clock: dict) -> dict[int, dict[str, Any]]:
    """A first-sample process like LsaIso.exe: its executable reads, its
    arguments take a second of psutil's retries and then fail."""
    table = _dead_parent_service()
    table[60] = {
        **table[60],
        "exe": exe,
        "cmdline": psutil.AccessDenied(60),
        "cmdline_seconds": 1.0,
        "clock": clock,
    }
    return table


def test_on_windows_a_non_browser_image_is_settled_by_one_read():
    clock = {"now": 0.0}
    table = _slow_unreadable_arguments(LSAISO, clock)
    sampler = _sampler(table, root=10, no_exec=True, timer=lambda: clock["now"])
    before = clock["now"]
    for _ in range(5):
        sampler.sample()
    assert table[60]["cmdline_reads"] == 1
    # One slow read in all, not one per sample.
    assert clock["now"] - before == 1.0
    assert sampler.stats()["slow_sample_count"] == 1
    (failure,) = sampler.read_failures
    assert failure["resolution"] == "settled by its image"
    assert failure["failures"] == ["cmdline: AccessDenied"]
    assert not failure["possible_browser"] and sampler.relevant_read_failures == []


def test_on_windows_an_unreadable_browser_image_stays_uncertain():
    clock = {"now": 0.0}
    table = _slow_unreadable_arguments(BROWSER_EXE, clock)
    sampler = _sampler(table, root=10, no_exec=True, timer=lambda: clock["now"])
    for _ in range(5):
        sampler.sample()
    assert table[60]["cmdline_reads"] == 5
    assert sampler.relevant_read_failures


def test_on_posix_an_unreadable_non_browser_is_still_read_every_sample():
    clock = {"now": 0.0}
    table = _slow_unreadable_arguments("/usr/sbin/service", clock)
    sampler = _sampler(table, root=10, no_exec=False, timer=lambda: clock["now"])
    for _ in range(5):
        sampler.sample()
    assert table[60]["cmdline_reads"] == 5
    assert all(
        failure.get("resolution") != "settled by its image"
        for failure in sampler.read_failures
    )


# POSIX is unchanged: a readable process is read on every sample there.
@pytest.mark.parametrize(("no_exec", "reads"), [(True, 0), (False, 3)])
def test_on_windows_another_users_arguments_are_never_read(no_exec, reads):
    table = _dead_parent_service()
    table[60] = {**table[60], "user": "NT AUTHORITY\\SYSTEM"}
    sampler = _sampler(table, root=10, no_exec=no_exec)
    for _ in range(3):
        sampler.sample()
    assert table[60].get("cmdline_reads", 0) == reads
