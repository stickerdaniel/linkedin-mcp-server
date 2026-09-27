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
from typing import Any

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
from differential.signals import (
    HELD,
    UNKNOWN,
    UNOBSERVED,
    VIOLATED,
    Canaries,
    O2Result,
    ProcessHistory,
    SignalOracle,
    classes_direct_would_not_send,
    derive_o2,
    oracle_unavailable,
    parse_strace,
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


def test_a_call_split_by_another_thread_is_joined_and_other_lines_skipped():
    text = (
        "300  10.000001 kill(400, SIGKILL <unfinished ...>\n"
        "301  10.000002 --- SIGCHLD {si_signo=SIGCHLD} ---\n"
        "300  10.000003 <... kill resumed>) = 0\n"
        "300  10.000004 kill(0, SIGTERM) = 0\n"
        "300  10.000005 kill(-1, SIGKILL) = 0\n"
        "300  10.000006 tkill(401, SIGUSR1) = 0\n"
        "300  10.000007 pidfd_send_signal(5<anon_inode:[pidfd]>, SIGKILL, NULL, 0) = 0\n"
        "strace: Process 300 attached\n"
        "300  10.000008 +++ killed by SIGKILL +++\n"
    )
    calls = parse_strace(text)
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


def _o2(text: str, records=None, **kwargs) -> O2Result:
    history = ProcessHistory(records or _row(), outside=[HARNESS])
    return derive_o2(
        parse_strace(text),
        history,
        threads=kwargs.pop("threads", {}),
        oracle_available=kwargs.pop("oracle_available", True),
        **kwargs,
    )


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
def test_a_leaderless_group_without_the_rows_marker_stays_unknown(marker):
    result = _o2("21  6.0 kill(-40, SIGKILL) = 0\n", _crashpad(marker))
    assert result.state == UNKNOWN, result


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
    history = ProcessHistory(records, outside=[9157])
    result = derive_o2(
        parse_strace(_K1_FROZEN_TRACE),
        history,
        threads={},
        oracle_available=True,
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


def test_a_canary_death_violates_o2_even_without_an_oracle():
    death = [{"pid": 90}]
    assert _o2("", oracle_available=False).state == UNOBSERVED
    assert _o2("", oracle_available=False, canary_deaths=death).state == VIOLATED
    assert _o2("", canary_deaths=death).state == VIOLATED


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
    assert "trace=kill,tkill,tgkill,pidfd_send_signal" in command
    assert "signal=none" in command
    assert command[-4:] == ["-p", "20", "-p", "21"]


def test_an_unavailable_oracle_attaches_nothing_and_reads_nothing(tmp_path):
    oracle = SignalOracle(tmp_path)
    oracle.unavailable = "modelled"
    assert oracle.start([1]) == "modelled"
    assert oracle.stop() == []


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
) -> RowResult:
    vector = harness.RowVector(
        mode="daemon" if daemon else "direct",
        o1_single_browser=True,
        browser_seen=True,
        watcher_healthy=True,
        o4_session="retained",
        origin_saw_feed=True,
        feed_carried_session=True,
        tool_succeeded=True,
        owner_published=daemon,
        fell_back=False,
        host_exit_clean=True,
        cleanup_clean=True,
        o2=HELD if attached else UNOBSERVED,
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


def test_k2_reads_bang_from_the_guardians_group():
    result = _r6(group=4321, classes=[GUARDIAN_OWNER_GROUP_KILL])
    assert r6_reading(result) == "!"
    assert r6_verdict(result, experiment="K2", windows=False) == []
    # Without an oracle the argv alone carries the reading.
    unobserved = _r6(group=4321, attached=False)
    assert r6_verdict(unobserved, experiment="K2", windows=False) == []


@pytest.mark.parametrize("attached", [True, False])
def test_k2_reading_equal_is_a_harness_defect(attached):
    (problem,) = r6_verdict(
        _r6(group=0, attached=attached), experiment="K2", windows=False
    )
    assert "K2 read '='" in problem and "harness defect" in problem


def test_k2_whose_oracle_missed_the_group_kill_is_a_harness_defect():
    (problem,) = r6_verdict(_r6(group=4321), experiment="K2", windows=False)
    assert "oracle saw no kill" in problem


def test_k3_must_read_equal_and_recover():
    assert r6_verdict(_r6(group=0), experiment="K3", windows=False) == []
    assert r6_verdict(_r6(group=4321), experiment="K3", windows=False)
    assert r6_verdict(
        _r6(group=0, classes=[GUARDIAN_OWNER_GROUP_KILL]),
        experiment="K3",
        windows=False,
    )
    (problem,) = r6_verdict(
        _r6(group=0, recovered=False), experiment="K3", windows=False
    )
    assert "did not recover" in problem


def test_h_r6_needs_the_kill_and_on_posix_the_guardian():
    assert r6_verdict(
        _r6(group=0, exit="gone before the kill"), experiment="K3", windows=False
    )
    (problem,) = r6_verdict(_r6(group=None), experiment="K3", windows=False)
    assert "never seen" in problem
    # Windows starts no guardian: nothing to read, nothing required of it.
    assert r6_verdict(_r6(group=None), experiment="K2", windows=True) == []


def test_a_row_that_violated_o2_fails_and_differs_from_direct(profile_pair):
    healthy = _healthy(profile_pair, daemon=True)
    violated = O2Result(state=VIOLATED, violations=["canary 90 died during the row"])
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
    assert history.descends(crashpad, browser) is True


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
