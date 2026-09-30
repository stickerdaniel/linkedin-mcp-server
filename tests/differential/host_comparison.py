"""What a host's quit leaves, read at checkpoints: rows H-R3 and H-R2.

**The row.** The host actions are H-R1's: start, one ``get_feed`` call, stdin
EOF. What H-R3 adds is three readings of the profile around that quit, taken
by the harness (``harness.observe_checkpoint``) and judged here from the raw
record alone, so the verdict can be replayed from the published packet:

* ``before quit``: from the row's script, after the read returned;
* ``first post-exit``: from the host stub's post-exit hook, once the server
  has exited on EOF and before the stub waits for its stderr to close;
* ``settled``: after the owner's own exit (daemon) and a bounded passive wait
  for the profile's browser, before any cleanup, sweep or preservation.

**Freshness is measured from the send.** An expectation that depends on the
actor not having idled out yet holds only while the checkpoint *ends* inside
the idle timeout, less a margin, counted from when the harness sent the last
call. Receipt would be later than the actor's own quiet origin and could
hide an ordinary idle exit, so the earlier send is the conservative anchor.
A late window is an evidence failure, never a finding about the product, and
nothing in it is judged.

**Roots are the watcher's.** ``census_roots`` applies ``watcher.browser_roots``
to the census, so a renderer inside a root's tree is no root of its own. The
census itself is kept whole: an unresolved process, or an entry whose parent
or start could not be read, leaves the root count unknown, never zero.

**Capabilities are the platform's, not the record's.** The lock contender
(``lease_probe``) answers on POSIX, so Linux and macOS must answer at every
checkpoint that claims a lock state; only Linux can name the holder
(``lock_association``). Windows lock state is ``unobserved`` and is never
credited as held or free.

Direct and the daemon differ in one place: after the host's exit the Direct
server has closed its browser, while the owner keeps it until its own idle
exit. Direct's first post-exit reading is kept as it was read, even when it
is not yet empty and settlement follows; the daemon's must still show the
owner, its root and the held lock, in a fresh window. Neither mode needs the
browser or the lease to outlive the owner's process: at settlement the owner
has exited by itself and the profile is empty and free.

**H-R2** puts a second host, B, inside A's life on the same profile, with
the same launch: A reads (A1), B starts, reads and quits, A reads again (A2)
and quits. Its checkpoints are ``after A1``, ``after B quit`` (from B's own
post-exit hook), then A's ``first post-exit`` and ``settled``. Every read has
to be served by a request that arrived inside its own interval
(``attribute_requests``). In daemon mode the hot reuse is the claim: one
owner lifetime and instance throughout, B forwarding to it, and each gap
fresh from the last send; a late gap says the reuse was not observed.

No process is read here: this module holds readers and verdicts only.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from differential.watcher import ProcessRecord, browser_roots

ROW_H_R3 = "H-R3"

BEFORE_QUIT = "before quit"
FIRST_POST_EXIT = "first post-exit"
SETTLED = "settled"
CHECKPOINTS = (BEFORE_QUIT, FIRST_POST_EXIT, SETTLED)

#: How far inside the idle timeout a checkpoint that relies on the actor not
#: having idled out must end, counted from the last call's send.
FRESHNESS_MARGIN_SECONDS = 5.0

#: K2 is a regression-control column: the V7 matrix names no historical
#: daemon witness for this row, and the contract forbids inventing one.
K2_NOT_APPLICABLE = {
    "status": "not applicable",
    "reason": (
        "the plan specifies no historical-daemon regression witness for this "
        "row, and the contract forbids inventing one"
    ),
}

#: The lock contender's and the holder association's answers.
HELD = "held"
FREE = "free"
UNKNOWN = "unknown"
#: A lock file other than the one the row identified before the quit.
REPLACED = "replaced"
#: A platform whose helpers cannot answer: recorded, never credited.
UNOBSERVED = "unobserved"
ACTOR = "the actor"
NOT_ACTOR = "not the actor"

_START_TOLERANCE_SECONDS = 0.01


def capabilities(platform: str) -> tuple[bool, bool]:
    """Whether the lock contender answers, and whether the holder can be named.

    The contender takes POSIX ``flock``; ``/proc/locks`` is Linux's alone.
    """
    windows = platform.startswith("win")
    return not windows, platform.startswith("linux")


def _mapping(value: Any) -> Mapping[str, Any]:
    """*value* if it is a mapping, else an empty one: a missing part reads as
    every field missing, never as a field that holds."""
    return value if isinstance(value, Mapping) else {}


def _ns(value: Any) -> int | None:
    """A monotonic reading in nanoseconds, or None for anything that is not."""
    return value if type(value) is int and value >= 0 else None


def _lifetime(value: Any) -> tuple[int, float] | None:
    """A ``[pid, start]`` pair, or None."""
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 2:
        return None
    pid, start = value
    if type(pid) is not int or not isinstance(start, (int, float)):
        return None
    if isinstance(start, bool) or not math.isfinite(start):
        return None
    return pid, float(start)


def same_lifetime(one: Any, two: Any) -> bool:
    first, second = _lifetime(one), _lifetime(two)
    return (
        first is not None
        and second is not None
        and first[0] == second[0]
        and abs(first[1] - second[1]) <= _START_TOLERANCE_SECONDS
    )


def _pair(value: Any) -> tuple[int, int] | None:
    """A lock identity, ``[device, inode]``, or None."""
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 2:
        return None
    if not all(type(part) is int for part in value):
        return None
    return value[0], value[1]


def census_roots(census: Any, key: str) -> list[tuple[int, float]] | None:
    """The browser roots on *key*, by the watcher's own predicate.

    None when the census cannot say: a process it could not resolve, or an
    entry whose parent, start or profile was not read.
    """
    if not isinstance(census, Mapping) or census.get("unresolved") != []:
        return None
    entries = census.get("entries")
    if not isinstance(entries, list):
        return None
    sample: dict[int, ProcessRecord] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            return None
        pid, ppid = entry.get("pid"), entry.get("ppid")
        life = _lifetime([pid, entry.get("start")])
        if life is None or type(ppid) is not int or "profile" not in entry:
            return None
        profile = entry["profile"]
        if profile is not None and not isinstance(profile, str):
            return None
        sample[life[0]] = ProcessRecord(
            pid=life[0], ppid=ppid, start=life[1], exe=None, cmdline=(), profile=profile
        )
    return [(pid, sample[pid].start) for pid in browser_roots(sample).get(key, ())]


@dataclass(frozen=True)
class Reading:
    """One checkpoint, classified. Every field is a label, never a pid or time."""

    label: str
    error: bool
    #: How many browser roots, or None when the census cannot say.
    roots: int | None
    #: ``empty``, ``occupied`` or ``incomplete``: the whole census, children
    #: included, which settlement needs and a root count does not give.
    census: str
    #: Whether the one root descends from the actor; None when unknown or
    #: when there is not exactly one root.
    root_of_actor: bool | None
    #: ``alive``, ``gone``, ``transition`` (the two reads disagree) or
    #: ``unknown``.
    actor: str
    lock: str
    holder: str


def _root_of_actor(
    point: Mapping[str, Any], root: tuple[int, float], actor: Any
) -> bool | None:
    """Whether *root*'s recorded lineage passes through the actor's lifetime.

    A pid alone is not the actor: the start must match too. A lineage that
    stopped short without reaching it is unknown, not unrelated.
    """
    for lineage in point.get("lineages") or []:
        if not isinstance(lineage, Mapping):
            continue
        if not same_lifetime([lineage.get("pid"), lineage.get("start")], root):
            continue
        ancestors = lineage.get("ancestors")
        if not isinstance(ancestors, list) or _lifetime(actor) is None:
            return None
        if any(same_lifetime(ancestor, actor) for ancestor in ancestors):
            return True
        return False if lineage.get("complete") is True else None
    return None


def _actor_state(point: Mapping[str, Any]) -> str:
    reads = point.get("actor_alive")
    if not isinstance(reads, list) or len(reads) != 2:
        return UNKNOWN
    if any(read is not True and read is not False for read in reads):
        return UNKNOWN
    if reads[0] != reads[1]:
        return "transition"
    return "alive" if reads[0] else "gone"


def _lock_state(point: Mapping[str, Any], original: Any, contender: bool) -> str:
    if not contender:
        return UNOBSERVED
    lock = point.get("lock")
    if not isinstance(lock, Mapping):
        return UNKNOWN
    answer = lock.get("answer")
    if not isinstance(answer, Mapping) or answer.get("state") not in (HELD, FREE):
        return UNKNOWN
    identity = _pair(original)
    if identity is None:
        return UNKNOWN
    asked = _pair([answer.get("device"), answer.get("inode")])
    if _pair(lock.get("now")) != identity or asked != identity:
        return REPLACED
    return answer["state"]


def _holder_state(
    point: Mapping[str, Any], original: Any, actor: Any, association: bool
) -> str:
    if not association:
        return UNOBSERVED
    lock = point.get("lock")
    found = lock.get("association") if isinstance(lock, Mapping) else None
    if not isinstance(found, Mapping):
        return UNKNOWN
    # Tied to the actor's lifetime on both sides of the read and to the lock
    # the row identified: a matching number alone is not the holder.
    tied = (
        same_lifetime(found.get("holder"), actor)
        and found.get("same_before") is True
        and found.get("same_after") is True
        and _pair(found.get("identity")) == _pair(original)
        and _pair(original) is not None
    )
    if not tied:
        return UNKNOWN
    if found.get("state") == "holder":
        return ACTOR
    if found.get("state") == "not the holder":
        return NOT_ACTOR
    return UNKNOWN


def read_checkpoint(
    point: Mapping[str, Any],
    *,
    key: str,
    actor: Any,
    lock: Any,
    platform: str,
) -> tuple[Reading, list[tuple[int, float]] | None]:
    """*point* classified against the actor and the lock the row identified,
    and the roots it found."""
    contender, association = capabilities(platform)
    census = point.get("census")
    roots = census_roots(census, key)
    if roots is None:
        whole = "incomplete"
    else:
        entries = census.get("entries") if isinstance(census, Mapping) else None
        whole = "occupied" if entries else "empty"
    of_actor = (
        _root_of_actor(point, roots[0], actor)
        if roots is not None and len(roots) == 1
        else None
    )
    reading = Reading(
        label=str(point.get("label")),
        error=bool(point.get("error")),
        roots=len(roots) if roots is not None else None,
        census=whole,
        root_of_actor=of_actor,
        actor=_actor_state(point),
        lock=_lock_state(point, lock, contender),
        holder=_holder_state(point, lock, actor, association),
    )
    return reading, roots


def free_problems(lock: Any, original: Any, platform: str, *, label: str) -> list[str]:
    """Why a lock reading taken at a boundary is not free on the lock the
    row identified; nothing where the platform has no contender."""
    state = _lock_state({"lock": lock}, original, capabilities(platform)[0])
    if state in (FREE, UNOBSERVED):
        return []
    detail = _mapping(lock).get("error") or _mapping(_mapping(lock).get("answer")).get(
        "reason"
    )
    return [f"{label}: the lock was {state}, not free ({detail})"]


def window_problems(
    point: Mapping[str, Any],
    call: Mapping[str, Any] | None,
    *,
    idle_timeout: Any,
) -> list[str]:
    """Why *point* is not shown inside the actor's idle timeout after *call*.

    From the call's send to the end of the whole checkpoint, on the harness's
    monotonic clock, with every reading an ordered non-negative integer.
    """
    label = point.get("label")
    if (
        not isinstance(idle_timeout, (int, float))
        or isinstance(idle_timeout, bool)
        or not math.isfinite(idle_timeout)
        or idle_timeout <= FRESHNESS_MARGIN_SECONDS
    ):
        return [f"{label}: the idle timeout {idle_timeout!r} leaves no window"]
    call = call or {}
    sent, received = (
        _ns(call.get("began_monotonic_ns")),
        _ns(call.get("ended_monotonic_ns")),
    )
    began, ended = _ns(point.get("began_ns")), _ns(point.get("ended_ns"))
    if None in (sent, received, began, ended):
        return [f"{label}: the call's or the checkpoint's times are missing or invalid"]
    assert sent is not None and received is not None
    assert began is not None and ended is not None
    if not sent <= received <= began <= ended:
        return [
            f"{label}: the call and the checkpoint are out of order "
            f"(sent {sent}, received {received}, began {began}, ended {ended})"
        ]
    bound = round((idle_timeout - FRESHNESS_MARGIN_SECONDS) * 1_000_000_000)
    if ended - sent >= bound:
        return [
            f"{label}: the window is late: it ended {(ended - sent) / 1e9:.3f}s "
            f"after the read was sent, not within {bound / 1e9:.1f}s; evidence "
            f"only, and nothing in it is judged"
        ]
    return []


def held_problems(reading: Reading, *, role: str) -> list[str]:
    """Why *reading* does not show the actor's one browser and its lease."""
    label = reading.label
    problems = []
    if reading.roots != 1:
        problems.append(
            f"{label}: {reading.roots if reading.roots is not None else 'unknown'} "
            f"browser roots on the profile, not one"
        )
    elif reading.root_of_actor is not True:
        problems.append(f"{label}: the root is not shown to descend from the {role}")
    if reading.actor != "alive":
        problems.append(f"{label}: the {role} was {reading.actor}, not alive")
    if reading.lock not in (HELD, UNOBSERVED):
        problems.append(f"{label}: the lock was {reading.lock}, not held")
    if reading.holder not in (ACTOR, UNOBSERVED):
        problems.append(f"{label}: the holder is {reading.holder}, not the {role}")
    return problems


def released_problems(reading: Reading, *, role: str) -> list[str]:
    """Why *reading* does not show the profile empty and free, the actor gone."""
    label = reading.label
    problems = []
    if reading.census != "empty":
        problems.append(f"{label}: the profile census was {reading.census}, not empty")
    if reading.actor != "gone":
        problems.append(f"{label}: the {role} was {reading.actor}, not gone")
    if reading.lock not in (FREE, UNOBSERVED):
        problems.append(f"{label}: the lock was {reading.lock}, not free")
    return problems


def _points(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    points = record.get("checkpoints")
    if not isinstance(points, list):
        return []
    return [point for point in points if isinstance(point, Mapping)]


def _record_problems(
    record: Mapping[str, Any], *, row: str, daemon: bool, role: str
) -> list[str]:
    """What every comparison record needs before any checkpoint is read."""
    mode = "daemon" if daemon else "direct"
    problems: list[str] = []
    if record.get("row") != row:
        problems.append(f"the record is for row {record.get('row')!r}")
    if record.get("mode") != mode:
        problems.append(f"the record is for mode {record.get('mode')!r}, not {mode}")
    for name, what in (
        ("script_error", "the row's script failed"),
        ("after_exit_error", "the post-exit hook failed"),
    ):
        if record.get(name):
            problems.append(f"{what}: {record[name]}")
    problems += [str(p) for p in record.get("observation_problems") or []]
    return problems


def host_problems(host: Any, *, prefix: str = "") -> list[str]:
    """Why *host* was not one normal quit: started, answered, alive when the
    host quit, its stdin closed, then exited by itself with status 0, and
    both times recorded in order.

    An exit with status 0 and ordered times does not show that EOF reached a
    live server: one that ended before the quit, or whose stdin could not be
    closed, can record both. Each flag has to be recorded as True.
    """
    host = _mapping(host)
    problems = []
    if host.get("error"):
        problems.append(f"{prefix}the host session failed: {host['error']}")
    if host.get("alive_before_quit") is not True:
        problems.append(
            f"{prefix}the server was not shown alive when the host quit: "
            f"{host.get('alive_before_quit')!r}"
        )
    if host.get("stdin_closed") is not True:
        problems.append(
            f"{prefix}the server's stdin was not shown closed: "
            f"{host.get('stdin_closed')!r} ({host.get('stdin_close_error')})"
        )
    if (
        host.get("exited_on_quit") is not True
        or host.get("exit_code") != 0
        or host.get("killed_by_harness") is not False
    ):
        problems.append(
            f"{prefix}the host's quit was not a normal EOF exit: exited "
            f"{host.get('exited_on_quit')!r}, status {host.get('exit_code')!r}, "
            f"killed {host.get('killed_by_harness')!r}"
        )
    eof, exit_seen = _ns(host.get("eof_ns")), _ns(host.get("exit_seen_ns"))
    if eof is None or exit_seen is None or exit_seen < eof:
        problems.append(
            f"{prefix}the EOF and the exit after it are not recorded in order"
        )
    return problems


def _read_all(
    record: Mapping[str, Any], labels: Sequence[str]
) -> tuple[
    list[str],
    dict[Any, Mapping[str, Any]],
    dict[str, tuple[Reading, list[tuple[int, float]] | None]],
]:
    """Every checkpoint, once and in *labels*' order, each classified."""
    problems: list[str] = []
    points = _points(record)
    found_labels = [point.get("label") for point in points]
    if found_labels != list(labels):
        problems.append(f"the checkpoints were {found_labels}, not {list(labels)}")
    found = {point.get("label"): point for point in points}
    key, lock = str(record.get("browser_key")), record.get("lock")
    platform, actor = str(record.get("platform")), record.get("actor")
    readings: dict[str, tuple[Reading, list[tuple[int, float]] | None]] = {}
    for label, point in found.items():
        if point.get("error"):
            problems.append(f"{label}: the checkpoint failed: {point['error']}")
            continue
        began, ended = _ns(point.get("began_ns")), _ns(point.get("ended_ns"))
        if began is None or ended is None or ended < began:
            problems.append(f"{label}: the checkpoint's own times are not in order")
            continue
        readings[str(label)] = read_checkpoint(
            point, key=key, actor=actor, lock=lock, platform=platform
        )
        # Whatever the mode allows the profile to hold at this checkpoint, a
        # census that could not be read whole is no observation of it.
        if readings[str(label)][0].census == "incomplete":
            problems.append(
                f"{label}: the census is incomplete or malformed, so the "
                f"observation is incomplete"
            )
    return problems, found, readings


def _same_root(
    readings: Mapping[str, tuple[Reading, list[tuple[int, float]] | None]],
    label: str,
    earlier: str,
) -> list[str]:
    roots = readings[label][1]
    before = readings.get(earlier, (None, None))[1]
    if roots is not None and before is not None and len(roots) == 1:
        if not same_lifetime(roots[0], before[0] if before else None):
            return [f"{label}: the root is not the one {earlier} read"]
    return []


def _post_exit_problems(
    record: Mapping[str, Any],
    found: Mapping[Any, Mapping[str, Any]],
    readings: Mapping[str, tuple[Reading, list[tuple[int, float]] | None]],
    *,
    daemon: bool,
    role: str,
    anchor: Mapping[str, Any] | None,
    earlier: str,
) -> list[str]:
    """``first post-exit``: after the host's exit; for the owner, in a fresh
    window from *anchor*, still its root and its lease; Direct, as read."""
    after = found.get(FIRST_POST_EXIT)
    if after is None or FIRST_POST_EXIT not in readings:
        return []
    problems = []
    reading = readings[FIRST_POST_EXIT][0]
    exit_seen = _ns(_mapping(record.get("host")).get("exit_seen_ns"))
    if exit_seen is None or after["began_ns"] < exit_seen:
        problems.append(f"{FIRST_POST_EXIT}: it began before the exit was seen")
    if daemon:
        late = window_problems(
            after, anchor, idle_timeout=record.get("idle_timeout_seconds")
        )
        problems += late
        if not late:
            problems += held_problems(reading, role=role)
            problems += _same_root(readings, FIRST_POST_EXIT, earlier)
    else:
        # Kept as read: whether the profile is empty yet is the record's, and
        # settlement is judged at ``settled``. What is required is a reading
        # at all: the server gone, and a lock state where the platform can
        # give one.
        if reading.actor != "gone":
            problems.append(
                f"{FIRST_POST_EXIT}: the server was {reading.actor}, not gone"
            )
        if reading.lock not in (HELD, FREE, UNOBSERVED):
            problems.append(f"{FIRST_POST_EXIT}: the lock was {reading.lock}")
    return problems


def _settled_problems(
    record: Mapping[str, Any],
    found: Mapping[Any, Mapping[str, Any]],
    readings: Mapping[str, tuple[Reading, list[tuple[int, float]] | None]],
    *,
    daemon: bool,
    role: str,
) -> list[str]:
    """``settled``: after the owner's own exit, before the cleanup, the
    profile empty and free."""
    settled = found.get(SETTLED)
    if settled is None or SETTLED not in readings:
        return []
    problems = released_problems(readings[SETTLED][0], role=role)
    previous = _ns((found.get(FIRST_POST_EXIT) or {}).get("ended_ns"))
    if previous is None or settled["began_ns"] < previous:
        problems.append(f"{SETTLED}: it began before {FIRST_POST_EXIT} ended")
    cleanup = _ns(record.get("cleanup_began_ns"))
    if cleanup is None or settled["ended_ns"] > cleanup:
        problems.append(f"{SETTLED}: it is not shown to precede the cleanup")
    if daemon:
        left = _mapping(record.get("owner_exit"))
        if left.get("how") != "exited":
            problems.append(
                f"the owner was not seen to exit by itself: {left.get('how')!r}"
            )
        seen = _ns(left.get("seen_ns"))
        if seen is None or settled["began_ns"] < seen:
            problems.append(f"{SETTLED}: it began before the owner's exit was seen")
    return problems


def r3_problems(record: Mapping[str, Any], *, daemon: bool) -> list[str]:
    """Why H-R3's record does not establish its row; empty when it does.

    Every checkpoint, in order and once, with its own times; the read, the
    script, the post-exit hook and the EOF exit all recorded and normal; and
    each reading what its mode requires. A late window is reported and not
    judged; any problem fails the row.
    """
    role = "owner" if daemon else "server"
    problems = _record_problems(record, row=ROW_H_R3, daemon=daemon, role=role)
    call = record.get("call")
    if not isinstance(call, Mapping):
        call = None
        problems.append("the read call was not recorded")
    elif call.get("is_error") is not False or call.get("read_the_post") is not True:
        problems.append("the read did not return the synthetic post")
    host = _mapping(record.get("host"))
    problems += host_problems(host)
    if _lifetime(record.get("actor")) is None:
        problems.append(f"the {role} was never identified")

    read, found, readings = _read_all(record, CHECKPOINTS)
    problems += read
    eof = _ns(host.get("eof_ns"))
    before = found.get(BEFORE_QUIT)
    if before is not None and BEFORE_QUIT in readings:
        late = window_problems(
            before, call, idle_timeout=record.get("idle_timeout_seconds")
        )
        problems += late
        if eof is not None and _ns(before.get("ended_ns")) is not None:
            if before["ended_ns"] > eof:
                problems.append(f"{BEFORE_QUIT}: it ended after the EOF was sent")
        if not late:
            problems += held_problems(readings[BEFORE_QUIT][0], role=role)
    problems += _post_exit_problems(
        record,
        found,
        readings,
        daemon=daemon,
        role=role,
        anchor=call,
        earlier=BEFORE_QUIT,
    )
    problems += _settled_problems(record, found, readings, daemon=daemon, role=role)
    return problems


# --- Row H-R2: a second host while the first is open ------------------------------

ROW_H_R2 = "H-R2"

AFTER_A1 = "after A1"
AFTER_B_QUIT = "after B quit"
R2_CHECKPOINTS = (AFTER_A1, AFTER_B_QUIT, FIRST_POST_EXIT, SETTLED)
#: The three reads, in order: which host made each, and its name.
R2_CALLS = (("A", "A1"), ("B", "B"), ("A", "A2"))
#: When the row read which owner the descriptor names, in daemon mode.
OWNER_READS = ("after A1", "after B", "after A2")


def _is_feed(request: Mapping[str, Any]) -> bool:
    path = str(request.get("path") or "")
    host = str(request.get("host") or "")
    return path.split("?", 1)[0] == "/feed/" and host.split(":", 1)[0] == (
        "www.linkedin.com"
    )


@dataclass(frozen=True)
class Attribution:
    """The origin's feed requests against the calls' intervals.

    ``claimed`` counts, per call, the session-carrying feed requests only
    its own interval contains; ``outside`` and ``unplaced`` are diagnostics
    (a request no interval contains, one with no monotonic arrival), and
    ``contested`` the requests two intervals could claim.
    """

    claimed: dict[str, int]
    outside: int
    unplaced: int
    contested: tuple[str, ...]
    problems: tuple[str, ...]


def attribute_requests(
    calls: Sequence[Mapping[str, Any]], requests: Sequence[Mapping[str, Any]]
) -> Attribution:
    """Which call each feed request belongs to, by its arrival alone.

    The intervals, each a call's send to its receipt on the harness's
    monotonic clock, must be ordered and must not overlap. Each call needs a
    session-carrying ``/feed/`` request inside its own interval: several
    count as that call's, never as more calls, and one from another call's
    interval is never borrowed. A request two intervals could claim settles
    neither; one outside every interval stays a diagnostic.
    """
    problems: list[str] = []
    intervals: list[tuple[str, int, int]] = []
    for call in calls:
        name = str(call.get("call"))
        began = _ns(call.get("began_monotonic_ns"))
        ended = _ns(call.get("ended_monotonic_ns"))
        if began is None or ended is None or ended < began:
            problems.append(f"{name}: the call's interval is missing or reversed")
            continue
        intervals.append((name, began, ended))
    for (first, _, first_end), (second, second_began, _) in zip(
        intervals, intervals[1:]
    ):
        if second_began < first_end:
            problems.append(f"{first} and {second}: the call intervals overlap")
    claimed = {name: 0 for name, _, _ in intervals}
    outside = unplaced = 0
    contested: list[str] = []
    for request in requests:
        if not _is_feed(request) or request.get("session_valid") is not True:
            continue
        arrived = _ns(request.get("monotonic_ns"))
        if arrived is None:
            unplaced += 1
            continue
        owners = [name for name, began, ended in intervals if began <= arrived <= ended]
        if not owners:
            outside += 1
        elif len(owners) > 1:
            contested.append(f"{arrived} could be {' or '.join(owners)}'s")
        else:
            claimed[owners[0]] += 1
    for name in claimed:
        if claimed[name] < 1:
            problems.append(
                f"{name}: no session-carrying feed request arrived inside its "
                f"own interval"
            )
    if contested:
        problems.append(f"a request is claimed by two calls: {contested}")
    return Attribution(
        claimed=claimed,
        outside=outside,
        unplaced=unplaced,
        contested=tuple(contested),
        problems=tuple(problems),
    )


def distinct_roots(events: Sequence[Mapping[str, Any]], key: str) -> list[list]:
    """Every browser root the watcher reported on *key*, as ``[pid, start]``.

    Evidence only: from its ``browser.roots`` changes, each pid paired with
    the start it recorded for a row process there (None when it has none).
    """
    starts: dict[int, set[float]] = {}
    for event in events:
        if event.get("kind") in ("process.start", "process.update"):
            pid, start = event.get("pid"), event.get("start_identity")
            if type(pid) is int and isinstance(start, (int, float)):
                starts.setdefault(pid, set()).add(float(start))
    pids: set[int] = set()
    for event in events:
        if event.get("kind") == "browser.roots":
            for pid in _mapping(event.get("roots")).get(key) or []:
                if type(pid) is int:
                    pids.add(pid)
    found: list[list] = []
    for pid in sorted(pids):
        for start in sorted(starts[pid]) if pid in starts else [None]:
            found.append([pid, start])
    return found


#: What the Direct driver logs, at INFO, when it gives the browser up.
_HANDED_OVER = "Another process is waiting for the browser; handing over"
_IDLE_CLOSED = "Closing idle browser after"


def handoff_reading(lines: Sequence[str]) -> str:
    """How host A's server gave its browser up, from its own log: evidence
    only, since the log level is a setting and a release can go unlogged."""
    if any(_HANDED_OVER in line for line in lines):
        return "handed over"
    if any(_IDLE_CLOSED in line for line in lines):
        return "idle release"
    return "unobserved"


def _call(record: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    for call in record.get("calls") or []:
        if isinstance(call, Mapping) and call.get("call") == name:
            return call
    return None


def _span(label: str, call: Mapping[str, Any] | None) -> dict[str, Any]:
    """A call's own interval, as a window ``window_problems`` can read."""
    call = call or {}
    return {
        "label": label,
        "began_ns": call.get("began_monotonic_ns"),
        "ended_ns": call.get("ended_monotonic_ns"),
    }


def r2_problems(record: Mapping[str, Any], *, daemon: bool) -> list[str]:
    """Why H-R2's record does not establish its row; empty when it does.

    Both hosts quit normally, B nested inside A's life, the same launch for
    both; all three reads succeed, each served by a request inside its own
    interval; every checkpoint in order. Daemon: in windows fresh from the
    last send, the owner's root and lease after A1, after B's quit and after
    A's exit, one owner lifetime and instance throughout, B forwarding, and
    no second owner started. A late gap is evidence that the hot reuse was
    not observed, never a finding. Direct: B's leftovers settled before A2.
    """
    role = "owner" if daemon else "server"
    idle = record.get("idle_timeout_seconds")
    problems = _record_problems(record, row=ROW_H_R2, daemon=daemon, role=role)
    host, other = _mapping(record.get("host")), _mapping(record.get("host_b"))
    problems += host_problems(host)
    problems += host_problems(other, prefix="host B: ")
    if other.get("after_exit_error"):
        problems.append(f"host B's post-exit hook failed: {other['after_exit_error']}")
    if _lifetime(record.get("actor")) is None:
        problems.append(f"the {role} was never identified")

    calls = {name: _call(record, name) for _, name in R2_CALLS}
    listed = [
        (c.get("host"), c.get("call"))
        for c in record.get("calls") or []
        if isinstance(c, Mapping)
    ]
    if listed != list(R2_CALLS):
        problems.append(f"the calls were {listed}, not {list(R2_CALLS)}")
    for name, call in calls.items():
        if call is None:
            continue
        if call.get("is_error") is not False or call.get("read_the_post") is not True:
            problems.append(f"{name} did not return the synthetic post")
    attribution = attribute_requests(
        [call for call in calls.values() if call is not None],
        [r for r in record.get("requests") or [] if isinstance(r, Mapping)],
    )
    problems += list(attribution.problems)

    # The same launch for both hosts, and B wholly inside A's life.
    launch = _mapping(record.get("launch"))
    a, b = _mapping(launch.get("A")), _mapping(launch.get("B"))
    if (
        not a
        or a.get("command") != b.get("command")
        or (a.get("env_sha256") != b.get("env_sha256"))
    ):
        problems.append("host B did not start as host A did")
    opened = _mapping(record.get("a_open"))
    for moment in ("at_b_start", "after_b_exit"):
        if opened.get(moment) is not True:
            problems.append(f"host A was not shown open {moment.replace('_', ' ')}")
    b_launched = _ns(other.get("launched_ns"))
    b_exit, a_eof = _ns(other.get("exit_seen_ns")), _ns(host.get("eof_ns"))
    if b_exit is None or a_eof is None or b_exit > a_eof:
        problems.append("host B's exit is not shown before host A's EOF")
    forwarded = other.get("forwarded")
    if daemon and forwarded is not True:
        problems.append("host B did not report forwarding to the shared owner")
    if not daemon and forwarded is not False:
        problems.append("host B reached a shared owner in Direct mode")

    read, found, readings = _read_all(record, R2_CHECKPOINTS)
    problems += read
    a1, b_call, a2 = calls["A1"], calls["B"], calls["A2"]

    first = found.get(AFTER_A1)
    if first is not None and AFTER_A1 in readings:
        late = window_problems(first, a1, idle_timeout=idle)
        problems += late
        if b_launched is None or first["ended_ns"] > b_launched:
            problems.append(f"{AFTER_A1}: it is not shown to end before B started")
        if not late:
            problems += held_problems(readings[AFTER_A1][0], role=role)

    # The idle-sensitive gaps: B's read must reach the owner A1 left, and
    # A2's the owner B left, each inside the idle timeout from the send.
    late_b = late_a2 = False
    if daemon:
        gap = window_problems(_span("B's read", b_call), a1, idle_timeout=idle)
        late_b = bool(gap)
        problems += [f"hot reuse not established: {p}" for p in gap]
        gap = window_problems(_span("A2's read", a2), b_call, idle_timeout=idle)
        late_a2 = bool(gap)
        problems += [f"hot reuse not established: {p}" for p in gap]
    elif b_call is not None and b_launched is not None:
        if _ns(b_call.get("began_monotonic_ns")) is None or (
            b_call["began_monotonic_ns"] < b_launched
        ):
            problems.append("B's read is not shown after host B started")

    middle = found.get(AFTER_B_QUIT)
    if middle is not None and AFTER_B_QUIT in readings:
        reading = readings[AFTER_B_QUIT][0]
        if b_exit is None or middle["began_ns"] < b_exit:
            problems.append(f"{AFTER_B_QUIT}: it began before B's exit was seen")
        a2_sent = _ns((a2 or {}).get("began_monotonic_ns"))
        if a2_sent is not None and middle["ended_ns"] > a2_sent:
            problems.append(f"{AFTER_B_QUIT}: it ended after A2 was sent")
        if daemon:
            late = window_problems(middle, b_call, idle_timeout=idle)
            problems += late
            if not late and not late_b:
                problems += held_problems(reading, role=role)
                problems += _same_root(readings, AFTER_B_QUIT, AFTER_A1)
        else:
            # Kept as read; host A is still open, whether or not it holds
            # the browser again yet.
            if reading.actor != "alive":
                problems.append(
                    f"{AFTER_B_QUIT}: the server of host A was {reading.actor}"
                )
            if reading.lock not in (HELD, FREE, UNOBSERVED):
                problems.append(f"{AFTER_B_QUIT}: the lock was {reading.lock}")

    if not daemon:
        settled = _mapping(record.get("b_settlement"))
        if settled.get("remaining") != [] or settled.get("unresolved") != []:
            problems.append(
                f"B's browser was not shown gone before A2: remaining "
                f"{settled.get('remaining')!r}, unresolved {settled.get('unresolved')!r}"
            )
        ended = _ns(settled.get("ended_ns"))
        a2_sent = _ns((a2 or {}).get("began_monotonic_ns"))
        if ended is None or a2_sent is None or ended > a2_sent:
            problems.append("B's settlement is not shown to precede A2")
    else:
        problems += _owner_problems(record, late_b=late_b, late_a2=late_a2)

    problems += _post_exit_problems(
        record, found, readings, daemon=daemon, role=role, anchor=a2, earlier=AFTER_A1
    )
    problems += _settled_problems(record, found, readings, daemon=daemon, role=role)
    return problems


def _owner_problems(
    record: Mapping[str, Any], *, late_b: bool, late_a2: bool
) -> list[str]:
    """The same owner lifetime and instance at every read the gaps leave
    fresh, and no other owner started, nor more than one release gate."""
    problems = []
    actor = record.get("actor")
    windows = record.get("platform") == "win32"
    reads = {
        o.get("label"): o for o in record.get("owners") or [] if isinstance(o, Mapping)
    }
    instance = _mapping(reads.get(OWNER_READS[0])).get("instance_id")
    if not isinstance(instance, str) or not instance:
        problems.append("the owner's instance was not read after A1")
    skipped = {"after B": late_b, "after A2": late_b or late_a2}
    for label in OWNER_READS:
        if skipped.get(label):
            continue
        seen = _mapping(reads.get(label))
        if not seen:
            problems.append(f"the owner was not read {label}")
            continue
        if not same_lifetime(seen.get("lifetime"), actor):
            problems.append(
                f"{label}: the descriptor names {seen.get('lifetime')!r}, not the "
                f"owner A1 reached ({seen.get('problem')})"
            )
        if seen.get("instance_id") != instance:
            problems.append(f"{label}: the owner's instance changed")
    if not isinstance(record.get("owner_processes"), list):
        problems.append("the row's owner processes were not recorded")
    elif not (late_b or late_a2):
        launches = owner_launches(record.get("owner_processes") or [], windows=windows)
        if len(launches) != 1 or not _of_launch(
            actor, launches[0], record.get("owner_processes") or []
        ):
            problems.append(
                f"the row started owners {launches}, not only the one A1 reached"
            )
    gates = record.get("gate_processes")
    if not isinstance(gates, list):
        problems.append("the row's release gates were not recorded")
    elif len(owner_launches(gates, windows=windows)) > 1:
        problems.append(
            f"an extra owner start was attempted: release gates "
            f"{owner_launches(gates, windows=windows)}"
        )
    return problems


def _launch_rows(processes: Any) -> list[Sequence[Any]]:
    if not isinstance(processes, list):
        return []
    return [
        p
        for p in processes
        if isinstance(p, Sequence)
        and not isinstance(p, str)
        and len(p) >= 3
        and _lifetime(p[:2]) is not None
    ]


def same_invocation(parent: Sequence[Any], child: Sequence[Any]) -> bool:
    """Whether *child* is the interpreter *parent* started for one invocation.

    The one construction measured, on Windows only (run 36677572983 and every
    Windows K3/K0 of run 36679881422): a venv's ``python.exe`` is a launcher
    that starts the interpreter with the same command line, gate nonce and
    target included, and waits for it. So both carry the same command digest,
    and the child's parent is that lifetime: its pid, begun no later than the
    child, and still found by a sample that started after the one that first
    saw the child ended. Only such a read shows that pid was the parent after
    the child's birth; an exit or a first sample is stamped at a sample's end
    and says nothing of when within it each process was read, so a pid
    reused in between is not excluded by either. The two births are compared
    exactly: both come from the same watcher, and a lifetime born even a
    moment after the child cannot be its parent. A matching number alone, a
    different command or a lifetime without its digest or its sample times is
    its own launch. Each is ``[pid, start, ppid, command digest, exit sample,
    first sample, last read]`` (``harness.launch_lifetimes``).
    """
    if len(parent) < 7 or len(child) < 7:
        return False
    first, read = child[5], parent[6]
    if not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in (first, read)
    ):
        return False
    digest = child[3]
    if not isinstance(digest, str) or not digest or parent[3] != digest:
        return False
    if type(child[2]) is not int or child[2] != parent[0]:
        return False
    began, child_began = float(parent[1]), float(child[1])
    if began > child_began:
        return False
    return float(read) > float(first)


def owner_launches(processes: Sequence[Any], *, windows: bool) -> list[list]:
    """Each launch among *processes*, as ``[pid, start]``.

    *processes* are the lifetimes of every row process that ran one command,
    the owner module or its release gate (``harness.launch_lifetimes``). On
    Windows, one that ``same_invocation`` ties to another of them is that
    launch; nothing else is collapsed, and elsewhere nothing at all.
    """
    rows = _launch_rows(processes)
    if not windows:
        return [list(row[:2]) for row in rows]
    return [
        list(child[:2])
        for child in rows
        if not any(
            parent is not child and same_invocation(parent, child) for parent in rows
        )
    ]


def _of_launch(actor: Any, launch: Sequence[Any], processes: Sequence[Any]) -> bool:
    """Whether *actor* is *launch*, or the interpreter it started for it.

    Only a Windows launch has one: elsewhere ``owner_launches`` keeps every
    process as its own launch, so a single launch has no other process.
    """
    if same_lifetime(actor, launch):
        return True
    rows = _launch_rows(processes)
    parents = [p for p in rows if same_lifetime(p[:2], launch)]
    return any(
        same_lifetime(child[:2], actor) and same_invocation(parent, child)
        for child in rows
        for parent in parents
    )


def problems_for(record: Mapping[str, Any], *, daemon: bool) -> list[str]:
    """The verdict for whichever comparison row *record* is."""
    if record.get("row") == ROW_H_R2:
        return r2_problems(record, daemon=daemon)
    return r3_problems(record, daemon=daemon)


def _anchor(record: Mapping[str, Any], label: str) -> Mapping[str, Any] | None:
    """The send a checkpoint's freshness is counted from."""
    if record.get("row") == ROW_H_R2:
        name = {AFTER_A1: "A1", AFTER_B_QUIT: "B"}.get(label, "A2")
        return _call(record, name)
    call = record.get("call")
    return call if isinstance(call, Mapping) else None


def _window(point: Mapping[str, Any], record: Mapping[str, Any]) -> str:
    """``fresh``, ``late``, or ``invalid`` when its times cannot say."""
    problems = window_problems(
        point,
        _anchor(record, str(point.get("label"))),
        idle_timeout=record.get("idle_timeout_seconds"),
    )
    if not problems:
        return "fresh"
    return "late" if "the window is late" in problems[0] else "invalid"


def semantics(record: Mapping[str, Any]) -> dict[str, Any]:
    """What K0 compares: every classification, and no pid, time or path."""
    call = _mapping(record.get("call"))
    host = _mapping(record.get("host"))
    left = _mapping(record.get("owner_exit"))
    found = {point.get("label"): point for point in _points(record)}
    second = record.get("row") == ROW_H_R2
    checkpoints = {}
    for label in R2_CHECKPOINTS if second else CHECKPOINTS:
        point = found.get(label)
        if point is None:
            checkpoints[label] = None
            continue
        reading, _ = read_checkpoint(
            point,
            key=str(record.get("browser_key")),
            actor=record.get("actor"),
            lock=record.get("lock"),
            platform=str(record.get("platform")),
        )
        checkpoints[label] = {**asdict(reading), "window": _window(point, record)}
    read: dict[str, Any] = {
        "row": record.get("row"),
        "mode": record.get("mode"),
        "read": (call.get("is_error"), call.get("read_the_post")),
        "host": (host.get("exited_on_quit"), host.get("exit_code")),
        "owner_exit": left.get("how"),
        "checkpoints": checkpoints,
    }
    if second:
        other = _mapping(record.get("host_b"))
        calls = [c for c in record.get("calls") or [] if isinstance(c, Mapping)]
        attribution = attribute_requests(
            calls, [r for r in record.get("requests") or [] if isinstance(r, Mapping)]
        )
        read.update(
            read=[
                (c.get("call"), c.get("is_error"), c.get("read_the_post"))
                for c in calls
            ],
            host_b=(
                other.get("exited_on_quit"),
                other.get("exit_code"),
                other.get("forwarded"),
            ),
            served={name: n > 0 for name, n in attribution.claimed.items()},
            owners=[
                (o.get("label"), same_lifetime(o.get("lifetime"), record.get("actor")))
                for o in record.get("owners") or []
                if isinstance(o, Mapping)
            ],
            owner_launches=len(
                owner_launches(
                    record.get("owner_processes") or [],
                    windows=record.get("platform") == "win32",
                )
            ),
            a_open=dict(_mapping(record.get("a_open"))),
        )
    return read


def semantic_differences(
    reference: Mapping[str, Any] | None,
    repeat: Mapping[str, Any] | None,
    *,
    daemon: bool,
) -> list[str]:
    """K0 against its reference: both valid by their own verdict, read again
    here, and alike in every classification. A missing or invalid record is
    a refusal, never an empty difference."""
    refusals = []
    for name, record in (("reference", reference), ("repeat", repeat)):
        if record is None:
            refusals.append(f"no {name} record to compare")
            continue
        problems = problems_for(record, daemon=daemon)
        if problems:
            refusals.append(f"the {name} record is not valid: {problems}")
    if refusals:
        return refusals
    assert reference is not None and repeat is not None
    one, two = semantics(reference), semantics(repeat)
    differences = []
    for name in one:
        if name != "checkpoints" and one[name] != two.get(name):
            differences.append(f"{name}: {one[name]!r} then {two.get(name)!r}")
    for label, first in one["checkpoints"].items():
        second = two["checkpoints"].get(label)
        if first != second:
            differences.append(f"{label}: {first!r} then {second!r}")
    return differences


def comparison_refusals(
    direct: Mapping[str, Any] | None, daemon: Mapping[str, Any] | None
) -> list[str]:
    """Why K3 cannot be held to K1 on this row: a record missing or invalid.

    The comparison itself is the vectors' (``compare_to_direct``); the two
    modes' checkpoints differ by design and are not compared as equal.
    """
    refusals = []
    for name, record, is_daemon in (
        ("Direct", direct, False),
        ("daemon", daemon, True),
    ):
        if record is None:
            refusals.append(f"no {name} record to compare")
            continue
        problems = problems_for(record, daemon=is_daemon)
        if problems:
            refusals.append(f"the {name} record is not valid: {problems}")
    return refusals
