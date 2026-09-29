"""The signal oracle's parser, O2's derivation, the canaries and H-R6's verdict.

The parser is fed lines strace 6.8 wrote on Ubuntu 24.04 (``strace -f -ttt
-yy -e trace=kill,tkill,tgkill,pidfd_send_signal -e signal=none -p PID``,
captured in a container from a script that signalled only its own children),
plus the split-call form strace's manual documents. O2 is derived from
modelled watcher records shaped as the watcher writes them. The canaries are
real processes, started and ended by the test. No process is signalled here
that the test did not start.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from types import SimpleNamespace
from typing import Any, cast

import psutil
import pytest

from differential import harness
from differential.harness import (
    GUARDIAN_OWNER_GROUP_KILL,
    RowResult,
    associate_server,
    compare_to_direct,
    guardian_launch,
    judge_row,
    r6_reading,
    r6_verdict,
)
from differential import signals
from differential.signals import (
    COMPLETE,
    HELD,
    INCOMPLETE,
    UNAVAILABLE,
    UNKNOWN,
    UNOBSERVED,
    VIOLATED,
    Canaries,
    O2Result,
    OracleOutcome,
    ProcessHistory,
    SignalOracle,
    classes_direct_would_not_send,
    derive_o2,
    oracle_required,
    oracle_unavailable,
    parse_strace,
    read_trace,
)
from differential.test_row_judgement import _healthy
from differential.test_watcher import (
    BROWSER_DIR,
    BROWSER_EXE,
    _observe,
    _row_table,
    _sampler,
)
from differential.watcher import BROWSER_MARKER_ENV, Tracker

# --- The parser ------------------------------------------------------------------

#: Real output, strace 6.8, Ubuntu 24.04 (see the module docstring).
CAPTURED = """\
2687  1790476253.935606 kill(2689, 0)   = 0
2687  1790476253.935730 kill(2689, SIGTERM) = 0
2687  1790476253.935803 kill(-2690, SIGKILL) = 0
2687  1790476253.935823 kill(999999, SIGKILL) = -1 ESRCH (No such process)
2687  1790476253.935890 pidfd_send_signal(3<pid:2687>, 0, NULL, 0) = 0
2687  1790476253.935985 tgkill(2687, 2687, 0) = 0
2687  1790476253.940306 +++ exited with 0 +++
"""


def test_captured_strace_lines_parse_to_their_targets():
    calls = parse_strace(CAPTURED)
    assert [(c.syscall, c.signal, c.target_pid, c.target_group) for c in calls] == [
        ("kill", "0", 2689, None),
        ("kill", "SIGTERM", 2689, None),
        ("kill", "SIGKILL", None, 2690),
        ("kill", "SIGKILL", 999999, None),
        ("pidfd_send_signal", "0", 2687, None),
        ("tgkill", "0", 2687, None),
    ]
    assert all(call.tid == 2687 for call in calls)
    assert calls[1].t == 1790476253.935730
    assert calls[3].reached_nobody and not calls[1].reached_nobody
    assert calls[0].probe and not calls[1].probe


def test_a_call_split_by_another_thread_is_joined_and_signal_lines_skipped():
    text = (
        "300  10.000001 kill(400, SIGKILL <unfinished ...>\n"
        "301  10.000002 --- SIGCHLD {si_signo=SIGCHLD} ---\n"
        "300  10.000003 <... kill resumed>) = 0\n"
        "300  10.000004 kill(0, SIGTERM) = 0\n"
        "300  10.000005 kill(-1, SIGKILL) = 0\n"
        "300  10.000006 tkill(401, SIGUSR1) = 0\n"
        "300  10.000007 pidfd_send_signal(5<anon_inode:[pidfd]>, SIGKILL, NULL, 0) = 0\n"
        "300  10.000008 +++ killed by SIGKILL +++\n"
    )
    trace = read_trace(text)
    assert trace.problems == [] and trace.ended == {300}
    calls = trace.calls
    assert [(c.syscall, c.target_pid, c.target_group, c.everyone) for c in calls] == [
        ("kill", 400, None, False),
        ("kill", None, 0, False),
        ("kill", None, None, True),
        ("tkill", 401, None, False),
        # A pidfd strace could not name: no target, never a group.
        ("pidfd_send_signal", None, None, False),
    ]
    assert calls[0].t == 10.000001


# --- O2 ----------------------------------------------------------------------------

HARNESS = 1


def _start(pid, ppid, t, *, start=None, actor="other", pgid=None, in_row=True):
    return {
        "kind": "process.start",
        "t": t,
        "pid": pid,
        "ppid": ppid,
        "pgid": pgid if pgid is not None else pid,
        "start_identity": float(start if start is not None else t),
        "in_row": in_row,
        "actor": actor,
    }


def _exit(pid, t, start):
    return {"kind": "process.exit", "t": t, "pid": pid, "start_identity": float(start)}


def _row() -> list[dict[str, Any]]:
    """The harness (1) starts a frontend (10), which starts an owner (20) in a
    session of its own; the owner starts its guardian (21) and the driver (22),
    which starts Chromium (30) in a group of its own with a helper (31). A
    canary (90) and an unrelated process (95) run beside them."""
    return [
        _start(10, HARNESS, 1.0, actor="frontend", pgid=5),
        _start(20, 10, 2.0, actor="owner", pgid=20),
        _start(21, 20, 3.0, actor="guardian", pgid=21),
        _start(22, 20, 3.0, actor="driver", pgid=20),
        _start(30, 22, 4.0, actor="browser", pgid=30),
        _start(31, 30, 4.0, actor="other", pgid=30),
        _start(90, HARNESS, 1.5, actor="other", pgid=90),
        _start(95, 7, 1.5, actor="other", pgid=95, in_row=False),
    ]


def _timeline(
    records,
    *,
    baseline=(1, 5, 7),
    last_pid=lambda end: 100000,
    failures=(),
    extra=(),
) -> list[dict[str, Any]]:
    """*records* as the watcher publishes them: a ready line naming the first
    sample's groups, and a summary logging every sample. A sample ends at
    every event's time and every quarter second; each began 0.01 earlier."""
    ends = sorted(
        {float(r["t"]) for r in records}
        | {round(0.25 * i, 2) for i in range(1, 60)}
        | set(extra)
    )
    return [
        {"kind": "watcher.ready", "t": 0.1, "baseline_pgids": list(baseline)},
        *records,
        {
            "kind": "watcher.summary",
            "t": ends[-1],
            "sample_log": [[end - 0.01, end, last_pid(end)] for end in ends],
            "read_failures": list(failures),
        },
    ]


def _outcome(text: str, **fields) -> OracleOutcome:
    return OracleOutcome(status=COMPLETE, calls=parse_strace(text), **fields)


def _o2(text: str, records=None, **kwargs) -> O2Result:
    history = ProcessHistory(_timeline(records or _row()), outside=[HARNESS])
    available = kwargs.pop("oracle_available", True)
    outcome = _outcome(text, threads=kwargs.pop("threads", {}))
    if not available:
        outcome = OracleOutcome(status=UNAVAILABLE)
    return derive_o2(outcome, history, **kwargs)


def test_the_guardians_drain_of_its_browser_group_holds():
    result = _o2("21  6.0 kill(-30, SIGKILL) = 0\n")
    assert result.state == HELD
    assert result.classes == ("guardian:browser-group",)
    assert result.resolved[0]["targets"] == [[30, 4.0], [31, 4.0]]


@pytest.mark.parametrize(
    ("line", "why"),
    [
        pytest.param("21  6.0 kill(90, SIGKILL) = 0\n", "canary", id="a-canary"),
        pytest.param("21  6.0 kill(10, SIGTERM) = 0\n", "frontend", id="the-frontend"),
        pytest.param(
            "21  6.0 kill(95, SIGKILL) = -1 EPERM (Operation not permitted)\n",
            "outside the row",
            id="outside-the-row",
        ),
        pytest.param("21  6.0 kill(-1, SIGKILL) = 0\n", "everyone", id="everyone"),
    ],
)
def test_a_signal_outside_the_launched_set_violates_o2(line, why):
    result = _o2(line)
    assert result.state == VIOLATED, (why, result)


def test_a_group_with_a_member_outside_the_set_violates_o2():
    # The canary joined Chromium's group in this model: the group signal
    # reaches it too.
    records = [*_row(), {**_start(91, HARNESS, 4.5, pgid=30), "actor": "other"}]
    assert _o2("21  6.0 kill(-30, SIGKILL) = 0\n", records).state == VIOLATED


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(
            "21  6.0 kill(-7, SIGKILL) = 0\n", id="group-led-before-the-watcher"
        ),
        # The frontend's group existed at the first sample: members the
        # watcher never reported may be in it beside the one it did.
        pytest.param("21  6.0 kill(-5, SIGKILL) = 0\n", id="group-in-the-baseline"),
        pytest.param("21  6.0 kill(4242, SIGKILL) = 0\n", id="pid-never-reported"),
        pytest.param("777  6.0 kill(-30, SIGKILL) = 0\n", id="sender-unknown"),
        pytest.param(
            "21  6.0 pidfd_send_signal(5<anon_inode:[pidfd]>, SIGKILL, NULL, 0) = 0\n",
            id="pidfd-unnamed",
        ),
    ],
)
def test_what_cannot_be_resolved_makes_o2_unknown(line):
    assert _o2(line).state == UNKNOWN


def _crashpad(marker: str | None, *, pid: int = 41, group: int = 40) -> list[dict]:
    """The row's browser (30) with its marker, and a crashpad handler it
    double-forked: parented to pid 1, leading nothing, in a group whose leader
    (*group*) exited before the watcher saw it. Its record is the shape the
    watcher writes for a process outside the row's tree."""
    records = _row()
    records[4] = {**records[4], "browser_marker": "m-row"}
    handler = _start(pid, 1, 4.1, pgid=group, in_row=False)
    if marker is not None:
        handler["browser_marker"] = marker
    return [*records, handler]


def test_the_guardians_drain_of_a_crashpad_group_holds():
    # As on the arm64 runner: kill(-9330) and kill(-9332) from the guardian.
    result = _o2("21  6.0 kill(-40, SIGKILL) = 0\n", _crashpad("m-row"))
    assert result.state == HELD, result
    assert result.classes == ("guardian:browser-group",)
    assert result.resolved[0]["targets"] == [[41, 4.1]]


def test_a_crashpad_handler_is_in_its_browsers_launched_set():
    result = _o2("21  6.0 kill(41, SIGKILL) = 0\n", _crashpad("m-row"))
    assert result.state == HELD and result.classes == ("guardian:browser",)


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param(None, id="no-marker"),
        pytest.param("m-other", id="another-launchs-marker"),
    ],
)
def test_a_group_of_a_process_without_the_rows_marker_is_outside(marker):
    # The samples say who was in group 40; its member is no launch of the
    # row's, whatever the group's leader was.
    result = _o2("21  6.0 kill(-40, SIGKILL) = 0\n", _crashpad(marker))
    assert result.state == VIOLATED, result


def test_a_process_outside_the_tree_without_the_rows_marker_is_outside():
    result = _o2("21  6.0 kill(41, SIGKILL) = 0\n", _crashpad("m-other"))
    assert result.state == VIOLATED, result


def _packet(kind, t, pid, ppid, pgid, start, actor, *, in_row=True, marker=None):
    record = {
        "kind": kind,
        "t": t,
        "pid": pid,
        "ppid": ppid,
        "pgid": pgid,
        "start_identity": start,
        "in_row": in_row,
        "actor": actor,
    }
    if marker is not None:
        record["browser_marker"] = marker
    return record


#: Run 36294934506, ubuntu-24.04-arm, H-R6 K1 frozen, as the watcher wrote it
#: (times less 1790484300, the harness at 9157). The frozen Direct server
#: (9327) was killed at 10.95; its guardian (9333) then drained the browser's
#: group and both crashpad groups, exited at 12.004, and one sample (12.005)
#: caught it between its exit and its reaping: reparented to pid 1, no
#: command line, so it read as ``other``.
_MARK = "48d804df7f802656"
_K1_FROZEN = [
    _packet("process.start", 5.714, 9327, 9157, 1889, 5.14, "frontend"),
    _packet("process.start", 6.669, 9333, 9327, 9333, 6.05, "guardian"),
    _packet("process.start", 6.669, 9334, 9327, 1889, 6.08, "driver"),
    _packet("process.start", 6.971, 9348, 9334, 9348, 6.39, "browser", marker=_MARK),
    _packet(
        "process.start", 7.026, 9350, 1, 9349, 6.41, "other", in_row=False, marker=_MARK
    ),
    _packet(
        "process.start", 7.026, 9352, 1, 9351, 6.41, "other", in_row=False, marker=_MARK
    ),
    _packet("process.start", 7.026, 9355, 9348, 9348, 6.42, "browser"),
    _packet("process.start", 7.026, 9356, 9348, 9348, 6.42, "browser"),
    _packet("process.start", 7.026, 9376, 9355, 9348, 6.45, "browser"),
    _packet("process.start", 7.071, 9379, 9348, 9348, 6.45, "browser"),
    _packet("process.start", 7.071, 9391, 9356, 9348, 6.47, "browser"),
    _packet("process.exit", 10.955, 9327, 9157, 1889, 5.14, "frontend"),
    *[
        _packet("process.exit", 11.002, pid, ppid, pgid, start, actor, in_row=row)
        for pid, ppid, pgid, start, actor, row in [
            (9334, 1, 1889, 6.08, "driver", True),
            (9348, 9334, 9348, 6.39, "browser", True),
            (9350, 1, 9349, 6.41, "other", False),
            (9352, 1, 9351, 6.41, "other", False),
            (9355, 9348, 9348, 6.42, "browser", True),
            (9356, 9348, 9348, 6.42, "browser", True),
            (9376, 9355, 9348, 6.45, "browser", True),
            (9379, 9348, 9348, 6.45, "browser", True),
            (9391, 9356, 9348, 6.47, "browser", True),
        ]
    ],
    _packet("process.update", 12.005, 9333, 1, 9333, 6.05, "other"),
    _packet("process.exit", 12.054, 9333, 1, 9333, 6.05, "other"),
]
_K1_FROZEN_TRACE = """\
9333  10.962584 kill(-9348, SIGKILL) = 0
9333  10.967286 kill(-9349, SIGKILL) = 0
9333  10.967993 kill(-9351, SIGKILL) = 0
9333  12.004077 +++ exited with 0 +++
"""


@pytest.mark.parametrize(
    "records",
    [
        pytest.param(_K1_FROZEN, id="exiting-guardian-sampled"),
        pytest.param(
            [r for r in _K1_FROZEN if r["kind"] != "process.update"],
            id="exiting-guardian-missed",
        ),
        pytest.param(
            # The same race on the browser, whose marker ties crashpad to it.
            [
                *_K1_FROZEN,
                _packet("process.update", 10.99, 9348, 1, 9348, 6.39, "other"),
            ],
            id="exiting-browser-sampled",
        ),
    ],
)
def test_a_guardian_is_judged_by_its_role_when_it_signalled(records):
    history = ProcessHistory(
        _timeline(records, baseline=(1, 1889), last_pid=lambda end: 9455),
        outside=[9157],
    )
    result = derive_o2(
        _outcome(_K1_FROZEN_TRACE, traced=[9327, 9333], attached_at=10.9),
        history,
    )
    assert result.state == HELD, result
    assert result.classes == ("guardian:browser-group",)
    assert [r["principal"] for r in result.resolved] == [[9327, 5.14]] * 3


def test_a_group_is_resolved_with_the_members_it_had_when_signalled():
    # 31 leaves Chromium's group (30) after the signal: it was still reached.
    records = [
        *_row(),
        {
            **_start(31, 30, 7.0, pgid=31),
            "kind": "process.update",
            "start_identity": 4.0,
        },
    ]
    result = _o2("21  6.0 kill(-30, SIGKILL) = 0\n", records)
    assert result.resolved[0]["targets"] == [[30, 4.0], [31, 4.0]]
    later = _o2("21  8.0 kill(-30, SIGKILL) = 0\n", records)
    assert later.resolved[0]["targets"] == [[30, 4.0]]
    # Between the sample that saw it in 30 and the one that saw it in 31,
    # where it was at the send is not known: neither is the group.
    between = _o2("21  6.9 kill(-30, SIGKILL) = 0\n", records)
    assert between.state == UNKNOWN and "one sample only" in between.unknowns[0]


def test_a_thread_of_a_traced_process_is_attributed_through_the_map():
    line = "2001  6.0 kill(-30, SIGKILL) = 0\n"
    assert _o2(line).state == UNKNOWN
    assert _o2(line, threads={2001: 21}).state == HELD


def test_probes_and_signals_that_reached_nobody_are_not_deliveries():
    text = (
        "21  6.0 kill(90, 0) = 0\n"
        "21  6.0 kill(4242, SIGKILL) = -1 ESRCH (No such process)\n"
    )
    result = _o2(text)
    assert result.state == HELD and result.resolved == []


def test_a_signal_to_a_target_that_already_exited_is_not_resolved_to_it():
    records = [*_row(), _exit(30, 5.0, 4.0)]
    assert _o2("21  6.0 kill(30, SIGKILL) = 0\n", records).state == UNKNOWN


def test_a_canary_death_violates_the_rows_o2_even_without_an_oracle():
    death = [{"pid": 90}]
    unobserved = _o2("", oracle_available=False)
    assert (unobserved.state, unobserved.row) == (UNOBSERVED, UNOBSERVED)
    dead = _o2("", oracle_available=False, canary_deaths=death)
    assert (dead.state, dead.row) == (UNOBSERVED, VIOLATED)
    assert _o2("", canary_deaths=death).row == VIOLATED


def test_the_pre_path_a_guardian_kill_is_a_signal_direct_would_not_send():
    # The owner's guardian kills the owner's group (20): the driver, which the
    # owner launched. Per row that holds; across experiments it is '!'.
    result = _o2("21  6.0 kill(-20, SIGKILL) = 0\n")
    assert result.state == HELD
    assert result.classes == (GUARDIAN_OWNER_GROUP_KILL,)
    assert classes_direct_would_not_send(result.classes) == [GUARDIAN_OWNER_GROUP_KILL]
    assert classes_direct_would_not_send(("guardian:browser-group",)) == []


# --- The oracle ---------------------------------------------------------------------

RUNNER = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"}


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        (
            {"platform": "darwin", "environ": RUNNER, "strace": "/x"},
            "no strace on darwin",
        ),
        (
            {"platform": "win32", "environ": RUNNER, "strace": "/x"},
            "no strace on win32",
        ),
        ({"platform": "linux", "environ": {}, "strace": "/x"}, "disposable"),
        (
            {"platform": "linux", "environ": {**RUNNER, "ACT": "true"}, "strace": "/x"},
            "disposable",
        ),
        (
            {"platform": "linux", "environ": RUNNER, "strace": "/x", "scope": 3},
            "ptrace_scope is 3",
        ),
    ],
)
def test_the_oracle_says_why_it_cannot_run(kwargs, reason):
    found = oracle_unavailable(**kwargs)
    assert found is not None and reason in found


def test_the_oracle_runs_on_a_disposable_linux_runner_with_strace():
    for scope in (0, 1, 2, None):
        assert (
            oracle_unavailable(
                platform="linux", environ=RUNNER, strace="/usr/bin/strace", scope=scope
            )
            is None
        )


def test_the_oracle_traces_every_signal_syscall_of_the_pids_given(tmp_path):
    command = SignalOracle(tmp_path).command([20, 21])
    assert command[:3] == ["sudo", "-n", "strace"]
    assert "-yy" in command and "-f" in command and "-ttt" in command
    assert "-T" in command
    assert (
        "trace=kill,tkill,tgkill,pidfd_send_signal,rt_sigqueueinfo,"
        "rt_tgsigqueueinfo,clone,clone3,fork,vfork" in command
    )
    # Every end of a tracee is written, one a signal caused included.
    assert not any(part.startswith("signal=") for part in command)
    assert command[-4:] == ["-p", "20", "-p", "21"]


@pytest.mark.parametrize(
    ("required", "status"), [(False, UNAVAILABLE), (True, INCOMPLETE)]
)
def test_an_unavailable_oracle_attaches_nothing(tmp_path, required, status):
    oracle = SignalOracle(tmp_path, required=required)
    oracle.unavailable = "modelled"
    assert oracle.start([1]) == "modelled"
    outcome = oracle.stop()
    assert outcome.status == status and outcome.calls == []
    assert outcome.reasons == ["modelled"]


def test_the_oracle_is_required_only_in_native_linux_ci():
    ci = {**RUNNER, "LINKEDIN_MCP_DIFFERENTIAL_CI": "1"}
    assert oracle_required(platform="linux", environ=ci)
    assert not oracle_required(platform="linux", environ=RUNNER)
    assert not oracle_required(platform="darwin", environ=ci)
    assert not oracle_required(platform="linux", environ={**ci, "ACT": "true"})


# --- Canaries -----------------------------------------------------------------------


def test_canaries_run_outside_the_harness_and_are_ended_by_it():
    canaries = Canaries(count=2)
    started = canaries.start()
    try:
        assert len(started) == 2
        assert canaries.outside_the_harness() == []
        assert canaries.deaths() == []
        # One dies during the row: that is a death nobody explained.
        started[0].process.kill()
        started[0].process.wait(timeout=10)
        assert [death["pid"] for death in canaries.deaths()] == [started[0].pid]
    finally:
        canaries.stop()
    assert all(canary.process.poll() is not None for canary in started)
    assert canaries.canaries == []


@pytest.mark.skipif(os.name == "nt", reason="sessions are POSIX's")
def test_a_canary_leads_a_session_of_its_own():
    canaries = Canaries(count=1)
    (canary,) = canaries.start()
    try:
        assert os.getsid(canary.pid) == canary.pid != os.getsid(0)
        assert os.getpgid(canary.pid) == canary.pid
    finally:
        canaries.stop()


# --- Guardian, association, verdicts -------------------------------------------------


def _guardian_record(ppid: int, group: str) -> dict[str, Any]:
    return {
        "kind": "process.start",
        "pid": 21,
        "ppid": ppid,
        "in_row": True,
        "cmdline": [
            "/venv/bin/python",
            "-I",
            "-S",
            "-u",
            "/x/linkedin_mcp_server/process_guardian.py",
            "5",
            "7",
            group,
        ],
    }


def test_the_guardians_group_is_read_from_its_argv():
    assert guardian_launch([_guardian_record(20, "20")], 20) == (21, 20)
    assert guardian_launch([_guardian_record(20, "0")], 20) == (21, 0)
    # Another principal's guardian, or one outside the row, is not this one.
    assert guardian_launch([_guardian_record(19, "20")], 20) is None
    assert (
        guardian_launch([{**_guardian_record(20, "20"), "in_row": False}], 20) is None
    )


class _Process:
    def __init__(self, created: float) -> None:
        self.created = created

    def create_time(self) -> float:
        return self.created


def test_the_server_is_killed_only_through_a_handle_the_watcher_vouched_for():
    record = {
        "kind": "process.start",
        "actor": "frontend",
        "in_row": True,
        "pid": 10,
        "start_identity": 100.0,
    }
    process, created = associate_server(
        10, lambda: [record], open_process=lambda _pid: _Process(100.0), seconds=0.1
    )
    assert process is not None and created == 100.0
    # Another lifetime at that pid: no handle, so nothing is killed.
    process, _ = associate_server(
        10, lambda: [record], open_process=lambda _pid: _Process(250.0), seconds=0.1
    )
    assert process is None


def _r6(
    *,
    group: int | None,
    classes=(),
    recovered=True,
    exit="killed",
    attached=True,
    daemon=True,
    read=True,
) -> RowResult:
    vector = harness.RowVector(
        mode="daemon" if daemon else "direct",
        o1_single_browser=True,
        browser_seen=True,
        watcher_healthy=True,
        o4_session="retained",
        origin_saw_feed=True,
        feed_carried_session=True,
        tool_succeeded=read,
        owner_published=daemon,
        fell_back=False,
        host_exit_clean=True,
        cleanup_clean=True,
        o2_traced=HELD if attached else UNOBSERVED,
        o2_required=attached,
        oracle_collection=COMPLETE if attached else UNAVAILABLE,
        signal_classes=tuple(classes),
        guardian_owner_group=group,
        recovered=recovered if daemon else None,
    )
    return RowResult(
        "K2",
        "daemon" if daemon else "direct",
        vector=vector,
        killed={"exit": exit, "oracle": {"attached": attached}},
    )


@pytest.mark.parametrize("experiment", ["K1", "K2", "K3"])
@pytest.mark.parametrize("windows", [False, True])
def test_a_kill_after_a_first_call_that_read_nothing_does_not_count(
    experiment, windows
):
    # The group kill alone would carry K2's reading; the row still needs the
    # first call to have read the synthetic post before the kill.
    result = _r6(group=4321, classes=[GUARDIAN_OWNER_GROUP_KILL], read=False)
    problems = r6_verdict(result, experiment=experiment, windows=windows, linux=False)
    assert "the first call did not read the synthetic post" in problems


def test_k2_reads_bang_from_the_guardians_group():
    result = _r6(group=4321, classes=[GUARDIAN_OWNER_GROUP_KILL])
    assert r6_reading(result) == "!"
    assert r6_verdict(result, experiment="K2", windows=False, linux=False) == []
    # Without an oracle the argv alone carries the reading.
    unobserved = _r6(group=4321, attached=False)
    assert r6_verdict(unobserved, experiment="K2", windows=False, linux=False) == []


@pytest.mark.parametrize("attached", [True, False])
def test_k2_reading_equal_is_a_harness_defect(attached):
    (problem,) = r6_verdict(
        _r6(group=0, attached=attached), experiment="K2", windows=False, linux=False
    )
    assert "K2 read '='" in problem and "harness defect" in problem


def test_k2_whose_oracle_missed_the_group_kill_is_a_harness_defect():
    (problem,) = r6_verdict(_r6(group=4321), experiment="K2", windows=False, linux=True)
    assert "oracle saw no kill" in problem


def test_k3_must_read_equal_and_recover():
    assert r6_verdict(_r6(group=0), experiment="K3", windows=False, linux=False) == []
    assert r6_verdict(_r6(group=4321), experiment="K3", windows=False, linux=False)
    assert r6_verdict(
        _r6(group=0, classes=[GUARDIAN_OWNER_GROUP_KILL]),
        experiment="K3",
        windows=False,
        linux=False,
    )
    (problem,) = r6_verdict(
        _r6(group=0, recovered=False), experiment="K3", windows=False, linux=False
    )
    assert "did not recover" in problem


def test_h_r6_needs_the_kill_and_on_posix_the_guardian():
    assert r6_verdict(
        _r6(group=0, exit="gone before the kill"),
        experiment="K3",
        windows=False,
        linux=False,
    )
    (problem,) = r6_verdict(
        _r6(group=None), experiment="K3", windows=False, linux=False
    )
    assert "never seen" in problem
    # Windows starts no guardian: nothing to read, nothing required of it.
    assert r6_verdict(_r6(group=None), experiment="K2", windows=True, linux=False) == []


def test_on_windows_the_candidate_must_still_recover():
    assert r6_verdict(_r6(group=None), experiment="K3", windows=True, linux=False) == []
    (problem,) = r6_verdict(
        _r6(group=None, recovered=False), experiment="K3", windows=True, linux=False
    )
    assert "did not recover" in problem


def test_a_row_that_violated_o2_fails_and_differs_from_direct(profile_pair):
    healthy = _healthy(profile_pair, daemon=True)
    violated = O2Result(
        state=UNOBSERVED,
        row=VIOLATED,
        canary_deaths=["canary 90 died during the row"],
    )
    vector, failures = judge_row(dataclasses.replace(healthy, o2=violated))
    assert vector.o2 == VIOLATED
    assert any("O2" in failure for failure in failures)
    direct, _ = judge_row(_healthy(profile_pair, daemon=False))
    assert any(d.startswith("o2") for d in compare_to_direct(direct, vector))


def test_a_class_direct_would_not_send_differs_from_direct(profile_pair):
    held = O2Result(state=HELD, classes=(GUARDIAN_OWNER_GROUP_KILL,))
    vector, _ = judge_row(dataclasses.replace(_healthy(profile_pair), o2=held))
    direct = dataclasses.replace(vector, mode="direct", signal_classes=())
    (difference,) = compare_to_direct(direct, vector)
    assert GUARDIAN_OWNER_GROUP_KILL in difference


def test_a_killed_direct_server_is_not_a_failed_host_quit(profile_pair):
    healthy = _healthy(profile_pair, daemon=False)
    host = dataclasses.replace(
        healthy.host, alive_before_quit=False, exit_code=-9, exited_on_quit=True
    )
    killed = {"actor": "frontend", "exit": "killed", "guardian_owner_group": 0}
    vector, failures = judge_row(dataclasses.replace(healthy, host=host, killed=killed))
    assert vector.host_exit_clean and failures == []
    assert vector.guardian_owner_group == 0
    # Not killed by the harness: the same host is a failed quit.
    _, failures = judge_row(dataclasses.replace(healthy, host=host))
    assert any("already gone" in failure for failure in failures)


def test_the_recovery_is_read_from_the_second_call(profile_pair):
    healthy = _healthy(profile_pair, daemon=True)
    killed = {"actor": "owner", "exit": "killed"}
    for second, recovered in (
        ({"is_error": False, "read_the_post": True}, True),
        ({"is_error": True, "read_the_post": False}, False),
        (None, False),
    ):
        host = dataclasses.replace(healthy.host, second_tool=second)
        vector, _ = judge_row(dataclasses.replace(healthy, host=host, killed=killed))
        assert vector.recovered is recovered


@pytest.fixture
def profile_pair(tmp_path):
    from differential.session import LAST_VERSION_FILE, write_synthetic_cookie_file
    from linkedin_mcp_server.session_state import (
        portable_cookie_path,
        write_source_state,
    )

    directory = tmp_path / "auth" / "profile"
    directory.mkdir(parents=True)
    (directory / LAST_VERSION_FILE).write_text("153.0.8010.12")
    staged = write_synthetic_cookie_file(portable_cookie_path(directory))
    write_source_state(directory)
    return directory, staged


# --- The watcher records groups ------------------------------------------------------


def test_the_watcher_records_a_process_group_and_reports_a_change_of_it():
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = {"start": 2.0, "ppid": 1, "cmdline": ["python", "server"], "pgid": 1}
    (start,) = [e for e in _observe(sampler, tracker, 1.0) if e[1] == "process.start"]
    assert start[2]["pgid"] == 1
    table[2]["pgid"] = 2
    (update,) = [e for e in _observe(sampler, tracker, 2.0) if e[1] == "process.update"]
    assert update[2]["pgid"] == 2


# --- The watcher reads browser markers -------------------------------------------------


def _crashpad_table(environ) -> dict[int, dict[str, Any]]:
    table = _row_table()
    # Adopted by init; in this table pid 1 is the harness, so 50 stands in.
    table[60] = {
        "start": 3.0,
        "ppid": 50,
        "cmdline": ["chrome_crashpad_handler", "--database=/root/.config/x"],
        "exe": f"{BROWSER_DIR}/chromium-1/chrome-linux/chrome_crashpad_handler",
        "environ": environ,
    }
    return table


def test_the_watcher_ties_a_crashpad_handler_to_its_browser_by_marker():
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    marker = {BROWSER_MARKER_ENV: "a" * 64}
    table[2] = {
        "start": 2.0,
        "ppid": 1,
        "cmdline": [BROWSER_EXE, "--user-data-dir=/p"],
        "exe": BROWSER_EXE,
        "environ": marker,
    }
    table.update({60: _crashpad_table(marker)[60]})
    # The records as the watcher writes them, and O2 read from them.
    records = [
        {"t": 1.0, "actor": actor, "kind": kind, **fields}
        for actor, kind, fields in _observe(sampler, tracker, 1.0)
    ]
    handler = next(r for r in records if r.get("pid") == 60)
    assert handler["in_row"] is False and "browser_marker" in handler
    # A digest: the value the guardian acts on is not in the evidence.
    assert "a" * 64 not in str(records)
    history = ProcessHistory(records)
    (browser,) = [life for life in history.lifetimes if life.pid == 2]
    (crashpad,) = [life for life in history.lifetimes if life.pid == 60]
    assert history.descends(crashpad, browser, 1.0) is True


def test_a_marker_is_read_once_per_lifetime_and_again_after_a_failed_read():
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table.update(_crashpad_table(psutil.AccessDenied(60)))
    _observe(sampler, tracker, 1.0)
    assert sampler.sample()[60].browser_marker is None
    table[60]["environ"] = {BROWSER_MARKER_ENV: "b" * 64}
    reads = table[60]["environ_reads"]
    assert sampler.sample()[60].browser_marker is not None
    sampler.sample()
    assert table[60]["environ_reads"] == reads + 1


def test_only_a_possible_browser_started_after_the_baseline_is_asked():
    table = _crashpad_table({BROWSER_MARKER_ENV: "c" * 64})
    # Its parent is not in the table, so its ancestry is never settled and it
    # stays watched: only having run before any actor keeps it from being asked.
    table[60]["ppid"] = 70
    table[61] = {"start": 3.0, "ppid": 1, "cmdline": ["python"], "environ": {}}
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[62] = {"start": 4.0, "ppid": 1, "cmdline": ["node"], "environ": {}}
    _observe(sampler, tracker, 1.0)
    assert "environ_reads" not in table[60]  # running before any actor
    assert "environ_reads" not in table[62]  # cannot be the browser


# --- E1EA-01: what the traced O2 covers --------------------------------------------


def test_the_traced_o2_never_speaks_for_the_row():
    result = _o2("21  6.0 kill(-30, SIGKILL) = 0\n")
    assert (result.state, result.row) == (HELD, UNOBSERVED)
    assert result.scope["syscalls"][-2:] == ["rt_sigqueueinfo", "rt_tgsigqueueinfo"]


def test_a_sender_outside_the_traced_scope_is_not_placed():
    history = ProcessHistory(_timeline(_row()), outside=[HARNESS])
    scoped = _outcome("30  6.0 kill(31, SIGTERM) = 0\n", traced=[21], attached_at=5.0)
    result = derive_o2(scoped, history)
    assert result.state == UNKNOWN and "outside the traced scope" in result.unknowns[0]
    assert result.scope["senders"] == [[21, 3.0]]
    traced = _outcome("21  6.0 kill(-30, SIGKILL) = 0\n", traced=[21], attached_at=5.0)
    assert derive_o2(traced, history).state == HELD


def test_a_row_that_traced_held_still_reads_unobserved(profile_pair):
    held = _o2("21  6.0 kill(-30, SIGKILL) = 0\n")
    vector, failures = judge_row(
        dataclasses.replace(_healthy(profile_pair, daemon=True), o2=held)
    )
    assert (vector.o2, vector.o2_traced) == (UNOBSERVED, HELD)
    assert not any("O2" in failure for failure in failures)


# --- E1EA-02: missing evidence is never an empty trace ------------------------------


class _Strace:
    """The oracle's helper as a double: nothing is started, traced or signalled."""

    def __init__(self, returncode=0, *, hangs=0):
        self.pid = 777777
        self.returncode = returncode
        #: How many waits time out before it exits.
        self.hangs = hangs

    def wait(self, timeout=None):
        if self.hangs:
            self.hangs -= 1
            raise subprocess.TimeoutExpired("strace", timeout or 0.0)
        return self.returncode

    def poll(self):
        return None if self.hangs else self.returncode


_ENDS = "20  9.0 +++ killed by SIGKILL +++\n21  9.5 +++ exited with 0 +++\n"


def _attached(tmp_path, trace, *, process=None, stderr="", run=None):
    oracle = SignalOracle(
        tmp_path,
        required=True,
        run=run or (lambda *a, **k: SimpleNamespace(returncode=0)),
    )
    oracle.unavailable = None
    oracle.pids = [20, 21]
    oracle.attached_at = 5.0
    oracle._process = cast(Any, process or _Strace())
    if trace is not None:
        oracle.out.write_text(trace)
    oracle.err.write_text(stderr)
    return oracle


def test_a_complete_empty_trace_holds(tmp_path):
    outcome = _attached(tmp_path, _ENDS).stop()
    assert (outcome.status, outcome.calls, outcome.reasons) == (COMPLETE, [], [])
    history = ProcessHistory(_timeline(_row()), outside=[HARNESS])
    assert derive_o2(outcome, history).state == HELD


@pytest.mark.parametrize(
    ("trace", "process", "reason"),
    [
        pytest.param(None, None, "could not be read", id="no-trace-file"),
        pytest.param(_ENDS, _Strace(1), "status 1", id="strace-failed"),
        pytest.param(
            _ENDS + "21  6.1 kill(95, SIGTERM <unfinished ...>\n",
            None,
            "never finished",
            id="unfinished-at-the-end",
        ),
        pytest.param(
            _ENDS + "21  6.1 <... kill resumed>) = 0\n",
            None,
            "resumed without its start",
            id="resumed-without-start",
        ),
        pytest.param(
            _ENDS + "21  6.0 kill(95, SIG", None, "not a traced call", id="truncated"
        ),
        pytest.param(
            _ENDS + "strace: something else\n", None, "not a trace line", id="stray"
        ),
        pytest.param(
            "21  9.5 +++ exited with 0 +++\n",
            None,
            "stopped following 20",
            id="a-tracee-without-an-end",
        ),
    ],
)
def test_lost_evidence_makes_the_oracle_incomplete(tmp_path, trace, process, reason):
    outcome = _attached(tmp_path, trace, process=process).stop()
    assert outcome.status == INCOMPLETE
    assert any(reason in line for line in outcome.reasons), outcome.reasons
    history = ProcessHistory(_timeline(_row()), outside=[HARNESS])
    assert derive_o2(outcome, history).state == INCOMPLETE


def test_a_tracee_the_harness_killed_ends_there_unless_strace_let_it_go(tmp_path):
    trace = "21  9.5 +++ exited with 0 +++\n"
    ended = _attached(tmp_path, trace).stop(confirmed_dead=[20])
    assert ended.status == COMPLETE
    lost = _attached(tmp_path, trace, stderr="strace: Process 20 detached\n")
    assert lost.stop(confirmed_dead=[20]).status == INCOMPLETE


def test_a_stop_that_cannot_end_strace_is_incomplete(tmp_path):
    sent: list[list[str]] = []

    def run(command, **kwargs):
        sent.append(command)
        return SimpleNamespace(returncode=1)

    oracle = _attached(tmp_path, _ENDS, process=_Strace(hangs=5), run=run)
    outcome = oracle.stop(seconds=0.01)
    assert outcome.status == INCOMPLETE
    assert any("could not be interrupted" in line for line in outcome.reasons)
    assert any("still running" in line for line in outcome.reasons)
    # Only the oracle's own helper was ever signalled, and bounded.
    assert [command[-2:] for command in sent] == [
        ["-INT", "777777"],
        ["-KILL", "777777"],
    ]


def test_an_interrupt_that_detaches_ends_the_interval_there(tmp_path):
    trace = "21  6.0 kill(-30, SIGKILL) = 0\n"
    outcome = _attached(tmp_path, trace, process=_Strace(hangs=1)).stop(seconds=0.01)
    assert outcome.status == COMPLETE and len(outcome.calls) == 1


def test_an_oracle_that_never_attached_is_incomplete(tmp_path):
    oracle = _attached(tmp_path, None)
    oracle._process = None
    oracle.attach_failure = "strace did not attach to [20, 21]"
    outcome = oracle.stop()
    assert outcome.status == INCOMPLETE and outcome.reasons == [oracle.attach_failure]


@pytest.mark.parametrize("required", [True, False])
def test_incomplete_evidence_fails_the_row_only_where_required(profile_pair, required):
    history = ProcessHistory(_timeline(_row()), outside=[HARNESS])
    outcome = OracleOutcome(
        status=INCOMPLETE, required=required, reasons=["strace exited with status 1"]
    )
    o2 = derive_o2(outcome, history)
    vector, failures = judge_row(
        dataclasses.replace(_healthy(profile_pair, daemon=True), o2=o2)
    )
    assert vector.o2_traced == INCOMPLETE and vector.o2_required is required
    required_line = "the required signal oracle's evidence is incomplete"
    assert any(required_line in failure for failure in failures) is required
    assert any("strace exited" in failure for failure in failures) is required


# --- E1EA-03: a recipient is what the samples pin down ------------------------------


def test_a_pid_reused_between_samples_is_not_the_old_process():
    # The browser at 30 exits at 6.01; an unrelated process takes pid 30 at
    # 6.02; the next sample, at 6.05, sees the new one.
    reused = [
        *_row(),
        _exit(30, 6.05, 4.0),
        _start(30, 7, 6.05, start=6.02, pgid=30, in_row=False),
    ]
    assert _o2("21  6.03 kill(30, SIGTERM) = 0\n", reused).state == UNKNOWN
    assert _o2("21  6.06 kill(30, SIGTERM) = 0\n", reused).state == VIOLATED


@pytest.mark.parametrize(
    ("last_pid", "state"),
    [
        pytest.param(lambda end: 100000, UNKNOWN, id="cursor-past-the-pid"),
        pytest.param(
            lambda end: 20 if end < 6.02 else 40, UNKNOWN, id="pid-reallocated"
        ),
        pytest.param(
            lambda end: 100000 if end < 6.02 else 5, UNKNOWN, id="allocation-wrapped"
        ),
    ],
)
def test_a_target_that_exited_between_the_samples(last_pid, state):
    # Seen before the send, gone in the sample after it: not pinned, whatever
    # the kernel's allocation cursor read (it is a diagnostic only).
    records = [*_row(), _exit(30, 6.25, 4.0)]
    history = ProcessHistory(_timeline(records, last_pid=last_pid), outside=[HARNESS])
    result = derive_o2(_outcome("21  6.1 kill(30, SIGKILL) = 0\n"), history)
    assert result.state == state, result


def test_a_member_that_joined_between_the_samples_leaves_the_group_unknown():
    records = [*_row(), _start(92, 30, 6.25, pgid=30)]
    result = _o2("21  6.1 kill(-30, SIGKILL) = 0\n", records)
    assert result.state == UNKNOWN and "one sample only" in result.unknowns[0]


@pytest.mark.parametrize(
    ("failures", "state"),
    [
        pytest.param(["open: AccessDenied"], UNKNOWN, id="unopenable"),
        pytest.param(["identity: AccessDenied"], UNKNOWN, id="unidentified"),
        # As ``sudo -n strace`` on every native Linux row: its executable is
        # root's to read, its group is not hidden.
        pytest.param(["exe: AccessDenied"], HELD, id="executable-only"),
    ],
)
def test_a_process_that_could_not_be_identified_leaves_the_group_unknown(
    failures, state
):
    failure = {"pid": 93, "first": 5.9, "last": 6.3, "failures": failures}
    history = ProcessHistory(_timeline(_row(), failures=[failure]), outside=[HARNESS])
    result = derive_o2(_outcome("21  6.1 kill(-30, SIGKILL) = 0\n"), history)
    assert result.state == state, result


def test_a_member_whose_group_was_not_read_leaves_the_group_unknown():
    records = [*_row(), {**_start(94, 30, 5.0), "pgid": None}]
    result = _o2("21  6.1 kill(-30, SIGKILL) = 0\n", records)
    assert result.state == UNKNOWN and "group was not read" in result.unknowns[0]


def test_a_send_after_the_last_sample_is_unknown():
    assert _o2("21  99.0 kill(30, SIGKILL) = 0\n").state == UNKNOWN


# --- E1EA-04: pidfd scope ----------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "state", "targets"),
    [
        pytest.param("0", HELD, [[30, 4.0]], id="the-process"),
        pytest.param("PIDFD_SIGNAL_THREAD_GROUP", HELD, [[30, 4.0]], id="thread-group"),
        pytest.param(
            "PIDFD_SIGNAL_PROCESS_GROUP",
            VIOLATED,
            [[30, 4.0], [31, 4.0], [91, 4.5]],
            id="process-group",
        ),
        pytest.param(
            "0x4", VIOLATED, [[30, 4.0], [31, 4.0], [91, 4.5]], id="numeric-group"
        ),
        pytest.param("PIDFD_SIGNAL_THREAD", UNKNOWN, None, id="one-thread"),
        pytest.param("PIDFD_SIGNAL_THREAD|0x4", UNKNOWN, None, id="combined"),
        pytest.param("PIDFD_SOMETHING_NEW", UNKNOWN, None, id="unknown-flag"),
    ],
)
def test_a_pidfd_signal_reaches_the_scope_its_flags_name(flags, state, targets):
    # An outside process (91) is in Chromium's group.
    records = [*_row(), _start(91, HARNESS, 4.5, pgid=30)]
    line = f"21  6.0 pidfd_send_signal(5<pid:30>, SIGTERM, NULL, {flags}) = 0\n"
    result = _o2(line, records)
    assert result.state == state, result
    if targets is not None:
        assert result.resolved[0]["targets"] == targets


def test_a_queued_signal_is_a_signal():
    line = (
        "21  6.0 rt_sigqueueinfo(95, SIGUSR1, {si_signo=SIGUSR1, "
        "si_code=SI_QUEUE, si_pid=21, si_uid=1000, si_int=7}) = 0\n"
    )
    (call,) = parse_strace(line)
    assert (call.target_pid, call.signal) == (95, "SIGUSR1")
    assert _o2(line).state == VIOLATED


# --- E1EA-07: a marker read later reaches O2, from then on --------------------------


def test_a_marker_read_on_retry_is_published_and_used_from_then_on():
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    records = []

    def observe(t):
        records.extend(
            {"t": t, "actor": actor, "kind": kind, **fields}
            for actor, kind, fields in _observe(sampler, tracker, t)
        )

    observe(0.0)
    marker = {BROWSER_MARKER_ENV: "z" * 64}
    table[2] = {
        "start": 2.0,
        "ppid": 1,
        "cmdline": [BROWSER_EXE, "--user-data-dir=/p"],
        "exe": BROWSER_EXE,
        "environ": marker,
    }
    table[60] = {**_crashpad_table(psutil.AccessDenied(60))[60]}
    observe(1.0)
    table[60]["environ"] = marker
    observe(2.0)
    (update,) = [r for r in records if r["kind"] == "process.update"]
    assert update["pid"] == 60 and update["t"] == 2.0 and update["browser_marker"]
    history = ProcessHistory(records)
    (browser,) = [life for life in history.lifetimes if life.pid == 2]
    (crashpad,) = [life for life in history.lifetimes if life.pid == 60]
    assert history.descends(crashpad, browser, 2.5) is True
    # Before the watcher had it, the marker is no evidence.
    assert history.descends(crashpad, browser, 1.5) is False


# --- E1EA-08: attempts, deliveries and deaths --------------------------------------


@pytest.mark.parametrize("error", ["EPERM (Operation not permitted)", "EINVAL (x)"])
def test_a_refused_call_is_an_attempt_not_a_delivery(error):
    result = _o2(f"21  6.0 kill(95, SIGKILL) = -1 {error}\n")
    assert result.state == VIOLATED
    assert result.violations[0].startswith("attempted (refused: -1 E")
    assert result.resolved[0]["outcome"] == "rejected"


def test_a_nonfatal_delivery_is_a_delivered_signal():
    result = _o2("21  6.0 kill(95, SIGTERM) = 0\n")
    assert result.violations[0].startswith("delivered SIGTERM to")
    assert result.canary_deaths == []


# --- E1EA-09: canaries that fail to start ------------------------------------------


class _Child:
    def __init__(self, pid):
        self.pid = pid
        self.killed = False

    def poll(self):
        return -9 if self.killed else None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.poll()


def _spawns(monkeypatch, *, fail_at: int | None = None):
    children: list[_Child] = []

    def popen(*args, **kwargs):
        if fail_at is not None and len(children) == fail_at:
            raise OSError("modelled spawn failure")
        children.append(_Child(900000 + len(children)))
        return children[-1]

    monkeypatch.setattr(signals.subprocess, "Popen", popen)
    monkeypatch.setattr(signals.os, "getsid", lambda pid: pid, raising=False)
    monkeypatch.setattr(signals.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(signals, "_in_any_job", lambda pid: None)
    return children


def test_a_failed_second_spawn_ends_the_first_canary(monkeypatch):
    children = _spawns(monkeypatch, fail_at=1)
    monkeypatch.setattr(
        psutil, "Process", lambda pid: SimpleNamespace(create_time=lambda: 1.0)
    )
    canaries = Canaries(count=2)
    with pytest.raises(OSError, match="modelled spawn failure"):
        canaries.start()
    assert [child.killed for child in children] == [True]
    assert canaries.canaries == []


def test_a_canary_whose_identity_cannot_be_read_is_ended(monkeypatch):
    children = _spawns(monkeypatch)

    def unreadable(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", unreadable)
    canaries = Canaries(count=2)
    with pytest.raises(psutil.NoSuchProcess):
        canaries.start()
    assert [child.killed for child in children] == [True]


# --- E1EB-01: only the samples on both sides pin a recipient; the call names --------


def test_a_hidden_allocator_wrap_does_not_pin_the_old_browser():
    # e1eb's countermodel: the cursor goes 1000 -> 1001, yet the allocator
    # wrapped past occupied pids and gave 500 to an unrelated process, which
    # received the signal and was gone before the next sample.
    rows = []
    for record in _row():
        record = dict(record)
        for key in ("pid", "ppid", "pgid"):
            if record.get(key) == 30:
                record[key] = 500
        rows.append(record)
    rows += [_exit(500, 6.25, 4.0), _exit(31, 6.25, 4.0)]
    history = ProcessHistory(
        _timeline(rows, last_pid=lambda end: 1000 if end <= 6.0 else 1001),
        outside=[HARNESS],
    )
    result = derive_o2(_outcome("21  6.1 kill(500, SIGKILL) = 0\n"), history)
    assert result.state == UNKNOWN, result
    # What the call named is still read: the guardian aimed at a browser pid.
    assert result.classes == ("guardian:browser",)
    assert result.resolved[0]["targets"] is None


def test_a_group_whose_members_died_before_the_next_sample_is_unknown():
    records = [*_row(), _exit(30, 6.25, 4.0), _exit(31, 6.25, 4.0)]
    result = _o2("21  6.1 kill(-30, SIGKILL) = 0\n", records)
    assert result.state == UNKNOWN and "one sample only" in result.unknowns[0]
    assert result.classes == ("guardian:browser-group",)


def test_the_pre_path_a_class_is_read_from_the_call_not_its_recipients():
    # The owner (20) and its driver died before the next sample: nobody the
    # kill reached is pinned, yet the call named the principal's group.
    records = [*_row(), _exit(20, 6.25, 2.0), _exit(22, 6.25, 3.0)]
    result = _o2("21  6.1 kill(-20, SIGKILL) = 0\n", records)
    assert result.state == UNKNOWN
    assert result.classes == (GUARDIAN_OWNER_GROUP_KILL,)


def test_a_group_no_row_browser_was_recorded_in_is_another_group():
    result = _o2("21  6.0 kill(-95, SIGTERM) = 0\n")
    assert result.classes == ("guardian:other-group",)
    assert classes_direct_would_not_send(result.classes) == ["guardian:other-group"]


def test_a_recipient_seen_on_both_sides_is_pinned():
    assert _o2("21  6.1 kill(30, SIGTERM) = 0\n").state == HELD
    assert _o2("21  6.1 kill(95, SIGTERM) = 0\n").state == VIOLATED


def test_unknown_is_recorded_not_failed(profile_pair):
    records = [*_row(), _exit(30, 6.25, 4.0), _exit(31, 6.25, 4.0)]
    unknown = _o2("21  6.1 kill(-30, SIGKILL) = 0\n", records)
    vector, failures = judge_row(
        dataclasses.replace(_healthy(profile_pair, daemon=True), o2=unknown)
    )
    assert vector.o2_traced == UNKNOWN and not any("O2" in f for f in failures)
    # Against a Direct reference that happened to pin its recipients.
    held = dataclasses.replace(vector, mode="direct", o2_traced=HELD)
    assert compare_to_direct(held, vector) == []
    violated = dataclasses.replace(held, o2_traced=VIOLATED)
    assert compare_to_direct(violated, vector)


# --- E1EB-02: a group that could not be read is not the last one read ---------------


def _cached_process_joins(reading):
    """e1eb's model: 50 is settled unrelated at the first sample, in group 50;
    the row's owner, guardian, driver and browser start; then the group of 50
    is read as *reading* while the guardian kills group 30."""
    table = {
        **_row_table(),
        50: {"start": 1.0, "ppid": 0, "cmdline": ["svc"], "exe": "/usr/bin/svc"},
    }
    table[50]["pgid"] = 50
    sampler, tracker = _sampler(table, root=1), Tracker()
    records: list[dict[str, Any]] = []

    def observe(t):
        records.extend(
            {"t": t, "actor": actor, "kind": kind, **fields}
            for actor, kind, fields in _observe(sampler, tracker, t)
        )

    observe(5.5)
    table.update(
        {
            20: {
                "start": 6.0,
                "ppid": 1,
                "cmdline": ["python", "-m", "owner"],
                "pgid": 20,
            },
            21: {
                "start": 6.1,
                "ppid": 20,
                "cmdline": ["python", "/x/process_guardian.py", "5", "6", "0"],
                "pgid": 21,
            },
            30: {
                "start": 6.2,
                "ppid": 20,
                "cmdline": [BROWSER_EXE, "--user-data-dir=/p"],
                "exe": BROWSER_EXE,
                "pgid": 30,
            },
        }
    )
    observe(7.0)
    observe(8.0)
    table[50]["pgid"] = reading
    observe(8.25)
    history = ProcessHistory(
        _timeline(records, baseline=sampler.baseline_pgids), outside=[1]
    )
    result = derive_o2(_outcome("21  8.1 kill(-30, SIGUSR1) = 0\n"), history)
    return result, records, sampler


def test_an_unread_group_of_a_settled_process_leaves_the_group_unknown():
    result, records, sampler = _cached_process_joins(PermissionError("denied"))
    (update,) = [r for r in records if r["kind"] == "process.update"]
    assert (update["pid"], update["pgid"]) == (50, None)
    assert update["pgid_error"] == "unread: PermissionError"
    assert sampler.stats()["group_read_failure_count"] == 1
    assert result.state == UNKNOWN and "not read" in result.unknowns[0]


def test_a_settled_process_seen_joining_leaves_the_group_unknown():
    result, _, _ = _cached_process_joins(30)
    assert result.state == UNKNOWN and "one sample only" in result.unknowns[0]


def test_a_settled_process_found_gone_is_an_exit_not_a_group():
    result, records, sampler = _cached_process_joins(ProcessLookupError())
    assert [r["kind"] for r in records if r.get("pid") == 50] == ["process.exit"]
    assert sampler.stats()["group_read_failure_count"] == 0
    assert result.state == HELD, result


# --- E1EB-03: K2's Linux witness needs the complete required oracle -----------------


#: The baseline guardian's group kill, and a signal outside its launched set.
_WITNESS_AND_OUTSIDER = (
    "21  6.0 kill(-20, SIGKILL) = 0\n21  6.1 kill(95, SIGTERM) = 0\n"
)


def _k2(profile_pair, outcome):
    """A K2 row as its native test judges it: ``r6_verdict`` alone."""
    history = ProcessHistory(_timeline(_row()), outside=[HARNESS])
    o2 = derive_o2(outcome, history)
    healthy = _healthy(profile_pair, daemon=True)
    killed = {"actor": "owner", "exit": "killed", "guardian_owner_group": 20}
    vector, failures = judge_row(dataclasses.replace(healthy, o2=o2, killed=killed))
    return RowResult("K2", "daemon", vector=vector, failures=failures, killed=killed)


@pytest.mark.parametrize(
    ("outcome", "linux", "passes"),
    [
        pytest.param(
            OracleOutcome(status=INCOMPLETE, required=True, reasons=["no attach"]),
            True,
            False,
            id="linux-required-attach-failed",
        ),
        pytest.param(
            OracleOutcome(
                status=INCOMPLETE,
                required=True,
                reasons=["strace let 23 go before it ended"],
                calls=parse_strace("21  6.0 kill(-20, SIGKILL) = 0\n"),
            ),
            True,
            False,
            id="linux-incomplete-collection",
        ),
        pytest.param(
            OracleOutcome(
                status=COMPLETE,
                required=True,
                calls=parse_strace("21  6.0 kill(-20, SIGKILL) = 0\n"),
            ),
            True,
            True,
            id="linux-complete-witness",
        ),
        pytest.param(
            OracleOutcome(status=COMPLETE, required=True),
            True,
            False,
            id="linux-complete-without-the-class",
        ),
        pytest.param(
            OracleOutcome(
                status=INCOMPLETE,
                required=True,
                reasons=["strace let 23 go before it ended"],
                calls=parse_strace(_WITNESS_AND_OUTSIDER),
            ),
            True,
            False,
            id="linux-incomplete-plus-violation",
        ),
        pytest.param(
            OracleOutcome(
                status=COMPLETE,
                required=True,
                calls=parse_strace(_WITNESS_AND_OUTSIDER),
            ),
            True,
            True,
            id="linux-complete-with-violation-is-baseline-evidence",
        ),
        pytest.param(
            OracleOutcome(status=UNAVAILABLE, reasons=["no strace on darwin"]),
            False,
            True,
            id="macos-argv-only",
        ),
    ],
)
def test_the_k2_acceptance_on_each_platform(profile_pair, outcome, linux, passes):
    result = _k2(profile_pair, outcome)
    # The native K2 test's own expression.
    problems = r6_verdict(result, experiment="K2", windows=False, linux=linux)
    assert (problems == []) is passes, problems


# --- E1EB-04: the whole traced cohort is accounted for ------------------------------


def test_a_followed_child_strace_let_go_is_incomplete(tmp_path):
    oracle = _attached(
        tmp_path,
        _ENDS,
        stderr="strace: Process 23 attached\nstrace: Process 23 detached\n",
    )
    outcome = oracle.stop()
    assert outcome.status == INCOMPLETE
    assert any("let 23 go" in line for line in outcome.reasons)


def test_a_root_strace_let_go_is_incomplete_even_if_it_was_killed(tmp_path):
    trace = "21  9.5 +++ exited with 0 +++\n"
    oracle = _attached(tmp_path, trace, stderr="strace: Process 20 detached\n")
    outcome = oracle.stop(confirmed_dead=[20])
    assert outcome.status == INCOMPLETE and outcome.cohort[20]["end"] is None


_CHILD = "21  6.0 clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|SIGCHLD) = 23\n"
_THREAD = (
    "20  6.0 clone3({flags=CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD"
    "|CLONE_SYSVSEM, child_tid=0x1, stack=0x2, stack_size=0x3} => "
    "{parent_tid=[24]}, 88) = 24\n"
)


@pytest.mark.parametrize(
    ("trace", "status", "tid", "end"),
    [
        pytest.param(
            _CHILD + "23  6.5 +++ exited with 0 +++\n" + _ENDS,
            COMPLETE,
            23,
            "its exit line",
            id="followed-child-that-exited",
        ),
        pytest.param(_CHILD + _ENDS, INCOMPLETE, 23, None, id="followed-child-no-end"),
        pytest.param(
            _THREAD + _ENDS, COMPLETE, 24, "its process ended", id="thread-of-a-root"
        ),
        pytest.param(
            _THREAD + "21  9.5 +++ exited with 0 +++\n",
            INCOMPLETE,
            24,
            None,
            id="thread-of-a-root-that-never-ended",
        ),
        pytest.param(
            _ENDS + "99  6.0 kill(95, SIGTERM) = 0\n",
            INCOMPLETE,
            99,
            None,
            id="a-tracee-nothing-accounts-for",
        ),
    ],
)
def test_every_tracee_of_the_cohort_needs_its_end(tmp_path, trace, status, tid, end):
    outcome = _attached(tmp_path, trace).stop()
    assert outcome.status == status, outcome.reasons
    assert outcome.cohort[tid]["end"] == end


def test_a_thread_the_trace_saw_born_speaks_for_its_process(tmp_path):
    trace = _THREAD + "24  6.1 kill(-30, SIGKILL) = 0\n" + _ENDS
    outcome = _attached(tmp_path, trace).stop()
    assert outcome.threads[24] == 20
    history = ProcessHistory(_timeline(_row()), outside=[HARNESS])
    assert derive_o2(outcome, history).classes == ("owner:browser-group",)


def test_a_deliberate_stop_is_each_remaining_tracees_boundary(tmp_path):
    def run(command, **kwargs):
        # The interrupt detaches what is left; strace reports it as it goes.
        with oracle.err.open("a") as err:
            err.write("strace: Process 21 detached\n")
        return SimpleNamespace(returncode=0)

    trace = "20  9.0 +++ killed by SIGKILL +++\n"
    oracle = _attached(tmp_path, trace, process=_Strace(hangs=1), run=run)
    outcome = oracle.stop(seconds=0.01)
    assert outcome.status == COMPLETE, outcome.reasons
    assert outcome.cohort[21]["end"].startswith("detached at the stop")
    assert outcome.cohort[20]["end"] == "its exit line"


def test_a_violation_does_not_hide_an_incomplete_collection(profile_pair):
    outcome = OracleOutcome(
        status=INCOMPLETE,
        required=True,
        reasons=["strace let 23 go before it ended"],
        calls=parse_strace(_WITNESS_AND_OUTSIDER),
    )
    result = _k2(profile_pair, outcome)
    vector = result.vector
    assert vector is not None
    assert (vector.o2_traced, vector.oracle_collection) == (VIOLATED, INCOMPLETE)
    # Both are on record: the violation, and the collection the row requires.
    assert any("outside the launched set" in f for f in result.failures)
    required = "the required signal oracle's evidence is incomplete"
    assert any(required in f for f in result.failures)


# --- E1EC-02: a tracee is a lifetime, and the trace's order is not the parent's ------

#: e1ec's model: tid 24 was a thread of the owner (20) at the attach, ended,
#: and the guardian (21) then started a process that got 24 again.
_REUSED = (
    "24  5.1 +++ exited with 0 +++\n"
    "21  5.5 clone(child_stack=NULL, flags=SIGCHLD) = 24\n"
    "24  6.0 kill(30, SIGTERM) = 0\n"
    "24  6.5 +++ exited with 0 +++\n"
)


@pytest.mark.parametrize(
    "trace",
    [
        pytest.param(_REUSED + _ENDS, id="every-end-present"),
        pytest.param(
            _REUSED.replace("24  6.5 +++ exited with 0 +++\n", "") + _ENDS,
            id="the-new-lifetime-without-its-end",
        ),
    ],
)
def test_a_reused_tid_leaves_the_collection_incomplete(tmp_path, trace):
    oracle = _attached(tmp_path, trace)
    oracle.threads = {24: 20}
    outcome = oracle.stop()
    assert outcome.status == INCOMPLETE
    assert any("reused id" in reason for reason in outcome.reasons)
    # The stale association is dropped, not applied to the new process.
    assert 24 not in outcome.threads
    records = [*_row(), _start(24, 21, 5.6, start=5.5, pgid=24)]
    history = ProcessHistory(_timeline(records), outside=[HARNESS])
    result = derive_o2(outcome, history)
    assert result.state != HELD
    assert result.resolved[0]["sender"] == [24, 5.5]


def test_a_tid_that_writes_after_its_end_is_reused(tmp_path):
    trace = "23  6.0 +++ exited with 0 +++\n23  6.1 kill(30, SIGTERM) = 0\n" + _ENDS
    outcome = _attached(tmp_path, trace).stop()
    assert outcome.status == INCOMPLETE and outcome.cohort[23]["reused"]


_PARENT_FIRST = (
    "20  5.5 clone(child_stack=NULL, flags=SIGCHLD) = 23\n"
    "23  5.6 clone(child_stack=0x1, flags=CLONE_VM|CLONE_SIGHAND|CLONE_THREAD) = 24\n"
)
#: The child starts its thread before its parent's clone has returned.
_CHILD_FIRST = (
    "20  5.5 clone(child_stack=NULL, flags=SIGCHLD <unfinished ...>\n"
    "23  5.6 clone(child_stack=0x1, flags=CLONE_VM|CLONE_SIGHAND|CLONE_THREAD) = 24\n"
    "20  5.7 <... clone resumed>) = 23\n"
)


@pytest.mark.parametrize(
    "births",
    [
        pytest.param(_PARENT_FIRST, id="parent-first"),
        pytest.param(_CHILD_FIRST, id="child-first"),
    ],
)
def test_a_thread_is_placed_whatever_order_the_returns_came_in(tmp_path, births):
    trace = (
        births
        + "24  6.0 kill(25, SIGTERM) = 0\n"
        + "24  6.2 +++ exited with 0 +++\n"
        + "23  6.5 +++ exited with 0 +++\n"
        + _ENDS
    )
    outcome = _attached(tmp_path, trace).stop()
    assert outcome.status == COMPLETE, outcome.reasons
    assert outcome.threads[24] == 23
    assert outcome.cohort[23]["kind"] == "child"
    records = [
        *_row(),
        _start(23, 20, 5.6, start=5.5, pgid=23),
        _start(25, 23, 5.9, start=5.8, pgid=25),
    ]
    history = ProcessHistory(_timeline(records), outside=[HARNESS])
    result = derive_o2(outcome, history)
    assert result.resolved[0]["sender"] == [23, 5.5]
    assert result.state == HELD, result


# --- E1EC-03: a class is read from evidence at or before the send -------------------

#: e1ec's model: 40, a driver in group 40 when the guardian signals that group,
#: is only later seen as a browser.
_DRIVER_40 = _start(40, 22, 4.5, start=4.4, actor="driver", pgid=40)


@pytest.mark.parametrize(
    ("later", "target", "named", "after_exec"),
    [
        pytest.param(40, "-40", "other-group", "browser-group", id="same-group"),
        # It execs into a browser in another group: its old group never was a
        # browser's, and its new one was not one yet at the send.
        pytest.param(41, "-40", "other-group", "other-group", id="exec-regroup-old"),
        pytest.param(41, "-41", "other-group", "browser-group", id="exec-regroup-new"),
        pytest.param(40, "40", "other", "browser", id="pid"),
    ],
)
def test_a_later_observation_does_not_change_an_earlier_class(
    later, target, named, after_exec
):
    line = f"21  6.1 kill({target}, SIGCONT) = 0\n"
    before = _o2(line, [*_row(), _DRIVER_40])
    future = {
        **_start(40, 22, 7.0, start=4.4, actor="browser", pgid=later),
        "kind": "process.update",
    }
    after = _o2(line, [*_row(), _DRIVER_40, future])
    assert before.classes == after.classes == (f"guardian:{named}",)
    # The same call once the exec was on record names what it had become.
    late = _o2(line.replace(" 6.1 ", " 7.5 "), [*_row(), _DRIVER_40, future])
    assert late.classes == (f"guardian:{after_exec}",)


# --- E1EE-01: only what the trace can place confirms a birth -----------------------

_AMBIGUOUS_END = "cannot be ordered unambiguously against its creation call"


@pytest.mark.parametrize(
    ("trace", "stderr", "reason"),
    [
        pytest.param(
            "21  6.0 vfork( <unfinished ...>\n"
            "23  6.1 +++ exited with 0 +++\n"
            "21  6.2 <... vfork resumed>) = 23\n",
            "",
            _AMBIGUOUS_END,
            id="vfork-child-ends-before-the-return",
        ),
        pytest.param(
            "21  6.0 clone(child_stack=NULL, flags=CLONE_VM|CLONE_VFORK|SIGCHLD"
            " <unfinished ...>\n"
            "23  6.1 +++ exited with 0 +++\n"
            "21  6.2 <... clone resumed>) = 23\n",
            "strace: Process 23 attached\n",
            _AMBIGUOUS_END,
            id="clone-vfork-announced-once",
        ),
        # e1ee's model: an earlier 23 ends inside the call and the new child's
        # end is missing; strace announced 23 twice.
        pytest.param(
            "21  6.0 clone(child_stack=NULL, flags=SIGCHLD <unfinished ...>\n"
            "23  6.1 +++ exited with 0 +++\n"
            "21  6.2 <... clone resumed>) = 23\n",
            "strace: Process 23 attached\nstrace: Process 23 attached\n",
            _AMBIGUOUS_END,
            id="old-exit-inside-the-call-new-end-missing",
        ),
        pytest.param(
            "21  6.0 vfork() = 23\n23  6.1 +++ exited with 0 +++\n",
            "strace: Process 23 attached\nstrace: Process 23 attached\n",
            "announced attached 2 times",
            id="announced-twice",
        ),
        pytest.param(
            "23  5.0 +++ exited with 0 +++\n"
            "21  6.0 vfork( <unfinished ...>\n"
            "21  6.2 <... vfork resumed>) = 23\n",
            "",
            "before its creation call began",
            id="ended-before-the-call-began",
        ),
    ],
)
def test_what_the_trace_cannot_place_leaves_the_collection_incomplete(
    tmp_path, trace, stderr, reason
):
    outcome = _attached(tmp_path, trace + _ENDS, stderr=stderr).stop()
    assert outcome.status == INCOMPLETE
    assert any(reason in line for line in outcome.reasons), outcome.reasons
    assert outcome.cohort[23]["reused"]


@pytest.mark.parametrize(
    "trace",
    [
        pytest.param(
            "21  6.0 clone(child_stack=NULL, flags=SIGCHLD <unfinished ...>\n"
            "23  6.1 kill(23, 0) = 0\n"
            "21  6.2 <... clone resumed>) = 23\n"
            "23  6.5 +++ exited with 0 +++\n",
            id="writes-in-the-call-ends-after-the-return",
        ),
        pytest.param(
            "21  6.0 vfork() = 23\n23  6.1 +++ exited with 0 +++\n",
            id="return-before-end",
        ),
    ],
)
def test_a_child_the_trace_places_is_one_lifetime(tmp_path, trace):
    outcome = _attached(
        tmp_path, trace + _ENDS, stderr="strace: Process 23 attached\n"
    ).stop()
    assert outcome.status == COMPLETE, outcome.reasons
    assert outcome.cohort[23]["kind"] == "child"
    assert outcome.cohort[23]["lifetimes"] == 1
    assert outcome.cohort[23]["end"] == "its exit line"


_THREAD_24 = (
    "20  5.1 clone(child_stack=0x1, flags=CLONE_VM|CLONE_SIGHAND|CLONE_THREAD) = 24\n"
    "24  8.0 +++ exited with 0 +++\n"
)
#: The same unsplit birth, bounded by another line at 5.15 before the census.
_THREAD_24_BOUNDED = (
    "20  5.1 clone(child_stack=0x1, flags=CLONE_VM|CLONE_SIGHAND|CLONE_THREAD)"
    " = 24 <0.000050>\n"
    "21  5.15 kill(21, 0) = 0 <0.000004>\n"
    "24  8.0 +++ exited with 0 +++\n"
)
#: A thread-creation call that runs from 6.0 to 6.2, returning 24.
_THREAD_24_CALL = (
    "20  6.0 clone(child_stack=0x1, flags=CLONE_VM|CLONE_SIGHAND|CLONE_THREAD"
    " <unfinished ...>\n"
    "20  6.2 <... clone resumed>) = 24\n"
    "24  8.0 +++ exited with 0 +++\n"
)


@pytest.mark.parametrize(
    ("trace", "census", "times", "status", "reason"),
    [
        pytest.param(_THREAD_24, {}, {}, COMPLETE, None, id="trace-only"),
        pytest.param(
            "24  8.0 +++ exited with 0 +++\n",
            {24: 20},
            {24: (5.2, 5.2)},
            COMPLETE,
            None,
            id="census-only",
        ),
        # Born after the attach, read after its creation call returned.
        pytest.param(
            _THREAD_24_BOUNDED,
            {24: 20},
            {24: (5.2, 5.3)},
            COMPLETE,
            None,
            id="read-after",
        ),
        pytest.param(
            _THREAD_24_CALL,
            {24: 20},
            {24: (6.1, 6.1)},
            INCOMPLETE,
            "before the available return bound",
            id="read-within-the-call",
        ),
        pytest.param(
            _THREAD_24_CALL,
            {24: 20},
            {24: (5.8, 5.9)},
            INCOMPLETE,
            "before its creation call began",
            id="read-before-the-call",
        ),
        # e1ee's model: the listing began before the call and finished after
        # it; the old 24 ended inside the call; the new end is missing.
        pytest.param(
            "20  6.0 clone(child_stack=0x1, flags=CLONE_VM|CLONE_SIGHAND|CLONE_THREAD"
            " <unfinished ...>\n"
            "24  6.1 +++ exited with 0 +++\n"
            "20  6.2 <... clone resumed>) = 24\n",
            {24: 20},
            {24: (5.9, 6.3)},
            INCOMPLETE,
            _AMBIGUOUS_END,
            id="listing-spans-the-call",
        ),
        pytest.param(
            _THREAD_24_BOUNDED,
            {24: 21},
            {24: (5.2, 5.3)},
            INCOMPLETE,
            "the census had it in 21",
            id="census-names-another-process",
        ),
        pytest.param(
            "20  5.1 clone(child_stack=NULL, flags=SIGCHLD) = 24\n"
            "24  8.0 +++ exited with 0 +++\n",
            {24: 20},
            {24: (5.2, 5.3)},
            INCOMPLETE,
            "a process born",
            id="census-thread-born-a-process",
        ),
        pytest.param(
            _THREAD_24, {24: 20}, {}, INCOMPLETE, "time is unknown", id="untimed"
        ),
        pytest.param(
            "24  5.1 +++ exited with 0 +++\n"
            "21  5.5 clone(child_stack=NULL, flags=SIGCHLD) = 24\n"
            "24  6.5 +++ exited with 0 +++\n",
            {24: 20},
            {24: (5.0, 5.0)},
            INCOMPLETE,
            "reused id",
            id="actual-reuse",
        ),
    ],
)
def test_the_thread_census_is_placed_by_when_each_entry_was_read(
    tmp_path, trace, census, times, status, reason
):
    oracle = _attached(tmp_path, trace + _ENDS)
    oracle.threads = dict(census)
    oracle.census_times = dict(times)
    outcome = oracle.stop()
    assert outcome.status == status, outcome.reasons
    if reason is None:
        assert outcome.cohort[24]["lifetimes"] == 1
        assert outcome.threads[24] == 20
    else:
        assert any(reason in line for line in outcome.reasons), outcome.reasons
        assert 24 not in outcome.threads


def test_each_census_entry_keeps_when_its_task_directory_was_read(
    tmp_path, monkeypatch
):
    oracle = SignalOracle(tmp_path)
    oracle.pids = [20, 21]
    clock = iter([1.0, 2.0, 3.0, 4.0])
    monkeypatch.setattr(oracle, "_now", lambda: next(clock))
    monkeypatch.setattr(
        oracle, "_tasks", lambda pid: {20: ["20", "24"], 21: ["21"]}[pid]
    )
    oracle.read_threads()
    assert oracle.threads == {20: 20, 24: 20, 21: 21}
    assert oracle.census_times == {20: (1.0, 2.0), 24: (1.0, 2.0), 21: (3.0, 4.0)}


# --- E1EF-01: a creation call's return is bounded only by what strace wrote after --

_CLONE_24 = "20  6.0 clone(child_stack=0x1, flags=CLONE_VM|CLONE_SIGHAND|CLONE_THREAD"


@pytest.mark.parametrize(
    ("trace", "times", "status", "reason"),
    [
        # e1ef's models. Unsplit: its prefix is its entry; the next line, at
        # 8.0, is all that bounds its return.
        pytest.param(
            _CLONE_24 + ") = 24\n24  8.0 +++ exited with 0 +++\n",
            {24: (6.10, 6.15)},
            INCOMPLETE,
            "before the available return bound",
            id="unsplit-census-possibly-mid-call",
        ),
        pytest.param(
            _CLONE_24 + " <unfinished ...>\n"
            "21  6.05 kill(21, 0) = 0\n"
            "20  6.2 <... clone resumed>) = 24\n"
            "24  8.0 +++ exited with 0 +++\n",
            {24: (6.10, 6.15)},
            INCOMPLETE,
            "before the available return bound",
            id="resumed-return-after-the-census",
        ),
        pytest.param(
            _CLONE_24 + " <unfinished ...>\n"
            "21  6.05 kill(21, 0) = 0\n"
            "20  6.2 <... clone resumed>) = 24\n"
            "24  8.0 +++ exited with 0 +++\n",
            {24: (6.3, 6.4)},
            COMPLETE,
            None,
            id="census-after-the-resumed-return",
        ),
        pytest.param(
            _CLONE_24 + ") = 24\n24  8.0 +++ exited with 0 +++\n",
            {},
            COMPLETE,
            None,
            id="unsplit-without-a-census",
        ),
        pytest.param(
            _CLONE_24 + ") = 24\n24  8.0 +++ exited with 0 +++\n",
            {24: (5.9, 5.9)},
            INCOMPLETE,
            "before its creation call began",
            id="census-before-entry",
        ),
        # strace -T: entry plus the call's time is no bound (its source takes
        # the -T start after printing the entry prefix); the next line is.
        pytest.param(
            _CLONE_24 + ") = 24 <0.000100>\n"
            "21  6.5 kill(21, 0) = 0 <0.000003>\n"
            "24  8.0 +++ exited with 0 +++\n",
            {24: (6.2, 6.3)},
            INCOMPLETE,
            "before the available return bound",
            id="census-after-entry-plus-duration-before-the-next-line",
        ),
        pytest.param(
            _CLONE_24 + ") = 24 <0.000100>\n"
            "21  6.5 kill(21, 0) = 0 <0.000003>\n"
            "24  8.0 +++ exited with 0 +++\n",
            {24: (6.6, 6.7)},
            COMPLETE,
            None,
            id="census-after-the-next-line",
        ),
    ],
)
def test_a_census_is_placed_only_after_a_bounded_return(
    tmp_path, trace, times, status, reason
):
    oracle = _attached(tmp_path, trace + _ENDS)
    oracle.threads = {tid: 20 for tid in times}
    oracle.census_times = dict(times)
    outcome = oracle.stop()
    assert outcome.status == status, outcome.reasons
    if reason is not None:
        assert any(reason in line for line in outcome.reasons), outcome.reasons


def test_a_creation_call_with_nothing_after_it_has_no_known_return(tmp_path):
    # The traced root 20 was killed by the harness and wrote no end; its
    # clone is the last line, so nothing bounds when it returned.
    trace = (
        "21  9.5 +++ exited with 0 +++\n"
        "20  9.6 clone(child_stack=0x1, flags=CLONE_VM|CLONE_SIGHAND|CLONE_THREAD)"
        " = 24 <0.000020>\n"
    )
    (birth,) = read_trace(trace).births
    assert birth.returned is None and birth.duration == 0.00002
    oracle = _attached(tmp_path, trace)
    oracle.threads = {24: 20}
    oracle.census_times = {24: (9.7, 9.8)}
    outcome = oracle.stop(confirmed_dead=[20])
    assert outcome.status == INCOMPLETE
    assert any("return time is unknown" in line for line in outcome.reasons)


@pytest.mark.parametrize(
    ("line", "returned"),
    [
        pytest.param(_CLONE_24 + ") = 24 <0.2>\n", 7.0, id="unsplit-bounded-next"),
        pytest.param(
            _CLONE_24 + " <unfinished ...>\n20  6.2 <... clone resumed>) = 24 <0.2>\n",
            6.2,
            id="resumed",
        ),
    ],
)
def test_the_return_bound_and_duration_are_read_from_the_trace(line, returned):
    (birth,) = read_trace(line + "21  7.0 +++ exited with 0 +++\n").births
    assert (birth.returned, birth.duration) == (returned, 0.2)


def test_a_duration_is_not_part_of_a_calls_result():
    (call,) = parse_strace(
        "21  6.0 kill(95, SIGTERM) = -1 EPERM (Operation not permitted) <0.000010>\n"
    )
    assert call.result == "-1 EPERM (Operation not permitted)"
    assert call.rejected


@pytest.mark.parametrize(
    ("text", "returned"),
    [
        pytest.param(
            "21  6.0 kill(95, SIGTERM) = 0 <0.000010>\n22  7.0 +++ exited with 0 +++\n",
            7.0,
            id="unsplit-bounded-by-the-next-line",
        ),
        pytest.param(
            "21  6.0 kill(95, SIGTERM <unfinished ...>\n"
            "22  6.5 --- SIGCHLD ---\n"
            "21  8.0 <... kill resumed>) = 0 <2.0>\n",
            8.0,
            id="split-bounded-by-its-resumed-line",
        ),
        pytest.param(
            "21  6.0 kill(95, SIGTERM) = 0 <0.000010>\n", None, id="nothing-after-it"
        ),
        pytest.param(
            "21  6.0 kill(-21, SIGKILL) = ?\n21  6.1 +++ killed by SIGKILL +++\n",
            None,
            id="never-returned",
        ),
        pytest.param(
            "21  6.0 kill(95, SIGTERM) = 0 <0.000010>\n22  6.0 --- SIGCHLD ---\n",
            6.0,
            id="bounded-by-any-line-strace-wrote",
        ),
    ],
)
def test_a_signal_call_is_bounded_like_a_creation_and_never_by_its_duration(
    text, returned
):
    (call,) = read_trace(text).calls
    assert call.returned == returned
    assert call.as_event_fields()["returned"] == returned


def test_a_call_that_never_returned_is_not_read_as_delivered():
    (fatal, returned) = read_trace(
        "21  6.0 kill(-22, SIGKILL) = 0 <0.000010>\n"
        "21  6.1 kill(-21, SIGKILL) = ?\n"
        "21  6.2 +++ killed by SIGKILL +++\n"
    ).calls[::-1]
    assert fatal.outcome == "no return" and returned.outcome == "delivered"


@pytest.mark.parametrize(
    ("result", "description"),
    [
        ("?", "attempted SIGKILL (no return observed)"),
        ("0", "delivered SIGKILL"),
        ("-1 EPERM (Operation not permitted)", "attempted (refused:"),
    ],
)
def test_a_violation_report_distinguishes_attempts_from_delivery(result, description):
    trace = read_trace(f"20 6.0 kill(-1, SIGKILL) = {result}\n")
    records = [
        {
            "kind": "process.start",
            "pid": 20,
            "start_identity": 1.0,
            "ppid": os.getpid(),
            "in_row": True,
            "t": 1.0,
            "actor": "owner",
            "pgid": 20,
        },
    ]
    o2 = derive_o2(
        OracleOutcome(status=COMPLETE, calls=trace.calls, traced=[20]),
        ProcessHistory(records, outside=[os.getpid()]),
    )
    assert o2.state == VIOLATED
    assert len(o2.violations) == 1
    assert o2.violations[0].startswith(description)


def test_each_unknown_recipient_is_marked_on_the_call_it_belongs_to():
    # One call whose group no sample brackets, one reaching nobody (no entry).
    text = (
        "20  6.0 kill(-500, SIGTERM) = 0 <0.000010>\n"
        "20  6.1 kill(-501, SIGTERM) = -1 ESRCH (No such process) <0.000010>\n"
    )
    trace = read_trace(text)
    records = [
        {"kind": "watcher.ready", "baseline_pgids": [1]},
        {
            "kind": "process.start",
            "pid": 20,
            "start_identity": 1.0,
            "ppid": os.getpid(),
            "in_row": True,
            "t": 1.0,
            "actor": "owner",
            "pgid": 20,
        },
    ]
    o2 = derive_o2(
        OracleOutcome(status=COMPLETE, calls=trace.calls, traced=[20]),
        ProcessHistory(records, outside=[os.getpid()]),
    )
    assert [entry["unknown"] for entry in o2.resolved] == [True]
    assert len(o2.unknowns) == 1


def test_a_group_is_tied_to_a_marker_only_by_a_record_carrying_both():
    def reading(t, pgid, **fields):
        return {
            "kind": "process.update" if t > 2.0 else "process.start",
            "pid": 30,
            "start_identity": 2.0,
            "ppid": os.getpid(),
            "in_row": True,
            "t": t,
            "actor": "browser",
            "pgid": pgid,
            **fields,
        }

    history = ProcessHistory(
        [
            reading(2.0, 999),
            reading(3.0, 30, browser_marker="a" * 16),
            reading(4.0, 31, browser_marker="a" * 16),
            reading(5.0, None, browser_marker="a" * 16),
        ],
        outside=[os.getpid()],
    )
    (life,) = history.lifetimes
    # Read in 999 before its marker was: one of its groups, not a marked one.
    assert life.groups_by(3.0) == {999, 30}
    assert life.groups_marked_by("a" * 16, 2.5) == set()
    assert life.groups_marked_by("a" * 16, 3.0) == {30}
    assert life.groups_marked_by("a" * 16, 5.0) == {30, 31}
    assert life.groups_marked_by("b" * 16, 5.0) == set()
