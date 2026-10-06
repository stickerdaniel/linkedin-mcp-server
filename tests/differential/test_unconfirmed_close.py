"""H-R7's own evidence, without a browser: what the native row will judge by.

* Clocks: a drain return is placed on strace's clock only where one offset
  fits every bracketed sample, and a traced line only on the side its whole
  microsecond lies; everything else is ambiguous.
* The lock: ``/proc/locks`` as proc(5) prints it, and the original actor
  shown holding the ``flock`` only when it is listed and holds a descriptor.
* Owned workers: a cancelled wait does not overlap the work it waited on, a
  worker past its bound stays owned and stops every later step, and every
  failure to settle the lease contender's helpers counts.
* The launch marker, read only from a browser the original actor launched,
  and early use of the profile ordered by creation, not by first sight.
* The calibration: its child and tracer ended on every path, and what it
  cannot show ended retained until it is.
* The phase, on a real transcript: the baseline owner's fatal own-group kill
  (arm64 CI run 36381619115, K2, verbatim lines) is K2's witness only in the
  calibrated shape and never the guardian's; any original-actor signal after
  the return, or an unplaceable one, fails K3; each call is placed by its
  entry and its bounded return.
* The gate each cell passes, the ledger, and the composition, in which only
  what the source assigns to the shared drain is set aside.
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
from types import SimpleNamespace
from typing import Any, cast

import psutil
import pytest

from differential import harness, lease_probe, r7_fault, unconfirmed_close
from differential.baseline import BASELINE_SHA, git
from differential.fault_overlay import FAULT_SHA256
from differential.signals import (
    COMPLETE,
    INCOMPLETE,
    OracleOutcome,
    ProcessHistory,
    derive_o2,
    read_trace,
)
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
    R7Setup,
    SharedReduction,
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
    shared_reduction,
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
    # The return known to within [.666149100, .666149900] seconds; a call
    # entered and returned within the one printed microsecond.
    boundary = (1790573446_666149_100, 1790573446_666149_900)
    assert place(t, t, boundary) == placement


@pytest.mark.parametrize(
    ("entry", "returned", "placement"),
    [
        pytest.param(10.0, 10.5, BEFORE, id="returned-before"),
        pytest.param(10.0, 12.0, AMBIGUOUS, id="crosses-the-return"),
        pytest.param(10.0, None, AMBIGUOUS, id="no-return-bound"),
        pytest.param(11.5, None, IN_PHASE, id="entered-after-without-a-return"),
        pytest.param(
            10.0, 10.999999, AMBIGUOUS, id="returned-in-the-return-microsecond"
        ),
        pytest.param(10.0, 10.999998, BEFORE, id="returned-the-microsecond-before"),
        pytest.param(11.000001, 12.0, IN_PHASE, id="entered-the-microsecond-after"),
    ],
)
def test_an_operation_is_placed_by_its_entry_and_its_bounded_return(
    entry, returned, placement
):
    # The drain returned within [10.999999100, 11.000000900] seconds.
    boundary = (10_999_999_100, 11_000_000_900)
    assert place(entry, returned, boundary) == placement


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
    monkeypatch.setattr(unconfirmed_close, "_RETAINED", [])
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


def _elected(browser_start: float | None, *, seen: float | None = None, parent=300):
    """The original owner 100; successor 300, elected after the close (9.0)
    and before the barrier (10.0); and a browser begun at *browser_start*,
    which the watcher first reported at *seen*."""
    records = [
        _start(100, 1.0, os.getpid(), "owner"),
        _start(300, 9.5, os.getpid(), "owner"),
    ]
    if browser_start is not None:
        browser = _start(400, browser_start, parent, "browser")
        records.append({**browser, "t": browser_start if seen is None else seen})
    return records


@pytest.mark.parametrize(
    ("records", "since", "why"),
    [
        pytest.param(
            _elected(9.95, seen=10.05),
            9.0,
            "began at 9.95",
            id="born-before-seen-after",
        ),
        pytest.param(_elected(10.2), 9.0, None, id="born-after-the-barrier"),
        pytest.param(
            _elected(9.95, parent=999), 9.0, "began at 9.95", id="ancestry-unknown"
        ),
        pytest.param(_elected(10.0), 9.0, "same clock tick", id="the-barriers-tick"),
        pytest.param(_elected(9.0), 9.0, "same clock tick", id="the-closes-tick"),
        pytest.param(_elected(None), 9.0, None, id="an-election-without-a-browser"),
        pytest.param(_elected(10.2), None, "not placed", id="the-close-unplaced"),
    ],
)
def test_early_use_is_ordered_by_creation_not_by_first_sight(records, since, why):
    problems = early_browsers(records, (100, 1.0), since=since, until=10.0)
    if why is None:
        assert problems == []
    else:
        assert len(problems) == 1 and why in problems[0], problems


def _accepted(window: dict, observed: list[dict]) -> R7Continuation:
    """The continuation the row builds once every actor has gone."""
    return harness.r7_continuation(
        R7Setup(None, False, 2),
        window=window,
        experiment="K3",
        run="run",
        daemon=True,
        identity={"head": "revision"},
        fault_dir=None,
        activation=None,
        runtime=harness.candidate_runtime(),
        env={},
        host=cast(Any, SimpleNamespace(tool=None)),
        vector=None,
        phase=None,
        validity=[],
        observed=observed,
    )


@pytest.mark.parametrize(
    ("browser_start", "early"),
    [
        pytest.param(9.95, True, id="born-before-the-barrier"),
        pytest.param(10.2, False, id="born-after-the-barrier"),
    ],
)
def test_a_browser_reported_after_the_barriers_look_still_decides_the_row(
    browser_start, early
):
    records = _elected(browser_start, seen=10.25)
    window = {"principal": [100, 1.0], "close_created": 9.0, "barrier_created": 10.0}
    # What the barrier's own look saw: the browser was not yet reported.
    assert early_browsers(records[:2], (100, 1.0), since=9.0, until=10.0) == []
    accepted = _accepted(window, records)
    assert bool(accepted.early_use) is early
    ledger = _full_ledger({("K3", 2): {"early_use": accepted.early_use}})
    problems = _compose(ledger)
    assert (
        any("K3 #2" in p and "before the recovery barrier" in p for p in problems)
        is early
    ), problems


def test_a_row_that_never_reached_its_barrier_is_not_asked_about_early_use():
    accepted = _accepted({"principal": [100, 1.0]}, _elected(9.95))
    assert accepted.early_use == ()


@pytest.mark.parametrize(
    ("browser_start", "served"),
    [
        pytest.param(9.95, False, id="begun-before-the-barrier"),
        pytest.param(10.2, True, id="begun-after-the-barrier"),
    ],
)
def test_only_a_browser_begun_after_the_barrier_serves_the_recovery(
    browser_start, served
):
    closing = harness.OwnerIdentity(100, 1.0, "first", "/auth", None)
    successor = harness.OwnerIdentity(300, 9.5, "second", "/auth", None)

    def after(boundary: float):
        # Kernel ticks in the row; creation times stand in for them here.
        return lambda pid, start: start > boundary

    problems = harness.successor_problems(
        _elected(browser_start, seen=10.25),
        closing,
        successor,
        probe=(10.1, 10.3),
        probe_requests=1,
        after_close=after(9.0),
        browser_after=after(10.0),
    )
    assert (problems == []) is served, problems
    if not served:
        assert any("begun after the recovery barrier" in p for p in problems)


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
#: The same lines without the owner's fatal own-group kill, which the
#: candidate's hard exit does not make: a K3 owner's transcript.
K3_TRACE = K2_TRACE.replace("13240 1790573446.666149 kill(-13240, SIGKILL) = ?\n", "")
#: After every line of either transcript.
LATE = (1790573448_000000_000, 1790573448_000001_000)


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


#: The probe child's pid in the calibration doubles, and what the tracer
#: writes for it: a fatal own-group kill, then its end.
PROBE = 424242
PROBE_TRACE = (
    f"{PROBE} 1790573446.666149 kill(-{PROBE}, SIGKILL) = ?\n"
    f"{PROBE} 1790573446.673977 +++ killed by SIGKILL +++\n"
)


class _Pipe:
    def __init__(self, child: _Child):
        self.child = child

    def write(self, data):
        if self.child.write_error is not None:
            raise self.child.write_error
        self.child.log.append("released")

    def close(self):
        self.child.log.append("stdin closed")
        # Released or not, the probe reads its end of line and kills itself.
        if self.child.kills_itself:
            self.child.returncode = -9


class _Child:
    """The probe child's ``Popen``. It kills its own group once its stdin
    closes, unless *kills_itself* is off; a kill ends it unless *dies* is off;
    *wait_error* is what its first wait raises."""

    pid = PROBE

    def __init__(
        self,
        log: list,
        *,
        write_error: BaseException | None = None,
        wait_error: BaseException | None = None,
        kills_itself: bool = True,
        dies: bool = True,
    ):
        self.log, self.write_error, self.wait_error = log, write_error, wait_error
        self.kills_itself, self.dies = kills_itself, dies
        self.returncode: int | None = None
        self.stdin = _Pipe(self)

    def poll(self):
        return self.returncode

    def kill(self):
        self.log.append("child killed")
        if self.dies:
            self.returncode = -9

    def wait(self, timeout=None):
        if self.wait_error is not None:
            error, self.wait_error = self.wait_error, None
            raise error
        if self.returncode is None:
            raise subprocess.TimeoutExpired("probe", timeout or 0)
        self.log.append("child reaped")
        return self.returncode


class _Tracer:
    """A tracer that exists from its ``start`` until something ends it.
    *start_error* is raised after it exists, *stop_error* by every stop, and
    with *ends* off nothing ends it."""

    available = True
    unavailable = None

    def __init__(
        self,
        log: list,
        out: Path,
        *,
        start_error: BaseException | None = None,
        stop_error: BaseException | None = None,
        ends: bool = True,
    ):
        self.log, self.out = log, out
        self.start_error, self.stop_error, self.ends = start_error, stop_error, ends
        self.running = False
        out.write_text(PROBE_TRACE)

    def start(self, pids):
        self.running = True
        self.log.append("tracer started")
        if self.start_error is not None:
            raise self.start_error
        return None

    def stop(self, *, seconds=30.0, confirmed_dead=()):
        self.log.append("tracer stopped")
        if self.stop_error is not None:
            raise self.stop_error
        if self.ends:
            self.running = False
        return OracleOutcome(
            status=COMPLETE, calls=read_trace(PROBE_TRACE).calls, traced=[PROBE]
        )

    def settled(self):
        return not self.running

    def end(self):
        self.log.append("tracer ended")
        if self.ends:
            self.running = False
        return not self.running


def _calibrate(monkeypatch, tmp_path, child: _Child, tracer: Any):
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: child)
    return calibrate_fatal_group(tmp_path, oracle=tracer, seconds=1, grace=0.1)


def test_a_calibration_releases_the_probe_only_once_traced_and_ends_everything(
    monkeypatch, tmp_path
):
    log: list = []
    tracer = _Tracer(log, tmp_path / "strace.txt")
    found = _calibrate(monkeypatch, tmp_path, _Child(log), tracer)
    assert found.problems == () and found.shape is not None
    assert log.index("tracer started") < log.index("released")
    assert "child reaped" in log and not tracer.running
    assert settlement_problems() == []


@pytest.mark.parametrize(
    ("child", "tracer", "raised"),
    [
        pytest.param(
            {},
            {"start_error": OSError("planted: the attach output was unread")},
            OSError,
            id="attach-read-failure-after-the-tracer-started",
        ),
        pytest.param(
            {"wait_error": KeyboardInterrupt()},
            {},
            KeyboardInterrupt,
            id="wait-interrupted",
        ),
        pytest.param(
            {}, {"stop_error": RuntimeError("planted")}, RuntimeError, id="stop-failed"
        ),
    ],
)
def test_a_failed_calibration_ends_its_child_and_tracer_before_raising(
    monkeypatch, tmp_path, child, tracer, raised
):
    log: list = []
    probe = _Child(log, kills_itself=False, **child)
    tracing = _Tracer(log, tmp_path / "strace.txt", **tracer)
    with pytest.raises(raised):
        _calibrate(monkeypatch, tmp_path, probe, tracing)
    assert "stdin closed" in log and "child reaped" in log
    assert probe.returncode is not None and not tracing.running
    # Everything shown ended, so nothing is retained and the next step runs.
    assert settlement_problems() == []


@pytest.mark.parametrize(
    ("child", "why"),
    [
        pytest.param(
            {"write_error": BrokenPipeError("planted")},
            "could not be released",
            id="release-failed",
        ),
        pytest.param(
            {"wait_error": subprocess.TimeoutExpired("probe", 1)},
            "did not end within",
            id="wait-timed-out",
        ),
    ],
)
def test_a_calibration_that_could_not_run_its_probe_has_no_shape(
    monkeypatch, tmp_path, child, why
):
    log: list = []
    tracer = _Tracer(log, tmp_path / "strace.txt")
    probe = _Child(log, **child)
    found = _calibrate(monkeypatch, tmp_path, probe, tracer)
    assert found.shape is None and any(why in p for p in found.problems)
    assert "stdin closed" in log and probe.returncode is not None
    assert not tracer.running
    assert settlement_problems() == []
    if "write_error" in child:
        # Its stdin closed at once, the probe ended by itself: no wait spent.
        assert not any("did not end within" in p for p in found.problems)


def test_a_retained_resource_whose_check_fails_stays_retained():
    def unanswered(grace):
        raise OSError("planted: the process table was unread")

    unconfirmed_close.retain("a planted helper", unanswered)
    for _ in range(2):
        problems = settlement_problems()
        assert any("a planted helper could not be checked" in p for p in problems)
    with pytest.raises(UnsettledWorker, match="a planted helper"):
        gate("the next measurement")


@pytest.mark.parametrize("left", ["child", "tracer"])
def test_what_a_calibration_cannot_end_refuses_every_later_step_until_it_goes(
    monkeypatch, tmp_path, left
):
    log: list = []
    child = _Child(log, kills_itself=left != "child", dies=left != "child")
    tracer = _Tracer(log, tmp_path / "strace.txt", ends=left != "tracer")
    with pytest.raises(UnsettledWorker, match="stays retained"):
        _calibrate(monkeypatch, tmp_path, child, tracer)
    named = "probe child" if left == "child" else "calibration's tracer"
    assert any(named in p for p in settlement_problems())
    with pytest.raises(UnsettledWorker, match=named):
        gate("the next measurement")
    # Settled later, the next measurement may start.
    child.dies, tracer.ends = True, True
    assert settlement_problems() == []
    gate("the next measurement")


def test_a_primary_failure_keeps_its_type_and_names_what_stays_retained(
    monkeypatch, tmp_path
):
    log: list = []
    child = _Child(log, kills_itself=False, dies=False)
    tracer = _Tracer(log, tmp_path / "strace.txt", start_error=OSError("planted"))
    with pytest.raises(OSError, match="planted") as raised:
        _calibrate(monkeypatch, tmp_path, child, tracer)
    assert any("probe child" in note for note in raised.value.__notes__)
    assert any("probe child" in p for p in settlement_problems())


class _StuckTracer:
    """A tracer process no wait ever sees end until *code* is set."""

    pid = 99_999_999

    def __init__(self):
        self.code: int | None = None

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        if self.code is None:
            raise subprocess.TimeoutExpired("strace", timeout or 0)
        return self.code


def test_a_returned_stop_is_not_the_tracer_ended(monkeypatch, tmp_path):
    # The real stop and its _end: each bounded wait times out and is
    # suppressed, and stop returns an outcome with strace still running.
    stuck = _StuckTracer()
    helpers: list = []

    def run(command, **kwargs):
        helpers.append(command)
        return SimpleNamespace(returncode=0)

    tracer = harness.SignalOracle(tmp_path, run=run)
    monkeypatch.setattr(tracer, "unavailable", None)

    def start(pids, **kwargs):
        tracer._process = cast(Any, stuck)
        tracer.pids = list(pids)
        return None

    monkeypatch.setattr(tracer, "start", start)
    log: list = []
    with pytest.raises(UnsettledWorker, match="calibration's tracer"):
        _calibrate(monkeypatch, tmp_path, _Child(log), tracer)
    assert ["sudo", "-n", "kill", "-KILL", str(stuck.pid)] in helpers
    with pytest.raises(UnsettledWorker):
        gate("the next measurement")
    stuck.code = 0
    gate("the next measurement")


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
        pytest.param(
            # Whatever shape was calibrated, a call shown returned before the
            # drain did is not after it.
            "13240 1790573446.666149 kill(-13240, SIGKILL) = 0 <0.000010>\n"
            "13240 1790573446.673977 +++ killed by SIGKILL +++\n",
            (1790573447_000000_000, 1790573447_000001_000),
            FatalCalibration({**(_calibration().shape or {}), "result": "0"}, ()),
            "was not traced killing its own group",
            id="returned-before-the-return-in-the-calibrated-shape",
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
    assert continuation_signals(_reading(K3_TRACE, boundary=LATE)) == []


def test_a_call_that_never_returned_is_not_shown_before_the_return():
    # The fatal kill is entered 1.3 s before this boundary and its caller
    # died at once, but a call that never returned has no return bound: the
    # death line is the recipient's, not the call's.
    problems = continuation_signals(_reading(boundary=LATE))
    assert problems == [
        "original actor 13240 sent SIGKILL by kill (ambiguous, no return)"
    ]


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


#: The drain returned within the printed microsecond 11.000000.
AT_ELEVEN = (11_000_000_000, 11_000_000_999)


@pytest.mark.parametrize(
    ("text", "placement"),
    [
        pytest.param(
            "13240 10.000000 kill(999, SIGTERM <unfinished ...>\n"
            "13240 12.000000 <... kill resumed>) = 0 <2.000000>\n",
            AMBIGUOUS,
            id="split-and-crossing",
        ),
        pytest.param(
            "13240 10.000000 kill(999, SIGTERM <unfinished ...>\n"
            "13240 10.500000 <... kill resumed>) = 0 <0.500000>\n",
            BEFORE,
            id="split-and-returned-before",
        ),
        pytest.param(
            # Its -T time says it returned at 10.00001; only the next line
            # bounds it, and that is after the return.
            "13240 10.000000 kill(999, SIGTERM) = 0 <0.000010>\n"
            "13266 12.000000 kill(-13272, 0) = -1 ESRCH (No such process) <0.000008>\n",
            AMBIGUOUS,
            id="unsplit-and-the-next-line-after",
        ),
        pytest.param(
            "13240 10.000000 kill(999, SIGTERM) = 0 <0.000010>\n"
            "13266 10.500000 kill(-13272, 0) = -1 ESRCH (No such process) <0.000008>\n",
            BEFORE,
            id="unsplit-and-the-next-line-before",
        ),
        pytest.param(
            "13240 10.000000 kill(999, SIGTERM) = 0 <0.000010>\n",
            AMBIGUOUS,
            id="unsplit-with-no-line-after",
        ),
        pytest.param(
            "13240 10.000000 kill(999, SIGTERM) = 0 <0.000010>\n"
            "13240 10.500000 +++ exited with 0 +++\n",
            BEFORE,
            id="bounded-by-an-end-line",
        ),
        pytest.param(
            "13240 10.000000 kill(999, SIGTERM <unfinished ...>\n"
            "13240 10.999999 <... kill resumed>) = 0 <0.999999>\n",
            BEFORE,
            id="returned-the-microsecond-before",
        ),
        pytest.param(
            "13240 10.000000 kill(999, SIGTERM <unfinished ...>\n"
            "13240 11.000000 <... kill resumed>) = 0 <1.000000>\n",
            AMBIGUOUS,
            id="returned-in-the-return-microsecond",
        ),
        pytest.param(
            "13240 11.000000 kill(999, SIGTERM) = 0 <0.000010>\n"
            "13240 11.500000 +++ exited with 0 +++\n",
            AMBIGUOUS,
            id="entered-in-the-return-microsecond",
        ),
        pytest.param(
            "13240 11.000001 kill(999, SIGTERM) = 0 <0.000010>\n",
            IN_PHASE,
            id="entered-the-microsecond-after",
        ),
        pytest.param(
            "13240 10.000000 kill(-13240, SIGKILL) = ?\n"
            "13240 10.000100 +++ killed by SIGKILL +++\n",
            AMBIGUOUS,
            id="never-returned",
        ),
    ],
)
def test_a_traced_signal_is_placed_by_its_whole_operation(text, placement):
    reading = _reading(text, AT_ELEVEN)
    (call,) = [
        c for c in reading.calls if c["sender"] == "original actor" and not c["probe"]
    ]
    assert call["placement"] == placement
    problems = continuation_signals(reading)
    if placement == BEFORE:
        assert problems == []
    else:
        assert problems and f"({placement}," in problems[0], problems


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        pytest.param(
            "7777 10.000000 kill(999, SIGTERM <unfinished ...>\n"
            "7777 12.000000 <... kill resumed>) = 0 <2.000000>\n",
            True,
            id="unplaced-and-crossing",
        ),
        pytest.param(
            "7777 10.000000 kill(999, SIGTERM <unfinished ...>\n"
            "7777 10.500000 <... kill resumed>) = 0 <0.500000>\n",
            False,
            id="unplaced-and-before",
        ),
    ],
)
def test_a_thread_nobody_owns_is_placed_like_any_sender(text, flagged):
    problems = continuation_signals(_reading(text, AT_ELEVEN))
    assert any("unplaced 7777" in p for p in problems) is flagged, problems


def test_an_own_group_kill_that_returned_is_not_the_fatal_witness():
    # An ordinary return, and the owner's own death by another's signal: in
    # the phase and on its own group, and still not the calibrated shape.
    text = (
        "13240 1790573446.666149 kill(-13240, SIGKILL) = 0 <0.000010>\n"
        "13240 1790573446.673977 +++ killed by SIGKILL +++\n"
    )
    found, problems = own_group_operations(_reading(text), _calibration())
    assert found == [] and any("was not traced killing" in p for p in problems)


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


def _close_path_model(
    baseline_driver: str,
    candidate_driver: str | None = None,
    *,
    path: str = CLOSE_PATH[1],
):
    files = {path: (_REPO / path).read_text(encoding="utf-8") for path in CLOSE_PATH}
    candidate = {**files, path: candidate_driver or files[path]}
    return alias_model(
        {CANDIDATE: Path(process_tree.__file__).read_text(encoding="utf-8")},
        close_path={
            CANDIDATE: candidate,
            BASELINE: {**files, path: baseline_driver},
        },
    )


def _changed(text: str, *edits: tuple[str, str]) -> str:
    """*text* with each edit made exactly once, where it is written once."""
    for old, new in edits:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


def test_a_baseline_close_path_unlike_the_candidates_fails_the_model():
    driver = (_REPO / CLOSE_PATH[1]).read_text(encoding="utf-8")
    assert _close_path_model(driver).problems == ()
    # A second binding of a global the close reads, past the first.
    other = _close_path_model(driver + "\n_browser_lifecycle_lock = None\n")
    assert any(CLOSE_PATH[1] in p for p in other.problems)


def test_a_close_path_differing_in_comments_or_docstrings_passes_the_model():
    driver = (_REPO / CLOSE_PATH[1]).read_text(encoding="utf-8")
    commented = driver + "\n# another close\n"
    module = driver.replace('"""\n', '"""\nAnother close.\n', 1)
    documented = _changed(
        module,
        ("Check whether startup", "Tell whether startup"),
        # A close root's own docstring, inside the slice compared.
        ("Close the browser, releasing", "Close the browser, then release"),
    )
    assert driver not in (commented, module) and module != documented
    assert _close_path_model(commented).problems == ()
    assert _close_path_model(documented).problems == ()


def test_a_coding_comment_that_decodes_into_code_fails_the_model():
    # In UTF-7 "+AAo-" is a newline, so the last line assigns when imported.
    driver = (_REPO / CLOSE_PATH[1]).read_text(encoding="utf-8")
    encoded = "# coding: utf-7\n" + driver + "\n# +AAo-_browser_lifecycle_lock = None\n"
    assert any(CLOSE_PATH[1] in p for p in _close_path_model(encoded).problems)


def test_a_change_the_close_runs_fails_the_model():
    driver = (_REPO / CLOSE_PATH[1]).read_text(encoding="utf-8")
    in_a_root = _changed(
        driver,
        (
            "    await _close_browser_locked()\n    return True\n",
            "    await _close_browser_locked()\n    return False\n",
        ),
    )
    # Reached only through ``_close_browser_locked``, never named as a root.
    in_a_helper = _changed(driver, ("        lease.mark_browser_closed()\n", ""))
    for changed in (in_a_root, in_a_helper):
        assert any(CLOSE_PATH[1] in p for p in _close_path_model(changed).problems)
    # The core is compared whole: even a method no close names counts.
    core = (_REPO / CLOSE_PATH[0]).read_text(encoding="utf-8")
    started = _changed(
        core,
        (
            '"Browser already started. Call close() first."',
            '"Browser already started."',
        ),
    )
    model = _close_path_model(started, path=CLOSE_PATH[0])
    assert any(CLOSE_PATH[0] in p for p in model.problems)
    # So does a function only another module calls: the driver's close runs
    # the primitive through ``linkedin_mcp_server.core``, whether or not the
    # core's own text still names it.
    unnamed = core.replace("await await_deferring_cancels(", "await _held_back(")
    assert unnamed.count("await _held_back(") == 3
    held = _changed(
        unnamed,
        (
            "                return task.result(), True\n",
            "                return task.result(), False\n",
        ),
    )
    model = _close_path_model(unnamed, candidate_driver=held, path=CLOSE_PATH[0])
    assert any(CLOSE_PATH[0] in p for p in model.problems)


def test_a_changed_import_the_close_uses_fails_the_model():
    driver = (_REPO / CLOSE_PATH[1]).read_text(encoding="utf-8")
    moved = _changed(
        driver,
        ("    release_browser_guardian,\n", ""),
        (
            "from linkedin_mcp_server.profile_lease import",
            "from linkedin_mcp_server.guardian import release_browser_guardian\n"
            "from linkedin_mcp_server.profile_lease import",
        ),
    )
    # The idle close reads ``time.monotonic()``.
    swapped = _changed(driver, ("import time\n", "import trio as time\n"))
    for changed in (moved, swapped):
        assert any(CLOSE_PATH[1] in p for p in _close_path_model(changed).problems)
    core = (_REPO / CLOSE_PATH[0]).read_text(encoding="utf-8")
    renamed = _changed(
        core,
        (
            "    drain_browser_process_marker,\n    forget_browser_process_marker,\n",
            "    drain_marked_groups as drain_browser_process_marker,\n"
            "    forget_browser_process_marker,\n",
        ),
    )
    model = _close_path_model(renamed, path=CLOSE_PATH[0])
    assert any(CLOSE_PATH[0] in p for p in model.problems)


def test_a_change_the_close_never_reaches_passes_the_model():
    # A feed-check change: the check rewritten, a helper for it, and the
    # names it uses added to the existing ``linkedin_mcp_server.core`` line.
    driver = (_REPO / CLOSE_PATH[1]).read_text(encoding="utf-8")
    feed = _changed(
        driver,
        (
            "    is_logged_in,\n",
            "    is_another_site,\n    is_logged_in,\n    refuse_off_linkedin,\n",
        ),
        (
            "async def _feed_auth_succeeds(",
            "async def _refuse_a_landing(browser: BrowserManager) -> None:\n"
            "    if is_another_site(browser.page.url):\n"
            "        refuse_off_linkedin(browser.page.url)\n\n\n"
            "async def _feed_auth_succeeds(",
        ),
        (
            '        await stabilize_navigation("feed navigation", logger)\n',
            '        await stabilize_navigation("feed navigation", logger)\n'
            "        await _refuse_a_landing(browser)\n",
        ),
    )
    assert _close_path_model(feed).problems == ()
    assert _close_path_model(driver, candidate_driver=feed).problems == ()


#: A close that skips the drain, installed when the module is imported by a
#: statement whose own name nothing reads.
_IMPORT_HOOK = """
async def _fast_close_for_registration(self):
    return True

_close_registration = BrowserManager.close = _fast_close_for_registration
"""


def test_a_hook_installed_at_import_fails_the_model():
    for path in CLOSE_PATH[:2]:
        text = (_REPO / path).read_text(encoding="utf-8")
        model = _close_path_model(text + _IMPORT_HOOK, path=path)
        assert any(path in p for p in model.problems), path


def test_a_module_imported_for_its_effects_fails_the_model():
    # Importing a module runs it, and it can replace the close from there,
    # whether or not anything here uses the name it binds.
    driver = (_REPO / CLOSE_PATH[1]).read_text(encoding="utf-8")
    for line in (
        "import r7_close_registration\n",
        "import r7_close_registration as _unused\n",
        "from r7_close_registration import registration\n",
        "from . import r7_close_registration\n",
    ):
        model = _close_path_model(driver + "\n" + line)
        assert any(CLOSE_PATH[1] in p for p in model.problems), line


def test_a_module_hook_python_calls_by_itself_fails_the_model():
    # A ``from`` import of this module asks its ``__getattr__`` for
    # ``__path__``, with no name here ever calling it.
    driver = (_REPO / CLOSE_PATH[1]).read_text(encoding="utf-8")
    getattr_hook = (
        "\n\nasync def _fast_close_for_registration(self):\n    return True\n\n\n"
        "def __getattr__(name):\n"
        "    BrowserManager.close = _fast_close_for_registration\n"
        "    raise AttributeError(name)\n"
    )
    # Or one taken onto a line that already imports the module, so only the
    # name is new.
    imported = _changed(
        driver, ("    is_logged_in,\n", "    is_logged_in,\n    __getattr__,\n")
    )
    for changed in (driver + getattr_hook, imported):
        model = _close_path_model(changed)
        assert any(CLOSE_PATH[1] in p for p in model.problems)


def test_a_definition_the_close_never_calls_that_runs_at_import_fails_the_model():
    driver = (_REPO / CLOSE_PATH[1]).read_text(encoding="utf-8")
    install = (
        "def _install(function=None):\n"
        "    BrowserManager.close = _fast_close_for_registration\n"
        "    return function\n\n\n"
        "async def _fast_close_for_registration(self):\n"
        "    return True\n\n\n"
        "async def _feed_auth_succeeds("
    )
    feed = ("async def _feed_auth_succeeds(", install)
    # Defined and never called, the hook is two more functions nobody runs.
    assert _close_path_model(_changed(driver, feed)).problems == ()
    decorated = _changed(
        driver,
        feed,
        ("async def _feed_auth_succeeds(", "@_install\nasync def _feed_auth_succeeds("),
    )
    defaulted = _changed(
        driver,
        feed,
        (
            "    allow_remember_me: bool = True,\n",
            "    allow_remember_me=_install(),\n",
        ),
    )
    annotated = _changed(driver, feed, ("\n) -> bool:\n", "\n) -> _install():\n"))
    for changed in (decorated, defaulted, annotated):
        assert any(CLOSE_PATH[1] in p for p in _close_path_model(changed).problems)


def test_a_hook_on_the_manager_that_no_name_reaches_fails_the_model():
    core = (_REPO / CLOSE_PATH[0]).read_text(encoding="utf-8")
    hooked = _changed(
        core,
        (
            "    def __init__(\n",
            "    def __getattribute__(self, name):\n"
            '        if name == "_close_proven":\n'
            "            return True\n"
            "        return object.__getattribute__(self, name)\n\n"
            "    def __init__(\n",
        ),
    )
    model = _close_path_model(hooked, path=CLOSE_PATH[0])
    assert any(CLOSE_PATH[0] in p for p in model.problems)


def test_a_function_the_close_finds_by_its_name_fails_the_model():
    driver = (_REPO / CLOSE_PATH[1]).read_text(encoding="utf-8")
    by_name = _changed(
        driver,
        (
            "    await _close_browser_locked()\n    return True\n",
            '    await globals()["_close_later"]()\n    return True\n',
        ),
    )
    later = "\n\nasync def _close_later():\n    await _close_browser_locked()\n"
    skipped = "\n\nasync def _close_later():\n    return None\n"
    model = _close_path_model(by_name + later, candidate_driver=by_name + skipped)
    assert any(CLOSE_PATH[1] in p for p in model.problems)


def test_a_close_root_gone_fails_the_model_on_either_side_or_both():
    driver = (_REPO / CLOSE_PATH[1]).read_text(encoding="utf-8")
    # Renamed with its one caller, so the rest of the slice reads the same.
    renamed = _changed(
        driver,
        (
            "async def _close_browser_if_still_idle(",
            "async def _close_browser_when_idle(",
        ),
        (
            "_run_deferring_cancels(_close_browser_if_still_idle())",
            "_run_deferring_cancels(_close_browser_when_idle())",
        ),
    )
    gone = "has no close root _close_browser_if_still_idle"
    for model in (
        _close_path_model(renamed),
        _close_path_model(driver, candidate_driver=renamed),
        _close_path_model(renamed, candidate_driver=renamed),
    ):
        assert any(CLOSE_PATH[1] in p and gone in p for p in model.problems)


def test_a_close_path_that_does_not_parse_fails_the_model():
    broken = (_REPO / CLOSE_PATH[1]).read_text(encoding="utf-8") + "\ndef (\n"
    model = _close_path_model(broken, candidate_driver=broken)
    assert any(CLOSE_PATH[1] in p for p in model.problems)


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
        reading = _reading(K3_TRACE, boundary=LATE)
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


def test_a_vector_difference_no_traced_call_accounts_for_is_a_difference():
    # The routine drain's class label and an unknown recipient, with no
    # transcript that places either in that drain: not timing, a difference.
    later = _Vector(o2_traced="unknown", signal_classes=("owner:browser-group",))
    ledger = _full_ledger({("K3", 2): {"vector": later}})
    problems = _compose(ledger)
    assert any("repetition 2 reads unlike" in p and "vector" in p for p in problems)


# --- What the shared drain's timing may decide, and nothing else --------------------------

BROWSER, OTHER_LAUNCH, CHILD = 15042, 17000, 16000


def _launch(*, marker_seen_at: float = 2.0) -> list[dict]:
    """The watcher's records: the owner, its guardian, a browser of the
    original launch in a group of its own, a child of the owner that is not
    a browser, and a browser of another launch (another marker)."""
    return [
        {"kind": "watcher.ready", "baseline_pgids": [1]},
        _start(OWNER, 1.0, os.getpid(), "owner"),
        _start(GUARDIAN, 1.1, OWNER, "guardian"),
        {
            **_start(BROWSER, 2.0, OWNER, "browser", browser_marker=DIGEST),
            "t": marker_seen_at,
        },
        _start(CHILD, 2.5, OWNER, "driver"),
        _start(OTHER_LAUNCH, 3.0, OWNER, "browser", browser_marker="f" * 16),
    ]


#: A group the browser was read in before anything read its marker.
EARLIER_GROUP, LATER_GROUP = 999, 16500


def _regrouped(*, marked_first: bool) -> list[dict]:
    """``_launch``, with the browser read in two groups in turn. Marked first,
    it carries the marker into its second group (``LATER_GROUP``), as the
    watcher carries it; otherwise it is read in ``EARLIER_GROUP`` without
    one, and its marker is read only once it has moved to its own group."""
    records = _launch()
    browser = next(r for r in records if r.get("pid") == BROWSER)
    if marked_first:
        update = {**browser, "kind": "process.update", "t": 3.0, "pgid": LATER_GROUP}
    else:
        browser.pop("browser_marker")
        browser["pgid"] = EARLIER_GROUP
        update = {**browser, "kind": "process.update", "t": 3.0, "pgid": BROWSER}
        update["browser_marker"] = DIGEST
    records.append(update)
    return records


#: The owner's routine drain after the close: a probe that reaches nobody,
#: and, when a Chromium helper outlived the graceful close, the kill of its
#: group before that (``process_tree._kill_marked_process_groups``).
QUIET = (
    "13240 1790573446.666085 kill(-15042, 0) = -1 ESRCH (No such process) <0.000014>\n"
    "13240 1790573446.700000 +++ exited with 0 +++\n"
)
DRAIN = "13240 1790573446.601691 kill(-15042, SIGKILL) = 0 <0.000595>\n"
#: The drain's return: after its kill, and bounded by the probe's line.
DRAIN_RETURNED = (1790573446_680000_000, 1790573446_680001_000)


def _row_vector(mode: str, **changes: Any) -> harness.RowVector:
    return harness.RowVector(
        mode=mode,
        o1_single_browser=True,
        browser_seen=True,
        watcher_healthy=True,
        o4_session="retained",
        origin_saw_feed=True,
        feed_carried_session=True,
        tool_succeeded=True,
        owner_published=mode == "daemon",
        fell_back=False,
        host_exit_clean=True,
        cleanup_clean=True,
        owner_launched=mode == "daemon",
        owner_start_attempted=mode == "daemon",
        o2_required=True,
        oracle_collection=COMPLETE,
        **changes,
    )


def _read_row(text: str, *, records: list[dict] | None = None, boundary: Any):
    """What a row reads of one transcript, through the real parser, O2 and
    phase reading: the phase, the row vector's O2, and what of that O2 the
    shared drain accounts for."""
    history = ProcessHistory(
        _launch() if records is None else records, outside=[os.getpid()]
    )
    trace = read_trace(text)
    outcome = OracleOutcome(
        status=COMPLETE,
        calls=trace.calls,
        traced=[OWNER, GUARDIAN],
        cohort={
            OWNER: {"kind": "root", "process": OWNER},
            GUARDIAN: {"kind": "root", "process": GUARDIAN},
            CHILD: {"kind": "child", "process": CHILD},
        },
        reasons=list(trace.problems),
    )
    o2 = derive_o2(outcome, history)
    phase = read_phase(
        outcome,
        text,
        owner=OWNER,
        guardian=GUARDIAN,
        owner_group=GROUP,
        boundary=boundary,
        history=history,
        marker=DIGEST,
    )
    return {
        "phase": phase,
        "vector": _row_vector("daemon", o2_traced=o2.state, signal_classes=o2.classes),
        "shared": shared_reduction(o2.resolved, len(o2.unknowns), phase),
    }


#: Per experiment: the cells read from the first transcript, the one read
#: from the second, and the problem that names a difference between them.
_WHERE = {
    "K0": ([("K0", UNSHIMMED)], ("K0", INERT), "inert overlay differs"),
    "K3": (
        [("K3", "1"), ("K3", "3")],
        ("K3", "2"),
        "repetition 2 reads unlike repetition 1",
    ),
}


def _ledger_of(experiment: str, first: dict, second: dict) -> R7Ledger:
    """A full ledger, every cell with a real row vector, and *experiment*'s
    cells given the readings *first* and *second*."""
    ledger = _full_ledger()
    for key, cell in list(ledger._cells.items()):
        mode = "direct" if key[0] == "K1" else "daemon"
        ledger._cells[key] = replace(cell, vector=_row_vector(mode, o2_traced="held"))
    ones, two, _ = _WHERE[experiment]
    for key in ones:
        ledger._cells[key] = replace(ledger._cells[key], **first)
    ledger._cells[two] = replace(ledger._cells[two], **second)
    return ledger


def _composed(experiment: str, first: str, second: str, **reading: Any) -> list[str]:
    """The final composition, *experiment*'s cells read from the transcripts
    *first* and *second*, against the real whole-row comparison."""
    # A control has no selected drain, so no boundary: nothing is placed.
    boundary = (
        "no real drain return was published" if experiment == "K0" else DRAIN_RETURNED
    )
    ledger = _ledger_of(
        experiment,
        _read_row(first, boundary=boundary),
        _read_row(second, boundary=boundary, **reading),
    )
    return _compose(ledger, compare_to_direct=harness.compare_to_direct)


@pytest.mark.parametrize("experiment", ["K0", "K3"])
def test_a_helper_the_shared_drain_killed_on_one_run_is_no_difference(experiment):
    # One run's graceful close left a Chromium helper for the drain, whose
    # recipient the watcher could not pin: the drain's class and its unknown
    # recipient are set aside, since the source places that call in it.
    reading = _read_row(DRAIN + QUIET, boundary=DRAIN_RETURNED)
    assert reading["shared"] == SharedReduction(("owner:browser-group",), True)
    assert reading["vector"].o2_traced == "unknown"
    assert _composed(experiment, QUIET, DRAIN + QUIET) == []


@pytest.mark.parametrize("experiment", ["K0", "K3"])
@pytest.mark.parametrize(
    ("group", "marked_first"),
    [
        pytest.param(LATER_GROUP, True, id="a-group-it-moved-to-marked"),
        pytest.param(BROWSER, False, id="the-group-its-marker-was-read-in"),
    ],
)
def test_a_group_read_together_with_the_marker_is_the_drains(
    experiment, group, marked_first
):
    # Whichever group the browser moved through, one read in the same record
    # as the launch's marker is the drain's target, unknown recipients and all.
    drain = f"13240 1790573446.601691 kill(-{group}, SIGKILL) = 0 <0.000595>\n"
    records = _regrouped(marked_first=marked_first)
    reading = _read_row(drain + QUIET, records=records, boundary=DRAIN_RETURNED)
    assert reading["shared"] == SharedReduction(("owner:browser-group",), True)
    assert _composed(experiment, QUIET, drain + QUIET, records=records) == []


@pytest.mark.parametrize("experiment", ["K0", "K3"])
@pytest.mark.parametrize(
    ("second", "reading", "why"),
    [
        pytest.param(
            "13240 1790573446.601691 kill(-999, SIGTERM) = 0 <0.000010>\n" + QUIET,
            {},
            "original actor:kill:SIGTERM:another group",
            id="an-inert-only-operation",
        ),
        pytest.param(
            "13240 1790573446.601691 kill(-15042, SIGTERM) = 0 <0.000010>\n" + QUIET,
            {},
            "original actor:kill:SIGTERM:a group of the original launch",
            id="the-drains-target-but-another-signal",
        ),
        pytest.param(
            "16000 1790573446.601691 kill(-15042, SIGKILL) = 0 <0.000595>\n" + QUIET,
            {},
            "descendant:kill:SIGKILL",
            id="the-drains-call-from-another-sender",
        ),
        pytest.param(
            "13240 1790573446.601691 kill(-17000, SIGKILL) = 0 <0.000595>\n" + QUIET,
            {},
            "original actor:kill:SIGKILL:another group",
            id="the-same-class-on-another-launchs-group",
        ),
        pytest.param(
            DRAIN + QUIET,
            {"records": _launch(marker_seen_at=1790573447.0)},
            "original actor:kill:SIGKILL:another group",
            id="a-marker-read-only-after-the-call",
        ),
        pytest.param(
            DRAIN
            + "13240 1790573446.601700 kill(-999, SIGTERM) = 0 <0.000010>\n"
            + QUIET,
            {},
            "'o2_traced': 'unknown'",
            id="a-shared-unknown-beside-an-unassigned-one",
        ),
        pytest.param(
            DRAIN
            + "7777 1790573446.601700 kill(-999, SIGTERM) = 0 <0.000010>\n"
            + QUIET,
            {},
            "'o2_traced': 'unknown'",
            id="a-shared-unknown-beside-one-no-call-explains",
        ),
        pytest.param(
            DRAIN
            + "13240 1790573446.601700 kill(-17000, SIGKILL) = 0 <0.000595>\n"
            + QUIET,
            {},
            "'signal_classes': ['owner:browser-group']",
            id="the-drains-class-also-sent-elsewhere",
        ),
        pytest.param(
            f"13240 1790573446.601691 kill(-{EARLIER_GROUP}, SIGKILL) = 0 <0.000010>\n"
            + QUIET,
            {"records": _regrouped(marked_first=False)},
            "original actor:kill:SIGKILL:another group",
            id="a-group-left-before-its-marker-was-read",
        ),
    ],
)
def test_an_operation_the_source_does_not_assign_to_the_drain_is_a_difference(
    experiment, second, reading, why
):
    problems = _composed(experiment, QUIET, second, **reading)
    label = _WHERE[experiment][2]
    assert any(label in p and why in p for p in problems), problems


def test_a_class_no_call_of_the_drain_accounts_for_is_a_difference():
    drained = _read_row(DRAIN + QUIET, boundary=DRAIN_RETURNED)
    vector = replace(
        drained["vector"],
        signal_classes=(*drained["vector"].signal_classes, "guardian:browser"),
    )
    ledger = _ledger_of(
        "K3",
        _read_row(QUIET, boundary=DRAIN_RETURNED),
        {**drained, "vector": vector},
    )
    problems = _compose(ledger, compare_to_direct=harness.compare_to_direct)
    assert any("reads unlike" in p and "guardian:browser" in p for p in problems)


@pytest.mark.parametrize(
    "shared",
    [
        pytest.param(SharedReduction(), id="nothing-shared"),
        pytest.param(SharedReduction((), True), id="every-unknown-shared"),
    ],
)
def test_a_violation_in_one_repetition_is_a_difference(shared):
    ledger = _full_ledger(
        {("K3", 2): {"vector": _Vector(o2_traced="violated"), "shared": shared}}
    )
    assert any("reads unlike" in p and "'violated'" in p for p in _compose(ledger))


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
