"""Row H-R6-contended: a hard kill raced by a pre-armed contender (#808).

H-R6 kills the owner and only then asks for the profile, through a second
call, with no ready contender and no reading of the old browser's members, so
it cannot tell whether a successor's browser ran beside the old one's. This
row can. Windows only, where #808 lives: the killed owner's profile lock and
its browser's kill-on-close Job close together in its teardown, and the Job's
members end asynchronously after that.

After the warm-up read, with the browser live, the row:

1. ties the actor H-R6 kills (the owner in K3, the frozen Direct server in
   K1) and reads its process tree and every process on the profile: the old
   browser's root and members (``actor_tree``);
2. starts the contender (``crash_contender``) on the profile's real lock,
   watching the actor and each of those processes through a process handle
   and never a Job handle, and waits for its first answer, which must be
   ``held``;
3. kills the actor through the handle the row tied it by;
4. waits for the contender's one grant of the lease, given back, and only
   then asks for a successor's browser, so the successor cannot meet the
   contender on the lock: in K3 a read through the same host, whose frontend
   elects a successor owner; in K1 a fresh host's read, a new Direct server.
   Neither waits for the old browser;
5. stops the contender once that read returned and a second passed.

The verdict (``crash_reading``) reads the raw record alone, after the
watcher's completed history added every browser process it sampled and its
tree's root (``harness.browser_processes``). A successor root is one read as
a root on the profile, begun after the row asked for it: **overlap** when it
began while an old browser process, root or member, was still seen alive
after it; **no overlap** when every old browser process was seen gone before
the successor root began, by more than the row waited for the grant before
asking for it; **inconclusive** otherwise, a missing successor, an old
process the row never watched and a possible browser helper whose ancestry
was lost included. Neither the snapshots nor the history is exhaustive: the
watcher samples, and a process whose whole lifetime falls between two
samples is in neither, a known limit. A lease seen free is never an O1
finding by itself; it is reported as a number. The comparison
(``o1_findings``) is the contract's O1:
an overlap in K3 that K1 does not show, the watcher's second root included
(``lane_failures``). K0 runs K3 again and must reach its verdict
(``repeat_findings``). K2 is not applicable.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from differential.call_loss import (
    CALIBRATION_IDLE_TIMEOUT_SECONDS,
    INVALID,
    _phase,
)
from differential.crash_contender import ACTOR, FREE, HELD, Contender
from differential.owner_loss import (
    CALL_END_SECONDS,
    _called,
    _identity,
    _mapping,
    _ns,
    _number,
    _sequence,
)
from differential.retirement_race import WARM_TOOL
from differential.unconfirmed_close import LOCK_FILE
from differential.watcher import (
    USER_DATA_DIR_FLAG,
    canonical_user_data_dir,
    read_arguments,
    user_data_dir,
)

if TYPE_CHECKING:
    from differential.harness import RowContext

ROW = "H-R6-contended"

#: The calibration's idle timeout: long enough that neither the owner nor
#: Direct's browser idles out between the warm-up read and the kill.
IDLE_TIMEOUT_SECONDS = CALIBRATION_IDLE_TIMEOUT_SECONDS

#: How long the contender may take to give its first answer, and its grant.
READY_SECONDS = 30.0
#: Between two rounds of the contender: a lock request and one wait per
#: watched process, each well under a millisecond.
INTERVAL_SECONDS = 0.005
#: The contender's own bound, past the successor's read and its arming.
CONTENDER_DEADLINE_SECONDS = CALL_END_SECONDS + 120.0
#: How long the contender keeps watching after the successor's read returned.
TRAIL_SECONDS = 1.0
#: How long a stopped contender may take to write its result and exit.
STOP_SECONDS = 15.0
#: How far a process's create time and the one first read may differ.
START_TOLERANCE_SECONDS = 0.05
#: How late a kernel creation time may run behind the moment it marks: one
#: default Windows clock tick. An old process seen alive within it of the
#: successor's creation is not shown concurrent.
CLOCK_TOLERANCE_NS = 16_000_000

FEED_ARGUMENTS = {"num_posts": 1}

#: The kinds ``actor_tree`` gives: the browser's root on the profile, any
#: other browser process of it, and the rest of the tree (driver, interpreter).
BROWSER_ROOT = "browser-root"
BROWSER = "browser"
OTHER = "other"

OVERLAP = "overlap"
NO_OVERLAP = "no overlap"
INCONCLUSIVE = "inconclusive"

K2_NOT_APPLICABLE = {
    "status": "not applicable",
    "reason": (
        "the plan names a historical-daemon regression witness only for R6, R7, "
        "R11 and R12, none for #808's race, and the contract forbids inventing one"
    ),
}
NOT_WINDOWS = (
    "#808 is a Windows race: only there do the killed owner's lock and its "
    "browser's kill-on-close Job close together, with the Job's members ending "
    "asynchronously after; POSIX has no Job, and H-R6 covers its guardian"
)


# --- Reading processes ------------------------------------------------------------


def _kind(cmdline: Sequence[str] | None, browser_key: str) -> str:
    """A browser root on the profile, another browser process on it, or other."""
    if not cmdline:
        return OTHER
    if user_data_dir(cmdline) == browser_key:
        return BROWSER_ROOT
    for argument in cmdline:
        if argument.startswith(USER_DATA_DIR_FLAG):
            value = argument[len(USER_DATA_DIR_FLAG) :]
            if canonical_user_data_dir(value) == browser_key:
                return BROWSER
    return OTHER


def _entry(
    process: Any, browser_key: str, *, descendant: bool
) -> dict[str, Any] | None:
    """One process as the row records it, its kind provisional: None when it
    is gone, or neither the actor's nor on the profile. A relevant process
    whose times cannot be read raises, since it could not be watched."""
    try:
        try:
            cmdline = read_arguments(process, "cmdline")
        except psutil.AccessDenied:
            cmdline = None
        kind = _kind(cmdline, browser_key)
        if kind == OTHER and not descendant:
            return None
        with process.oneshot():
            return {
                "pid": process.pid,
                "start": process.create_time(),
                "ppid": process.ppid(),
                "kind": kind,
            }
    except psutil.NoSuchProcess:
        return None


def actor_tree(pid: int, start: float, browser_key: str) -> list[dict[str, Any]]:
    """The actor's descendants and every process on the profile, now.

    A descendant of a browser process is a browser process too. Raises when
    *pid* no longer names the actor created at *start*.
    """
    actor = psutil.Process(pid)
    if abs(actor.create_time() - start) > START_TOLERANCE_SECONDS:
        raise RuntimeError(f"pid {pid} no longer names the actor")
    descendants = {child.pid: child for child in actor.children(recursive=True)}
    entries: dict[int, dict[str, Any]] = {}
    # The descendants first, then the whole table for a browser process whose
    # parent chain no longer reaches the actor.
    for process in [*descendants.values(), *psutil.process_iter()]:
        if process.pid in entries or process.pid == pid:
            continue
        entry = _entry(process, browser_key, descendant=process.pid in descendants)
        if entry is not None:
            entries[process.pid] = entry
    browsers = {key for key, entry in entries.items() if entry["kind"] != OTHER}
    changed = True
    while changed:
        changed = False
        for key, entry in entries.items():
            if entry["kind"] == OTHER and entry["ppid"] in browsers:
                entry["kind"] = BROWSER
                browsers.add(key)
                changed = True
    return sorted(entries.values(), key=lambda entry: (entry["start"], entry["pid"]))


def untracked(
    armed: Sequence[Mapping[str, Any]], now: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The processes in *now* that *armed* does not hold, by id and start."""
    return [
        dict(entry)
        for entry in now
        if not any(
            entry["pid"] == known["pid"]
            and abs(entry["start"] - known["start"]) <= START_TOLERANCE_SECONDS
            for known in armed
        )
    ]


# --- The script -------------------------------------------------------------------


async def script(ctx: RowContext) -> None:
    """The row's scripted phase, after the warm-up read; see the module."""
    record = ctx.record
    problems: list[str] = record["observation_problems"]
    seams = ctx.owner_loss
    if seams is None:
        problems.append(f"{INVALID}the row was given no way to kill its actor")
        return
    if ctx.daemon:
        record["owner_identified"] = _identity(ctx)
        if record["owner_identified"] is None:
            problems.append(f"{INVALID}the owner was never identified")
            return
    prepared = await seams.prepare()
    record["prepared"] = prepared
    pid, start = prepared.get("pid"), _number(prepared.get("start_identity"))
    if prepared.get("error") or type(pid) is not int or start is None:
        problems.append(
            f"{INVALID}the actor to kill was not tied: {prepared.get('error')!r}"
        )
        return
    contended: dict[str, Any] = {
        "role": "owner" if ctx.daemon else "server",
        "actor": {"pid": pid, "start": start},
    }
    record["contended"] = contended
    key = ctx.account.browser_key
    try:
        armed = await ctx.run_owned(
            "the actor's tree", actor_tree, pid, start, key, seconds=30.0
        )
    except Exception as exc:  # noqa: BLE001 - the row's evidence
        problems.append(f"{INVALID}the actor's tree was not read: {exc!r}")
        return
    contended["armed"] = armed
    if not any(entry["kind"] == BROWSER_ROOT for entry in armed):
        problems.append(f"{INVALID}no live browser root on the profile to race")
        return
    # A contender that would not stop may still hold its files open.
    with tempfile.TemporaryDirectory(
        prefix="crash-contender-", ignore_cleanup_errors=True
    ) as scratch:
        contender = Contender(
            Path(scratch),
            ctx.account.auth_root / LOCK_FILE,
            [{"pid": pid, "start": start, "kind": ACTOR}, *armed],
            interval=INTERVAL_SECONDS,
            deadline=CONTENDER_DEADLINE_SECONDS,
        )
        try:
            await _race(ctx, contender)
        finally:
            contended["contender"] = await ctx.run_owned(
                "stop the contender",
                contender.stop,
                STOP_SECONDS,
                seconds=STOP_SECONDS + 15.0,
                gated=False,
            )
            if not contender.reaped:
                # Left to the next gate: nothing is measured while it runs.
                ctx.retain("the crash contender", contender.end)


async def _race(ctx: RowContext, contender: Contender) -> None:
    """Arm the contender, kill the actor and ask for a successor at once."""
    record = ctx.record
    problems: list[str] = record["observation_problems"]
    contended = record["contended"]
    seams = ctx.owner_loss
    assert seams is not None
    await ctx.run_owned("start the contender", contender.start, seconds=30.0)
    ready = await ctx.run_owned(
        "the contender's first answer",
        contender.wait_ready,
        READY_SECONDS,
        seconds=READY_SECONDS + 15.0,
    )
    contended["ready"] = ready
    if ready is None or ready.get("state") != HELD:
        problems.append(
            f"{INVALID}the contender did not see the lease held before the kill: "
            f"{ready!r}"
        )
        return
    _phase(ctx, "contender armed")
    kill: dict[str, Any] = {
        "wall_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
    }
    contended["kill"] = kill
    fault = await seams.kill()
    kill.update(exit=fault.get("exit"), fired_ns=fault.get("monotonic_ns"))
    if fault.get("exit") != "killed":
        problems.append(
            f"{INVALID}the actor was not shown killed: {fault.get('exit')!r}"
        )
        return
    _phase(
        ctx,
        "owner killed" if ctx.daemon else "server killed",
        fault.get("monotonic_ns"),
    )
    contended["granted"] = await ctx.run_owned(
        "the contender's grant",
        contender.wait_granted,
        READY_SECONDS,
        seconds=READY_SECONDS + 15.0,
    )
    if contended["granted"] is None:
        problems.append(f"{INVALID}the contender was never granted the lease")
        return
    successor: dict[str, Any] = {"issued_wall_ns": time.time_ns()}
    contended["successor"] = successor
    if ctx.daemon:
        successor["via"] = "the same frontend"
        await _called(ctx, WARM_TOOL, dict(FEED_ARGUMENTS))
    else:
        successor.update(via="a fresh host", **await seams.fresh_read())
    _phase(ctx, "successor returned")
    await asyncio.sleep(TRAIL_SECONDS)


# --- Reading a record -------------------------------------------------------------


@dataclass(frozen=True)
class _Life:
    """An old process as the contender saw it: last seen alive and first seen
    gone, on the wall clock; ``known`` when its handle was opened on it, or it
    was already gone when the contender first looked."""

    pid: int
    start: float
    kind: str
    known: bool
    alive_until: int | None
    gone_by: int | None


def _lives(contender: Mapping[str, Any]) -> list[_Life]:
    lives = []
    for raw in _sequence(contender.get("watched")):
        entry = _mapping(raw)
        pid, start = entry.get("pid"), _number(entry.get("start"))
        if type(pid) is not int or start is None:
            continue
        lives.append(
            _Life(
                pid=pid,
                start=start,
                kind=str(entry.get("kind")),
                known=(
                    entry.get("opened") is True and entry.get("verified") is not False
                )
                or entry.get("gone_before_watched") is True,
                alive_until=_ns(entry.get("last_alive_wall_ns")),
                gone_by=_ns(entry.get("gone_wall_ns")),
            )
        )
    return lives


def _is_browser(life: _Life) -> bool:
    return life.kind in (BROWSER_ROOT, BROWSER)


@dataclass(frozen=True)
class Reading:
    """One lane's verdict, why, and its numbers."""

    verdict: str
    reasons: tuple[str, ...]
    #: Milliseconds from the kill: the lease first seen free, the actor, the
    #: old root, the last old browser process and the last old process seen
    #: gone, and the successor's root begun; None where not observed.
    ms: Mapping[str, float | None]
    #: The old browser processes seen alive after the successor's root began.
    overlapping: tuple[tuple[int, float, str], ...] = ()
    #: The old driver or interpreter seen alive then: reported, no O1 finding.
    others_overlapping: tuple[tuple[int, float, str], ...] = ()
    #: Whether the lease came free while an old process was still seen alive:
    #: #808's premise, never an O1 finding by itself.
    lease_free_while_alive: bool | None = None

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


def _ms(at: int | None, kill: int) -> float | None:
    return None if at is None else round((at - kill) / 1e6, 3)


def _successor_root(
    record: Mapping[str, Any], since: int, armed: Sequence[Mapping[str, Any]]
) -> tuple[int, float] | None:
    """The first browser root on the profile begun no earlier than *since*,
    from the watcher's completed history (``harness.browser_processes``),
    which roots a tree only on a root's command line on the profile. *since*
    is when the successor was asked for: no root begun before that is its."""
    found = []
    for raw in _sequence(record.get("browser_processes")):
        entry = _sequence(raw)
        if len(entry) < 5 or type(entry[0]) is not int:
            continue
        pid, start = entry[0], _number(entry[1])
        if start is None or entry[3] != pid or _number(entry[4]) != start:
            continue
        if start * 1e9 < since:
            continue
        if untracked(armed, [{"pid": pid, "start": start}]):
            found.append((pid, start))
    return min(found, key=lambda root: root[1]) if found else None


def _unarmed(
    record: Mapping[str, Any],
    kill: int,
    armed: Sequence[Mapping[str, Any]],
    admitted: int,
    before: int,
) -> list[int] | None:
    """The old browser processes the watcher's completed history
    (``harness.browser_processes``) holds against the row; None without it.

    A root or member is old in a tree whose root began before the kill,
    whenever it began itself, and counts when not shown gone before the kill
    and never armed. A possible helper that history could not put in any
    tree counts when begun before the successor's root, which alone owns its
    tree, and not shown gone *before* that, armed or not: its ancestry was
    lost, and the kill's stamp precedes the kill, so its birth after the
    stamp says nothing of whose it is.
    """
    history = _sequence(record.get("browser_processes"))
    if not history:
        return None
    missed = []
    for raw in history:
        entry = _sequence(raw)
        if len(entry) < 5 or type(entry[0]) is not int:
            continue
        start, gone, root = _number(entry[1]), _number(entry[2]), _number(entry[4])
        if start is None:
            continue
        if root is None:
            if start * 1e9 < admitted and (gone is None or gone * 1e9 >= before):
                missed.append(entry[0])
            continue
        if root * 1e9 >= kill or (gone is not None and gone * 1e9 < kill):
            continue
        if untracked(armed, [{"pid": entry[0], "start": start}]):
            missed.append(entry[0])
    return missed


def crash_reading(record: Mapping[str, Any]) -> Reading:
    """The lane's verdict from its raw record alone; see the module docstring."""
    contended = _mapping(record.get("contended"))
    contender = _mapping(contended.get("contender"))
    kill_record = _mapping(contended.get("kill"))
    kill = _ns(kill_record.get("wall_ns"))
    armed = [_mapping(entry) for entry in _sequence(contended.get("armed"))]
    if kill is None or kill_record.get("exit") != "killed":
        return Reading(INCONCLUSIVE, ("the actor was not shown killed",), {})
    gates: list[str] = []
    if contender.get("error"):
        gates.append(f"the contender failed: {contender['error']}")
    ready = _mapping(contender.get("ready"))
    ready_at = _ns(ready.get("wall_ns"))
    if ready.get("state") != HELD or ready_at is None or ready_at >= kill:
        gates.append("the contender did not see the lease held before the kill")
    runs = [_mapping(run) for run in _sequence(contender.get("lease"))]
    if any(
        run.get("state") == FREE and (_ns(run.get("first_wall_ns")) or kill) < kill
        for run in runs
    ):
        gates.append("the lease was seen free before the kill")
    lives = _lives(contender)
    actor = next((life for life in lives if life.kind == ACTOR), None)
    old = [life for life in lives if life.kind != ACTOR]
    browsers = [life for life in old if _is_browser(life)]
    if not any(life.kind == BROWSER_ROOT for life in browsers):
        gates.append("no old browser root was watched")
    free_at = next(
        (
            _ns(run.get("first_wall_ns"))
            for run in runs
            if run.get("state") == FREE and (_ns(run.get("first_wall_ns")) or 0) >= kill
        ),
        None,
    )
    issued = _ns(_mapping(contended.get("successor")).get("issued_wall_ns"))
    successor = _successor_root(record, kill if issued is None else issued, armed)
    admitted = int(successor[1] * 1e9) if successor is not None else None
    root = next((life for life in browsers if life.kind == BROWSER_ROOT), None)
    ms = {
        "lease_free": _ms(free_at, kill),
        "actor_gone": _ms(actor.gone_by if actor else None, kill),
        "old_root_gone": _ms(root.gone_by if root else None, kill),
        "last_browser_gone": _ms(_last_gone(browsers), kill),
        "last_old_gone": _ms(_last_gone(old), kill),
        "successor_admitted": _ms(admitted, kill),
    }
    seen_alive = [life.alive_until for life in old if life.alive_until is not None]
    lease_free_while_alive = (
        any(at > free_at for at in seen_alive) if free_at is not None else None
    )

    def alive_after(life: _Life) -> bool:
        return (
            admitted is not None
            and life.known
            and life.alive_until is not None
            and life.alive_until > admitted + CLOCK_TOLERANCE_NS
        )

    overlapping = tuple(
        (life.pid, life.start, life.kind) for life in browsers if alive_after(life)
    )
    others = tuple(
        (life.pid, life.start, life.kind)
        for life in old
        if not _is_browser(life) and alive_after(life)
    )

    def reading(verdict: str, *reasons: str) -> Reading:
        return Reading(
            verdict,
            reasons,
            ms,
            overlapping if verdict == OVERLAP else (),
            others,
            lease_free_while_alive,
        )

    if gates:
        return reading(INCONCLUSIVE, *gates)
    if overlapping:
        return reading(
            OVERLAP,
            f"the successor's root {successor} began while {len(overlapping)} old "
            f"browser process(es) were still seen alive",
        )
    if admitted is None:
        return reading(
            INCONCLUSIVE,
            "no successor browser root began on the profile after the kill; a "
            "lease seen free alone is not an O1 finding",
        )
    if issued is None or free_at is None or free_at > issued:
        return reading(
            INCONCLUSIVE,
            "the successor was not shown asked for after the contender's grant "
            "was given back, so it may have met the contender on the lock",
        )
    # Asked for at the kill, the successor could have begun this much sooner.
    unpostponed = admitted - (issued - kill)
    reasons = [
        f"old browser process {life.pid} ({life.kind}) is not shown gone more "
        f"than {_ms(admitted, unpostponed)} ms, the wait for the grant, before "
        f"the successor's root began"
        for life in browsers
        if not (life.known and life.gone_by is not None and life.gone_by < unpostponed)
    ]
    unarmed = _unarmed(record, kill, armed, admitted, unpostponed)
    if unarmed is None:
        reasons.append("the watcher's history of browser processes is missing")
    reasons += [
        f"the watcher's history holds old browser process {pid}, not shown gone "
        f"in time and never watched as one"
        for pid in unarmed or ()
    ]
    if reasons:
        return reading(INCONCLUSIVE, *reasons)
    return reading(
        NO_OVERLAP,
        "every old browser process was seen gone before the successor's root began",
    )


def _last_gone(lives: Sequence[_Life]) -> int | None:
    """When the last of *lives* was seen gone; None if any was not."""
    gone = [life.gone_by for life in lives]
    if not gone or any(at is None for at in gone):
        return None
    return max(at for at in gone if at is not None)


def problems_for(record: Mapping[str, Any], *, daemon: bool) -> list[str]:
    """The row's verdict (``harness.ROW_VERDICTS``): invalid evidence and an
    inconclusive reading are problems; an overlap is a measurement, which
    ``o1_findings`` holds to Direct."""
    problems: list[str] = []
    mode = "daemon" if daemon else "direct"
    if record.get("mode") != mode:
        problems.append(f"the record is for mode {record.get('mode')!r}, not {mode}")
    if record.get("platform") != "win32":
        problems.append(f"{INVALID}{NOT_WINDOWS}")
    if record.get("script_error"):
        problems.append(f"the row's script failed: {record['script_error']}")
    problems += [str(p) for p in _sequence(record.get("observation_problems"))]
    if record.get("idle_timeout_seconds") != IDLE_TIMEOUT_SECONDS:
        problems.append(
            f"the row ran with an idle timeout of "
            f"{record.get('idle_timeout_seconds')!r}, not {IDLE_TIMEOUT_SECONDS}"
        )
    if record.get("k2") != K2_NOT_APPLICABLE:
        problems.append("the record does not say why K2 is not applicable")
    calls = [_mapping(call) for call in _sequence(record.get("calls"))]
    if (
        not calls
        or calls[0].get("tool") != WARM_TOOL
        or not calls[0].get("read_the_post")
    ):
        problems.append(f"{INVALID}the warm-up read is not recorded as returned")
    reading = crash_reading(record)
    if reading.verdict == INCONCLUSIVE:
        problems.append(f"{INVALID}inconclusive: {'; '.join(reading.reasons)}")
    return problems


def o1_findings(direct: Reading | None, daemon: Reading | None) -> list[str]:
    """The contract's O1 between the lanes: the daemon's overlap that Direct
    does not show. An overlap in both is a shared hazard, not a finding here;
    a lane missing or inconclusive decides nothing, and says so."""
    if direct is None or daemon is None:
        return ["both lanes must have been read in this process first"]
    undecided = [
        f"{name} is {reading.verdict}: {'; '.join(reading.reasons)}"
        for name, reading in (("K1 frozen", direct), ("K3", daemon))
        if reading.verdict == INCONCLUSIVE
    ]
    if undecided:
        return undecided
    if daemon.verdict == OVERLAP and direct.verdict != OVERLAP:
        return [
            f"O1: the successor owner's browser ran beside the killed owner's "
            f"{list(daemon.overlapping)}, which the frozen Direct server's did not"
        ]
    return []


def repeat_findings(candidate: Reading | None, repeat: Reading | None) -> list[str]:
    """K0 against K3: the same verdict. The milliseconds are a race's and
    differ between two healthy runs, so they are reported, never compared."""
    if candidate is None or repeat is None:
        return ["K3 and K0 must both have been read in this process first"]
    if repeat.verdict != candidate.verdict:
        return [f"K3 read {candidate.verdict}, its repeat {repeat.verdict}"]
    return []


def lane_failures(failures: Sequence[str]) -> list[str]:
    """A lane's row failures, less the watcher's second root on the profile:
    the concurrency this row measures, which ``o1_findings`` judges between
    the lanes. The watcher's health still fails on its own."""
    # Here, not at the top: the harness imports this module for its rows.
    from differential.harness import SECOND_BROWSER

    return [failure for failure in failures if failure != SECOND_BROWSER]
