"""The watcher finds a second browser on one profile, and only that.

The logic runs on synthetic samples first. The last two cases run the real
watcher process against stand-in "browsers": plain Python processes whose
command line carries ``--user-data-dir=``, which is all the watcher reads. So
they measure the sampling on this platform's process table without a browser,
and a watcher that cannot see a process fails here rather than passing O1 in a
native row by never seeing anything.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import psutil

from differential.events import read_jsonl
from differential.harness import watcher_failures
from differential.watcher import (
    ProcessRecord,
    Sampler,
    Tracker,
    browser_roots,
    canonical_user_data_dir,
    classify,
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
    assert any(
        r["kind"] in ("process.update", "process.start")
        and r["actor"] == "browser"
        and f"--user-data-dir={profile}" in r["cmdline"]
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


def test_a_watcher_stopped_on_request_after_the_actors_is_healthy(tmp_path):
    watcher = _start_watcher(tmp_path)
    began = time.time()
    time.sleep(0.3)
    ended = time.time()
    records = _stop_watcher(tmp_path, watcher)
    (summary,) = [r for r in records if r["kind"] == "watcher.summary"]
    assert summary["observation_start"] <= began
    assert summary["observation_end"] >= ended
    assert watcher_failures(summary, actors_began=began, actors_ended=ended) == []


class _FakeProcess:
    def __init__(self, table, pid):
        self.pid = pid
        self._entry = table[pid]

    def create_time(self):
        return self._entry["start"]

    def ppid(self):
        return self._entry["ppid"]

    def exe(self):
        return "/bin/stand-in"

    def cmdline(self):
        cmdline = self._entry["cmdline"]
        if isinstance(cmdline, Exception):
            raise cmdline
        return cmdline


def _sampler(table, *, root=1):
    return Sampler(
        root,
        own_pid=999,
        pids=lambda: list(table),
        open_process=lambda pid: _FakeProcess(table, pid),
    )


def test_a_row_actor_whose_command_line_cannot_be_read_is_recorded():
    table = {
        1: {"start": 1.0, "ppid": 0, "cmdline": ["pytest"]},
        50: {"start": 1.0, "ppid": 0, "cmdline": ["system-service"]},
    }
    sampler = _sampler(table)
    sampler.sample()
    table[2] = {"start": 2.0, "ppid": 1, "cmdline": psutil.AccessDenied(2)}
    table[3] = {"start": 2.0, "ppid": 50, "cmdline": psutil.AccessDenied(3)}
    sampler.sample()
    sampler.sample()
    assert [(f["pid"], f["failure"]) for f in sampler.relevant_read_failures] == [
        (2, "cmdline: AccessDenied")
    ]
    failures = watcher_failures(
        {
            "stopped_by": "stop file",
            "observation_start": 0.0,
            "observation_end": 10.0,
            "max_gap_seconds": 0.1,
            "relevant_read_failures": sampler.relevant_read_failures,
        },
        actors_began=1.0,
        actors_ended=9.0,
    )
    assert any("could not read" in failure for failure in failures)


def test_actors_descend_from_the_root_and_are_re_read_every_sample():
    table = {1: {"start": 1.0, "ppid": 0, "cmdline": ["pytest"]}}
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
