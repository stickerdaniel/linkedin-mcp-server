"""The watcher finds a second browser on one profile, and only that.

The logic runs on synthetic samples and a modelled process table first. The
process cases run the real watcher against stand-in "browsers": plain Python
processes whose command line carries ``--user-data-dir=``, which is all the
watcher reads, including one that only execs into that command line after
three seconds. So they measure the sampling on this platform's process table
without a browser, and a watcher that cannot see a process fails here rather
than passing O1 in a native row by never seeing anything.

A row actor whose metadata cannot be read is on record either way, and makes
the census uncertain only once it has stayed alive and unreadable past the
bound. On macOS that is exercised with the real setuid ``/bin/ps``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

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
        return _field(self._entry, "start")

    def ppid(self):
        return self._entry["ppid"]

    def exe(self):
        return _field({"exe": self._entry.get("exe", "/usr/bin/python3")}, "exe")

    def cmdline(self):
        return _field(self._entry, "cmdline")


#: The harness's user in the model; a table entry may name another in "user".
HARNESS = "harness-user"


def _user_of(process) -> object:
    return process._entry.get("user", HARNESS)


def _sampler(table, *, root=1, browser_exe=BROWSER_EXE):
    return Sampler(
        root,
        own_pid=999,
        pids=lambda: list(table),
        open_process=lambda pid: _FakeProcess(table, pid),
        user_of=_user_of,
        user=HARNESS,
        browser_exe=browser_exe,
        browser_dir=BROWSER_DIR,
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


def test_a_later_reading_that_shows_another_user_resolves_it():
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = {"start": 2.0, "ppid": 1, "open": psutil.AccessDenied(2)}
    _observe(sampler, tracker, 1.0)
    table[2] = {"start": 2.0, "ppid": 50, "cmdline": ["daemon"], "user": "root"}
    _observe(sampler, tracker, 2.0)
    (episode,) = sampler.read_failures
    assert not episode["possible_browser"]
    assert episode["resolved_by"] == "another user"
    assert _judged(sampler, tracker) == []


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
