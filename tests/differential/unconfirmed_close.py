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
stepped and nothing is placed. A traced call is in the phase only when it was
entered after the latest possible return, before it only when the trace
bounds its return before the earliest (``place``); anything else is
ambiguous, never resolved by a tolerance.

**Workers, helpers and resources stay owned.** Every blocking step runs on a
thread the row owns (``run_owned``), the lease contender's helper is owned by
``lease_probe``, and a tracer, child, owner or guardian the row could not
show ended is retained (``retain``). ``gate`` refuses the next measurement
while any of them is not shown finished, whatever the failure to settle was.
A cleanup holds every cancellation until it has run whole (``Deferral``).
"""

from __future__ import annotations

import ast
import asyncio
import builtins
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
from differential.watcher import BROWSER_MARKER_ENV, read_arguments

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

    Premise, conditional and not measured here: on Linux both clocks are
    slewed alike, so their difference changes only when the realtime clock
    steps. Every sample then brackets that one difference; if no difference
    fits all of them, the clock stepped. The converse does not hold: samples
    this sparse cannot see a step and its restoring step between two of
    them, so an agreeing set shows only that no net step was seen. The time
    placed must lie between the first and the last sample.
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


def _micros(t: float) -> int:
    """A strace prefix as the integer microseconds it printed."""
    return round(t * 1_000_000)


def place(entry: float, returned: float | None, interval: tuple[int, int]) -> str:
    """A traced signal operation against the drain's return, *interval*.

    Both times are strace prefixes, truncated to their microsecond, so each
    stands for any instant of it. The signal is sent after the entry prefix
    was read, and the call had returned by the *returned* prefix
    (``SignalCall.returned``); nothing else bounds it, the ``-T`` time least
    of all. In the phase only when the operation began after the latest
    possible return; before it only when the whole operation, to its bounded
    return, ended before the earliest. A call with no return bound, or one
    that crosses, is ambiguous.
    """
    low, high = interval
    if _micros(entry) * 1000 > high:
        return IN_PHASE
    if returned is not None and _micros(returned) * 1000 + 999 < low:
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


@dataclass(eq=False)
class Retained:
    """Something outside this process a row started and could not show ended:
    a tracer, a child, an owner, a guardian. *check* makes one bounded
    attempt to settle it, never signalling anything the row does not own,
    and says whether it is now settled."""

    label: str
    check: Callable[[float], bool]


#: Every retained resource, whichever row or calibration left it: nothing is
#: measured, by any row, until each is shown settled.
_RETAINED: list[Retained] = []


def retain(label: str, check: Callable[[float], bool]) -> Retained:
    resource = Retained(label, check)
    _RETAINED.append(resource)
    return resource


def retained(resource: Retained) -> bool:
    """Whether *resource* is still held: not yet shown settled."""
    return resource in _RETAINED


def discharge(resource: Retained) -> None:
    """Stop holding *resource*, which its owner has just shown settled."""
    if resource in _RETAINED:
        _RETAINED.remove(resource)


def unsettled_resources(grace: float = 5.0) -> list[str]:
    """Ask each retained resource once more; those still not settled.

    A check that fails is no settlement, and the resource stays retained.
    """
    problems = []
    for resource in list(_RETAINED):
        try:
            settled = resource.check(grace) is True
        except BaseException as exc:  # noqa: BLE001 - an unanswered check is no settlement
            problems.append(f"{resource.label} could not be checked: {exc!r}")
            continue
        if settled:
            _RETAINED.remove(resource)
        else:
            problems.append(f"{resource.label} is not shown settled")
    return problems


def settlement_problems(grace: float = 5.0) -> list[str]:
    """Why the next step may not start: a worker still running, a retained
    resource not settled, or the lease contender's helpers not settled.
    Every way ``settle`` can fail counts, an interrupt included; none of them
    shows the helper gone."""
    problems = [f"worker {label!r} is still running" for label in running_workers()]
    problems += unsettled_resources(grace)
    try:
        lease_probe.settle(grace)
    except BaseException as exc:  # noqa: BLE001 - a failed settlement, whatever it was
        problems.append(f"the lease contender's helpers are not settled: {exc!r}")
    return problems


class Deferral:
    """The cancellations a cleanup transaction held back, raised once it ends.

    Every awaited cleanup step hands one of these to ``run_owned``, so a
    cancellation, a first one or a later one, never cuts the transaction
    short: each step still runs and keeps its result, and the first
    cancellation is what the transaction raises when it is done.
    """

    def __init__(self) -> None:
        self.cancelled: BaseException | None = None
        self.count = 0

    def hold(self, exc: BaseException) -> None:
        self.count += 1
        if self.cancelled is None:
            self.cancelled = exc

    def take(self) -> BaseException | None:
        """The first held cancellation, which the caller now raises."""
        held, self.cancelled = self.cancelled, None
        return held


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
    defer: Deferral | None = None,
    **kwargs: Any,
) -> Any:
    """Run blocking *func* on a thread the row owns, and wait for it.

    Gated first, unless it is cleanup (*gated* False): ending what the row
    started must not wait on what the row could not settle. A cancellation
    that arrives while the thread runs is held until the thread has finished,
    so nothing after it overlaps the work; then it is raised, or, with
    *defer*, handed to that transaction and the work's result returned, so
    the cleanup it belongs to goes on. A thread that outlives *seconds* is
    not forgotten: it stays in ``_OWNED``, and this raises ``UnsettledWorker``
    (or, without *defer*, the held cancellation), so every later ``gate``
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
            if defer is not None:
                defer.hold(exc)
            else:
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
            value = read_arguments(process, "environ").get(BROWSER_MARKER_ENV)
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
    since: float | None,
    until: float | None,
) -> list[str]:
    """Row browsers begun after the close and before the recovery barrier that
    are not the original actor's (E1EZ-02, E1FE-05).

    Ordered by creation, never by when the watcher first saw a process: a
    browser born before the barrier and first sampled after it is still
    early. Each lifetime's ``start`` is psutil's creation time, the kernel's
    start ticks over the boot time; *since* and *until* are the creation
    times of processes the harness started at the close and at the barrier
    (``creation_marker``), on the same scale. Premise, not measured here:
    the harness's and the watcher's psutil read the same boot time. A
    creation in the same tick as either marker cannot be ordered, and an
    unplaced close or barrier orders nothing; neither is accepted. An
    elected successor is allowed before the barrier, a browser on the
    profile is not, and one whose ancestry cannot be traced is not shown to
    be the original's. Asked again of the completed history before the row
    is judged, so a browser the watcher reported late still counts.
    """
    if since is None or until is None:
        return [
            "the close or the recovery barrier was not placed on the creation clock"
        ]
    history = ProcessHistory(observed, outside=[os.getpid()])
    origin = _lifetime(history, *principal)
    now = time.time()
    problems = []
    for life in history.lifetimes:
        if not (life.in_row and life.was("browser")):
            continue
        if life.start < since or life.start > until:
            continue
        if origin is not None and history.descends(life, origin, now) is True:
            continue
        if life.start in (since, until):
            problems.append(
                f"browser {life.pid} began in the same clock tick as the close or "
                f"the barrier, so it cannot be ordered against them"
            )
            continue
        problems.append(
            f"browser {life.pid} began at {life.start}, after the close and before "
            f"the recovery barrier, and is not the original's"
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


def _marked_group(
    history: ProcessHistory | None, marker: str | None, group: int, t: float
) -> bool:
    """Whether the watcher had recorded, by *t*, a process in *group* carrying
    the original launch's marker (*marker*, the watcher's digest): one record
    holding both, never a marker read at one time and a group at another
    (``Lifetime.groups_marked_by``)."""
    if history is None or not marker or group <= 0:
        return False
    return any(group in life.groups_marked_by(marker, t) for life in history.lifetimes)


def _target_kind(
    call: SignalCall,
    *,
    owner_group: int | None,
    history: ProcessHistory | None,
    marker: str | None,
) -> str:
    """What the call aimed at, from the call and the watcher's records alone."""
    if call.everyone:
        return "everyone"
    if call.unresolvable is not None:
        return "unresolved"
    group = call.target_group
    if group is not None:
        if group == 0:
            return "the sender's own group"
        if owner_group is not None and group == owner_group:
            return "the original actor's group"
        if _marked_group(history, marker, group, call.t):
            return "a group of the original launch"
        return "another group"
    if call.group_of_pid is not None:
        return "the group of a process"
    return "a process"


#: The one operation the shared marked drain sends, in its owner and in its
#: guardian alike: ``os.killpg(group, SIGKILL)`` on a group carrying the
#: launch's marker (``process_tree._kill_marked_process_groups``, the
#: guardian's ``_drain``; its signal-0 probes are no signal).
_SHARED_DRAIN = ("kill", "SIGKILL", "a group of the original launch")


def read_phase(
    outcome: OracleOutcome,
    text: str,
    *,
    owner: int | None,
    guardian: int | None,
    owner_group: int | None,
    boundary: tuple[int, int] | str | None,
    history: ProcessHistory | None = None,
    marker: str | None = None,
) -> PhaseReading:
    """Place every call of the complete trace, after it was read whole.

    Nothing is dropped: calls before the phase, calls the clock cannot place,
    and calls from a sender that cannot be named stay in the reading, and the
    collection's own reasons (malformed lines, unfinished calls, reused ids)
    stay with it. Each call is placed by its whole operation, its entry to
    its bounded return (``place``).

    A call is *shared* only when the source assigns it to the marked drain
    both modes run: sent by the original actor or its guardian, exactly
    ``_SHARED_DRAIN``, at a group the watcher had seen, by then, carry the
    original launch's *marker*. Nothing else is, whatever its class is called.
    """
    ends = trace_ends(text)
    interval = boundary if isinstance(boundary, tuple) else None
    calls = []
    for call in outcome.calls:
        pid, sender = _sender(call, outcome, owner=owner, guardian=guardian)
        kind = _target_kind(
            call, owner_group=owner_group, history=history, marker=marker
        )
        fields = {
            **call.as_event_fields(),
            "tid": call.tid,
            "pid": pid,
            "sender": sender,
            "placement": (
                place(call.t, call.returned, interval)
                if interval is not None
                else AMBIGUOUS
            ),
            "end": _end_after(ends, call.tid, call.t),
            "probe": call.probe,
            "target_kind": kind,
            "shared": sender in (OWNER, GUARDIAN)
            and (call.syscall, call.signal, kind) == _SHARED_DRAIN,
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


@dataclass(frozen=True)
class SharedReduction:
    """What of a row's whole-trace O2 the shared marked drain accounts for.

    *classes*: each O2 class every call of which is a shared call
    (``read_phase``); *unknowns*: whether every recipient O2 left unknown
    was a shared call's. A class or an unknown any other call contributed
    stays as it is.
    """

    classes: tuple[str, ...] = ()
    unknowns: bool = False


def shared_reduction(
    resolved: Sequence[Mapping[str, Any]], unknowns: int, reading: PhaseReading | None
) -> SharedReduction:
    """``SharedReduction`` of ``derive_o2``'s *resolved* entries and its count of
    *unknowns*, matched to *reading*'s calls by sender process and the
    microsecond sent. An entry no call matches is no shared call, and an
    unknown with no entry (an unplaced sender) is no shared one either."""
    if reading is None:
        return SharedReduction()
    calls: dict[tuple[Any, int], list[Mapping[str, Any]]] = {}
    for call in reading.calls:
        calls.setdefault((call["pid"], _micros(call["sent_at"])), []).append(call)
    by_class: dict[str, list[bool]] = {}
    unknown_entries = 0
    unknown_shared = True
    for entry in resolved:
        sender = entry.get("sender") or [None]
        matched = calls.get((sender[0], _micros(float(entry.get("sent_at", 0.0)))))
        shared = bool(matched) and all(call["shared"] for call in matched or [])
        by_class.setdefault(str(entry.get("class")), []).append(shared)
        if entry.get("unknown"):
            unknown_entries += 1
            unknown_shared = unknown_shared and shared
    return SharedReduction(
        classes=tuple(sorted(cls for cls, flags in by_class.items() if all(flags))),
        unknowns=unknown_shared and unknowns == unknown_entries,
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


def _ended(child: subprocess.Popen[bytes]) -> Callable[[float], bool]:
    """A check that ends *child* through its own ``Popen`` and reaps it."""

    def check(grace: float) -> bool:
        if child.poll() is None:
            # Popen polls before it signals: a reaped child is never signalled.
            child.kill()
            try:
                child.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                return False
        return True

    return check


def _clean_calibration(
    child: subprocess.Popen[bytes] | None,
    tracer: SignalOracle,
    *,
    stopped: bool,
    grace: float,
) -> list[str]:
    """Each cleanup step of the calibration, whatever the others did; notes."""
    notes: list[str] = []
    if child is not None and child.stdin is not None:
        try:
            child.stdin.close()
        except BaseException as exc:  # noqa: BLE001 - noted, the next step still runs
            notes.append(f"the probe's stdin could not be closed: {exc!r}")
    if child is not None:
        try:
            if not _ended(child)(grace):
                notes.append(f"the probe child outlived its kill by {grace}s")
        except BaseException as exc:  # noqa: BLE001 - noted, the next step still runs
            notes.append(f"the probe child could not be ended: {exc!r}")
    if not stopped:
        try:
            # With its tracee gone, strace ends by itself.
            tracer.stop(seconds=grace)
        except BaseException as exc:  # noqa: BLE001 - noted, the next step still runs
            notes.append(f"the tracer could not be stopped: {exc!r}")
    try:
        if not tracer.end():
            notes.append("the tracer is still running after its end")
    except BaseException as exc:  # noqa: BLE001 - noted, the check below decides
        notes.append(f"the tracer could not be ended: {exc!r}")
    return notes


def calibrate_fatal_group(
    directory: Path,
    *,
    seconds: float = 30.0,
    grace: float = 10.0,
    oracle: SignalOracle | None = None,
) -> FatalCalibration:
    """Trace a product-free child killing its own group, as the rows trace.

    Disposable CI only: ``SignalOracle`` refuses anywhere else, and so does
    this. The child leads a session of its own, so its group is itself.

    The tracer and the child are retained (``retain``) before either exists,
    so nothing that fails between their start and their end can lose them.
    Cleanup then runs every step whatever the one before it did: the child's
    stdin closed, the child killed and reaped through its ``Popen``, the
    tracer stopped and ended. Each is asked afterwards whether it is settled,
    by its own process, not by whether a cleanup call returned. The first
    failure is raised with the cleanup's notes. One still unsettled stays
    retained, refusing every later measurement, and no calibration is
    returned while it is.
    """
    directory.mkdir(parents=True, exist_ok=True)
    tracer = oracle or SignalOracle(directory, required=True)
    if not tracer.available:
        return FatalCalibration(None, (f"no tracer here: {tracer.unavailable}",))
    gate("the fatal-kill calibration")
    problems: list[str] = []
    child: subprocess.Popen[bytes] | None = None
    owners = [retain("the calibration's tracer", lambda grace: tracer.end())]
    outcome: OracleOutcome | None = None
    returncode: int | None = None
    primary: BaseException | None = None
    try:
        child = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", FATAL_PROBE],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        owners.append(retain("the calibration's probe child", _ended(child)))
        reason = tracer.start([child.pid])
        if reason is not None:
            problems.append(f"strace did not attach to the probe: {reason}")
        assert child.stdin is not None
        # Released only once traced, or to end at once when it could not be:
        # its stdin is closed either way, and the probe reads its end.
        try:
            child.stdin.write(b"go\n")
        except OSError as exc:
            problems.append(f"the probe could not be released: {exc!r}")
        try:
            child.stdin.close()
        except OSError as exc:
            problems.append(f"the probe's stdin could not be closed: {exc!r}")
        try:
            returncode = child.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            problems.append(f"the probe did not end within {seconds}s")
        outcome = tracer.stop(seconds=seconds)
    except BaseException as exc:  # noqa: BLE001 - raised again once cleanup ran
        primary = exc
    notes = _clean_calibration(child, tracer, stopped=outcome is not None, grace=grace)
    unsettled = []
    for owner in owners:
        try:
            settled = owner.check(grace) is True
        except BaseException as exc:  # noqa: BLE001 - an unanswered check settles nothing
            notes.append(f"{owner.label} could not be checked: {exc!r}")
            settled = False
        if settled:
            _RETAINED.remove(owner)
        else:
            unsettled.append(f"{owner.label} is not shown settled; it stays retained")
    if primary is not None:
        for note in [*notes, *unsettled]:
            primary.add_note(note)
        raise primary
    if unsettled:
        raise UnsettledWorker(f"the calibration left {unsettled}; notes: {notes}")
    problems += notes
    assert child is not None and outcome is not None
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


#: Where the driver's close begins, by module-level name. The browser-free
#: controls (``test_r7_fault``) enter it through ``close_browser`` and the
#: idle close behind ``release_profile_if_idle_or_requested``.
#: ``_close_holding_back_cancels`` is the other way a manager's close is
#: entered, and the poll is what starts an idle close in a running actor; no
#: control drives them, and each is kept because a close entered there is
#: still this one. A helper needs no entry of its own (``close_path_slice``),
#: and a root that goes missing reads as another close, never a smaller one.
#:
#: What the slice still cannot see. A name dropped from a ``from`` line could
#: itself be a submodule whose import has effects; like any module outside
#: ``CLOSE_PATH``, its code is not compared here. The names the driver takes
#: from ``linkedin_mcp_server.core`` are functions that package re-exports.
#: And a name built at run time (``globals()["close_" + suffix]``) reaches
#: nothing; only a literal string is followed. A name inside a dropped def's
#: annotation is only looked up, though a ``typing`` form such as
#: ``Optional[X]``, or a union, may hash or compare it, which runs a metaclass
#: hook ``X`` already has. And the exemption for builtins and the standard
#: library trusts their objects as the interpreter provides them: a statement
#: that stores another object in one (``asyncio.x = Hook``, an entry in
#: ``sys.modules``) is compared like any other, but a dropped def whose
#: annotation later reaches that object through it is not.
#:
#: Every other file is compared whole (None). The core is nearly all
#: ``BrowserManager``, whose close reaches the rest of the class by
#: dispatch no name shows (``__getattribute__``, a descriptor, a hook
#: installed by an assignment), and the lease and the role are mostly the
#: close's own bookkeeping, the release, the marker and the stand-down. Only
#: the driver holds whole features the close never enters, such as the feed
#: check.
CLOSE_ROOTS: Mapping[str, tuple[str, ...] | None] = {
    "linkedin_mcp_server/core/browser.py": None,
    "linkedin_mcp_server/drivers/browser.py": (
        "close_browser",
        "_close_browser_locked",
        "_close_holding_back_cancels",
        "release_profile_if_idle_or_requested",
        "_close_unless_a_call_arrived",
        "_close_browser_if_still_idle",
        "watch_for_handoff_requests",
    ),
    "linkedin_mcp_server/profile_lease.py": None,
    "linkedin_mcp_server/server_role.py": None,
}

#: The close path whose one-drain serialization the browser-free controls run
#: on this checkout's own bodies (``test_r7_fault``): the core and driver
#: close, the lease and the role. Those controls speak for a runtime only
#: while its close is this one in code, comments and docstrings aside: the
#: controls run the bodies, and text that never executes cannot change what
#: they prove (``close_path_code``). Nor can a driver function nothing calls
#: on the way, such as the feed check, which is all a slice leaves out
#: (``CLOSE_ROOTS``, ``close_path_slice``).
CLOSE_PATH = tuple(CLOSE_ROOTS)


def alias_model(
    sources: Mapping[str, str],
    *,
    close_path: Mapping[str, Mapping[str, str]] | None = None,
) -> AliasModel:
    """``AliasModel`` for each named source text.

    With *close_path*, each revision's copy of ``CLOSE_PATH`` too: a baseline
    whose close differs from the candidate's is one the serialization
    controls never ran, and the model says so. A copy that does not parse,
    or has lost a root, says so in its own words and matches no other.
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
            for path, roots in CLOSE_ROOTS.items():
                if path not in files or path not in reference:
                    problems.append(f"{revision}: {path} was not read")
                    continue
                try:
                    code = close_path_slice(files[path], roots)
                except ValueError as exc:
                    problems.append(f"{revision}: {path} {exc}")
                    continue
                try:
                    same = code == close_path_slice(reference[path], roots)
                except ValueError:
                    # The candidate's own entry above names why.
                    same = False
                if not same:
                    problems.append(
                        f"{revision}: {path} differs from the candidate's, whose "
                        f"close the serialization controls ran"
                    )
    return AliasModel(
        sha256={name: source_sha256(text) for name, text in sources.items()},
        problems=tuple(problems),
    )


def _code_tree(text: str) -> ast.Module | None:
    """*text* parsed as it runs, docstrings removed; None when it does not parse.

    Parsed from bytes, as the importer reads a file: a ``coding`` comment then
    decodes the copy as it would run, and a line it turns into code counts. A
    body left empty keeps a ``pass``.
    """
    try:
        tree = ast.parse(text.encode("utf-8"))
    except (SyntaxError, ValueError):
        return None
    scopes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, scopes) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]
    return tree


def close_path_code(text: str) -> str | None:
    """*text* as the code it runs, or None when it does not parse.

    The syntax tree without positions and without docstrings; comments never
    reach it. None compares unequal to any tree, so a copy that does not parse
    never matches one that does.
    """
    tree = _code_tree(text)
    return None if tree is None else ast.dump(tree, include_attributes=False)


def close_path_slice(text: str, roots: Sequence[str] | None) -> str:
    """The code of *text* a close entered at *roots* can run, as a dump.

    *roots* None is the whole file, as ``close_path_code`` reads it. Otherwise
    the whole file less what provably does nothing until something calls it:
    a plain module-level ``def`` that no kept code names, whose defaults are
    constants and whose annotations subscript, ``|`` or take attributes of
    builtins and the standard library only (``_inert_def``), and a name a
    ``from`` import binds that no kept code names. Everything
    else runs at import or may, so it is kept and its names followed: every
    import statement, with its module, even when none of its names is used;
    an assignment of any target, a class, an ``if`` or ``try``, a call, a
    decorated def, and any def or name Python looks up by itself (a dunder
    such as a module ``__getattr__``). A def that kept code names is kept,
    nested defs and all, and its names are followed in turn; so is one a
    literal string in kept code spells, which is how ``getattr`` and
    ``globals()`` usually reach a function. ``__future__`` and star imports
    stay whole.

    Raises ValueError, naming the reason, when *text* does not parse or a
    root is not bound in it: a close that cannot be found is never the same
    as one that can, nor as another that cannot.
    """
    tree = _code_tree(text)
    if tree is None:
        raise ValueError("does not parse")
    if roots is None:
        return ast.dump(tree, include_attributes=False)
    return ast.dump(ast.Module(body=_close_slice(tree.body, roots), type_ignores=[]))


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _close_slice(body: list[ast.stmt], roots: Sequence[str]) -> list[ast.stmt]:
    """*body* less the inert defs, and the ``from`` names, nothing kept names."""
    bound: set[str] = set()
    # Name to the inert defs binding it, and to the (index, position) of each
    # name a ``from`` import binds; both are left out until a name reaches
    # them. The import statement itself always stays, names or none.
    defs: dict[str, list[int]] = {}
    aliases: dict[str, list[tuple[int, int]]] = {}
    kept: set[int] = set()
    taken: dict[int, set[int]] = {}
    stdlib = _stdlib_names(body)
    for index, node in enumerate(body):
        if (
            isinstance(node, _DEFS)
            and _inert_def(node, stdlib)
            and not _dunder(node.name)
        ):
            defs.setdefault(node.name, []).append(index)
            bound.add(node.name)
        elif isinstance(node, ast.ImportFrom) and not (
            node.module == "__future__" or any(a.name == "*" for a in node.names)
        ):
            taken[index] = set()
            for position, alias in enumerate(node.names):
                name = alias.asname or alias.name
                if _dunder(name):
                    taken[index].add(position)
                aliases.setdefault(name, []).append((index, position))
                bound.add(name)
        else:
            kept.add(index)
            bound |= _bound_names(node)
    for root in roots:
        if root not in bound:
            raise ValueError(f"has no close root {root}")
    pending = [*roots, *(name for i in kept for name in _named(body[i]))]
    reached: set[str] = set()
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        for index, position in aliases.get(name, []):
            taken.setdefault(index, set()).add(position)
        for index in defs.get(name, []):
            kept.add(index)
            pending += _named(body[index])
    sliced: list[ast.stmt] = []
    for index, node in enumerate(body):
        if index in kept:
            sliced.append(node)
        elif index in taken and isinstance(node, ast.ImportFrom):
            # Empty when no name is used: never compiled, only compared, and
            # the module it imports is still on the line.
            names = [node.names[p] for p in sorted(taken[index])]
            sliced.append(
                ast.ImportFrom(module=node.module, names=names, level=node.level)
            )
    return sliced


def _dunder(name: str) -> bool:
    """Whether Python may look *name* up on the module by itself, as it does a
    module ``__getattr__`` during a ``from`` import: never inert."""
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


#: What an annotation may hold at all: names, attributes, subscripts, unions
#: and constants. A call is not among them. Even these call a hook when the
#: module is imported, since nothing here postpones annotations: ``X[...]``
#: calls ``X.__class_getitem__`` or its metaclass's ``__getitem__``, ``A | B``
#: calls ``__or__`` and ``__ror__``, and ``m.X`` may call a module
#: ``__getattr__``. So ``_inert_def`` also needs whatever a subscript, a ``|``
#: or an attribute is applied to to be a builtin or the standard library's
#: (``_stdlib_names``), whose hooks come with the interpreter.
_INERT_ANNOTATION = (
    ast.Name,
    ast.Attribute,
    ast.Subscript,
    ast.Constant,
    ast.Tuple,
    ast.List,
    ast.BinOp,
    ast.BitOr,
    ast.Load,
)


def _inert_def(
    node: ast.FunctionDef | ast.AsyncFunctionDef, stdlib: frozenset[str]
) -> bool:
    """Whether defining *node* runs nothing: no decorator, constant defaults,
    and annotations of ``_INERT_ANNOTATION`` parts whose every subscript,
    ``|`` operand and attribute applies to an object of *stdlib*
    (``_stdlib_receiver``, ``_stdlib_operand``). Evaluated when the module is imported, every one of
    those can install a hook; a body cannot until it is called. A name
    inside a subscript, as ``Page`` in ``dict[str, Page]``, is only looked
    up."""
    if node.decorator_list:
        return False
    arguments = node.args
    defaults = [*arguments.defaults, *arguments.kw_defaults]
    if not all(d is None or isinstance(d, ast.Constant) for d in defaults):
        return False
    every = (
        *arguments.posonlyargs,
        *arguments.args,
        *arguments.kwonlyargs,
        arguments.vararg,
        arguments.kwarg,
    )
    annotations = [a.annotation for a in every if a is not None and a.annotation]
    if node.returns is not None:
        annotations.append(node.returns)
    for sub in (sub for a in annotations for sub in ast.walk(a)):
        if not isinstance(sub, _INERT_ANNOTATION):
            return False
        if isinstance(sub, (ast.Subscript, ast.Attribute)):
            if not _stdlib_receiver(sub.value, stdlib):
                return False
        elif isinstance(sub, ast.BinOp):
            if not (
                _stdlib_operand(sub.left, stdlib) and _stdlib_operand(sub.right, stdlib)
            ):
                return False
    return True


def _stdlib_receiver(node: ast.expr, stdlib: frozenset[str]) -> bool:
    """Whether *node* is a builtin or the standard library's own object: a
    name of *stdlib* or an attribute chain on one. Never a subscript's
    result: a generic alias carries its arguments, and those can be any
    object (``dict[str, X].__args__[1]`` is ``X``)."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, ast.Name) and node.id in stdlib


def _stdlib_operand(node: ast.expr, stdlib: frozenset[str]) -> bool:
    """Whether a ``|`` operand's hooks are a builtin's or the standard
    library's: a ``_stdlib_receiver``, None, a generic alias of one such as
    ``dict[str, int]``, or a union of those."""
    if isinstance(node, ast.Constant):
        return node.value is None
    if isinstance(node, ast.Subscript):
        return _stdlib_receiver(node.value, stdlib)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _stdlib_operand(node.left, stdlib) and _stdlib_operand(
            node.right, stdlib
        )
    return _stdlib_receiver(node, stdlib)


def _stdlib_names(body: list[ast.stmt]) -> frozenset[str]:
    """The module-level names of *body* that hold a builtin or the standard
    library's: a builtin no statement rebinds, and a name only a top-level
    import from the standard library binds. A name bound anywhere else, as
    ``dict = Hook`` or ``class Any: ...``, is not, and a star import from
    outside the standard library, at any depth, leaves none, since it may
    rebind any."""
    if any(
        isinstance(sub, ast.ImportFrom)
        and not _stdlib_from(sub)
        and any(a.name == "*" for a in sub.names)
        for node in body
        for sub in ast.walk(node)
    ):
        return frozenset()
    imported: set[str] = set()
    other: set[str] = set()
    for node in body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.partition(".")[0]
                (imported if _stdlib_module(alias.name) else other).add(name)
        elif isinstance(node, ast.ImportFrom):
            stdlib = _stdlib_from(node)
            for alias in node.names:
                (imported if stdlib else other).add(alias.asname or alias.name)
        else:
            other |= _bound_names(node)
    return frozenset((imported | set(dir(builtins))) - other)


def _stdlib_from(node: ast.ImportFrom) -> bool:
    return node.level == 0 and _stdlib_module(node.module or "")


def _stdlib_module(module: str) -> bool:
    return module.partition(".")[0] in sys.stdlib_module_names


def _named(node: ast.AST) -> list[str]:
    """Every module-level name *node* can reach: a name it reads or writes,
    a ``global``, or a string spelling one."""
    named: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            named.append(sub.id)
        elif isinstance(sub, (ast.Global, ast.Nonlocal)):
            named += sub.names
        elif (
            isinstance(sub, ast.Constant)
            and isinstance(sub.value, str)
            and sub.value.isidentifier()
        ):
            named.append(sub.value)
    return named


def _bound_names(node: ast.stmt) -> set[str]:
    """Every name a module-level statement binds, anywhere inside it."""
    bound: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
            bound.add(sub.id)
        elif isinstance(sub, (*_DEFS, ast.ClassDef)):
            bound.add(sub.name)
        elif isinstance(sub, ast.alias):
            bound.add(sub.asname or sub.name.partition(".")[0])
        elif isinstance(sub, (ast.MatchAs, ast.MatchStar)) and sub.name:
            bound.add(sub.name)
        elif isinstance(sub, ast.MatchMapping) and sub.rest:
            bound.add(sub.rest)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            bound.add(sub.name)
    return bound


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
    #: What of the whole-trace O2 the shared marked drain accounts for, and
    #: nothing else (``shared_reduction``).
    shared: SharedReduction = SharedReduction()
    #: Row browsers begun after the close and before the recovery barrier that
    #: are not the original's, read again from the completed history
    #: (``early_browsers``); empty where no recovery was asked for.
    early_use: tuple[str, ...] = ()
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
    problems += [f"before the recovery barrier: {p}" for p in c.early_use]
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


def _vector_semantics(c: R7Continuation) -> dict[str, Any] | None:
    """The row vector without what the shared marked drain's timing decides.

    Whether that drain found a Chromium helper still to kill after the
    graceful close, and whether a recipient it killed was pinned (``held``)
    or not (``unknown``), is timing, not what the experiment established.
    So only its classes are left out, and an ``unknown`` reads as ``held``
    only when every unknown recipient was one of its calls
    (``SharedReduction``): what is left once that drain is set aside held.
    Every other class, every other unknown, a violation and an incomplete
    trace still differ.
    """
    if c.vector is None:
        return None
    fields = asdict(c.vector)
    shared = set(c.shared.classes)
    fields["signal_classes"] = sorted(
        cls for cls in fields.get("signal_classes") or () if cls not in shared
    )
    if fields.get("o2_traced") == UNKNOWN_O2 and c.shared.unknowns:
        fields["o2_traced"] = HELD_O2
    return fields


def unassigned_operations(phase: PhaseReading | None) -> list[str]:
    """Every traced signal no source reduction assigns to the shared drain,
    over the whole trace and whoever sent it: sender, syscall, signal and
    target. Compared as it is: a potentially nonshared operation one
    execution has and another lacks is a difference, never timing."""
    if phase is None:
        return []
    return sorted(
        f"{call['sender']}:{call['syscall']}:{call['signal']}:{call['target_kind']}"
        for call in phase.calls
        if not call["probe"] and not call["shared"]
    )


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
            f"{call['signal']}:{call['target_kind']}"
            for call in (phase.of(OWNER, IN_PHASE) if phase else [])
            if not call["probe"] and not call["shared"]
        ),
        "unassigned_operations": unassigned_operations(phase),
        "early_use": list(c.early_use),
        "vector": _vector_semantics(c),
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
