"""What a host's quit leaves, read at checkpoints: row H-R3's own evidence.

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


def r3_problems(record: Mapping[str, Any], *, daemon: bool) -> list[str]:
    """Why H-R3's record does not establish its row; empty when it does.

    Every checkpoint, in order and once, with its own times; the read, the
    script, the post-exit hook and the EOF exit all recorded and normal; and
    each reading what its mode requires. A late window is reported and not
    judged; any problem fails the row.
    """
    mode = "daemon" if daemon else "direct"
    role = "owner" if daemon else "server"
    problems: list[str] = []
    if record.get("row") != ROW_H_R3:
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

    call = record.get("call")
    if not isinstance(call, Mapping):
        call = None
        problems.append("the read call was not recorded")
    elif call.get("is_error") is not False or call.get("read_the_post") is not True:
        problems.append("the read did not return the synthetic post")
    host = _mapping(record.get("host"))
    if (
        host.get("exited_on_quit") is not True
        or host.get("exit_code") != 0
        or host.get("killed_by_harness") is not False
    ):
        problems.append(
            f"the host's quit was not a normal EOF exit: exited "
            f"{host.get('exited_on_quit')!r}, status {host.get('exit_code')!r}, "
            f"killed {host.get('killed_by_harness')!r}"
        )
    eof, exit_seen = _ns(host.get("eof_ns")), _ns(host.get("exit_seen_ns"))
    if eof is None or exit_seen is None or exit_seen < eof:
        problems.append("the EOF and the exit after it are not recorded in order")
    actor = record.get("actor")
    if _lifetime(actor) is None:
        problems.append(f"the {role} was never identified")

    points = _points(record)
    labels = [point.get("label") for point in points]
    if labels != list(CHECKPOINTS):
        problems.append(f"the checkpoints were {labels}, not {list(CHECKPOINTS)}")
    found = {point.get("label"): point for point in points}
    key, lock = str(record.get("browser_key")), record.get("lock")
    platform = str(record.get("platform"))
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

    idle = record.get("idle_timeout_seconds")
    before = found.get(BEFORE_QUIT)
    if before is not None and BEFORE_QUIT in readings:
        late = window_problems(before, call, idle_timeout=idle)
        problems += late
        if eof is not None and _ns(before.get("ended_ns")) is not None:
            if before["ended_ns"] > eof:
                problems.append(f"{BEFORE_QUIT}: it ended after the EOF was sent")
        if not late:
            problems += held_problems(readings[BEFORE_QUIT][0], role=role)

    after = found.get(FIRST_POST_EXIT)
    if after is not None and FIRST_POST_EXIT in readings:
        reading, roots = readings[FIRST_POST_EXIT]
        if exit_seen is None or after["began_ns"] < exit_seen:
            problems.append(f"{FIRST_POST_EXIT}: it began before the exit was seen")
        if daemon:
            late = window_problems(after, call, idle_timeout=idle)
            problems += late
            if not late:
                problems += held_problems(reading, role=role)
                earlier = readings.get(BEFORE_QUIT, (None, None))[1]
                if roots is not None and earlier is not None and len(roots) == 1:
                    if not same_lifetime(roots[0], earlier[0] if earlier else None):
                        problems.append(
                            f"{FIRST_POST_EXIT}: the root is not the one "
                            f"{BEFORE_QUIT} read"
                        )
        else:
            # Kept as read: whether the profile is empty yet is the record's,
            # and settlement is judged at ``settled``. What is required is a
            # reading at all: the server gone, and a lock state where the
            # platform can give one.
            if reading.actor != "gone":
                problems.append(
                    f"{FIRST_POST_EXIT}: the server was {reading.actor}, not gone"
                )
            if reading.lock not in (HELD, FREE, UNOBSERVED):
                problems.append(f"{FIRST_POST_EXIT}: the lock was {reading.lock}")

    settled = found.get(SETTLED)
    if settled is not None and SETTLED in readings:
        problems += released_problems(readings[SETTLED][0], role=role)
        previous = _ns((after or {}).get("ended_ns"))
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


def _window(point: Mapping[str, Any], record: Mapping[str, Any]) -> str:
    """``fresh``, ``late``, or ``invalid`` when its times cannot say."""
    call = record.get("call") if isinstance(record.get("call"), Mapping) else None
    problems = window_problems(
        point, call, idle_timeout=record.get("idle_timeout_seconds")
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
    checkpoints = {}
    for label in CHECKPOINTS:
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
    return {
        "row": record.get("row"),
        "mode": record.get("mode"),
        "read": (call.get("is_error"), call.get("read_the_post")),
        "host": (host.get("exited_on_quit"), host.get("exit_code")),
        "owner_exit": left.get("how"),
        "checkpoints": checkpoints,
    }


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
        problems = r3_problems(record, daemon=daemon)
        if problems:
            refusals.append(f"the {name} record is not valid: {problems}")
    if refusals:
        return refusals
    assert reference is not None and repeat is not None
    one, two = semantics(reference), semantics(repeat)
    differences = []
    for name in one:
        if name != "checkpoints" and one[name] != two[name]:
            differences.append(f"{name}: {one[name]!r} then {two[name]!r}")
    for label in CHECKPOINTS:
        first, second = one["checkpoints"][label], two["checkpoints"][label]
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
        problems = r3_problems(record, daemon=is_daemon)
        if problems:
            refusals.append(f"the {name} record is not valid: {problems}")
    return refusals
