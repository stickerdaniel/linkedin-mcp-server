"""O2: the signals a row's traced actors send, whom they reached, and the canaries.

**The oracle is strace, on Linux only, and it sees only what it traces.**
``strace -f -ttt -yy -e trace=<TRACED_SYSCALLS> -p <server|owner> -p
<guardian>`` records every signal those two processes send, and those their
children started after the attach send, from the moment each is attached
until it exits. That is its scope and nothing else: the frontend, the driver,
the browser, a replacement owner and every moment before the attach are
outside it. So a row has two O2 readings. ``traced`` is the oracle's, for
that scope only. ``row`` is what the row can say about every actor, which is
``violated`` on evidence (a traced violation, a dead canary) and otherwise
``unobserved``: nothing here watches the other senders, so their silence is
never promoted to ``held``.

Ubuntu's default ``kernel.yama.ptrace_scope`` is 1, which lets a tracer attach
only to its own descendants, and strace is not an ancestor of the owner, so the
oracle runs ``sudo -n strace``: only on a disposable GitHub-hosted runner,
behind the same guard as the trust step. Where the native rows opted in on
Linux the oracle is *required*: an oracle that cannot run there fails the row.
Anywhere else it is *unavailable* and says why; nothing stands in for it.

**Missing evidence is never an empty trace.** The oracle's outcome is
``complete`` only when strace attached to every pid asked for, followed each
until it ended (an exit line, or the harness's own confirmed kill of it with
no detach reported), exited on its own with status 0 or was detached on
request, and left a trace file whose every line parsed. Anything less is
``incomplete``, whatever it did record.

**A recipient is the lifetime the samples pin down.** The watcher publishes
when each sample began and ended, and on Linux the last pid the kernel had
allocated when it began. A pid target at a send time is the lifetime seen at
that pid in the samples on both sides of it, or seen in the one before when
the kernel allocated that pid to nobody in between. A group target is the set
of processes in that group in those two samples, and it is complete only when
the group did not exist at the watcher's baseline, no read failed around the
send, and every member is pinned down the same way; a member that changed
group, or appeared, between the two samples leaves it unknown. What cannot be
resolved makes the traced O2 *unknown*, never *held*.

**A browser's marker is its launch.** On Linux, Chromium's crashpad handler
double-forks out of the browser's tree, carrying the browser's environment and
with it the random marker the product sets per launch, which is what the
guardian drains by. A process carrying the marker of one of the row's browsers
is in that browser's launched set, from the moment the watcher knew the marker.
The marker says nothing about whether a group is complete.

**The traced O2** holds when every call reached only the sender's launched set
(its principal, the server or owner it belongs to, and that principal's
descendants). A call that returned an error other than ``ESRCH`` is an
attempt, not a delivery; one aimed outside the set is still a violation, as an
attempt. ``ESRCH`` reached nobody, and signal 0 is a liveness probe. A signal is
evidence of a signal, not of a death: deaths are only what was observed.

**Across experiments**, each call has a class, sender role and target kind, and
a row's classes are compared with those Direct's actors send by construction
(``DIRECT_CLASSES``) and those the Direct reference actually sent: the
pre-Path-A guardian's ``killpg`` of the owner's group reaches processes the
owner launched, yet no Direct guardian sends it.

**Canaries** are processes the harness starts before the row in a session of
their own (POSIX) or outside every Job it can leave (Windows). One that dies
during the row died from something no actor should have sent.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from differential.synthetic_origin import OPT_IN_ENV

#: The syscalls that send a signal. ``killpg`` is ``kill`` with a negative pid.
TRACED_SYSCALLS = (
    "kill",
    "tkill",
    "tgkill",
    "pidfd_send_signal",
    "rt_sigqueueinfo",
    "rt_tgsigqueueinfo",
)

HELD = "held"
VIOLATED = "violated"
UNKNOWN = "unknown"
#: No oracle on this platform or runner; canaries alone cannot establish O2.
UNOBSERVED = "unobserved"
#: The oracle ran, but its evidence does not cover what it was asked to.
INCOMPLETE = "incomplete"

#: The oracle's own outcome.
COMPLETE = "complete"
UNAVAILABLE = "unavailable"

#: ``pidfd_send_signal`` flags (``linux/pidfd.h``).
PIDFD_SIGNAL_THREAD = 1 << 0
PIDFD_SIGNAL_THREAD_GROUP = 1 << 1
PIDFD_SIGNAL_PROCESS_GROUP = 1 << 2
_PIDFD_FLAGS = {
    "PIDFD_SIGNAL_THREAD": PIDFD_SIGNAL_THREAD,
    "PIDFD_SIGNAL_THREAD_GROUP": PIDFD_SIGNAL_THREAD_GROUP,
    "PIDFD_SIGNAL_PROCESS_GROUP": PIDFD_SIGNAL_PROCESS_GROUP,
}

#: What Direct's actors send by construction, as ``sender role:target kind``:
#: the guardian's marked drain of browser groups, a server or its driver
#: closing its own browser, and a browser managing itself. The owner's routine
#: close is the same as a Direct server's. The pre-Path-A guardian's kill of its
#: principal's group (``guardian:principal-group``) is not here.
DIRECT_CLASSES = frozenset(
    {
        "guardian:browser-group",
        "guardian:browser",
        "frontend:browser",
        "frontend:browser-group",
        "owner:browser",
        "owner:browser-group",
        "driver:browser",
        "driver:browser-group",
        "browser:browser",
        "browser:browser-group",
        "browser:self",
    }
)

_LINE = re.compile(r"^(?P<tid>\d+)\s+(?P<t>\d+\.\d+)\s+(?P<rest>.*)$")
_NAMES = "|".join(TRACED_SYSCALLS)
_COMPLETE = re.compile(r"^(?P<name>" + _NAMES + r")\((?P<args>.*)\)\s+=\s+(?P<ret>.*)$")
_UNFINISHED = re.compile(
    r"^(?P<name>" + _NAMES + r")\((?P<args>.*)\s<unfinished \.\.\.>$"
)
_RESUMED = re.compile(
    r"^<\.\.\. (?P<name>" + _NAMES + r") resumed>(?P<args>.*)\)\s+=\s+(?P<ret>.*)$"
)
_PIDFD = re.compile(r"<pid:(?P<pid>\d+)>")
_ENDED = re.compile(
    r"^\+\+\+ (exited with -?\d+|killed by \S+( \(core dumped\))?) \+\+\+$"
)


@dataclass(frozen=True)
class SignalCall:
    """One traced signal syscall, as strace wrote it."""

    #: The thread that made the call; with ``-f`` strace names threads.
    tid: int
    t: float
    syscall: str
    signal: str
    #: The call's return value as strace printed it: ``0`` or ``-1 ESRCH (...)``.
    result: str
    #: A process target (``kill`` with a pid, ``tgkill``, a pidfd, a queue).
    target_pid: int | None = None
    #: A group target: ``kill`` with a negative pid, or 0 for the sender's own.
    target_group: int | None = None
    #: The group of this process (a pidfd with ``PIDFD_SIGNAL_PROCESS_GROUP``).
    group_of_pid: int | None = None
    #: ``kill(-1, ...)``: every process the sender may signal.
    everyone: bool = False
    #: Why the target's scope cannot be read, when it cannot.
    unresolvable: str | None = None
    raw: str = ""

    @property
    def probe(self) -> bool:
        """Signal 0 checks that a target exists and delivers nothing."""
        return self.signal == "0"

    @property
    def reached_nobody(self) -> bool:
        return "ESRCH" in self.result

    @property
    def rejected(self) -> bool:
        """The kernel refused it (``EPERM``, ``EINVAL`` ...): an attempt only."""
        return self.result.strip().startswith("-") and not self.reached_nobody

    @property
    def outcome(self) -> str:
        if self.probe:
            return "probe"
        if self.reached_nobody:
            return "reached nobody"
        if self.rejected:
            return "rejected"
        return "delivered"

    def as_event_fields(self) -> dict[str, Any]:
        return {
            "source": "strace",
            "sender_tid": self.tid,
            "sent_at": self.t,
            "syscall": self.syscall,
            "signal": self.signal,
            "result": self.result,
            "outcome": self.outcome,
            "target_pid": self.target_pid,
            "target_group": self.target_group,
            "group_of_pid": self.group_of_pid,
            "everyone": self.everyone,
            "unresolvable": self.unresolvable,
        }


def _arguments(text: str) -> list[str]:
    """The call's arguments, split at top-level commas only."""
    parts, depth, current = [], 0, []
    for char in text:
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    parts.append("".join(current).strip())
    return parts


def _pidfd_flags(text: str) -> int | None:
    """The flags as a number, named or numeric; None if any part is unknown."""
    value = 0
    for part in text.split("|"):
        part = part.strip()
        if part in _PIDFD_FLAGS:
            value |= _PIDFD_FLAGS[part]
            continue
        try:
            value |= int(part, 0)
        except ValueError:
            return None
    return value


def _call(tid: int, t: float, name: str, args: str, ret: str, raw: str) -> SignalCall:
    """One call; ``ValueError`` when its arguments cannot be read."""
    parts = _arguments(args)
    if name == "kill":
        target, signal = int(parts[0]), parts[1]
        if target > 0:
            return SignalCall(tid, t, name, signal, ret, target_pid=target, raw=raw)
        if target == -1:
            return SignalCall(tid, t, name, signal, ret, everyone=True, raw=raw)
        return SignalCall(tid, t, name, signal, ret, target_group=-target, raw=raw)
    if name == "tkill":
        # A thread: its process is known only when the tid is the leader's.
        return SignalCall(
            tid, t, name, parts[1], ret, target_pid=int(parts[0]), raw=raw
        )
    if name == "tgkill":
        return SignalCall(
            tid, t, name, parts[2], ret, target_pid=int(parts[0]), raw=raw
        )
    if name == "rt_sigqueueinfo":
        target, signal = int(parts[0]), parts[1]
        if target > 0:
            return SignalCall(tid, t, name, signal, ret, target_pid=target, raw=raw)
        return SignalCall(
            tid, t, name, signal, ret, unresolvable=f"a queue to {target}", raw=raw
        )
    if name == "rt_tgsigqueueinfo":
        return SignalCall(
            tid, t, name, parts[2], ret, target_pid=int(parts[0]), raw=raw
        )
    # pidfd_send_signal(fd<pid:N>, SIG, info, flags); ``-yy`` names the pid.
    signal = parts[1]
    found = _PIDFD.search(parts[0])
    if found is None:
        return SignalCall(
            tid,
            t,
            name,
            signal,
            ret,
            unresolvable="a pidfd strace could not name",
            raw=raw,
        )
    pid = int(found["pid"])
    flags = _pidfd_flags(parts[3]) if len(parts) > 3 else None
    if flags in (0, PIDFD_SIGNAL_THREAD_GROUP):
        return SignalCall(tid, t, name, signal, ret, target_pid=pid, raw=raw)
    if flags == PIDFD_SIGNAL_PROCESS_GROUP:
        return SignalCall(tid, t, name, signal, ret, group_of_pid=pid, raw=raw)
    return SignalCall(
        tid,
        t,
        name,
        signal,
        ret,
        unresolvable=f"pidfd scope {parts[3] if len(parts) > 3 else '?'!r}",
        raw=raw,
    )


@dataclass
class Trace:
    """What strace wrote: the calls, which tracees ended, and what did not parse."""

    calls: list[SignalCall] = field(default_factory=list)
    #: Tids strace reported ending (``+++ exited ...`` or ``+++ killed by ...``).
    ended: set[int] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)


def read_trace(text: str) -> Trace:
    """Every signal syscall in strace's ``-f -ttt`` output, and its integrity.

    A call split by another thread (``<unfinished ...>`` then ``<... resumed>``)
    is joined. Signal lines (``---``) are skipped. A line that is not strace's,
    a call that cannot be read, a resumed call without its start and a call
    still unfinished at the end are problems: evidence lost, never no call.
    """
    trace = Trace()
    pending: dict[tuple[int, str], tuple[float, str]] = {}
    for raw in text.splitlines():
        if not raw.strip():
            continue
        line = _LINE.match(raw.strip())
        if line is None:
            trace.problems.append(f"not a trace line: {raw[:200]!r}")
            continue
        tid, t, rest = int(line["tid"]), float(line["t"]), line["rest"]
        try:
            if rest.startswith("---"):
                continue
            if rest.startswith("+++"):
                if _ENDED.match(rest) is None:
                    raise ValueError("an unreadable exit line")
                trace.ended.add(tid)
            elif (found := _COMPLETE.match(rest)) is not None:
                trace.calls.append(
                    _call(tid, t, found["name"], found["args"], found["ret"], raw)
                )
            elif (found := _UNFINISHED.match(rest)) is not None:
                pending[(tid, found["name"])] = (t, found["args"])
            elif (found := _RESUMED.match(rest)) is not None:
                began = pending.pop((tid, found["name"]), None)
                if began is None:
                    raise ValueError("resumed without its start")
                trace.calls.append(
                    _call(
                        tid,
                        began[0],
                        found["name"],
                        began[1] + found["args"],
                        found["ret"],
                        raw,
                    )
                )
            else:
                raise ValueError("not a traced call")
        except (ValueError, IndexError) as exc:
            trace.problems.append(f"{exc}: {raw[:200]!r}")
    for (tid, name), (t, _) in pending.items():
        trace.problems.append(f"{name} by {tid} at {t} never finished")
    return trace


def parse_strace(text: str) -> list[SignalCall]:
    """The calls ``read_trace`` found, for callers that want only those."""
    return read_trace(text).calls


# --- What the watcher saw ---------------------------------------------------------


@dataclass(frozen=True)
class Sample:
    """One watcher sample: when it began and ended, and the kernel's last pid."""

    began: float
    ended: float
    #: ``/proc/sys/kernel/ns_last_pid`` as the sample began; None off Linux.
    last_pid: int | None


@dataclass
class Lifetime:
    """One process lifetime, from the watcher's records.

    Its role and group are what the watcher read at each sample, since both
    change: an exec turns the driver's fork into the browser, and a process
    the watcher catches between its exit and its reaping shows no command line
    at all, so it reads as ``other``. ``first_t`` and ``exit_t`` are the ends
    of samples: the first that saw it and the first that no longer did.
    """

    pid: int
    start: float
    #: The parent at the first reading. Not updated: a reparenting to pid 1
    #: says nothing about who launched it.
    ppid: int
    in_row: bool
    first_t: float
    exit_t: float | None = None
    #: The watcher's digest of its browser marker, and when it first had it.
    marker: str | None = None
    marker_t: float | None = None
    #: ``(sample end, actor, group)`` for each reading, in order.
    readings: list[tuple[float, str, int | None]] = field(default_factory=list)

    @property
    def identity(self) -> tuple[int, float]:
        return (self.pid, self.start)

    def alive_at(self, t: float) -> bool:
        return self.first_t <= t and (self.exit_t is None or t < self.exit_t)

    def seen_in(self, sample: Sample) -> bool:
        """Whether *sample* saw this lifetime."""
        return self.first_t <= sample.ended and (
            self.exit_t is None or sample.ended < self.exit_t
        )

    def _reading(self, t: float) -> tuple[float, str, int | None]:
        in_effect = [reading for reading in self.readings if reading[0] <= t]
        return in_effect[-1] if in_effect else self.readings[0]

    def actor_at(self, t: float) -> str:
        """The role the watcher read for it at *t*."""
        return self._reading(t)[1]

    def pgid_at(self, t: float) -> int | None:
        """The group the watcher read for it by *t*."""
        return self._reading(t)[2]

    def was(self, actor: str) -> bool:
        """Whether any reading of it showed *actor*."""
        return any(reading[1] == actor for reading in self.readings)

    def marker_by(self, t: float) -> str | None:
        """Its marker, if the watcher had read it by *t*."""
        if self.marker is None or self.marker_t is None or self.marker_t > t:
            return None
        return self.marker


#: How a recipient was pinned down.
BOTH_SAMPLES = "seen on both sides"
PID_NOT_REUSED = "seen before, pid not reallocated since"


class ProcessHistory:
    """Every lifetime the watcher reported, and its samples, to resolve a send.

    Only processes that appeared, or changed group, after the watcher's first
    sample are reported; ``baseline_pgids`` are the groups that existed then.
    """

    def __init__(
        self, records: Iterable[Mapping[str, Any]], *, outside: Iterable[int] = ()
    ) -> None:
        #: Pids known to be no actor's launch: the harness itself, whose
        #: children (the host's server, the canaries) are in the row without
        #: being any actor's.
        self.outside = frozenset(outside)
        self.lifetimes: list[Lifetime] = []
        self.samples: list[Sample] = []
        self.baseline_pgids: frozenset[int] | None = None
        #: ``(first, last)`` of every failure to open or identify a process
        #: that was not established unrelated: the watcher then has no record
        #: of it, so no group it could have been in. A failure to read its
        #: executable, arguments or parent leaves its record, group included.
        self.failures: list[tuple[float, float]] = []
        current: dict[tuple[int, float], Lifetime] = {}
        for entry in records:
            kind = entry.get("kind")
            if kind == "watcher.ready" and entry.get("baseline_pgids") is not None:
                self.baseline_pgids = frozenset(entry["baseline_pgids"])
                continue
            if kind == "watcher.summary":
                self.samples = [
                    Sample(float(began), float(ended), last)
                    for began, ended, last in entry.get("sample_log") or []
                ]
                self.failures = [
                    (float(e["first"]), float(e.get("last", e["first"])))
                    for e in entry.get("read_failures") or []
                    if isinstance(e.get("first"), (int, float))
                    and any(
                        str(failure).split(":", 1)[0] in ("open", "identity")
                        for failure in e.get("failures") or []
                    )
                ]
                continue
            if kind not in ("process.start", "process.update", "process.exit"):
                continue
            pid, start = entry.get("pid"), entry.get("start_identity")
            if not isinstance(pid, int) or not isinstance(start, (int, float)):
                continue
            key = (pid, float(start))
            t = float(entry.get("t", 0.0))
            known = current.get(key)
            if kind == "process.exit":
                if known is not None and known.exit_t is None:
                    known.exit_t = t
                continue
            if known is None:
                known = Lifetime(
                    pid=pid,
                    start=float(start),
                    ppid=int(entry.get("ppid", -1)),
                    in_row=entry.get("in_row") is True,
                    first_t=t,
                )
                current[key] = known
                self.lifetimes.append(known)
            else:
                known.in_row = known.in_row or entry.get("in_row") is True
            if known.marker is None and entry.get("browser_marker"):
                known.marker, known.marker_t = entry["browser_marker"], t
            # A start or an update: exec, a new group, a marker, an exit image.
            known.readings.append(
                (t, str(entry.get("actor", "other")), entry.get("pgid"))
            )

    # --- by life span, for senders and parents

    def at(self, pid: int, t: float) -> Lifetime | None:
        """The one lifetime reported alive at *pid* at *t*, or None."""
        alive = [
            life for life in self.lifetimes if life.pid == pid and life.alive_at(t)
        ]
        return alive[-1] if len(alive) == 1 else None

    def first(self, pid: int) -> Lifetime | None:
        seen = [life for life in self.lifetimes if life.pid == pid]
        return seen[0] if seen else None

    # --- by samples, for recipients

    def brackets(self, t: float) -> tuple[Sample, Sample] | None:
        """The last sample that ended by *t* and the first that began after it."""
        before = [s for s in self.samples if s.ended <= t]
        after = [s for s in self.samples if s.began >= t]
        if not before or not after:
            return None
        return before[-1], after[0]

    @staticmethod
    def pid_not_reallocated(pid: int, before: Sample, after: Sample) -> bool:
        """The kernel gave *pid* to no process between the two samples.

        Pids are allocated upwards from the last one until they wrap; a window
        whose last pid did not go down allocated exactly the pids above the
        first reading, up to and including the second.
        """
        first, second = before.last_pid, after.last_pid
        if first is None or second is None or second < first:
            return False
        return not first < pid <= second

    def holder(self, pid: int, t: float) -> tuple[Lifetime, str] | str:
        """The lifetime that held *pid* at *t*, and how; or why that is unknown."""
        bracket = self.brackets(t)
        if bracket is None:
            return "no samples on both sides of the send"
        before, after = bracket
        seen_before = [x for x in self.lifetimes if x.pid == pid and x.seen_in(before)]
        seen_after = [x for x in self.lifetimes if x.pid == pid and x.seen_in(after)]
        if (
            seen_before
            and seen_after
            and seen_before[0].identity == seen_after[0].identity
        ):
            return seen_before[0], BOTH_SAMPLES
        if seen_before and not seen_after:
            if self.pid_not_reallocated(pid, before, after):
                return seen_before[0], PID_NOT_REUSED
            return f"pid {pid} may have been reallocated between the samples"
        if seen_after:
            return f"pid {pid} was not seen in the sample before the send"
        return f"pid {pid} was in neither sample around the send"

    def group_members(self, pgid: int, t: float) -> list[tuple[Lifetime, str]] | str:
        """Every process in group *pgid* at *t*, and how; or why that is unknown."""
        if self.baseline_pgids is None:
            return "the watcher's baseline groups are unknown"
        if pgid in self.baseline_pgids:
            return f"group {pgid} existed before the watcher's first sample"
        bracket = self.brackets(t)
        if bracket is None:
            return "no samples on both sides of the send"
        before, after = bracket
        for first, last in self.failures:
            if first <= after.ended and last >= before.began:
                return "a process could not be identified around the send"
        members: list[tuple[Lifetime, str]] = []
        for life in self.lifetimes:
            for sample in (before, after):
                if life.seen_in(sample) and life.pgid_at(sample.ended) is None:
                    return f"{life.identity}'s group was not read around the send"
            was_in = life.seen_in(before) and life.pgid_at(before.ended) == pgid
            is_in = life.seen_in(after) and life.pgid_at(after.ended) == pgid
            if not (was_in or is_in):
                continue
            if life.seen_in(before) and life.seen_in(after):
                if was_in != is_in:
                    return f"{life.identity} changed group between the samples"
                members.append((life, BOTH_SAMPLES))
            elif was_in:
                if not self.pid_not_reallocated(life.pid, before, after):
                    return f"pid {life.pid} may have been reallocated"
                members.append((life, PID_NOT_REUSED))
            else:
                return f"{life.identity} joined between the samples"
        if not members:
            return f"no member of group {pgid} was seen around the send"
        return members

    def group_of(self, life: Lifetime, t: float) -> int | str:
        """The group *life* was in at *t*, or why that is unknown."""
        bracket = self.brackets(t)
        if bracket is None:
            return "no samples on both sides of the send"
        before, after = bracket
        if not life.seen_in(before):
            return f"{life.identity} was not seen before the send"
        group = life.pgid_at(before.ended)
        if group is None:
            return f"{life.identity}'s group was never read"
        if life.seen_in(after) and life.pgid_at(after.ended) != group:
            return f"{life.identity} changed group around the send"
        return group

    # --- launches

    def browsers(self, marker: str, t: float) -> list[Lifetime]:
        """The row's browser processes known by *t* to carry *marker*."""
        return [
            life
            for life in self.lifetimes
            if life.in_row and life.was("browser") and life.marker_by(t) == marker
        ]

    def marked(self, life: Lifetime, t: float) -> bool:
        """Whether *life* carries, by *t*, the marker of one of the row's browsers."""
        marker = life.marker_by(t)
        return marker is not None and bool(self.browsers(marker, t))

    def leader(self, pgid: int, t: float) -> Lifetime | None:
        """The last lifetime at pid *pgid* the watcher saw start by *t*."""
        leaders = [
            life for life in self.lifetimes if life.pid == pgid and life.first_t <= t
        ]
        return leaders[-1] if leaders else None

    def descends(self, life: Lifetime, ancestor: Lifetime, t: float) -> bool | None:
        """Whether *life* is *ancestor* or descends from it; None if unknown."""
        seen: set[tuple[int, float]] = set()
        current: Lifetime | None = life
        while current is not None:
            if current.identity == ancestor.identity:
                return True
            if current.identity in seen:
                return None
            seen.add(current.identity)
            if not current.in_row:
                marker = current.marker_by(t)
                if marker is not None:
                    # Out of the tree, but launched with a row browser.
                    carriers = self.browsers(marker, t)
                    if carriers:
                        found = [self.descends(b, ancestor, t) for b in carriers]
                        if any(found):
                            return True
                        return None if None in found else False
                # Outside the row: not the launch of any row actor.
                return False
            if current.ppid in self.outside:
                # Started by the harness itself, not by the ancestor.
                return False
            parent = self.at(current.ppid, current.first_t)
            if parent is None:
                return None
            current = parent
        return None


# --- The oracle's outcome -----------------------------------------------------------


@dataclass
class OracleOutcome:
    """What the signal oracle delivered, and whether it covers its scope."""

    status: str
    required: bool = False
    reasons: list[str] = field(default_factory=list)
    calls: list[SignalCall] = field(default_factory=list)
    #: Thread id -> process id, read from ``/proc`` while the tracees ran.
    threads: dict[int, int] = field(default_factory=dict)
    #: The pids strace was attached to: the senders it covers.
    traced: list[int] = field(default_factory=list)
    attached_at: float | None = None
    stopped_at: float | None = None
    returncode: int | None = None

    def as_event_fields(self) -> dict[str, Any]:
        fields = asdict(self)
        fields.pop("calls")
        fields.pop("threads")
        fields["calls"] = len(self.calls)
        return fields


# --- O2 ---------------------------------------------------------------------------


@dataclass
class O2Result:
    #: The traced scope's O2: held, violated, unknown, incomplete, unobserved.
    state: str
    #: The whole row's: violated on evidence, otherwise unobserved.
    row: str = UNOBSERVED
    required: bool = False
    classes: tuple[str, ...] = ()
    violations: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    #: Why the oracle's evidence is incomplete, when it is.
    incomplete: list[str] = field(default_factory=list)
    canary_deaths: list[str] = field(default_factory=list)
    #: Each resolved call with the identities it could have reached.
    resolved: list[dict[str, Any]] = field(default_factory=list)
    #: What the traced state covers: senders, interval, syscalls.
    scope: dict[str, Any] = field(default_factory=dict)


def _principal(sender: Lifetime, history: ProcessHistory, t: float) -> Lifetime | None:
    """Whom a sender acts for at *t*: a guardian for the process that started it."""
    if sender.actor_at(t) == "guardian":
        return history.at(sender.ppid, sender.first_t)
    return sender


def _target_kind(
    targets: Sequence[Lifetime],
    sender: Lifetime,
    principal: Lifetime,
    pgid: int | None,
    history: ProcessHistory,
    t: float,
) -> str:
    """What a signal was aimed at, in the terms Direct's construction uses.

    *pgid* is the group signalled, or None for a single process. A group the
    principal leads is its own group whatever is left in it: that is the
    pre-Path-A guardian's ``killpg(owner_group)``. A group a browser leads is a
    browser group, whatever helpers such as crashpad it also holds: the driver
    starts Chromium in a group of its own. So is a leaderless group of
    processes carrying a row browser's marker: crashpad's own.
    """

    def browser(life: Lifetime) -> bool:
        return life.actor_at(t) == "browser" or history.marked(life, t)

    if pgid is not None:
        if pgid == principal.pid:
            return "principal-group"
        leader = history.leader(pgid, t)
        if leader is not None and leader.actor_at(t) == "browser":
            return "browser-group"
        if leader is None and targets and all(browser(life) for life in targets):
            return "browser-group"
        return "descendant-group"
    if len(targets) == 1 and targets[0].identity == sender.identity:
        return "self"
    if targets and all(browser(life) for life in targets):
        return "browser"
    return "descendant"


def _sender(
    call: SignalCall,
    history: ProcessHistory,
    outcome: OracleOutcome,
    traced: Mapping[int, Lifetime | None],
) -> Lifetime | str:
    """The traced process that made *call*, or why it cannot be named."""
    pid = outcome.threads.get(call.tid, call.tid)
    if pid in traced:
        life = traced[pid]
        return life if life is not None else f"traced pid {pid} was never reported"
    life = history.at(pid, call.t)
    if life is None:
        return f"sender {call.tid} unknown"
    if traced and not any(
        root is not None and history.descends(life, root, call.t)
        for root in traced.values()
    ):
        return f"sender {pid} is outside the traced scope"
    return life


def derive_o2(
    outcome: OracleOutcome,
    history: ProcessHistory,
    *,
    canary_deaths: Sequence[Mapping[str, Any]] = (),
) -> O2Result:
    """O2 for one row: the traced scope's, from the oracle, and the row's."""
    result = O2Result(state=HELD, required=outcome.required)
    result.canary_deaths = [
        f"canary {death.get('pid')} died during the row" for death in canary_deaths
    ]
    attach = outcome.attached_at
    traced: dict[int, Lifetime | None] = {
        pid: history.at(pid, attach) if attach is not None else history.first(pid)
        for pid in outcome.traced
    }
    result.scope = {
        "senders": [
            list(life.identity) if life is not None else [pid, None]
            for pid, life in traced.items()
        ],
        "followed": "children the traced processes start after the attach",
        "from": outcome.attached_at,
        "to": outcome.stopped_at,
        "syscalls": list(TRACED_SYSCALLS),
        "unobserved": "every other process, and every moment before the attach",
    }
    classes: set[str] = set()
    for call in outcome.calls:
        if call.probe or call.reached_nobody:
            continue
        where = f"{call.syscall} at {call.t} ({call.raw.strip()})"
        verb = (
            f"attempted (refused: {call.result.strip()})"
            if call.rejected
            else f"delivered {call.signal}"
        )
        sender = _sender(call, history, outcome, traced)
        if isinstance(sender, str):
            result.unknowns.append(f"{sender}: {where}")
            continue
        principal = _principal(sender, history, call.t)
        if principal is None:
            result.unknowns.append(f"whom {sender.pid} acts for is unknown: {where}")
            continue
        if call.everyone:
            result.violations.append(f"{verb} to every process: {where}")
            continue
        if call.unresolvable is not None:
            result.unknowns.append(f"target unresolved ({call.unresolvable}): {where}")
            continue
        pgid: int | None = None
        found: list[tuple[Lifetime, str]] | str
        if call.target_group is not None or call.group_of_pid is not None:
            if call.target_group:
                pgid = call.target_group
            else:
                # ``kill(0, ...)``: the sender's own group. A process-group
                # pidfd: the group of the process it names.
                of: Lifetime = sender
                if call.group_of_pid is not None:
                    held = history.holder(call.group_of_pid, call.t)
                    if isinstance(held, str):
                        result.unknowns.append(f"target unresolved ({held}): {where}")
                        continue
                    of = held[0]
                group = history.group_of(of, call.t)
                if isinstance(group, str):
                    result.unknowns.append(f"target unresolved ({group}): {where}")
                    continue
                pgid = group
            found = history.group_members(pgid, call.t)
        elif call.target_pid is not None:
            held = history.holder(call.target_pid, call.t)
            found = [held] if isinstance(held, tuple) else held
        else:
            found = "no target"
        if isinstance(found, str):
            result.unknowns.append(f"target unresolved ({found}): {where}")
            continue
        targets = [life for life, _ in found]
        kind = (
            f"{sender.actor_at(call.t)}:"
            f"{_target_kind(targets, sender, principal, pgid, history, call.t)}"
        )
        classes.add(kind)
        result.resolved.append(
            {
                "sent_at": call.t,
                "outcome": call.outcome,
                "sender": [sender.pid, sender.start],
                "principal": [principal.pid, principal.start],
                "class": kind,
                "targets": [[life.pid, life.start] for life in targets],
                "pinned": [how for _, how in found],
            }
        )
        outside, maybe, undecided = [], [], []
        for life, how in found:
            inside = history.descends(life, principal, call.t)
            if inside is None:
                undecided.append(life.identity)
            elif inside is False:
                # A group member seen only before the send may have exited
                # before it: then it was never a recipient.
                certain = pgid is None or how == BOTH_SAMPLES
                (outside if certain else maybe).append(life.identity)
        if outside:
            result.violations.append(
                f"{verb} to {outside}, outside the launched set of "
                f"{principal.identity}: {where}"
            )
        if maybe:
            result.unknowns.append(
                f"may have {verb} to {maybe}, outside the launched set of "
                f"{principal.identity}: {where}"
            )
        if undecided:
            result.unknowns.append(f"could not place {undecided}: {where}")
    result.classes = tuple(sorted(classes))
    if outcome.status == INCOMPLETE:
        result.incomplete = list(outcome.reasons)
    if result.violations:
        result.state = VIOLATED
    elif outcome.status == INCOMPLETE:
        result.state = INCOMPLETE
    elif outcome.status == UNAVAILABLE:
        result.state = UNOBSERVED
    elif result.unknowns:
        result.state = UNKNOWN
    result.row = VIOLATED if result.violations or result.canary_deaths else UNOBSERVED
    return result


def classes_direct_would_not_send(
    classes: Iterable[str], reference: Iterable[str] = ()
) -> list[str]:
    """The signal classes neither Direct's construction nor its run allows."""
    return sorted(set(classes) - DIRECT_CLASSES - set(reference))


# --- The oracle process ---------------------------------------------------------------

YAMA_PTRACE_SCOPE = Path("/proc/sys/kernel/yama/ptrace_scope")


def ptrace_scope() -> int | None:
    try:
        return int(YAMA_PTRACE_SCOPE.read_text().strip())
    except (OSError, ValueError):
        return None


def disposable_runner(environ: Mapping[str, str] = os.environ) -> bool:
    """The guard the trust step uses: a GitHub-hosted runner, not ``act``."""
    return (
        environ.get("GITHUB_ACTIONS") == "true"
        and environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and not environ.get("ACT")
    )


def oracle_unavailable(
    *,
    platform: str = sys.platform,
    environ: Mapping[str, str] = os.environ,
    strace: str | None = None,
    scope: int | None = None,
) -> str | None:
    """Why no signal oracle can run here, or None when one can."""
    if not platform.startswith("linux"):
        return f"no strace on {platform}"
    if not disposable_runner(environ):
        return (
            "the oracle attaches with sudo strace, which only a disposable "
            "GitHub-hosted runner may do"
        )
    if (strace or shutil.which("strace")) is None:
        return "strace is not installed"
    if scope == 3:
        return "kernel.yama.ptrace_scope is 3: no process may be traced"
    return None


def oracle_required(
    *, platform: str = sys.platform, environ: Mapping[str, str] = os.environ
) -> bool:
    """Whether a row that kills an actor must have the oracle: native Linux CI."""
    return (
        platform.startswith("linux")
        and environ.get(OPT_IN_ENV) == "1"
        and disposable_runner(environ)
    )


#: Read at import: the suite's autouse fixture deletes every ``LINKEDIN*``
#: variable before each test runs.
ORACLE_REQUIRED = oracle_required()


class SignalOracle:
    """``sudo strace`` on the row's server or owner and its guardian."""

    def __init__(
        self,
        directory: Path,
        *,
        required: bool = False,
        run: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.out = directory / "strace.txt"
        self.err = directory / "strace.stderr"
        self.scope = ptrace_scope()
        self.unavailable = oracle_unavailable(scope=self.scope)
        self.required = required
        self.pids: list[int] = []
        #: Thread id -> process id, read from ``/proc`` while the tracees ran.
        self.threads: dict[int, int] = {}
        self.attached_at: float | None = None
        self.attach_failure: str | None = None
        self._process: subprocess.Popen[Any] | None = None
        self._run = run

    @property
    def available(self) -> bool:
        return self.unavailable is None

    def command(self, pids: Sequence[int]) -> list[str]:
        # No ``-e signal=``: strace then also writes ``+++ killed by ... +++``
        # for a tracee a signal ended, which is its end in the trace.
        command = [
            "sudo",
            "-n",
            "strace",
            "-f",
            "-ttt",
            "-yy",
            "-e",
            "trace=" + ",".join(TRACED_SYSCALLS),
            "-o",
            str(self.out),
        ]
        for pid in pids:
            command += ["-p", str(pid)]
        return command

    def start(self, pids: Sequence[int], *, seconds: float = 10.0) -> str | None:
        """Attach to *pids*; the reason it could not, or None once attached."""
        if self.unavailable is not None:
            return self.unavailable
        self.pids = list(pids)
        with self.err.open("wb") as err:
            self._process = subprocess.Popen(
                self.command(pids), stdin=subprocess.DEVNULL, stdout=err, stderr=err
            )
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            text = self.err.read_text(errors="replace")
            attached = {
                int(found) for found in re.findall(r"Process (\d+) attached", text)
            }
            if set(pids) <= attached:
                self.attached_at = time.time()
                self.read_threads()
                return None
            if self._process.poll() is not None:
                break
            time.sleep(0.05)
        self.attach_failure = (
            f"strace did not attach to {list(pids)}: "
            f"{self.err.read_text(errors='replace')[-500:]}"
        )
        return self.attach_failure

    def read_threads(self) -> None:
        """Remember which threads belong to which traced process, while they run."""
        for pid in self.pids:
            with contextlib.suppress(OSError):
                for task in Path(f"/proc/{pid}/task").iterdir():
                    if task.name.isdigit():
                        self.threads[int(task.name)] = pid

    def _signal_helper(self, pid: int, name: str) -> str | None:
        """Send *name* to the oracle's own helper; why that failed, or None."""
        try:
            done = self._run(
                ["sudo", "-n", "kill", f"-{name}", str(pid)],
                check=False,
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"{type(exc).__name__}: {exc}"
        return None if done.returncode == 0 else f"exit status {done.returncode}"

    def stop(
        self, *, seconds: float = 30.0, confirmed_dead: Iterable[int] = ()
    ) -> OracleOutcome:
        """Wait for strace to finish with its tracees, and judge what it left.

        It ends by itself once every tracee has exited. If one is still running
        at the deadline, strace (the harness's own helper) is interrupted,
        which detaches it without touching the tracee; that ends the interval
        there. *confirmed_dead* are tracees the harness killed and saw gone.
        """
        outcome = OracleOutcome(
            status=COMPLETE,
            required=self.required,
            traced=list(self.pids),
            threads=dict(self.threads),
            attached_at=self.attached_at,
        )
        process = self._process
        if process is None or self.attach_failure is not None:
            if self.attach_failure is not None:
                outcome.status = INCOMPLETE
                outcome.reasons.append(self.attach_failure)
            elif self.unavailable is not None:
                outcome.status = INCOMPLETE if self.required else UNAVAILABLE
                outcome.reasons.append(self.unavailable)
            else:
                outcome.status = INCOMPLETE if self.required else UNAVAILABLE
                outcome.reasons.append("the oracle was never attached")
            if process is not None:
                self._end(process, outcome)
            outcome.stopped_at = time.time()
            return outcome
        detached = False
        try:
            outcome.returncode = process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            failure = self._signal_helper(process.pid, "INT")
            if failure is not None:
                outcome.reasons.append(f"strace could not be interrupted: {failure}")
            try:
                outcome.returncode = process.wait(timeout=15)
                detached = failure is None
            except subprocess.TimeoutExpired:
                outcome.reasons.append("strace was still running after the interrupt")
                self._end(process, outcome)
        outcome.stopped_at = time.time()
        if outcome.returncode is not None and outcome.returncode != 0:
            outcome.reasons.append(f"strace exited with status {outcome.returncode}")
        try:
            text = self.out.read_text(errors="replace")
        except OSError as exc:
            outcome.reasons.append(f"the trace could not be read: {exc}")
            text = None
        if text is not None:
            trace = read_trace(text)
            outcome.calls = trace.calls
            outcome.reasons += trace.problems
            stderr = self.err.read_text(errors="replace") if self.err.exists() else ""
            lost = {
                int(found) for found in re.findall(r"Process (\d+) detached", stderr)
            }
            dead = set(confirmed_dead)
            for pid in self.pids:
                if pid in trace.ended:
                    continue
                if pid in dead and pid not in lost:
                    continue
                if detached:
                    continue
                outcome.reasons.append(
                    f"strace stopped following {pid} before it ended"
                )
        if outcome.reasons:
            outcome.status = INCOMPLETE
        return outcome

    def _end(self, process: subprocess.Popen[Any], outcome: OracleOutcome) -> None:
        """Bounded cleanup of the oracle's own helper, whatever state it is in."""
        if process.poll() is not None:
            return
        failure = self._signal_helper(process.pid, "KILL")
        if failure is not None:
            outcome.reasons.append(f"strace could not be ended: {failure}")
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


# --- Canaries -------------------------------------------------------------------------

_CANARY_PROGRAM = "import time\ntime.sleep(3600)\n"
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _in_any_job(pid: int) -> bool | None:
    """Windows: whether *pid* is in any Job, or None if that cannot be read."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        result = wintypes.BOOL()
        if not kernel32.IsProcessInJob(handle, None, ctypes.byref(result)):
            return None
        return bool(result.value)
    finally:
        kernel32.CloseHandle(handle)


@dataclass
class Canary:
    process: subprocess.Popen[Any]
    pid: int
    start: float | None = None
    #: POSIX: its own session and group. Windows: whether it is in any Job.
    session: int | None = None
    group: int | None = None
    in_job: bool | None = None
    broke_away: bool = False

    def as_event_fields(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "start_identity": self.start,
            "session": self.session,
            "pgid": self.group,
            "in_job": self.in_job,
            "broke_away": self.broke_away,
        }


class Canaries:
    """Processes started outside every actor, whose death is a wrong target."""

    def __init__(self, count: int = 2) -> None:
        self.count = count
        self.canaries: list[Canary] = []

    def start(self) -> list[Canary]:
        """Start them; if any step fails, end those already started and raise."""
        try:
            for _ in range(self.count):
                self._start_one()
        except BaseException:
            self.stop()
            raise
        return list(self.canaries)

    def _start_one(self) -> None:
        import psutil

        # No stream of the harness's: a canary that outlived its row must not
        # hold the output of whatever ran the harness open behind it.
        quiet: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        command = [sys.executable, "-I", "-c", _CANARY_PROGRAM]
        broke_away = False
        if sys.platform == "win32":
            try:
                process = subprocess.Popen(
                    command,
                    creationflags=_CREATE_NEW_PROCESS_GROUP
                    | _CREATE_BREAKAWAY_FROM_JOB,
                    **quiet,
                )
                broke_away = True
            except OSError:
                # The harness's own Job forbids breakaway: the canary shares
                # that Job, which is no actor's.
                process = subprocess.Popen(
                    command, creationflags=_CREATE_NEW_PROCESS_GROUP, **quiet
                )
        else:
            process = subprocess.Popen(command, start_new_session=True, **quiet)
        # Owned from here: whatever fails next, ``stop`` ends it.
        canary = Canary(process=process, pid=process.pid, broke_away=broke_away)
        self.canaries.append(canary)
        if sys.platform != "win32":
            canary.session = os.getsid(process.pid)
            canary.group = os.getpgid(process.pid)
        canary.start = psutil.Process(process.pid).create_time()
        canary.in_job = _in_any_job(process.pid)

    def outside_the_harness(self) -> list[str]:
        """Why a canary shares the harness's session or group, if it does."""
        problems = []
        if sys.platform == "win32":
            return problems
        for canary in self.canaries:
            if canary.session != canary.pid or canary.group != canary.pid:
                problems.append(
                    f"canary {canary.pid} does not lead its own session and group"
                )
            if canary.session == os.getsid(0):
                problems.append(f"canary {canary.pid} shares the harness's session")
        return problems

    def deaths(self) -> list[dict[str, Any]]:
        """Every canary no longer running as the lifetime it was started as."""
        import psutil

        dead = []
        for canary in self.canaries:
            code = canary.process.poll()
            try:
                same = (
                    code is None
                    and canary.start is not None
                    and abs(psutil.Process(canary.pid).create_time() - canary.start)
                    <= 0.01
                )
            except psutil.Error:
                same = False
            if not same:
                dead.append({**canary.as_event_fields(), "exit_code": code})
        return dead

    def stop(self) -> None:
        """End the canaries: the harness's own children, each by its own handle."""
        for canary in self.canaries:
            if canary.process.poll() is None:
                with contextlib.suppress(OSError):
                    canary.process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                canary.process.wait(timeout=10)
        self.canaries = []
