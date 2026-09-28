"""H-R7's own evidence, without a browser: what the native row will judge by.

* Clocks: a drain return is placed on strace's clock only where one offset
  fits every bracketed sample, and a traced line only on the side its whole
  microsecond lies; everything else is ambiguous.
* The lock: ``/proc/locks`` as proc(5) prints it, and the original actor
  shown holding the ``flock`` only when it is listed and holds a descriptor.
* Owned workers: a cancelled wait does not overlap the work it waited on, a
  worker past its bound stays owned and stops every later step, and every
  failure to settle the lease contender's helpers counts.
* The launch marker, read only from a browser the original actor launched.
* The phase, on a real transcript: the baseline owner's fatal own-group kill
  (arm64 CI run 36381619115, K2, verbatim lines) is K2's witness only in the
  calibrated shape and never the guardian's; any original-actor signal after
  the return, or an unplaceable one, fails K3.
* The gate each cell passes, the ledger, and the composition.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import psutil
import pytest

from differential import harness, lease_probe, r7_fault, unconfirmed_close
from differential.baseline import BASELINE_SHA, git
from differential.fault_overlay import FAULT_SHA256
from differential.signals import COMPLETE, INCOMPLETE, OracleOutcome, read_trace
from differential.unconfirmed_close import (
    AFTER_CONFIRMED_CLOSE,
    AFTER_CONSUMPTION,
    AMBIGUOUS,
    BASELINE,
    BEFORE,
    BEFORE_CLOSE,
    BEFORE_PRESERVATION,
    BEFORE_QUIT,
    BEFORE_RECOVERY,
    CANDIDATE,
    CLOSE_PATH,
    HOLDER,
    IN_PHASE,
    INERT,
    NO_RECOVERY,
    POST_SETTLEMENT,
    UNSHIMMED,
    AliasModel,
    ClockSample,
    FatalCalibration,
    PhaseReading,
    R7Continuation,
    R7Ledger,
    UnsettledWorker,
    WorkerFailed,
    alias_model,
    calibrate_fatal_group,
    calibration_from,
    checkpoint_problems,
    clock_sample,
    continuation_signals,
    early_browsers,
    gate,
    launch_marker,
    lock_association,
    own_group_operations,
    parse_proc_locks,
    place,
    r7_composition,
    r7_environment,
    r7_problems,
    read_phase,
    realtime_interval,
    run_owned,
    running_workers,
    settlement_problems,
)
from linkedin_mcp_server import process_tree

_REPO = Path(__file__).resolve().parents[2]


# --- Clocks ----------------------------------------------------------------------------


def _sample(label: str, mono: int, offset: int, width: int = 10) -> ClockSample:
    """A realtime read at *mono* + *offset*, bracketed *width* ns either side."""
    return ClockSample(label, mono - width, mono + offset, mono + width)


def test_a_return_is_placed_where_one_offset_fits_every_sample():
    samples = [_sample("before", 1_000, 5_000_000), _sample("after", 9_000, 5_000_000)]
    interval = realtime_interval(4_000, samples)
    assert isinstance(interval, tuple)
    low, high = interval
    # Within the brackets' width of the true time, and no wider.
    assert low <= 4_000 + 5_000_000 <= high
    assert high - low == 20


def test_a_realtime_step_between_the_samples_places_nothing():
    samples = [_sample("before", 1_000, 5_000_000), _sample("after", 9_000, 7_000_000)]
    assert "stepped" in realtime_interval(4_000, samples)


@pytest.mark.parametrize(
    ("returned", "samples", "why"),
    [
        pytest.param(4_000, [], "not sampled on both sides", id="unsampled"),
        pytest.param(
            20_000,
            [_sample("before", 1_000, 5), _sample("after", 9_000, 5)],
            "not between",
            id="after-the-last-sample",
        ),
    ],
)
def test_a_return_outside_the_samples_is_not_placed(returned, samples, why):
    assert why in realtime_interval(returned, samples)


@pytest.mark.parametrize(
    ("t", "placement"),
    [
        pytest.param(1790573446.666150, IN_PHASE, id="the-next-microsecond"),
        pytest.param(1790573446.666148, BEFORE, id="the-microsecond-before"),
        pytest.param(1790573446.666149, AMBIGUOUS, id="the-same-microsecond"),
    ],
)
def test_a_line_is_placed_by_its_whole_microsecond(t, placement):
    # The return known to within [.666149100, .666149900] seconds.
    boundary = (1790573446_666149_100, 1790573446_666149_900)
    assert place(t, boundary) == placement


def test_the_narrowest_bracket_is_kept():
    # A wide first bracket (a pause between its reads), then a narrow one.
    reads = iter([0, 500, 1000, 1010, 2000, 2300])
    sample = clock_sample(
        "x", monotonic_ns=lambda: next(reads), realtime_ns=lambda: 7, reads=3
    )
    assert (sample.before_ns, sample.after_ns) == (1000, 1010)


# --- The lock ----------------------------------------------------------------------------

#: proc(5)'s own example of /proc/locks, and a waiter line.
PROC_LOCKS = """\
1: POSIX  ADVISORY  READ  5433 08:01:7864448 128 128
2: FLOCK  ADVISORY  WRITE 2001 08:01:7864554 0 EOF
2: -> FLOCK  ADVISORY  WRITE 2002 08:01:7864554 0 EOF
3: FLOCK  ADVISORY  WRITE 1568 00:2f:32388 0 EOF
8: OFDLCK ADVISORY  WRITE -1 08:01:8713209 128 191
"""


def test_proc_locks_reads_each_holder_and_skips_waiters():
    entries, problems = parse_proc_locks(PROC_LOCKS)
    assert problems == []
    assert [(e["kind"], e["mode"], e["pid"]) for e in entries] == [
        ("POSIX", "READ", 5433),
        ("FLOCK", "WRITE", 2001),
        ("FLOCK", "WRITE", 1568),
        ("OFDLCK", "WRITE", -1),
    ]
    assert entries[2]["device"] == (0, 0x2F) and entries[2]["inode"] == 32388


def test_an_unreadable_proc_locks_line_is_a_problem_not_a_lock():
    entries, problems = parse_proc_locks("1: FLOCK ADVISORY WRITE x 08:01:5 0 EOF\n")
    assert entries == [] and len(problems) == 1


@pytest.fixture
def lock(tmp_path) -> tuple[Path, tuple[int, int]]:
    path = tmp_path / "auth" / "profile.lock"
    path.parent.mkdir()
    path.write_text("")
    info = os.stat(path)
    return path, (info.st_dev, info.st_ino)


def _proc(tmp_path: Path, pid: int, *targets: Path) -> Path:
    """A /proc of one process whose descriptors open *targets*."""
    fds = tmp_path / "proc" / str(pid) / "fd"
    fds.mkdir(parents=True)
    for number, target in enumerate(targets, start=3):
        (fds / str(number)).symlink_to(target)
    return tmp_path / "proc"


def _locks(tmp_path: Path, identity, *, pid: int, kind="FLOCK", mode="WRITE"):
    device, inode = identity
    where = f"{os.major(device):02x}:{os.minor(device):02x}:{inode}"
    path = tmp_path / "locks"
    path.write_text(f"1: {kind}  ADVISORY  {mode} {pid} {where} 0 EOF\n")
    return path


def test_the_listed_holder_with_an_open_descriptor_holds_it(tmp_path, lock):
    path, identity = lock
    association = lock_association(
        identity,
        4242,
        locks=_locks(tmp_path, identity, pid=4242),
        proc=_proc(tmp_path, 4242, path),
    )
    assert association["state"] == HOLDER


@pytest.mark.parametrize(
    ("listed", "kind", "mode", "opened", "state"),
    [
        pytest.param(9999, "FLOCK", "WRITE", True, "not the holder", id="another-pid"),
        pytest.param(
            4242, "FLOCK", "WRITE", False, "not the holder", id="no-descriptor"
        ),
        pytest.param(4242, "FLOCK", "READ", True, "not the holder", id="shared"),
        pytest.param(4242, "POSIX", "WRITE", True, "not the holder", id="posix"),
    ],
)
def test_either_half_missing_is_no_holder(
    tmp_path, lock, listed, kind, mode, opened, state
):
    path, identity = lock
    other = tmp_path / "other"
    other.write_text("")
    association = lock_association(
        identity,
        4242,
        locks=_locks(tmp_path, identity, pid=listed, kind=kind, mode=mode),
        proc=_proc(tmp_path, 4242, path if opened else other),
    )
    assert association["state"] == state


def test_unread_descriptors_are_unknown(tmp_path, lock):
    _, identity = lock
    association = lock_association(
        identity,
        4242,
        locks=_locks(tmp_path, identity, pid=4242),
        proc=tmp_path / "no-proc",
    )
    assert association["state"] == "unknown"


def _point(label: str, state: str, *, holder: bool = False, **fields: Any):
    return {
        "label": label,
        "state": state,
        "same_lock": True,
        "association": {"state": HOLDER} if holder else None,
        **fields,
    }


@pytest.mark.parametrize(
    ("point", "why"),
    [
        pytest.param(_point("x", "held", holder=True), None, id="held-and-holding"),
        pytest.param(_point("x", "free", holder=True), "not 'held'", id="free"),
        pytest.param(_point("x", "held"), "not shown holding", id="unassociated"),
        pytest.param(
            _point("x", "held", holder=True, same_lock=False),
            "not the one identified",
            id="another-lock",
        ),
        pytest.param(
            _point(
                "x",
                "held",
                holder=True,
                expect_alive={"original actor": True},
                alive={"original actor": False},
            ),
            "gone, not alive",
            id="actor-gone",
        ),
        pytest.param(
            _point("x", "held", holder=True, error="OSError: planted"),
            "contender failed",
            id="contender-failed",
        ),
    ],
)
def test_a_held_checkpoint_needs_the_holder_and_the_lock(point, why):
    problems = checkpoint_problems(point, expect="held", holder=True)
    if why is None:
        assert problems == []
    else:
        assert any(why in p for p in problems), problems


# --- Owned workers ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def owned(monkeypatch):
    """Fresh registries, so one test's stranded worker never gates the next."""
    workers: list[Any] = []
    helpers: list[Any] = []
    monkeypatch.setattr(unconfirmed_close, "_OWNED", workers)
    monkeypatch.setattr(lease_probe, "_OWNED", helpers)
    return workers


async def test_an_owned_worker_returns_its_value_and_raises_its_error():
    assert await run_owned("sum", sum, [1, 2], seconds=5) == 3
    with pytest.raises(ValueError, match="planted"):
        await run_owned("fails", _raises(ValueError("planted")), seconds=5)
    assert running_workers() == []


def _raises(error: BaseException):
    def work():
        raise error

    return work


async def test_an_interrupt_in_the_work_is_a_failed_worker_not_an_interrupt():
    with pytest.raises(WorkerFailed):
        await run_owned("interrupted", _raises(KeyboardInterrupt()), seconds=5)


async def test_a_cancelled_wait_does_not_overlap_the_work():
    release, finished = threading.Event(), threading.Event()

    def work():
        release.wait(10)
        finished.set()

    task = asyncio.ensure_future(run_owned("slow", work, seconds=30))
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.sleep(0.2)
    # Held: the caller has not moved on, and nothing else may start.
    assert not task.done()
    with pytest.raises(UnsettledWorker):
        gate("the next step")
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The cancellation came only once the work had ended.
    assert finished.is_set()
    gate("the next step")


async def test_a_worker_past_its_bound_stays_owned_and_stops_every_later_step():
    release = threading.Event()
    called = []
    with pytest.raises(UnsettledWorker):
        await run_owned("stuck", lambda: release.wait(10), seconds=0.2)
    assert running_workers() == ["stuck"]
    with pytest.raises(UnsettledWorker):
        await run_owned("next", lambda: called.append(1), seconds=5)
    assert called == []
    release.set()
    deadline = time.monotonic() + 5
    while running_workers() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert running_workers() == []
    await run_owned("next", lambda: called.append(1), seconds=5)
    assert called == [1]


async def test_cleanup_still_runs_while_a_measurement_is_refused():
    # Ending what the row started must not wait on what it could not settle.
    release = threading.Event()
    with pytest.raises(UnsettledWorker):
        await run_owned("stuck", lambda: release.wait(10), seconds=0.2)
    ended = []
    with pytest.raises(UnsettledWorker):
        await run_owned("measure", lambda: ended.append("measured"), seconds=5)
    await run_owned("end", lambda: ended.append("ended"), seconds=5, gated=False)
    assert ended == ["ended"]
    release.set()


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(lease_probe.UnsettledHelper("planted"), id="unsettled"),
        pytest.param(OSError("planted"), id="os-error"),
        pytest.param(KeyboardInterrupt(), id="interrupted"),
    ],
)
def test_every_failure_to_settle_the_contender_is_a_failed_gate(monkeypatch, failure):
    def settle(grace=5.0):
        raise failure

    monkeypatch.setattr(lease_probe, "settle", settle)
    assert settlement_problems() != []
    with pytest.raises(UnsettledWorker):
        gate("the next measurement")


# --- The launch marker --------------------------------------------------------------------

MARKER = "the-launch-marker"
DIGEST = hashlib.sha256(MARKER.encode()).hexdigest()[:16]


def _start(pid, start, ppid, actor, **fields):
    return {
        "kind": "process.start",
        "pid": pid,
        "start_identity": start,
        "ppid": ppid,
        "in_row": True,
        "t": start,
        "actor": actor,
        "pgid": pid,
        **fields,
    }


class _Process:
    def __init__(self, created: float, environ: dict[str, str]):
        self._created, self._environ = created, environ

    def create_time(self):
        return self._created

    def environ(self):
        return self._environ


def _records(browser_parent: int = 100, digest: str = DIGEST):
    return [
        _start(100, 10.0, os.getpid(), "owner"),
        _start(200, 20.0, browser_parent, "browser", browser_marker=digest),
    ]


def _browser(created: float = 20.0, marker: str = MARKER):
    return lambda pid: _Process(
        created, {"LINKEDIN_MCP_BROWSER_PROCESS_MARKER": marker}
    )


def test_the_marker_is_read_from_the_original_actors_browser_and_kept_out_of_view():
    found = launch_marker(_records(), (100, 10.0), open_process=_browser())
    assert found is not None and found.value == MARKER and found.digest == DIGEST
    assert MARKER not in repr(found)


@pytest.mark.parametrize(
    ("records", "opener"),
    [
        pytest.param(_records(digest="0" * 16), _browser(), id="another-digest"),
        pytest.param(_records(), _browser(created=21.0), id="another-lifetime"),
        pytest.param(_records(), _browser(marker="other"), id="another-value"),
        pytest.param(_records(browser_parent=300), _browser(), id="not-its-browser"),
    ],
)
def test_no_other_browser_or_value_stands_in_for_the_marker(records, opener):
    assert launch_marker(records, (100, 10.0), open_process=opener) is None


def test_a_browser_before_the_barrier_that_is_not_the_originals_is_early():
    records = [
        _start(100, 10.0, os.getpid(), "owner"),
        _start(200, 20.0, 100, "browser"),
        _start(300, 30.0, os.getpid(), "owner"),
        _start(400, 40.0, 300, "browser"),
    ]
    assert early_browsers(records, (100, 10.0), since=35.0, until=50.0) != []
    # Later than the barrier, or the original's own: not early.
    assert early_browsers(records, (100, 10.0), since=35.0, until=39.0) == []
    assert early_browsers(records[:2], (100, 10.0), since=15.0, until=50.0) == []


def test_a_browser_whose_ancestry_is_lost_is_not_the_originals():
    # Its parent was never recorded: nothing ties it to the original actor.
    records = [
        _start(100, 10.0, os.getpid(), "owner"),
        _start(400, 40.0, 999, "browser"),
    ]
    assert early_browsers(records, (100, 10.0), since=35.0, until=50.0) != []


def test_a_browser_from_before_the_close_is_not_the_recoverys():
    # Another browser on the profile before the close is O1's to judge, not
    # a successor's early use.
    records = [
        _start(100, 10.0, os.getpid(), "owner"),
        _start(400, 20.0, 999, "browser"),
    ]
    assert early_browsers(records, (100, 10.0), since=35.0, until=50.0) == []


# --- The phase, on a real transcript --------------------------------------------------------

#: Verbatim lines of arm64 CI run 36381619115's K2 trace (the baseline owner
#: 13240, its drain thread 13266, its guardian 13257, which was given the
#: owner's group), shortened to these.
K2_TRACE = """\
13266 1790573436.142143 kill(-13272, 0) = -1 ESRCH (No such process) <0.000008>
13240 1790573446.601691 kill(-15042, SIGKILL) = 0 <0.000595>
13240 1790573446.666085 kill(-15042, 0) = -1 ESRCH (No such process) <0.000014>
13240 1790573446.666149 kill(-13240, SIGKILL) = ?
13266 1790573446.673971 +++ killed by SIGKILL +++
13240 1790573446.673977 +++ killed by SIGKILL +++
13257 1790573446.674013 kill(-13240, SIGKILL) = 0 <0.000015>
13257 1790573447.715976 +++ exited with 0 +++
"""
OWNER, THREAD, GUARDIAN, GROUP = 13240, 13266, 13257, 13240
#: Between the drain thread's probe and the owner's hard exit.
RETURNED = (1790573440_000000_000, 1790573440_000001_000)


def _outcome(text: str = K2_TRACE, status: str = COMPLETE) -> OracleOutcome:
    trace = read_trace(text)
    return OracleOutcome(
        status=status,
        calls=trace.calls,
        threads={THREAD: OWNER},
        traced=[OWNER, GUARDIAN],
        cohort={
            OWNER: {"kind": "root", "process": OWNER},
            THREAD: {"kind": "thread", "process": OWNER},
            GUARDIAN: {"kind": "root", "process": GUARDIAN},
        },
        reasons=list(trace.problems),
    )


def _reading(text: str = K2_TRACE, boundary: Any = RETURNED, **kw) -> PhaseReading:
    return read_phase(
        _outcome(text, **kw),
        text,
        owner=OWNER,
        guardian=GUARDIAN,
        owner_group=GROUP,
        boundary=boundary,
    )


def _calibration(text: str = K2_TRACE) -> FatalCalibration:
    # The probe's own lines, as this tracer writes a fatal own-group kill.
    probe = (
        "\n".join(line for line in text.splitlines() if line.startswith(f"{OWNER} "))
        + "\n"
    )
    outcome = OracleOutcome(
        status=COMPLETE, calls=read_trace(probe).calls, traced=[OWNER]
    )
    return calibration_from(outcome, probe, pid=OWNER, returncode=-9)


def test_the_calibration_takes_the_fatal_calls_shape_without_its_pids():
    calibration = _calibration()
    assert calibration.problems == ()
    assert calibration.shape == {
        "syscall": "kill",
        "signal": "SIGKILL",
        "target": "own group",
        "result": "?",
        "end": "killed by SIGKILL",
    }


@pytest.mark.parametrize(
    ("returncode", "text", "why"),
    [
        pytest.param(0, None, "not killed by SIGKILL", id="survived"),
        pytest.param(
            -9,
            "13240 1790573446.666149 kill(-13240, SIGKILL) = ?\n",
            "not followed by its end",
            id="no-end",
        ),
        pytest.param(-9, "", "0 own-group kills", id="no-kill"),
    ],
)
def test_a_calibration_that_did_not_show_a_fatal_kill_is_none(returncode, text, why):
    probe = text if text is not None else K2_TRACE
    outcome = OracleOutcome(status=COMPLETE, calls=read_trace(probe).calls)
    found = calibration_from(outcome, probe, pid=OWNER, returncode=returncode)
    assert found.shape is None and any(why in p for p in found.problems)


def test_no_tracer_no_calibration_and_no_child(monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise AssertionError("a probe child was started without a tracer")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    tracer = harness.SignalOracle(tmp_path)
    monkeypatch.setattr(tracer, "unavailable", "not a disposable runner")
    found = calibrate_fatal_group(tmp_path, oracle=tracer)
    assert found.shape is None and "no tracer" in found.problems[0]


def test_the_owners_own_group_kill_after_the_return_is_k2s_witness():
    found, problems = own_group_operations(_reading(), _calibration())
    assert problems == []
    assert [(call["pid"], call["target_group"]) for call in found] == [(OWNER, GROUP)]


@pytest.mark.parametrize(
    ("text", "boundary", "calibration", "why"),
    [
        pytest.param(
            K2_TRACE.replace("13240 1790573446.666149 kill(-13240, SIGKILL) = ?\n", ""),
            RETURNED,
            None,
            "was not traced killing its own group",
            id="only-the-guardian-killed-it",
        ),
        pytest.param(
            K2_TRACE,
            (1790573447_000000_000, 1790573447_000001_000),
            None,
            "was not traced killing its own group",
            id="before-the-return",
        ),
        pytest.param(
            K2_TRACE,
            (1790573446_666149_000, 1790573446_666149_500),
            None,
            "was not traced killing its own group",
            id="unplaceable-against-the-return",
        ),
        pytest.param(
            K2_TRACE, "the clock stepped", None, "could not be placed", id="clock"
        ),
        pytest.param(
            K2_TRACE,
            RETURNED,
            FatalCalibration(None, ("no tracer",)),
            "no calibrated transcript",
            id="uncalibrated",
        ),
        pytest.param(
            K2_TRACE,
            RETURNED,
            FatalCalibration({**(_calibration().shape or {}), "result": "0"}, ()),
            "was not traced killing its own group",
            id="another-shape",
        ),
    ],
)
def test_nothing_else_is_k2s_witness(text, boundary, calibration, why):
    found, problems = own_group_operations(
        _reading(text, boundary), calibration or _calibration()
    )
    assert found == []
    assert any(why in p for p in problems), problems


def test_an_incomplete_trace_is_no_witness_whatever_it_shows():
    _, problems = own_group_operations(_reading(status=INCOMPLETE), _calibration())
    assert any("incomplete" in p for p in problems)


def test_a_guardian_kill_in_the_fatal_shape_is_still_not_the_owners():
    # Constructed from the lines above: the guardian, not the owner, making
    # a call of exactly the calibrated shape on the owner's group.
    text = (
        "13257 1790573446.674013 kill(-13240, SIGKILL) = ?\n"
        "13257 1790573446.674020 +++ killed by SIGKILL +++\n"
    )
    found, problems = own_group_operations(_reading(text), _calibration())
    assert found == [] and any("was not traced killing" in p for p in problems)


def test_an_incomplete_trace_never_reads_as_zero_signals():
    # Nothing of the original actor's after the return in what was read, and
    # still no zero: what was lost may have held one.
    late = (1790573448_000000_000, 1790573448_000001_000)
    problems = continuation_signals(_reading(boundary=late, status=INCOMPLETE))
    assert any("incomplete" in p for p in problems)


def test_an_original_actor_signal_after_the_return_fails_k3():
    problems = continuation_signals(_reading())
    # Both SIGKILLs of the owner after the return; its probes are no signal,
    # and the guardian's kill is the shared leg, read apart.
    assert len(problems) == 2 and all("SIGKILL" in p for p in problems), problems


def test_only_signals_before_the_return_leave_k3_at_zero():
    late = (1790573448_000000_000, 1790573448_000001_000)
    assert continuation_signals(_reading(boundary=late)) == []


@pytest.mark.parametrize(
    ("text", "boundary", "why"),
    [
        pytest.param(
            K2_TRACE,
            (1790573446_601691_000, 1790573446_601691_500),
            "(ambiguous",
            id="unplaceable-line",
        ),
        pytest.param(K2_TRACE, "the clock stepped", "could not be placed", id="clock"),
        pytest.param(
            "7777 1790573446.700000 kill(-500, SIGTERM) = 0 <0.000010>\n",
            RETURNED,
            "unplaced 7777",
            id="unplaced-sender",
        ),
    ],
)
def test_nothing_unknown_reads_as_no_signal(text, boundary, why):
    problems = continuation_signals(_reading(text, boundary))
    assert any(why in p for p in problems), problems


# --- The source model ------------------------------------------------------------------------


def _baseline_tree() -> str:
    shown = git(_REPO, "show", f"{BASELINE_SHA}:linkedin_mcp_server/process_tree.py")
    if shown is None:
        git(_REPO, "fetch", "--no-tags", "--depth=1", "origin", BASELINE_SHA)
        shown = git(
            _REPO, "show", f"{BASELINE_SHA}:linkedin_mcp_server/process_tree.py"
        )
    assert shown is not None
    return shown


def test_the_fault_model_holds_on_both_exact_sources():
    candidate = Path(process_tree.__file__).read_text(encoding="utf-8")
    model = alias_model({BASELINE: _baseline_tree(), CANDIDATE: candidate})
    assert model.problems == ()
    assert set(model.sha256) == {BASELINE, CANDIDATE}


def test_a_public_drain_that_skips_the_private_global_fails_the_model():
    candidate = Path(process_tree.__file__).read_text(encoding="utf-8")
    broken = candidate.replace(
        "    return _drain_marked_posix_groups(marker, deadline)\n",
        "    return True\n",
    )
    assert broken != candidate
    model = alias_model({CANDIDATE: broken})
    assert any("did not hand one real True back as False" in p for p in model.problems)


def test_a_baseline_close_path_unlike_the_candidates_fails_the_model():
    files = {path: (_REPO / path).read_text(encoding="utf-8") for path in CLOSE_PATH}
    changed = {**files, CLOSE_PATH[1]: files[CLOSE_PATH[1]] + "\n# another close\n"}
    candidate = Path(process_tree.__file__).read_text(encoding="utf-8")
    same = alias_model(
        {CANDIDATE: candidate}, close_path={CANDIDATE: files, BASELINE: files}
    )
    other = alias_model(
        {CANDIDATE: candidate}, close_path={CANDIDATE: files, BASELINE: changed}
    )
    assert same.problems == ()
    assert any(CLOSE_PATH[1] in p for p in other.problems)


# --- The gate each cell passes -------------------------------------------------------------


@dataclass(frozen=True)
class _Vector:
    """A row vector's stand-in: what the whole-row comparison is handed."""

    o4_session: str = "retained"
    o2_traced: str = "held"
    signal_classes: tuple[str, ...] = ("guardian:browser-group",)


def _cell(experiment: str, *, control: str | None = None, **changes: Any):
    daemon = experiment != "K1"
    points = [
        _point(
            BEFORE_CLOSE,
            "held",
            holder=True,
            expect_alive={"original actor": True},
            alive={"original actor": True},
        ),
        _point(BEFORE_PRESERVATION, "free"),
    ]
    if control is not None:
        points.append(_point(AFTER_CONFIRMED_CLOSE, "free"))
    elif experiment == "K1":
        points += [
            _point(AFTER_CONSUMPTION, "held", holder=True),
            _point(BEFORE_QUIT, "held", holder=True),
        ]
    else:
        points.append(_point(BEFORE_RECOVERY, "free"))
    reading = _reading(boundary=RETURNED)
    if experiment == "K3":
        reading = _reading(boundary=(1790573448_000000_000, 1790573448_000001_000))
    cell = R7Continuation(
        experiment=experiment,
        repetition=0 if control else 1,
        run="run",
        mode="daemon" if daemon else "direct",
        control=control,
        revision="revision",
        process_tree_sha256="tree",
        fault_sha256=None if control == UNSHIMMED else FAULT_SHA256,
        scenario=(),
        vector=_Vector(),
        first_read=True,
        principal=(OWNER, 1.0),
        role="owner" if daemon else "direct",
        guardian=(GUARDIAN, 1.1),
        guardian_group=GROUP if experiment == "K2" else 0,
        owner_group=GROUP,
        marker_digest=DIGEST,
        lock=(1, 2),
        checkpoints=tuple(points),
        traced_before_activation=True,
        activated=control is None,
        selection=(),
        consumed=0 if control else 1,
        owner_exit="exited" if daemon and control is None else None,
        guardian_exit="exited" if daemon and control is None else None,
        pre_probe=(),
        recovery=(
            NO_RECOVERY
            if experiment == "K1"
            else POST_SETTLEMENT
            if experiment == "K3"
            else "baseline's"
        ),
        successor_verified=True if experiment == "K3" else None,
        successor_problems=(),
        ended_by_harness=(),
        phase=reading,
        validity=(),
    )
    return replace(cell, **changes)


def _problems(cell: R7Continuation, **kw) -> list[str]:
    return r7_problems(
        cell,
        experiment=cell.experiment,
        repetition=cell.repetition,
        revision="revision",
        run="run",
        control=cell.control,
        calibration=_calibration(),
        **kw,
    )


@pytest.mark.parametrize(
    ("experiment", "control"),
    [("K0", UNSHIMMED), ("K0", INERT), ("K1", None), ("K2", None), ("K3", None)],
)
def test_a_complete_cell_passes_its_gate(experiment, control):
    assert _problems(_cell(experiment, control=control)) == []


@pytest.mark.parametrize(
    ("experiment", "changes", "why"),
    [
        pytest.param(
            "K2",
            {"validity": ("the host session failed: planted",)},
            "host session failed",
            id="k2-invalid-with-its-witness",
        ),
        pytest.param("K2", {"guardian_group": 0}, "given group 0", id="k2-group-zero"),
        pytest.param(
            "K3", {"guardian_group": GROUP}, "given group", id="k3-owner-group"
        ),
        pytest.param(
            "K3",
            {"owner_exit": "still running"},
            "still running",
            id="k3-owner-left-on",
        ),
        pytest.param(
            "K3",
            {"pre_probe": ("browser 400 was first seen ...",)},
            "browser 400",
            id="k3-early-browser",
        ),
        pytest.param(
            "K3",
            {"phase": _reading(boundary=RETURNED)},
            "SIGKILL",
            id="k3-signal-after-the-return",
        ),
        pytest.param(
            "K3", {"successor_verified": False}, "no successor", id="k3-no-successor"
        ),
        pytest.param(
            "K1",
            {
                "checkpoints": (
                    _point(BEFORE_CLOSE, "held", holder=True),
                    _point(AFTER_CONSUMPTION, "held"),
                    _point(BEFORE_QUIT, "held", holder=True),
                    _point(BEFORE_PRESERVATION, "free"),
                )
            },
            "not shown holding",
            id="k1-unassociated",
        ),
        pytest.param(
            "K1",
            {
                "checkpoints": (
                    _point(BEFORE_CLOSE, "held", holder=True),
                    _point(AFTER_CONSUMPTION, "held", holder=True),
                    _point(BEFORE_PRESERVATION, "free"),
                )
            },
            "not taken",
            id="k1-no-pre-quit-checkpoint",
        ),
        pytest.param(
            "K1",
            {"selection": ("the claimed entry is not after the send",)},
            "selected call",
            id="k1-unselected",
        ),
        pytest.param("K1", {"role": "owner"}, "role was", id="k1-wrong-role"),
        pytest.param(
            "K3",
            {"phase": _reading(status=INCOMPLETE)},
            "incomplete",
            id="k3-incomplete-trace",
        ),
        pytest.param(
            "K1",
            {"phase": _reading(status=INCOMPLETE)},
            "incomplete",
            id="k1-incomplete-trace",
        ),
        pytest.param(
            "K2",
            {
                "checkpoints": (
                    _point(BEFORE_CLOSE, "held", holder=True),
                    _point(BEFORE_RECOVERY, "free"),
                    _point(BEFORE_PRESERVATION, "held"),
                )
            },
            "before preservation",
            id="k2-held-before-preservation",
        ),
        pytest.param(
            "K2",
            {"traced_before_activation": False},
            "not attached",
            id="k2-traced-late",
        ),
        pytest.param("K3", {"scenario": ("idle 20",)}, "scenario", id="k3-idle"),
        pytest.param(
            "K3", {"fault_sha256": "other"}, "not the declared", id="k3-fault"
        ),
    ],
)
def test_each_gate_refuses_what_it_names(experiment, changes, why):
    problems = _problems(_cell(experiment, **changes))
    assert any(why in p for p in problems), problems


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        pytest.param({"activated": True}, "was activated", id="activated"),
        pytest.param({"consumed": 1}, "consumed as unconfirmed", id="consumed"),
        pytest.param(
            {
                "checkpoints": (
                    _point(BEFORE_CLOSE, "held", holder=True),
                    _point(AFTER_CONFIRMED_CLOSE, "held"),
                    _point(BEFORE_PRESERVATION, "free"),
                )
            },
            "after the confirmed close",
            id="still-held",
        ),
    ],
)
def test_a_control_that_did_not_confirm_is_refused(changes, why):
    problems = _problems(_cell("K0", control=INERT, **changes))
    assert any(why in p for p in problems), problems


# --- The ledger and the composition --------------------------------------------------------


def test_a_second_cell_for_one_key_is_refused_not_chosen():
    ledger = R7Ledger("run")
    ledger.record(_cell("K3"))
    ledger.record(_cell("K3", recovery="other"))
    cells, problems = ledger.take()
    assert len(cells) == 1 and problems and ledger.take() == ({}, [])


def _model(tree: str = "tree") -> AliasModel:
    return AliasModel(sha256={BASELINE: tree, CANDIDATE: tree}, problems=())


def _full_ledger(
    changes: dict[tuple[str, int], dict[str, Any]] | None = None,
) -> R7Ledger:
    ledger = R7Ledger("run")
    ledger.record(_cell("K0", control=UNSHIMMED))
    ledger.record(_cell("K0", control=INERT))
    for experiment in ("K1", "K2", "K3"):
        for repetition in (1, 2, 3):
            cell = _cell(experiment, repetition=repetition)
            cell = replace(cell, **(changes or {}).get((experiment, repetition), {}))
            ledger.record(cell)
    return ledger


def _compose(ledger: R7Ledger, model: AliasModel | None = None, **kw: Any):
    return r7_composition(
        _model() if model is None else model,
        ledger,
        revisions={name: "revision" for name in ("K0", "K1", "K2", "K3")},
        calibration=kw.pop("calibration", _calibration()),
        compare_to_direct=kw.pop("compare_to_direct", lambda direct, daemon: []),
    )


def test_every_cell_of_this_invocation_composes():
    assert _compose(_full_ledger()) == []


def test_a_missing_repetition_fails_the_composition():
    ledger = _full_ledger()
    ledger._cells.pop(("K2", "3"))
    assert any("K2 #3" in p and "no continuation" in p for p in _compose(ledger))


def test_repetitions_that_read_differently_fail_the_composition():
    ledger = _full_ledger({("K1", 2): {"recovery": "another"}})
    problems = _compose(ledger)
    assert any("repetition 2 reads unlike repetition 1" in p for p in problems)


def test_what_the_shared_prefixs_timing_decides_is_no_difference():
    # A Chromium helper that outlived the close on one run, and a recipient
    # the watcher missed: the routine drain's classes and held-or-unknown.
    later = _Vector(o2_traced="unknown", signal_classes=("owner:browser-group",))
    ledger = _full_ledger({("K3", 2): {"vector": later}})
    assert _compose(ledger) == []


def test_a_violation_in_one_repetition_is_a_difference():
    ledger = _full_ledger({("K3", 2): {"vector": _Vector(o2_traced="violated")}})
    assert any("reads unlike" in p and "o2_traced" in p for p in _compose(ledger))


def test_an_inert_overlay_that_reads_unlike_the_plain_runtime_fails():
    ledger = _full_ledger()
    ledger._cells[("K0", INERT)] = replace(
        ledger._cells[("K0", INERT)], first_read=False
    )
    assert any("inert overlay differs" in p for p in _compose(ledger))


@pytest.mark.parametrize(
    ("model", "calibration", "why"),
    [
        pytest.param(None, None, "no source-model run", id="no-model"),
        pytest.param(
            AliasModel({BASELINE: "tree", CANDIDATE: "tree"}, (), evidence="native"),
            None,
            "not source-model",
            id="model-labelled-native",
        ),
        pytest.param(
            _model("elsewhere"), None, "the model ran elsewhere", id="other-source"
        ),
        pytest.param(
            _model(),
            FatalCalibration(None, ("no tracer",)),
            "no calibrated fatal",
            id="uncalibrated",
        ),
    ],
)
def test_the_composition_needs_the_model_and_the_calibration(model, calibration, why):
    ledger = _full_ledger()
    problems = r7_composition(
        model,
        ledger,
        revisions={name: "revision" for name in ("K0", "K1", "K2", "K3")},
        calibration=calibration or _calibration(),
        compare_to_direct=lambda direct, daemon: [],
    )
    assert any(why in p for p in problems), problems


def test_k3_worse_than_k1_on_the_whole_row_fails_as_a_shared_prefix_reading():
    ledger = _full_ledger()
    problems = _compose(
        ledger, compare_to_direct=lambda direct, daemon: ["o4_session: planted"]
    )
    assert any("whole row (shared prefix)" in p for p in problems)


# --- The actors' environment and the harness's own settling -----------------------------------


def test_the_scenario_reaches_every_actor_and_the_fault_only_an_overlay(tmp_path):
    base = {"BROWSER_IDLE_TIMEOUT": "20.0", r7_fault.FAULT_DIR_ENV: "/stale"}
    plain = r7_environment(base, fault_dir=None)
    overlay = r7_environment(base, fault_dir=tmp_path)
    assert plain["BROWSER_IDLE_TIMEOUT"] == overlay["BROWSER_IDLE_TIMEOUT"] == "0"
    assert r7_fault.FAULT_DIR_ENV not in plain
    assert overlay[r7_fault.FAULT_DIR_ENV] == str(tmp_path)


class _Lifetime:
    def __init__(self, created: float, *, dead: bool = True):
        self.created, self.dead = created, dead

    def create_time(self):
        return self.created

    def status(self):
        if self.dead:
            raise psutil.NoSuchProcess(1)
        return psutil.STATUS_RUNNING


def _no_such_process(pid: int):
    raise psutil.NoSuchProcess(pid)


@pytest.mark.parametrize(
    ("opener", "state"),
    [
        pytest.param(_no_such_process, "exited", id="no-such-process"),
        pytest.param(
            lambda pid: _Lifetime(99.0, dead=False), "exited", id="pid-reused"
        ),
        pytest.param(lambda pid: _Lifetime(5.0), "exited", id="the-lifetime-ended"),
        pytest.param(
            lambda pid: _Lifetime(5.0, dead=False), "still running", id="still-there"
        ),
    ],
)
def test_a_guardian_is_gone_only_as_the_lifetime_recorded(opener, state):
    records = [_start(7, 5.0, 1, "guardian")]
    assert harness.lifetime_exit_state(records, 7, 0.1, open_process=opener) == state


def test_a_guardian_nobody_recorded_is_unknown_not_gone():
    found = harness.lifetime_exit_state(
        [], 7, 0.1, open_process=lambda pid: _Lifetime(5.0)
    )
    assert found.startswith("unknown")


class _Owner:
    """The handle an owner was identified by: records every signal sent to it."""

    def __init__(self, *, running: bool, dies: bool = True):
        self.running, self.dies, self.killed = running, dies, 0

    def is_running(self):
        return self.running

    def status(self):
        if not self.running:
            raise psutil.NoSuchProcess(1)
        return psutil.STATUS_RUNNING

    def kill(self):
        self.killed += 1
        self.running = not self.dies


def _identity(handle: _Owner) -> harness.OwnerIdentity:
    return harness.OwnerIdentity(7, 5.0, "instance", "/auth", process=handle)


@pytest.mark.parametrize(
    ("handle", "result", "kills"),
    [
        pytest.param(_Owner(running=True), "stopped", 1, id="ended-after-measurement"),
        pytest.param(_Owner(running=False), "gone", 0, id="already-gone"),
    ],
)
def test_the_harness_ends_only_an_owner_still_running(
    monkeypatch, handle, result, kills
):
    monkeypatch.setattr(harness, "_OWNER_KILL_WAIT_SECONDS", 0.2)
    assert harness.end_owner(_identity(handle)) == result
    assert handle.killed == kills


def test_an_owner_that_will_not_die_is_not_called_ended(monkeypatch):
    monkeypatch.setattr(harness, "_OWNER_KILL_WAIT_SECONDS", 0.2)
    handle = _Owner(running=True, dies=False)
    assert harness.end_owner(_identity(handle)) == "still running"


@pytest.mark.parametrize(
    ("window", "daemon", "why"),
    [
        pytest.param(
            {"server_exit": "exited", "guardian_after_quit": "still running"},
            False,
            "guardian",
            id="direct-guardian-on",
        ),
        pytest.param(
            {
                "ended_by_harness": [
                    {
                        "who": "serving owner",
                        "result": "still running",
                        "guardian_exit": "exited",
                    }
                ]
            },
            True,
            "serving owner",
            id="owner-not-ended",
        ),
        pytest.param(
            {
                "ended_by_harness": [
                    {
                        "who": "serving owner",
                        "result": "stopped",
                        "guardian_exit": "unknown",
                    }
                ]
            },
            True,
            "guardian",
            id="its-guardian-unknown",
        ),
    ],
)
def test_anything_the_row_leaves_unsettled_is_named(window, daemon, why):
    problems = harness.r7_settled_problems(window, daemon=daemon)
    assert any(why in p for p in problems), problems


def test_a_settled_row_names_nothing():
    assert (
        harness.r7_settled_problems(
            {"server_exit": "exited", "guardian_after_quit": "exited"}, daemon=False
        )
        == []
    )
    assert (
        harness.r7_settled_problems(
            {
                "ended_by_harness": [
                    {
                        "who": "serving owner",
                        "result": "stopped",
                        "guardian_exit": "exited",
                    }
                ]
            },
            daemon=True,
        )
        == []
    )
