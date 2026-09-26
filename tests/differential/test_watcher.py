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

from differential.events import read_jsonl
from differential.watcher import (
    ProcessRecord,
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


def _run_watcher(tmp_path: Path, profile: Path, browsers: int) -> list[dict]:
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
            "--deadline",
            "60",
        ]
    )
    started: list[subprocess.Popen[bytes]] = []
    try:
        deadline = time.monotonic() + 15
        while not any(r["kind"] == "watcher.ready" for r in read_jsonl(out)):
            assert time.monotonic() < deadline, "the watcher never took its baseline"
            time.sleep(0.05)
        for _ in range(browsers):
            started.append(_stand_in_browser(profile))
        key = canonical_user_data_dir(str(profile))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if any(
                r["kind"] == "browser.roots"
                and len(r["roots"].get(key, [])) == browsers
                for r in read_jsonl(out)
            ):
                break
            time.sleep(0.05)
    finally:
        for process in started:
            process.kill()
            process.wait(timeout=10)
        # Long enough for the exits to land in a sample before it stops.
        time.sleep(0.5)
        stop.touch()
        watcher.wait(timeout=15)
    return read_jsonl(out)


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
    browser_pids = {
        r["pid"]
        for r in records
        if r["kind"] == "process.start"
        and r["actor"] == "browser"
        and ours in r["cmdline"]
    }
    assert len(browser_pids) == 1
    assert browser_pids <= {r["pid"] for r in records if r["kind"] == "process.exit"}
    assert os.getpid() not in browser_pids
