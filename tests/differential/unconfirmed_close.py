"""Row H-R7's own evidence: a close the product cannot confirm, by a declared fault.

**The setup.** Every actor starts from a fault overlay (``fault_overlay``) of
its runtime, with ``BROWSER_IDLE_TIMEOUT=0`` from startup (E1EZ-01). After the
first read the harness identifies the original actor (the Direct server or
the owner), its guardian, the browser's launch marker (kept in memory, only
its digest written) and the profile lock, starts the external trace, and only
then publishes the activation (``publish_activation``) and sends
``close_session``. The fault hands that one close's real True back as False,
so the product proceeds as if the browser had not gone.

**Three kinds of evidence, never added up.** The *source model*
(``alias_model``) runs the declared fault against each runtime's exact
``process_tree`` in this process. The *native continuation*
(``R7Continuation``) is what the row observed of the actors: the selected
call and its consumption, the lease checkpoints, the guardian, the recovery.
The *phase reading* (``read_phase``) is the original actor's traced signals
after the real drain returned; the whole row's O2 stays the vector's, a
shared prefix read separately.

**Clocks.** The fault dates the real drain's return on the monotonic clock,
strace dates every line on the realtime one. The harness samples both,
bracketing each read (``clock_sample``), before the trace, after the close
and after the trace. One offset must fit every sample, or the realtime clock
stepped and nothing is placed. A traced call is in the phase only when its
earliest possible time is after the latest possible return, before it only
when its latest possible time is before the earliest; anything else is
ambiguous, never resolved by a tolerance.

**Workers and helpers stay owned.** Every blocking step runs on a thread the
row owns (``run_owned``), and the lease contender's helper is owned by
``lease_probe``. ``gate`` refuses the next measurement while either is not
shown finished, whatever the failure to settle was.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psutil

from differential import lease_probe, r7_fault
from differential.fault_overlay import (
    FAULT_SHA256,
    SCENARIO,
    Overlay,
    publish_activation,
)
from differential.signals import (
    COMPLETE,
    OracleOutcome,
    ProcessHistory,
    SignalCall,
    SignalOracle,
)
from differential.signals import HELD as HELD_O2
from differential.signals import UNKNOWN as UNKNOWN_O2
from differential.watcher import BROWSER_MARKER_ENV

ROW_H_R7 = "H-R7"
LOCK_FILE = "profile.lock"
REPETITIONS = (1, 2, 3)

#: The unshimmed control runs the plain runtime, the inert one the overlay
#: with its fault armed and never activated.
UNSHIMMED = "unshimmed"
INERT = "inert"

NATIVE = "native"
SOURCE_MODEL = "source-model"
#: The calibration of a fatal own-group call: a product-free child traced
#: exactly as the rows trace their actors.
NATIVE_PROBE = "native probe"

#: Where a traced call falls against the real drain's return.
BEFORE = "before"
IN_PHASE = "in phase"
AMBIGUOUS = "ambiguous"

#: Who made a traced call.
OWNER = "original actor"
GUARDIAN = "guardian"
DESCENDANT = "descendant"
UNPLACED = "unplaced"

#: No recovery is made after a Direct close: the host's quit is Direct's
#: settlement, and the owner's automatic exit is equated with it.
NO_RECOVERY = "none: Direct keeps the profile until host quit"
POST_SETTLEMENT = "post-settlement"

_START_TOLERANCE_SECONDS = 0.01


@dataclass(frozen=True)
class R7Setup:
    """One H-R7 execution: the overlay its actors start from (None for the
    unshimmed control), whether the fault is activated, and which repetition
    or control it is."""

    overlay: Overlay | None
    activate: bool
    repetition: int
    control: str | None = None


def r7_environment(
    environment: Mapping[str, str], *, fault_dir: Path | None
) -> dict[str, str]:
    """The row's actor environment: the scenario from actor startup, and the
    fault's directory only for an overlay."""
    env = dict(environment)
    env.pop(r7_fault.FAULT_DIR_ENV, None)
    env.update(SCENARIO)
    if fault_dir is not None:
        env[r7_fault.FAULT_DIR_ENV] = str(fault_dir)
    return env


# --- Clocks ---------------------------------------------------------------------


@dataclass(frozen=True)
class ClockSample:
    """One realtime read bracketed by two monotonic reads, in nanoseconds."""

    label: str
    before_ns: int
    realtime_ns: int
    after_ns: int

    @property
    def offset(self) -> tuple[int, int]:
        """Realtime minus monotonic at the read, as far as the bracket says."""
        return (self.realtime_ns - self.after_ns, self.realtime_ns - self.before_ns)


def clock_sample(
    label: str,
    *,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    realtime_ns: Callable[[], int] = time.time_ns,
    reads: int = 5,
) -> ClockSample:
    """The narrowest of *reads* brackets."""
    best: ClockSample | None = None
    for _ in range(reads):
        before = monotonic_ns()
        real = realtime_ns()
        after = monotonic_ns()
        sample = ClockSample(label, before, real, after)
        if best is None or after - before < best.after_ns - best.before_ns:
            best = sample
    assert best is not None
    return best


def realtime_interval(
    monotonic_ns: int, samples: Sequence[ClockSample]
) -> tuple[int, int] | str:
    """Where *monotonic_ns* falls on the realtime clock, or why it cannot be said.

    Both clocks are slewed alike, so their difference changes only when the
    realtime clock steps. Every sample then brackets that one difference; if
    no difference fits all of them, the clock stepped. The time placed must
    lie between the first and the last sample, where that holds.
    """
    if len(samples) < 2:
        return "the clocks were not sampled on both sides of the phase"
    if not samples[0].after_ns <= monotonic_ns <= samples[-1].before_ns:
        return (
            f"the drain's return at {monotonic_ns} is not between the first and the "
            f"last clock sample"
        )
    low = max(sample.offset[0] for sample in samples)
    high = min(sample.offset[1] for sample in samples)
    if low > high:
        return (
            "the realtime clock stepped against the monotonic one between the clock "
            "samples, so the drain's return cannot be placed on strace's clock"
        )
    return (monotonic_ns + low, monotonic_ns + high)


def published_return(directory: Path | None) -> int | None:
    """When the selected real drain returned, on the monotonic clock, as the
    fault published it; None when it published no complete outcome."""
    if directory is None:
        return None
    try:
        outcome = json.loads((directory / r7_fault.OUTCOME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = outcome.get("returned_ns") if isinstance(outcome, dict) else None
    return value if type(value) is int else None


def place(t: float, interval: tuple[int, int]) -> str:
    """A traced line's time (strace's microseconds) against *interval*."""
    earliest = round(t * 1_000_000) * 1000
    latest = earliest + 999
    low, high = interval
    if earliest > high:
        return IN_PHASE
    if latest < low:
        return BEFORE
    return AMBIGUOUS


# --- The profile lock -------------------------------------------------------------


def lock_identity(path: Path) -> tuple[int, int] | None:
    """The lock file's device and inode, never following a link; None if absent."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return (info.st_dev, info.st_ino)


def parse_proc_locks(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """The locks ``/proc/locks`` lists, as proc(5) documents its lines.

    ``N: KIND ADVISORY|MANDATORY MODE PID MAJOR:MINOR:INODE START END``, with
    the device numbers in hexadecimal. A line with ``->`` is a waiter blocked
    on the lock above it, which holds nothing.
    """
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    for line in text.splitlines():
        tokens = line.split()
        if not tokens:
            continue
        if len(tokens) > 1 and tokens[1] == "->":
            continue
        try:
            major, minor, inode = tokens[5].split(":")
            entries.append(
                {
                    "kind": tokens[1],
                    "mode": tokens[3],
                    "pid": int(tokens[4]),
                    "device": (int(major, 16), int(minor, 16)),
                    "inode": int(inode),
                }
            )
        except (IndexError, ValueError):
            problems.append(f"an unreadable /proc/locks line: {line[:200]!r}")
    return entries, problems


def lock_association(
    identity: tuple[int, int] | None,
    holder: int | None,
    *,
    locks: Path = Path("/proc/locks"),
    proc: Path = Path("/proc"),
) -> dict[str, Any]:
    """Whether *holder* holds the exclusive ``flock`` on the lock file.

    Both halves, since neither says it alone: the kernel lists an exclusive
    ``FLOCK`` on that device and inode taken by *holder*, and *holder* still
    has a descriptor open on it. An open descriptor is no lock, and a listed
    pid is who took the lock, not who has it now. Unread is unknown.
    """
    if identity is None or holder is None:
        return {"state": UNKNOWN_STATE, "reason": "no lock or no holder to ask about"}
    try:
        text = locks.read_text()
    except OSError as exc:
        return {"state": UNKNOWN_STATE, "reason": f"/proc/locks unread: {exc!r}"}
    entries, problems = parse_proc_locks(text)
    device = (os.major(identity[0]), os.minor(identity[0]))
    pids = sorted(
        entry["pid"]
        for entry in entries
        if entry["kind"] == "FLOCK"
        and entry["mode"] == "WRITE"
        and entry["device"] == device
        and entry["inode"] == identity[1]
    )
    try:
        descriptors = list((proc / str(holder) / "fd").iterdir())
    except OSError as exc:
        return {
            "state": UNKNOWN_STATE,
            "reason": f"pid {holder}'s descriptors unread: {exc!r}",
            "flock_pids": pids,
        }
    opened = False
    for descriptor in descriptors:
        try:
            info = os.stat(descriptor)
        except OSError:
            continue
        if (info.st_dev, info.st_ino) == identity:
            opened = True
            break
    if problems:
        state = UNKNOWN_STATE
    elif holder in pids and opened:
        state = HOLDER
    else:
        state = "not the holder"
    return {
        "state": state,
        "flock_pids": pids,
        "open": opened,
        "problems": problems,
    }


UNKNOWN_STATE = "unknown"
HOLDER = "holder"


def checkpoint_problems(
    point: Mapping[str, Any], *, expect: str, holder: bool = False
) -> list[str]:
    """Why a lease checkpoint is not the one expected.

    The contender's answer, on the lock file the row identified, by the same
    device and inode before the checkpoint and in the contender's own open;
    for a held checkpoint the positive association with the original actor;
    and each process the checkpoint says must be alive or gone.
    """
    label = point.get("label")
    problems = []
    if point.get("error"):
        problems.append(f"{label}: the contender failed: {point['error']}")
    if point.get("state") != expect:
        problems.append(
            f"{label}: the lock was {point.get('state')!r}, not {expect!r} "
            f"({point.get('reason')})"
        )
    if point.get("same_lock") is not True:
        problems.append(f"{label}: the lock asked about is not the one identified")
    if holder and (point.get("association") or {}).get("state") != HOLDER:
        problems.append(
            f"{label}: the original actor is not shown holding it: "
            f"{point.get('association')}"
        )
    words = {True: "alive", False: "gone", None: "unknown"}
    for name, wanted in (point.get("expect_alive") or {}).items():
        seen = (point.get("alive") or {}).get(name)
        if seen is not wanted:
            problems.append(
                f"{label}: {name} was {words.get(seen, repr(seen))}, not "
                f"{words[bool(wanted)]}"
            )
    return problems


# --- Owned workers ------------------------------------------------------------------


class UnsettledWorker(RuntimeError):
    """A worker or helper the row started is not shown finished: nothing more
    is measured until it is."""


class WorkerFailed(RuntimeError):
    """A worker ended in something other than an ordinary exception."""


@dataclass(eq=False)
class _Worker:
    label: str
    done: threading.Event = field(default_factory=threading.Event)
    outcome: tuple[str, Any] | None = None


#: Every worker started in this process and not yet shown finished, whichever
#: row started it: a later row refuses to start while one runs.
_OWNED: list[_Worker] = []


def running_workers() -> list[str]:
    _OWNED[:] = [worker for worker in _OWNED if not worker.done.is_set()]
    return [worker.label for worker in _OWNED]


def settlement_problems(grace: float = 5.0) -> list[str]:
    """Why the next step may not start: a worker still running, or the lease
    contender's helpers not settled. Every way ``settle`` can fail counts,
    an interrupt included; none of them shows the helper gone."""
    problems = [f"worker {label!r} is still running" for label in running_workers()]
    try:
        lease_probe.settle(grace)
    except BaseException as exc:  # noqa: BLE001 - a failed settlement, whatever it was
        problems.append(f"the lease contender's helpers are not settled: {exc!r}")
    return problems


def gate(label: str) -> None:
    """Refuse *label* while anything the row started is not settled."""
    problems = settlement_problems()
    if problems:
        raise UnsettledWorker(f"before {label}: {problems}")


async def run_owned(
    label: str,
    func: Callable[..., Any],
    *args: Any,
    seconds: float,
    gated: bool = True,
    **kwargs: Any,
) -> Any:
    """Run blocking *func* on a thread the row owns, and wait for it.

    Gated first, unless it is cleanup (*gated* False): ending what the row
    started must not wait on what the row could not settle. A cancellation
    that arrives while the thread runs is held until the thread has finished,
    then raised, so nothing after it overlaps the work. A thread that outlives
    *seconds* is not forgotten: it stays in ``_OWNED``, and this raises
    ``UnsettledWorker`` (or the held cancellation), so every later ``gate``
    refuses until it ends. Anything but an ordinary exception from the work
    comes back as ``WorkerFailed``.
    """
    if gated:
        gate(label)
    worker = _Worker(label)

    def body() -> None:
        try:
            worker.outcome = ("returned", func(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - kept as the outcome
            worker.outcome = ("raised", exc)
        finally:
            worker.done.set()

    _OWNED.append(worker)
    threading.Thread(target=body, name=f"r7: {label}", daemon=True).start()
    deadline = time.monotonic() + seconds
    cancelled: BaseException | None = None
    while not worker.done.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if cancelled is not None:
                raise cancelled
            raise UnsettledWorker(
                f"{label} outlived its {seconds}s bound; it stays owned, and the "
                f"row measures nothing more"
            )
        try:
            await asyncio.sleep(min(remaining, 0.05))
        except asyncio.CancelledError as exc:
            # Held, not honoured yet: the work is still running.
            cancelled = cancelled or exc
    running_workers()
    if cancelled is not None:
        raise cancelled
    assert worker.outcome is not None
    kind, value = worker.outcome
    if kind == "raised":
        if isinstance(value, Exception):
            raise value
        raise WorkerFailed(f"{label} ended with {value!r}") from value
    return value


# --- The launch marker and the processes around the close ----------------------------


def _lifetime(history: ProcessHistory, pid: int, created: float) -> Any:
    for life in history.lifetimes:
        if life.pid == pid and abs(life.start - created) <= _START_TOLERANCE_SECONDS:
            return life
    return None


@dataclass(frozen=True)
class LaunchMarker:
    """The original browser's launch marker. The value never leaves memory."""

    value: str = field(repr=False)
    #: The watcher's digest (``watcher.read_browser_marker``).
    digest: str
    browser: tuple[int, float]


def launch_marker(
    observed: Iterable[Mapping[str, Any]],
    principal: tuple[int, float],
    *,
    open_process: Callable[[int], Any] = psutil.Process,
) -> LaunchMarker | None:
    """The marker of a row browser the original actor launched, read from it.

    Only a browser the watcher recorded with a marker digest, whose recorded
    ancestry leads to the original actor, that is still the lifetime the
    watcher saw, and whose value hashes to that digest.
    """
    records = list(observed)
    history = ProcessHistory(records, outside=[os.getpid()])
    origin = _lifetime(history, *principal)
    if origin is None:
        return None
    now = time.time()
    for entry in records:
        if entry.get("kind") not in ("process.start", "process.update"):
            continue
        digest, start = entry.get("browser_marker"), entry.get("start_identity")
        if entry.get("in_row") is not True or not isinstance(digest, str):
            continue
        if not isinstance(start, (int, float)) or not isinstance(entry.get("pid"), int):
            continue
        life = _lifetime(history, entry["pid"], float(start))
        if life is None or history.descends(life, origin, now) is not True:
            continue
        try:
            process = open_process(entry["pid"])
            if abs(process.create_time() - float(start)) > _START_TOLERANCE_SECONDS:
                continue
            value = process.environ().get(BROWSER_MARKER_ENV)
        except (psutil.Error, OSError):
            continue
        if value and hashlib.sha256(value.encode()).hexdigest()[:16] == digest:
            return LaunchMarker(value, digest, (entry["pid"], float(start)))
    return None


def wait_for_marker(
    observed: Callable[[], Iterable[Mapping[str, Any]]],
    principal: tuple[int, float],
    *,
    seconds: float = 5.0,
) -> LaunchMarker | None:
    """``launch_marker``, asked until the watcher has read the browser's."""
    deadline = time.monotonic() + seconds
    while True:
        found = launch_marker(observed(), principal)
        if found is not None or time.monotonic() >= deadline:
            return found
        time.sleep(0.05)


def open_lifetime(
    observed: Iterable[Mapping[str, Any]],
    pid: int,
    *,
    open_process: Callable[[int], Any] = psutil.Process,
) -> tuple[Any, float] | None:
    """A handle to the row lifetime the watcher recorded at *pid*, taken only
    while the process there still is that lifetime. Used to wait, never to
    signal: the harness sends a guardian nothing."""
    starts = [
        float(entry["start_identity"])
        for entry in observed
        if entry.get("kind") in ("process.start", "process.update")
        and entry.get("pid") == pid
        and entry.get("in_row") is True
        and isinstance(entry.get("start_identity"), (int, float))
    ]
    try:
        process = open_process(pid)
        created = process.create_time()
    except psutil.Error:
        return None
    if any(abs(created - start) <= _START_TOLERANCE_SECONDS for start in starts):
        return process, created
    return None


def early_browsers(
    observed: Iterable[Mapping[str, Any]],
    principal: tuple[int, float],
    *,
    since: float,
    until: float,
) -> list[str]:
    """Row browsers first seen after the close began and by the recovery
    barrier that are not the original actor's (E1EZ-02).

    An elected successor is allowed before the barrier; a browser on the
    profile is not. One whose ancestry cannot be traced is not shown to be
    the original's.
    """
    history = ProcessHistory(observed, outside=[os.getpid()])
    origin = _lifetime(history, *principal)
    problems = []
    for life in history.lifetimes:
        if not (life.in_row and life.was("browser")):
            continue
        if not since < life.first_t <= until:
            continue
        if origin is not None and history.descends(life, origin, until) is True:
            continue
        problems.append(
            f"browser {life.pid} was first seen at {life.first_t}, after the close "
            f"began and before the recovery barrier, and is not the original's"
        )
    return problems


# --- The trace, and the phase after the real drain returned ------------------------

_END_LINE = re.compile(
    r"^(?P<tid>\d+)\s+(?P<t>\d+\.\d+)\s+\+\+\+ "
    r"(?P<end>exited with -?\d+|killed by \S+)(?: \(core dumped\))? \+\+\+$"
)


def trace_ends(text: str) -> dict[int, list[tuple[float, str]]]:
    """How strace saw each tid end: ``exited with N`` or ``killed by SIG``."""
    ends: dict[int, list[tuple[float, str]]] = {}
    for raw in text.splitlines():
        found = _END_LINE.match(raw.strip())
        if found is not None:
            ends.setdefault(int(found["tid"]), []).append(
                (float(found["t"]), found["end"])
            )
    return ends


def _end_after(ends: Mapping[int, list[tuple[float, str]]], tid: int, t: float):
    later = [end for when, end in ends.get(tid, []) if when >= t]
    return later[0] if later else None


def call_shape(call: Mapping[str, Any], own_group: int | None) -> dict[str, Any]:
    """What a call looks like in the transcript, without its pids and times."""
    group = call.get("target_group")
    return {
        "syscall": call.get("syscall"),
        "signal": call.get("signal"),
        "target": (
            "own group"
            if group is not None and own_group is not None and group == own_group
            else "other"
        ),
        "result": str(call.get("result", "")).strip(),
        "end": call.get("end"),
    }


def _sender(
    call: SignalCall, outcome: OracleOutcome, *, owner: int | None, guardian: int | None
) -> tuple[int | None, str]:
    pid = outcome.threads.get(call.tid, call.tid)
    if owner is not None and pid == owner:
        return pid, OWNER
    if guardian is not None and pid == guardian:
        return pid, GUARDIAN
    # A process a traced one started, or a thread of one: of the original
    # actor's tree or the guardian's, and never read as nobody's.
    process = outcome.cohort.get(pid) or {}
    caller = outcome.cohort.get(call.tid) or {}
    if (
        process.get("kind") == "child"
        and not process.get("reused")
        and not caller.get("reused")
    ):
        return pid, DESCENDANT
    return pid, UNPLACED


@dataclass(frozen=True)
class PhaseReading:
    """Every traced call, placed against the real drain's return, and what
    kept the collection from being complete."""

    collection: str
    reasons: tuple[str, ...]
    #: The drain's return on strace's clock, in nanoseconds; None with *clock*
    #: saying why it could not be placed.
    boundary: tuple[int, int] | None
    clock: str | None
    calls: tuple[Mapping[str, Any], ...]
    tracees: int

    def of(self, sender: str, *placements: str) -> list[Mapping[str, Any]]:
        return [
            call
            for call in self.calls
            if call["sender"] == sender and call["placement"] in placements
        ]


def read_phase(
    outcome: OracleOutcome,
    text: str,
    *,
    owner: int | None,
    guardian: int | None,
    owner_group: int | None,
    boundary: tuple[int, int] | str | None,
) -> PhaseReading:
    """Place every call of the complete trace, after it was read whole.

    Nothing is dropped: calls before the phase, calls the clock cannot place,
    and calls from a sender that cannot be named stay in the reading, and the
    collection's own reasons (malformed lines, unfinished calls, reused ids)
    stay with it.
    """
    ends = trace_ends(text)
    interval = boundary if isinstance(boundary, tuple) else None
    calls = []
    for call in outcome.calls:
        pid, sender = _sender(call, outcome, owner=owner, guardian=guardian)
        fields = {
            **call.as_event_fields(),
            "tid": call.tid,
            "pid": pid,
            "sender": sender,
            "placement": place(call.t, interval) if interval is not None else AMBIGUOUS,
            "end": _end_after(ends, call.tid, call.t),
            "probe": call.probe,
        }
        fields["shape"] = call_shape(fields, owner_group)
        calls.append(fields)
    return PhaseReading(
        collection=outcome.status,
        reasons=tuple(outcome.reasons),
        boundary=interval,
        clock=boundary if isinstance(boundary, str) else None,
        calls=tuple(calls),
        tracees=len(outcome.cohort),
    )


def continuation_signals(reading: PhaseReading | None) -> list[str]:
    """K3: why the original actor is not shown to send no signal after the
    drain returned. A signal probe is no signal. Nothing unknown reads as
    none: an unplaced sender or an unplaceable time in the phase fails it."""
    if reading is None:
        return ["no trace was read"]
    problems = []
    if reading.collection != COMPLETE:
        problems.append(
            f"the trace is {reading.collection}: {list(reading.reasons)[:5]}"
        )
    if reading.boundary is None:
        problems.append(f"the phase could not be placed: {reading.clock}")
        return problems
    for sender in (OWNER, DESCENDANT, UNPLACED):
        for call in reading.of(sender, IN_PHASE, AMBIGUOUS):
            if call["probe"]:
                continue
            problems.append(
                f"{sender} {call['pid']} sent {call['signal']} by {call['syscall']} "
                f"({call['placement']}, {call['outcome']})"
            )
    return problems


def own_group_operations(
    reading: PhaseReading | None, calibration: FatalCalibration | None
) -> tuple[list[Mapping[str, Any]], list[str]]:
    """K2: the original owner's own-group kill after the drain returned, as
    the calibrated transcript shows such a call, and why there is none.

    Only the owner itself, never its guardian's kill of the same group; only
    in the phase; and only in the exact shape the product-free probe left,
    since a fatal call has no ordinary return to read.
    """
    problems: list[str] = []
    if calibration is None or calibration.shape is None:
        problems.append(
            "no calibrated transcript of a fatal own-group kill: "
            f"{list(calibration.problems) if calibration else 'never run'}"
        )
    if reading is None:
        return [], [*problems, "no trace was read"]
    if reading.collection != COMPLETE:
        problems.append(
            f"the trace is {reading.collection}: {list(reading.reasons)[:5]}"
        )
    if reading.boundary is None:
        problems.append(f"the phase could not be placed: {reading.clock}")
    found = [
        call
        for call in reading.of(OWNER, IN_PHASE)
        if call["shape"]["target"] == "own group"
        and calibration is not None
        and calibration.shape is not None
        and dict(call["shape"]) == dict(calibration.shape)
    ]
    if not found:
        problems.append(
            "the original owner was not traced killing its own group after the "
            "drain returned, in the calibrated shape"
        )
    return found, problems


# --- Calibrating a fatal own-group kill ------------------------------------------------

#: The product-free probe: it waits to be traced, then kills its own group,
#: the call ``hard_exit_process_tree`` makes (``os.killpg(os.getpgrp(), ...)``).
FATAL_PROBE = (
    "import os, signal, sys\n"
    "sys.stdin.readline()\n"
    "os.killpg(os.getpgrp(), signal.SIGKILL)\n"
)


@dataclass(frozen=True)
class FatalCalibration:
    """What strace wrote for a process that killed its own group, and why no
    shape could be taken. Evidence of the tracer, never of the product."""

    shape: Mapping[str, Any] | None
    problems: tuple[str, ...]
    returncode: int | None = None
    transcript: tuple[str, ...] = ()
    evidence: str = NATIVE_PROBE


def calibration_from(
    outcome: OracleOutcome, text: str, *, pid: int, returncode: int | None
) -> FatalCalibration:
    """The probe's own-group kill, from its complete transcript."""
    problems = []
    if returncode != -9:
        problems.append(f"the probe ended with {returncode!r}, not killed by SIGKILL")
    if outcome.status != COMPLETE:
        problems.append(f"the probe's trace is {outcome.status}: {outcome.reasons[:5]}")
    reading = read_phase(
        outcome, text, owner=pid, guardian=None, owner_group=pid, boundary=None
    )
    kills = [
        call
        for call in reading.calls
        if call["sender"] == OWNER and call["shape"]["target"] == "own group"
    ]
    if len(kills) != 1:
        problems.append(
            f"the probe's trace holds {len(kills)} own-group kills, not one"
        )
    shape = dict(kills[0]["shape"]) if len(kills) == 1 else None
    if shape is not None and not shape.get("end"):
        problems.append("the probe's own-group kill is not followed by its end")
    return FatalCalibration(
        shape=None if problems else shape,
        problems=tuple(problems),
        returncode=returncode,
        transcript=tuple(line for line in text.splitlines() if line.strip())[:50],
    )


def calibrate_fatal_group(
    directory: Path,
    *,
    seconds: float = 30.0,
    oracle: SignalOracle | None = None,
) -> FatalCalibration:
    """Trace a product-free child killing its own group, as the rows trace.

    Disposable CI only: ``SignalOracle`` refuses anywhere else, and so does
    this. The child leads a session of its own, so its group is itself; it
    is the harness's own child, ended and reaped through its ``Popen`` if it
    does not end itself within *seconds*.
    """
    directory.mkdir(parents=True, exist_ok=True)
    tracer = oracle or SignalOracle(directory, required=True)
    if not tracer.available:
        return FatalCalibration(None, (f"no tracer here: {tracer.unavailable}",))
    child = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", FATAL_PROBE],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    problems: list[str] = []
    try:
        reason = tracer.start([child.pid])
        if reason is not None:
            problems.append(f"strace did not attach to the probe: {reason}")
        assert child.stdin is not None
        # Released only once traced, or to end at once when it could not be.
        try:
            child.stdin.write(b"go\n")
            child.stdin.close()
        except OSError as exc:
            problems.append(f"the probe could not be released: {exc!r}")
        try:
            returncode: int | None = child.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            returncode = None
            problems.append(f"the probe did not end within {seconds}s")
        outcome = tracer.stop(seconds=seconds)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    try:
        text = tracer.out.read_text(errors="replace")
    except OSError as exc:
        return FatalCalibration(None, (*problems, f"no transcript: {exc!r}"))
    found = calibration_from(outcome, text, pid=child.pid, returncode=returncode)
    return FatalCalibration(
        shape=None if problems else found.shape,
        problems=(*problems, *found.problems),
        returncode=returncode,
        transcript=found.transcript,
    )


# --- The source model ---------------------------------------------------------------------

BASELINE = "baseline"
CANDIDATE = "candidate"


@dataclass(frozen=True)
class AliasModel:
    """The declared fault run against each runtime's exact ``process_tree``.

    Per revision: without an activation the saved public alias passes the
    real True unchanged; activated for this lifetime, it calls the replaced
    private global once and returns False after the outcome is published; a
    foreign lifetime changes nothing. Source-model evidence, never native.
    """

    sha256: Mapping[str, str]
    problems: tuple[str, ...]
    evidence: str = SOURCE_MODEL


def _alias_problems(text: str) -> list[str]:
    marker = "r7-model-marker"
    problems: list[str] = []

    def load(identity: tuple[int, int], directory: str):
        module = types.ModuleType(r7_fault.MODULE)
        exec(compile(text, "process_tree.py", "exec"), module.__dict__)
        module.__dict__["_registered_browser_markers"] = {marker}
        module.__dict__["_IS_WINDOWS"] = False
        calls: list[tuple[str, float]] = []

        def private(value: str, deadline: float) -> bool:
            calls.append((value, deadline))
            return True

        module.__dict__[r7_fault.PRIVATE] = private
        # Saved by value first, as core.browser holds it.
        alias = module.__dict__[r7_fault.PUBLIC]
        r7_fault.Fault(
            directory, identity=lambda: identity, role=lambda: "owner"
        ).install(module)
        return module, alias, calls

    with tempfile.TemporaryDirectory(prefix="r7-model-") as raw:
        directory = Path(raw)
        module, alias, calls = load((os.getpid(), 11), raw)
        if alias.__globals__ is not module.__dict__:
            problems.append("the public alias has other globals")
        if alias(marker) is not True or len(calls) != 1:
            problems.append("without an activation the alias did not pass True once")
        publish_activation(
            directory,
            row=ROW_H_R7,
            experiment="model",
            repetition=0,
            run="model",
            pid=os.getpid(),
            start_ticks=11,
            role="owner",
            marker=marker,
            source={"model": True},
        )
        if alias(marker) is not False or len(calls) != 2 or calls[-1][0] != marker:
            problems.append(
                "activated, the alias did not hand one real True back as False"
            )
        try:
            outcome = json.loads((directory / r7_fault.OUTCOME).read_text())
        except (OSError, ValueError):
            outcome = {}
        if outcome.get("real") is not True:
            problems.append(f"the published outcome is {outcome!r}, not a real True")
        _, foreign, foreign_calls = load((os.getpid(), 12), raw)
        if foreign(marker) is not True or len(foreign_calls) != 1:
            problems.append("another lifetime's call did not pass its True unchanged")
    return problems


#: The close path whose one-drain serialization the browser-free controls run
#: on this checkout's own bodies (``test_r7_fault``): the core and driver
#: close, the lease and the role. Those controls speak for a runtime only
#: while its copies are these, byte for byte.
CLOSE_PATH = (
    "linkedin_mcp_server/core/browser.py",
    "linkedin_mcp_server/drivers/browser.py",
    "linkedin_mcp_server/profile_lease.py",
    "linkedin_mcp_server/server_role.py",
)


def alias_model(
    sources: Mapping[str, str],
    *,
    close_path: Mapping[str, Mapping[str, str]] | None = None,
) -> AliasModel:
    """``AliasModel`` for each named source text.

    With *close_path*, each revision's copy of ``CLOSE_PATH`` too: a baseline
    whose close path differs from the candidate's is one the serialization
    controls never ran, and the model says so.
    """
    problems: list[str] = []
    for revision, text in sources.items():
        try:
            problems += [f"{revision}: {p}" for p in _alias_problems(text)]
        except Exception as exc:  # noqa: BLE001 - a model that cannot run says so
            problems.append(f"{revision}: the model could not run: {exc!r}")
    if close_path is not None:
        reference = close_path.get(CANDIDATE) or {}
        for revision, files in close_path.items():
            for path in CLOSE_PATH:
                if path not in files or path not in reference:
                    problems.append(f"{revision}: {path} was not read")
                elif files[path] != reference[path]:
                    problems.append(
                        f"{revision}: {path} differs from the candidate's, whose "
                        f"close the serialization controls ran"
                    )
    return AliasModel(
        sha256={name: source_sha256(text) for name, text in sources.items()},
        problems=tuple(problems),
    )


def source_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: str | Path | None) -> str | None:
    if not path:
        return None
    try:
        return source_sha256(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return None


# --- The continuation, its gate, and the ledger --------------------------------------


@dataclass(frozen=True)
class R7Continuation:
    """What one native H-R7 execution established, as native evidence only.

    The selected call and its consumption (``selection``, the problems of
    ``fault_overlay.selection_problems``: empty when established), the lease
    checkpoints with their association, the original guardian and its group,
    the exits observed before any intervention, the recovery, what the
    harness ended after measuring, and the phase reading. ``validity`` holds
    every problem of observation, common to all experiments.
    """

    experiment: str
    repetition: int
    run: str
    mode: str
    #: ``inert`` or ``unshimmed`` for a K0 control, None for an injection.
    control: str | None
    revision: str | None
    process_tree_sha256: str | None
    fault_sha256: str | None
    scenario: tuple[str, ...]
    vector: Any
    first_read: bool
    principal: tuple[int, float] | None
    role: str | None
    guardian: tuple[int, float] | None
    guardian_group: int | None
    owner_group: int | None
    marker_digest: str | None
    lock: tuple[int, int] | None
    checkpoints: tuple[Mapping[str, Any], ...]
    traced_before_activation: bool | None
    activated: bool
    selection: tuple[str, ...]
    #: Consumptions of a False the overlay observed, by anyone; None where no
    #: overlay could observe one (the unshimmed control).
    consumed: int | None
    owner_exit: str | None
    guardian_exit: str | None
    pre_probe: tuple[str, ...]
    recovery: str
    successor_verified: bool | None
    successor_problems: tuple[str, ...]
    ended_by_harness: tuple[Mapping[str, Any], ...]
    phase: PhaseReading | None
    validity: tuple[str, ...]
    evidence: str = NATIVE


def _checkpoint(c: R7Continuation, label: str) -> Mapping[str, Any]:
    for point in c.checkpoints:
        if point.get("label") == label:
            return point
    return {"label": label, "state": "not taken"}


#: The checkpoints, by label.
BEFORE_CLOSE = "before the close"
AFTER_CONSUMPTION = "after the consumption"
BEFORE_QUIT = "before the host quits"
BEFORE_RECOVERY = "before the recovery"
AFTER_CONFIRMED_CLOSE = "after the confirmed close"
BEFORE_PRESERVATION = "before preservation"


def r7_problems(
    c: R7Continuation | None,
    *,
    experiment: str,
    repetition: int,
    revision: str | None,
    run: str | None = None,
    control: str | None = None,
    calibration: FatalCalibration | None = None,
) -> list[str]:
    """The common validity gate of an H-R7 cell, and what its experiment adds.

    Every cell, K2 included: the expected experiment, repetition, run, mode,
    revision, fault and scenario; a first read; the original actor, its
    guardian and the guardian's group (E1EZ-03); the launch marker read and
    matched; a held lock before the close, associated with the original
    actor; the trace attached before any activation and complete; nothing
    that kept the row from observing (``validity``); and a free lock before
    preservation. An injection adds the selected call and its consumption,
    exactly once. Then per experiment:

    * K1 (frozen Direct): held and associated after the consumption and
      before the host quits, the server alive; no recovery.
    * K2 (baseline owner): the owner and guardian exited and the lock free
      before the recovery; the owner's calibrated own-group kill in the phase.
    * K3 (candidate owner): the same barrier; no signal of the original owner
      after the drain returned; a successor that served the recovery, with no
      browser on the profile before the barrier.
    * K0 controls: the close confirmed by the product, nothing selected, the
      owner alive and the lock free after it.
    """
    if c is None:
        return [f"{experiment} repetition {repetition} left no continuation"]
    problems = list(c.validity)
    if c.experiment != experiment or c.repetition != repetition:
        problems.append(
            f"the continuation is {c.experiment} repetition {c.repetition}, not "
            f"{experiment} repetition {repetition}"
        )
    if c.control != control:
        problems.append(f"the continuation's control is {c.control!r}, not {control!r}")
    if run is not None and c.run != run:
        problems.append(f"the continuation is from run {c.run}, not {run}")
    if revision is None or c.revision != revision:
        problems.append(f"the actors ran {c.revision}, not {revision}")
    if c.evidence != NATIVE:
        problems.append(f"the continuation claims {c.evidence!r} evidence")
    expected_mode = "direct" if experiment == "K1" else "daemon"
    if c.mode != expected_mode:
        problems.append(f"{experiment} ran in {c.mode} mode, not {expected_mode}")
    if control != UNSHIMMED and c.fault_sha256 != FAULT_SHA256:
        problems.append(
            f"the fault was {c.fault_sha256}, not the declared {FAULT_SHA256}"
        )
    if c.process_tree_sha256 is None:
        problems.append("the process_tree the actors import could not be read")
    problems += [f"scenario: {p}" for p in c.scenario]
    if not c.first_read:
        problems.append("the first call did not read the synthetic post")
    if c.principal is None:
        problems.append("the original actor was never identified")
    if c.guardian is None:
        problems.append("the original actor's guardian was never identified")
    expected_group = c.owner_group if experiment == "K2" else 0
    if experiment == "K2" and c.owner_group is None:
        problems.append("K2: the owner's group was not observed")
    elif c.guardian_group != expected_group:
        problems.append(
            f"{experiment}: the guardian was given group {c.guardian_group!r}, not "
            f"{expected_group}"
        )
    if c.marker_digest is None:
        problems.append("the original browser's launch marker was not read and matched")
    if c.lock is None:
        problems.append("the profile lock was never identified")
    problems += checkpoint_problems(
        _checkpoint(c, BEFORE_CLOSE), expect=lease_probe.HELD, holder=True
    )
    problems += checkpoint_problems(
        _checkpoint(c, BEFORE_PRESERVATION), expect=lease_probe.FREE
    )
    if c.traced_before_activation is not True:
        problems.append(
            "the trace was not attached to the original actor before the close"
        )
    if c.phase is None or c.phase.collection != COMPLETE:
        reasons = list(c.phase.reasons)[:5] if c.phase else []
        problems.append(
            f"the trace is {c.phase.collection if c.phase else 'missing'}: {reasons}"
        )
    if control is not None:
        if c.activated:
            problems.append(f"the {control} control was activated")
        if c.consumed:
            problems.append(
                f"the {control} control's close was consumed as unconfirmed "
                f"{c.consumed} time(s)"
            )
        problems += checkpoint_problems(
            _checkpoint(c, AFTER_CONFIRMED_CLOSE), expect=lease_probe.FREE
        )
        return problems
    if not c.activated:
        problems.append("the fault was never activated")
    problems += [f"selected call: {p}" for p in c.selection]
    expected_role = "direct" if experiment == "K1" else "owner"
    if c.role != expected_role:
        problems.append(
            f"the original actor's role was {c.role!r}, not {expected_role!r}"
        )
    if experiment == "K1":
        problems += checkpoint_problems(
            _checkpoint(c, AFTER_CONSUMPTION), expect=lease_probe.HELD, holder=True
        )
        problems += checkpoint_problems(
            _checkpoint(c, BEFORE_QUIT), expect=lease_probe.HELD, holder=True
        )
        if c.recovery != NO_RECOVERY:
            problems.append(f"the Direct reference made a recovery: {c.recovery}")
        return problems
    if c.owner_exit != "exited":
        problems.append(f"the original owner was {c.owner_exit!r} before the recovery")
    if c.guardian_exit != "exited":
        problems.append(
            f"the original guardian was {c.guardian_exit!r} before the recovery"
        )
    problems += checkpoint_problems(
        _checkpoint(c, BEFORE_RECOVERY), expect=lease_probe.FREE
    )
    problems += [f"before the recovery: {p}" for p in c.pre_probe]
    if experiment == "K2":
        _, missing = own_group_operations(c.phase, calibration)
        return problems + [f"K2 witness: {p}" for p in missing]
    problems += [
        f"after the drain returned: {p}" for p in continuation_signals(c.phase)
    ]
    if c.recovery != POST_SETTLEMENT:
        problems.append(f"no post-settlement recovery: {c.recovery}")
    if c.successor_verified is not True:
        problems.append(
            "no successor is shown to have served the recovery"
            + (f": {'; '.join(c.successor_problems)}" if c.successor_problems else "")
        )
    return problems


def _vector_semantics(vector: Any) -> dict[str, Any] | None:
    """The row vector without what the shared prefix's timing decides.

    Which classes the routine drain and the guardian sent before the phase
    depends on whether a Chromium helper outlived the graceful close, and
    whether a recipient was pinned (``held``) or not (``unknown``) on when
    the watcher sampled it; neither is what the experiment established.
    Both stay recorded in the vector; a violation or an incomplete trace
    still differs.
    """
    if vector is None:
        return None
    fields = asdict(vector)
    fields.pop("signal_classes", None)
    if fields.get("o2_traced") in (HELD_O2, UNKNOWN_O2):
        fields["o2_traced"] = "held or unknown"
    return fields


def semantics(c: R7Continuation) -> dict[str, Any]:
    """What two executions of one experiment must agree on: no pid, nonce,
    time or path, only what the row established."""
    phase = c.phase
    return {
        "experiment": c.experiment,
        "mode": c.mode,
        "first_read": c.first_read,
        "activated": c.activated,
        "selected": not c.selection,
        "consumed": c.consumed,
        "role": c.role,
        "guardian_group_is_owner_group": (
            c.guardian_group is not None and c.guardian_group == c.owner_group
        ),
        "checkpoints": {
            point.get("label"): point.get("state") for point in c.checkpoints
        },
        "owner_exit": c.owner_exit,
        "guardian_exit": c.guardian_exit,
        "pre_probe": not c.pre_probe,
        "recovery": c.recovery,
        "successor_verified": c.successor_verified,
        "phase_collection": phase.collection if phase else None,
        "original_actor_in_phase": sorted(
            {
                f"{call['signal']}:{call['shape']['target']}"
                for call in (phase.of(OWNER, IN_PHASE) if phase else [])
                if not call["probe"]
            }
        ),
        "vector": _vector_semantics(c.vector),
    }


def semantic_differences(
    first: R7Continuation, second: R7Continuation, *, ignore: Iterable[str] = ()
) -> list[str]:
    one, two = semantics(first), semantics(second)
    skipped = set(ignore)
    return [
        f"{name}: {one[name]!r} then {two[name]!r}"
        for name in one
        if name not in skipped and one[name] != two[name]
    ]


#: What the two controls differ in by construction: only the overlay can
#: observe a consumption, and neither is activated.
_CONTROL_CONSTRUCTION = ("consumed",)


class R7Ledger:
    """The native continuations of one invocation of the row module, keyed by
    experiment and repetition (or control). A second continuation for one
    key is refused, never chosen between; the composition empties it."""

    def __init__(self, run: str) -> None:
        self.run = run
        self._cells: dict[tuple[str, str], R7Continuation] = {}
        self._problems: list[str] = []

    @staticmethod
    def key(c: R7Continuation) -> tuple[str, str]:
        return (c.experiment, c.control or str(c.repetition))

    def record(self, continuation: R7Continuation | None) -> None:
        if continuation is None:
            return
        key = self.key(continuation)
        if key in self._cells:
            self._problems.append(f"a second {key} continuation in one invocation")
            return
        self._cells[key] = continuation

    def get(self, experiment: str, which: str) -> R7Continuation | None:
        return self._cells.get((experiment, which))

    def take(self) -> tuple[dict[tuple[str, str], R7Continuation], list[str]]:
        cells, problems = self._cells, self._problems
        self._cells, self._problems = {}, []
        return cells, problems


def r7_composition(
    model: AliasModel | None,
    ledger: R7Ledger,
    *,
    revisions: Mapping[str, str | None],
    calibration: FatalCalibration | None,
    compare_to_direct: Callable[[Any, Any], list[str]],
) -> list[str]:
    """What stops H-R7's claim from being composed in this invocation.

    A composition of separate results, never a sum: the source model run in
    this process against the exact sources the native runtimes imported;
    the fatal-kill calibration K2 rests on; the two K0 controls, each valid
    and reading alike; every K1, K2 and K3 repetition through the common
    gate and its experiment, and each experiment's repetitions reading
    alike; and each K3 no worse than its K1 on the whole row's O1, O2 and O4,
    which stays a shared-prefix reading apart from the phase.
    """
    cells, problems = ledger.take()
    if model is None:
        problems.append("no source-model run in this invocation")
    else:
        if model.evidence != SOURCE_MODEL:
            problems.append(f"the model is {model.evidence!r}, not source-model")
        problems += [f"source model: {p}" for p in model.problems]
    if calibration is None or calibration.shape is None:
        problems.append(
            f"no calibrated fatal own-group transcript: "
            f"{list(calibration.problems) if calibration else 'never run'}"
        )
    elif calibration.evidence != NATIVE_PROBE:
        problems.append(f"the calibration is {calibration.evidence!r}")

    def modelled(experiment: str, cell: R7Continuation) -> list[str]:
        if model is None:
            return []
        expected = model.sha256.get(
            BASELINE if experiment in ("K1", "K2") else CANDIDATE
        )
        if cell.process_tree_sha256 != expected:
            return [
                f"the actors imported process_tree {cell.process_tree_sha256}, the "
                f"model ran {expected}"
            ]
        return []

    controls = {}
    for control in (UNSHIMMED, INERT):
        cell = cells.get(("K0", control))
        controls[control] = cell
        found = r7_problems(
            cell,
            experiment="K0",
            repetition=0,
            revision=revisions.get("K0"),
            run=ledger.run,
            control=control,
        )
        if cell is not None:
            found += modelled("K0", cell)
        problems += [f"K0 {control}: {p}" for p in found]
    unshimmed, inert = controls[UNSHIMMED], controls[INERT]
    if unshimmed is not None and inert is not None:
        problems += [
            f"K0: the inert overlay differs from the unshimmed runtime: {d}"
            for d in semantic_differences(
                unshimmed, inert, ignore=_CONTROL_CONSTRUCTION
            )
        ]
    for experiment in ("K1", "K2", "K3"):
        seen = []
        for repetition in REPETITIONS:
            cell = cells.get((experiment, str(repetition)))
            found = r7_problems(
                cell,
                experiment=experiment,
                repetition=repetition,
                revision=revisions.get(experiment),
                run=ledger.run,
                calibration=calibration,
            )
            if cell is not None:
                found += modelled(experiment, cell)
                seen.append(cell)
            problems += [f"{experiment} #{repetition}: {p}" for p in found]
        for later in seen[1:]:
            problems += [
                f"{experiment}: repetition {later.repetition} reads unlike "
                f"repetition {seen[0].repetition}: {d}"
                for d in semantic_differences(seen[0], later)
            ]
    for repetition in REPETITIONS:
        reference = cells.get(("K1", str(repetition)))
        candidate = cells.get(("K3", str(repetition)))
        if reference is None or candidate is None:
            continue
        if reference.vector is None or candidate.vector is None:
            problems.append(f"#{repetition}: K1 or K3 left no vector to compare")
            continue
        problems += [
            f"K3 #{repetition} differs from K1 frozen on the whole row (shared "
            f"prefix): {difference}"
            for difference in compare_to_direct(reference.vector, candidate.vector)
        ]
    return problems
