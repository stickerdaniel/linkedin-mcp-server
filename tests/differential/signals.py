"""O2: the signals a row's traced actors send, what they named, and the canaries.

**The oracle is strace, on Linux only, and it sees only what it traces.**
``strace -f -ttt -yy -e trace=<TRACED_SYSCALLS>,<FOLLOWED_SYSCALLS> -p
<server|owner> -p <guardian>`` records every signal those two processes send,
and those their threads and children send, from the moment each is attached
until it ends. That is its scope and nothing else: the frontend, the driver,
the browser, a replacement owner and every moment before the attach are
outside it. So a row has two O2 readings. ``traced`` is the oracle's, for that
scope only. ``row`` is what the row can say about every actor, which is
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
``complete`` only when strace attached to every pid asked for, exited on its
own with status 0 or was detached on request, left a trace file whose every
line parsed, and accounted for every tracee it covered: the roots, their
threads, and every thread or child their ``clone``, ``fork`` and ``vfork``
calls started. Each needs its own end (an exit line, or the harness's own
confirmed kill of a root), or for a thread its process's end, or the
deliberate stop's detach, which is then that tracee's boundary. A tracee strace
let go on its own, or one with no end, leaves it ``incomplete``. A tracee is a
lifetime, not a number: an end satisfies only the lifetime it follows, and a
thread id that turns up again (born anew, or writing after its end) is a
reused id the trace cannot place, so it too leaves the collection
``incomplete`` and its old association is dropped. The trace carries no
event that links a creation call to the child it made, only the id the call
returns, and a call's entry is not the child's allocation: an earlier
lifetime of the same id can still end while the call runs. So a child seen
writing while its parent's creation call ran is that call's child, since a
lifetime still writing has not released its id, but an end of the id before
the call returned could be either and leaves the collection ``incomplete``;
so does the id announced as attached more than once. A thread census entry,
timed by when its task directory was read, is the traced thread only when it
was read after the creation call returned, in the same process; read before
the call began it was an earlier lifetime, and read while the call ran it
could be either. Observations that cannot be placed leave the collection
``incomplete`` with the reason.
Whether the collection is complete is kept apart from what it showed:
``OracleOutcome.status`` is the collection, ``O2Result.state`` the verdict.

**What complete does not claim.** Collection completeness is evaluated from
the records strace emitted, not from an identity-complete kernel lifecycle log.
Exit-report timestamps need not be the instants when the corresponding
lifetimes ended or released their ids. An older lifetime reported late can
remain indistinguishable from a returned child when no separate new-lifetime
end or duplicate attachment is present; such histories are outside this
reconstruction's guarantee. H-R6 traces with no recorded creation calls do not
exercise birth reconciliation, and a row relying on a followed child's identity
needs separate validation before it counts as stronger evidence.

**What a signal was aimed at is what the call names.** Its class is the
sender's role and the kind of target the call names, read against the
watcher's records up to the send and never after it: a group number the
watcher had recorded for the principal (``principal-group``), a group or pid it
had recorded for one of the row's browsers while it was one, or for a process
by then known to carry such a browser's marker, the sender itself, or anything
else. ``principal-group`` means the call named a previously recorded
principal-group number: it is not proof that the group still belonged to the
principal, nor of the sender's intent, nor of any death. That is the Path A
witness, and it rests on the call alone: which processes a signal then reached
is a separate question.

**Who received a signal is known only when the samples show it.** The watcher
samples the process table; between two samples it sees nothing. A pid target is
pinned to a lifetime only when the same lifetime (pid and create time) is seen
in the samples on both sides of the send. A group target is pinned only when
every process in that group in either sample is seen in both, in that group
both times, the group did not exist at the watcher's first sample, and no
process could not be identified, and no member's group could not be read,
around the send. That is endpoint membership, not a continuous record: a
process that joined and left the group between the two samples is not seen,
so a ``held`` group under this rule is not an atomic census of every process
the signal reached.
A process a SIGKILL reached is gone by the next sample, so it can almost never
be pinned, and the traced O2 of a row whose guardian drained its browser groups
usually reads ``unknown``. That is the oracle's stated limit, not a pass: the
kernel's last allocated pid, which the watcher logs, is a cursor and no proof
that a pid was not reused, so nothing else stands in for the second sample.

**A browser's marker is its launch.** On Linux, Chromium's crashpad handler
double-forks out of the browser's tree, carrying the browser's environment and
with it the random marker the product sets per launch, which is what the
guardian drains by. A process carrying the marker of one of the row's browsers
is in that browser's launched set, from the moment the watcher knew the marker.

**The traced O2** is ``violated`` when a pinned recipient is outside the
sender's launched set (its principal, the server or owner it belongs to, and
that principal's descendants), ``unknown`` when a recipient could not be
pinned, and ``held`` only when every recipient was pinned inside it. A call
that returned an error other than ``ESRCH`` is an attempt, not a delivery; one
aimed outside the set is still a violation, as an attempt. ``ESRCH`` reached
nobody, and signal 0 is a liveness probe. A signal is evidence of a signal,
not of a death: deaths are only what was observed.

**Across experiments** a row's classes are compared with those Direct's actors
send by construction (``DIRECT_CLASSES``) and those the Direct reference
actually sent: the pre-Path-A guardian's ``killpg`` of the owner's group names
the principal's group, which no Direct guardian does.

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
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
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
#: The syscalls that start a tracee strace then follows: what the cohort is.
FOLLOWED_SYSCALLS = ("clone", "clone3", "fork", "vfork")

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
_NAMES = "|".join(TRACED_SYSCALLS + FOLLOWED_SYSCALLS)
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
_RETURNED = re.compile(r"^(?P<value>\d+)\b")
#: The ``-T`` time strace appends to a finished call's result.
_DURATION = re.compile(r"\s+<(?P<secs>\d+\.\d+)>$")


def _split_duration(ret: str) -> tuple[str, float | None]:
    """A result without its ``-T`` time, and that time if there was one."""
    found = _DURATION.search(ret)
    if found is None:
        return ret, None
    return ret[: found.start()], float(found["secs"])


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
    #: A time the call had certainly returned by, as ``read_trace`` bounds a
    #: creation call's: a split call's resumed line, or for an unsplit one the
    #: next line strace wrote. None when the trace gives no such line, and for
    #: a call that never returned (``= ?``, the caller died in it); the ``-T``
    #: time bounds nothing.
    returned: float | None = None

    @property
    def returns(self) -> bool:
        """Whether strace saw an ordinary return: not ``= ?``."""
        return self.result.strip() != "?"

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
        if not self.returns:
            # The caller died in it: no return reports what it delivered.
            return "no return"
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
            "returned": self.returned,
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


@dataclass(eq=False)
class Birth:
    """A tracee a traced thread started, which strace then follows."""

    parent: int
    child: int
    #: ``CLONE_THREAD``: a thread of the parent's process, not a process.
    thread: bool
    #: The call's line prefix, printed at syscall entry: the child's id was
    #: allocated no earlier.
    began: float = 0.0
    #: A time the call had certainly returned by, or None when the trace gives
    #: none. A split call's ``<... resumed>`` line has its own prefix, printed
    #: at syscall exit. An unsplit line's prefix is its entry, and strace's
    #: ``-T`` time is no upper bound either (see ``read_trace``); what bounds
    #: it is the next line strace wrote, since strace finishes a line before it
    #: starts another.
    returned: float | None = None
    #: The ``-T`` time strace reported for the call, as evidence only.
    duration: float | None = None


@dataclass
class Trace:
    """What strace wrote: calls, births, ends, and what did not parse."""

    calls: list[SignalCall] = field(default_factory=list)
    births: list[Birth] = field(default_factory=list)
    #: Tids strace reported ending (``+++ exited ...`` or ``+++ killed by ...``).
    ended: set[int] = field(default_factory=set)
    #: Every tid a line was written for.
    tids: set[int] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)
    #: ``("line", tid, t)``, ``("end", tid, t)`` and ``("birth", Birth)``, in
    #: the order strace wrote them: what the cohort is rebuilt from.
    events: list[tuple[Any, ...]] = field(default_factory=list)


def _record(
    trace: Trace,
    tid: int,
    t: float,
    name: str,
    args: str,
    ret: str,
    raw: str,
    returned: float | None = None,
) -> Birth | int | None:
    """Record one finished call. The birth it reports, if it reports one; for
    a signal call whose return only the next line can bound, its index."""
    ret, duration = _split_duration(ret)
    if name in FOLLOWED_SYSCALLS:
        found = _RETURNED.match(ret.strip())
        if found is not None and int(found["value"]) > 0:
            birth = Birth(
                tid,
                int(found["value"]),
                "CLONE_THREAD" in args,
                t,
                returned,
                duration,
            )
            trace.births.append(birth)
            trace.events.append(("birth", birth))
            return birth
        return None
    call = _call(tid, t, name, args, ret, raw)
    # A call that never returned gets no return bound, whatever line follows.
    if call.returns:
        call = replace(call, returned=returned)
    trace.calls.append(call)
    return len(trace.calls) - 1 if call.returns and returned is None else None


def read_trace(text: str) -> Trace:
    """Every signal syscall and tracee birth in strace's ``-f -ttt`` output.

    A call split by another thread (``<unfinished ...>`` then ``<... resumed>``)
    is joined. Signal lines (``---``) are skipped. A line that is not strace's,
    a call that cannot be read, a resumed call without its start and a call
    still unfinished at the end are problems: evidence lost, never no call.

    When a creation call returned is bounded from strace's own order, as its
    v6.11 source has it. ``syscall_entering_trace`` calls ``printleader``,
    which reads ``CLOCK_REALTIME`` for the prefix, at entry; only then does
    ``syscall_entering_finish`` take the ``-T`` start time, and
    ``syscall_exiting_decode`` takes its end once strace handles the exit
    stop. So entry plus ``-T`` falls short of that handling by the time spent
    printing the arguments, and can fall short of the exit itself: it bounds
    nothing. What does: ``print_syscall_resume`` prints a fresh prefix at
    exit for a split call, and ``printleader`` ends any unfinished line with
    ``<unfinished ...>`` before it starts another, so an unsplit call's result
    was written before the next line's prefix was read. That next prefix, or
    the resumed line's own, is a time the call had returned by. Signal calls
    are bounded the same way (``SignalCall.returned``); one that never
    returned (``= ?``) is not.
    """
    trace = Trace()
    pending: dict[tuple[int, str], tuple[float, str]] = {}
    #: Unsplit births still waiting for the next line to bound their return.
    unbounded: list[Birth] = []
    #: Unsplit signal calls waiting for the same, by index into ``calls``.
    open_calls: list[int] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        line = _LINE.match(raw.strip())
        if line is None:
            trace.problems.append(f"not a trace line: {raw[:200]!r}")
            continue
        tid, t, rest = int(line["tid"]), float(line["t"]), line["rest"]
        trace.tids.add(tid)
        for birth in unbounded:
            birth.returned = t
        unbounded.clear()
        for index in open_calls:
            trace.calls[index] = replace(trace.calls[index], returned=t)
        open_calls.clear()
        try:
            if rest.startswith("+++"):
                if _ENDED.match(rest) is None:
                    raise ValueError("an unreadable exit line")
                trace.ended.add(tid)
                trace.events.append(("end", tid, t))
                continue
            trace.events.append(("line", tid, t))
            if rest.startswith("---"):
                continue
            elif (found := _COMPLETE.match(rest)) is not None:
                recorded = _record(
                    trace, tid, t, found["name"], found["args"], found["ret"], raw
                )
                if isinstance(recorded, Birth):
                    unbounded.append(recorded)
                elif recorded is not None:
                    open_calls.append(recorded)
            elif (found := _UNFINISHED.match(rest)) is not None:
                pending[(tid, found["name"])] = (t, found["args"])
            elif (found := _RESUMED.match(rest)) is not None:
                began = pending.pop((tid, found["name"]), None)
                if began is None:
                    raise ValueError("resumed without its start")
                _record(
                    trace,
                    tid,
                    began[0],
                    found["name"],
                    began[1] + found["args"],
                    found["ret"],
                    raw,
                    returned=t,
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
    """One watcher sample: when it began and ended, and the kernel's last pid.

    The last pid is a diagnostic only: it is the allocator's cursor, which can
    wrap past occupied pids and come back higher, so it proves nothing about
    which pids were reused in between.
    """

    began: float
    ended: float
    last_pid: int | None


@dataclass
class Lifetime:
    """One process lifetime, from the watcher's records.

    Its role and group are what the watcher read at each sample, since both
    change: an exec turns the driver's fork into the browser, and a process
    the watcher catches between its exit and its reaping shows no command line
    at all, so it reads as ``other``. ``first_t`` and ``exit_t`` are the ends
    of samples: the first that saw it and the first that no longer did. A
    group of None is one the watcher could not read.
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
        """The group the watcher read for it by *t*; None if unread."""
        return self._reading(t)[2]

    def was(self, actor: str) -> bool:
        """Whether any reading of it showed *actor*."""
        return any(reading[1] == actor for reading in self.readings)

    def was_by(self, actor: str, t: float) -> bool:
        """Whether a reading taken by *t* showed *actor*."""
        return any(when <= t and role == actor for when, role, _ in self.readings)

    def groups_as(self, actor: str, t: float) -> set[int]:
        """The groups of the readings taken by *t* that showed *actor*."""
        return {
            group
            for when, role, group in self.readings
            if when <= t and role == actor and group is not None
        }

    def groups_by(self, t: float) -> set[int]:
        """Every group the watcher read for it by *t*."""
        return {g for when, _, g in self.readings if when <= t and g is not None}

    def marker_by(self, t: float) -> str | None:
        """Its marker, if the watcher had read it by *t*."""
        if self.marker is None or self.marker_t is None or self.marker_t > t:
            return None
        return self.marker


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
            # A start or an update: exec, a new or unreadable group, a
            # marker, an exiting image.
            known.readings.append(
                (t, str(entry.get("actor", "other")), entry.get("pgid"))
            )

    # --- by life span, for senders, parents and what a call names

    def at(self, pid: int, t: float) -> Lifetime | None:
        """The one lifetime reported alive at *pid* at *t*, or None."""
        alive = [
            life for life in self.lifetimes if life.pid == pid and life.alive_at(t)
        ]
        return alive[-1] if len(alive) == 1 else None

    def first(self, pid: int) -> Lifetime | None:
        seen = [life for life in self.lifetimes if life.pid == pid]
        return seen[0] if seen else None

    def is_browser(self, life: Lifetime, t: float) -> bool:
        """By *t*: one of the row's browsers, or carrying one's marker.

        Only readings taken by *t* count. A later reading can neither make it a
        browser then nor, as an exiting image does, unmake one it already was.
        """
        return (life.in_row and life.was_by("browser", t)) or self.marked(life, t)

    def names_a_browser(self, pid: int, t: float) -> bool:
        """Whether the watcher had recorded *pid*, by *t*, for a row browser."""
        return any(
            life.pid == pid and life.first_t <= t and self.is_browser(life, t)
            for life in self.lifetimes
        )

    def browser_groups(self, life: Lifetime, t: float) -> set[int]:
        """The groups the watcher had read, by *t*, for *life* as a browser.

        A row browser's groups from the readings that showed it as one; a
        marked process's from its readings once its marker was known.
        """
        groups: set[int] = set()
        if life.in_row:
            groups |= life.groups_as("browser", t)
        marker_t = life.marker_t
        if self.marked(life, t) and marker_t is not None:
            groups |= {
                group
                for when, _, group in life.readings
                if marker_t <= when <= t and group is not None
            }
        return groups

    def names_a_browser_group(self, pgid: int, t: float) -> bool:
        """Whether the watcher had recorded *pgid*, by *t*, as a row browser's group."""
        return any(pgid in self.browser_groups(life, t) for life in self.lifetimes)

    # --- by samples, for recipients

    def brackets(self, t: float) -> tuple[Sample, Sample] | None:
        """The last sample that ended by *t* and the first that began after it."""
        before = [s for s in self.samples if s.ended <= t]
        after = [s for s in self.samples if s.began >= t]
        if not before or not after:
            return None
        return before[-1], after[0]

    def holder(self, pid: int, t: float) -> Lifetime | str:
        """The lifetime that held *pid* at *t*, or why that is unknown.

        Only the same lifetime seen in the samples on both sides of *t*.
        """
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
            return seen_before[0]
        if seen_before and not seen_after:
            return f"pid {pid} was gone by the sample after the send"
        if seen_after:
            return f"pid {pid} was not seen in the sample before the send"
        return f"pid {pid} was in neither sample around the send"

    def group_members(self, pgid: int, t: float) -> list[Lifetime] | str:
        """Every process in group *pgid* at *t*, or why that is unknown.

        Endpoint membership: those in the group in both samples around *t*.
        A member in only one of them leaves the group unknown.
        """
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
        members: list[Lifetime] = []
        for life in self.lifetimes:
            for sample in (before, after):
                if life.seen_in(sample) and life.pgid_at(sample.ended) is None:
                    return f"{life.identity}'s group was not read around the send"
            was_in = life.seen_in(before) and life.pgid_at(before.ended) == pgid
            is_in = life.seen_in(after) and life.pgid_at(after.ended) == pgid
            if not (was_in or is_in):
                continue
            if not (was_in and is_in):
                return f"{life.identity} was in group {pgid} in one sample only"
            members.append(life)
        if not members:
            return f"no member of group {pgid} was seen around the send"
        return members

    def group_of(self, life: Lifetime, t: float) -> int | str:
        """The group *life* was in at *t*, or why that is unknown."""
        bracket = self.brackets(t)
        if bracket is None:
            return "no samples on both sides of the send"
        before, after = bracket
        if not (life.seen_in(before) and life.seen_in(after)):
            return f"{life.identity} was not seen on both sides of the send"
        group = life.pgid_at(before.ended)
        if group is None or life.pgid_at(after.ended) != group:
            return f"{life.identity}'s group around the send is not known"
        return group

    # --- launches

    def browsers(self, marker: str, t: float) -> list[Lifetime]:
        """The row's browser processes known by *t* to carry *marker*."""
        return [
            life
            for life in self.lifetimes
            if life.in_row and life.was_by("browser", t) and life.marker_by(t) == marker
        ]

    def marked(self, life: Lifetime, t: float) -> bool:
        """Whether *life* carries, by *t*, the marker of one of the row's browsers."""
        marker = life.marker_by(t)
        return marker is not None and bool(self.browsers(marker, t))

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
    #: Thread id -> process id, from ``/proc`` at the attach and from the
    #: threads the tracees started.
    threads: dict[int, int] = field(default_factory=dict)
    #: The pids strace was attached to.
    traced: list[int] = field(default_factory=list)
    #: Every tracee strace covered: tid -> kind, process, and how it ended.
    cohort: dict[int, dict[str, Any]] = field(default_factory=dict)
    attached_at: float | None = None
    stopped_at: float | None = None
    returncode: int | None = None

    def as_event_fields(self) -> dict[str, Any]:
        fields = asdict(self)
        fields.pop("calls")
        fields.pop("threads")
        fields["cohort"] = {str(tid): entry for tid, entry in self.cohort.items()}
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
    #: The oracle's collection: complete, incomplete or unavailable. Kept apart
    #: from ``state``: a violation found in an incomplete trace is both.
    collection: str = UNAVAILABLE
    #: ``sender role:target kind`` of every call, from what the call named.
    classes: tuple[str, ...] = ()
    violations: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    #: Why the oracle's evidence is incomplete, when it is.
    incomplete: list[str] = field(default_factory=list)
    canary_deaths: list[str] = field(default_factory=list)
    #: Each call, its class, and the recipients pinned for it, if any.
    resolved: list[dict[str, Any]] = field(default_factory=list)
    #: What the traced state covers: senders, interval, syscalls.
    scope: dict[str, Any] = field(default_factory=dict)


def _principal(sender: Lifetime, history: ProcessHistory, t: float) -> Lifetime | None:
    """Whom a sender acts for at *t*: a guardian for the process that started it."""
    if sender.actor_at(t) == "guardian":
        return history.at(sender.ppid, sender.first_t)
    return sender


def _named(
    call: SignalCall, sender: Lifetime, principal: Lifetime, history: ProcessHistory
) -> str:
    """The kind of target the call names, from the call and the records alone.

    No recipient is resolved here: this is what the sender aimed at.
    """
    t = call.t
    if call.everyone:
        return "everyone"
    if call.unresolvable is not None:
        return "unresolved"
    if call.target_group is not None or call.group_of_pid is not None:
        if call.target_group:
            group: int | None = call.target_group
        elif call.group_of_pid is not None:
            named = history.at(call.group_of_pid, t)
            group = named.pgid_at(t) if named is not None else None
        else:
            group = sender.pgid_at(t)
        if group is None:
            return "unnamed-group"
        if group in principal.groups_by(t):
            return "principal-group"
        if history.names_a_browser_group(group, t):
            return "browser-group"
        return "other-group"
    pid = call.target_pid
    if pid == sender.pid:
        return "self"
    if pid == principal.pid:
        return "principal"
    if pid is not None and history.names_a_browser(pid, t):
        return "browser"
    return "other"


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
    result = O2Result(state=HELD, required=outcome.required, collection=outcome.status)
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
        "followed": "threads and children the traced processes start",
        "from": outcome.attached_at,
        "to": outcome.stopped_at,
        "syscalls": list(TRACED_SYSCALLS),
        "unobserved": "every other process, and every moment before the attach",
        "recipients": "pinned only when seen on both sides of the send",
    }
    classes: set[str] = set()
    for call in outcome.calls:
        if call.probe or call.reached_nobody:
            continue
        where = f"{call.syscall} at {call.t} ({call.raw.strip()})"
        if call.rejected:
            verb = f"attempted (refused: {call.result.strip()})"
        elif not call.returns:
            verb = f"attempted {call.signal} (no return observed)"
        else:
            verb = f"delivered {call.signal}"
        sender = _sender(call, history, outcome, traced)
        if isinstance(sender, str):
            classes.add("unplaced-sender")
            result.unknowns.append(f"{sender}: {where}")
            continue
        principal = _principal(sender, history, call.t)
        if principal is None:
            classes.add(f"{sender.actor_at(call.t)}:unplaced-principal")
            result.unknowns.append(f"whom {sender.pid} acts for is unknown: {where}")
            continue
        kind = f"{sender.actor_at(call.t)}:{_named(call, sender, principal, history)}"
        classes.add(kind)
        entry: dict[str, Any] = {
            "sent_at": call.t,
            "outcome": call.outcome,
            "sender": [sender.pid, sender.start],
            "principal": [principal.pid, principal.start],
            "class": kind,
            "targets": None,
            # Whether this call's recipients stayed unknown: each unknown
            # below that belongs to a call is marked on that call's entry.
            "unknown": False,
        }
        result.resolved.append(entry)
        if call.everyone:
            result.violations.append(f"{verb} to every process: {where}")
            continue
        if call.unresolvable is not None:
            entry["unknown"] = True
            result.unknowns.append(f"target unresolved ({call.unresolvable}): {where}")
            continue
        found: list[Lifetime] | str
        if call.target_group is not None or call.group_of_pid is not None:
            if call.target_group:
                pgid: int | str = call.target_group
            else:
                # ``kill(0, ...)``: the sender's own group. A process-group
                # pidfd: the group of the process it names.
                of: Lifetime | str = sender
                if call.group_of_pid is not None:
                    of = history.holder(call.group_of_pid, call.t)
                pgid = history.group_of(of, call.t) if not isinstance(of, str) else of
            found = (
                history.group_members(pgid, call.t) if isinstance(pgid, int) else pgid
            )
        elif call.target_pid is not None:
            held = history.holder(call.target_pid, call.t)
            found = [held] if isinstance(held, Lifetime) else held
        else:
            found = "no target"
        if isinstance(found, str):
            entry["unknown"] = True
            result.unknowns.append(f"recipients not pinned ({found}): {where}")
            continue
        entry["targets"] = [[life.pid, life.start] for life in found]
        outside, undecided = [], []
        for life in found:
            inside = history.descends(life, principal, call.t)
            if inside is None:
                undecided.append(life.identity)
            elif inside is False:
                outside.append(life.identity)
        if outside:
            result.violations.append(
                f"{verb} to {outside}, outside the launched set of "
                f"{principal.identity}: {where}"
            )
        if undecided:
            entry["unknown"] = True
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

_ATTACHED = re.compile(r"Process (\d+) attached")
_DETACHED = re.compile(r"Process (\d+) detached")


@dataclass(eq=False)
class Tracee:
    """One lifetime of a tid strace covered."""

    tid: int
    #: ``root``, ``thread``, ``child``, ``attached`` or ``unaccounted``.
    kind: str
    #: When it was first on record: its birth, or its first line; None when
    #: it was there at the attach and has written nothing since.
    first: float | None = None
    #: Known only from the thread census, read in ``Cohort.census_times``.
    census: bool = False
    #: When the trace showed it end.
    ended_at: float | None = None
    #: A thread started in the trace: the lifetime that started it.
    parent: Tracee | None = None
    #: The process it belongs to, when that is its own or known at the attach.
    process_pid: int | None = None
    ended: bool = False
    end: str | None = None

    def process(self, seen: frozenset[int] = frozenset()) -> int | None:
        """The process this lifetime belongs to, however late that was learnt."""
        if self.process_pid is not None:
            return self.process_pid
        if self.kind == "thread" and self.parent is not None and id(self) not in seen:
            return self.parent.process(seen | {id(self)})
        return None


@dataclass
class Cohort:
    """Every lifetime strace covered, and the ids it could not keep apart."""

    lifetimes: list[Tracee] = field(default_factory=list)
    #: The latest lifetime of each tid.
    current: dict[int, Tracee] = field(default_factory=dict)
    #: Tids that turned up again, as a new birth or after their end, or whose
    #: observations contradict each other.
    reused: dict[int, str] = field(default_factory=dict)
    #: Tid -> when the census read the task directory it was in, from the
    #: start to the end of that listing.
    census_times: Mapping[int, tuple[float, float]] = field(default_factory=dict)

    def add(self, tracee: Tracee) -> Tracee:
        self.lifetimes.append(tracee)
        self.current[tracee.tid] = tracee
        return tracee


def _confirms(
    known: Tracee, birth: Birth, census_times: Mapping[int, tuple[float, float]]
) -> str | None:
    """Why *birth* cannot be the lifetime already on record, or None when it is.

    Nothing in the trace links a creation call to the child it made but the
    id it returns, and the id is allocated after the call's entry. What was
    seen of the id while the call ran is this child only if it cannot have been
    an earlier lifetime's: a line it wrote can be (a lifetime still writing
    has not released its id), an end cannot (an earlier lifetime may have ended
    between the entry and the allocation). A census entry is this thread only
    when its task directory was read after the call returned.
    """
    if known.first is not None and known.first < birth.began:
        return f"it was seen at {known.first}, before its creation call began"
    if known.ended and (
        birth.returned is None
        or (known.ended_at is not None and known.ended_at <= birth.returned)
    ):
        return (
            f"child id {birth.child}'s end report cannot be ordered "
            f"unambiguously against its creation call's completion; "
            f"confirmation and reuse remain indistinguishable"
        )
    if known.census:
        if not birth.thread:
            return "the census had it as a thread, the trace saw a process born"
        times = census_times.get(known.tid)
        if times is None:
            return "the census entry's time is unknown, so its order is too"
        read_from, read_to = times
        if read_to < birth.began:
            return f"the census read it at {read_to}, before its creation call began"
        if birth.returned is None:
            return "its creation call's return time is unknown, so its order is too"
        if read_from <= birth.returned:
            return (
                f"the census read it between {read_from} and {read_to}, before "
                f"the available return bound; the trace cannot establish that "
                f"it followed the creation call"
            )
        return None
    if known.kind not in ("unaccounted", "attached"):
        return f"an earlier lifetime of it ({known.kind}) is on record"
    return None


def cohort(
    roots: Sequence[int],
    threads: Mapping[int, int],
    trace: Trace,
    attached: Iterable[int] = (),
    *,
    census_times: Mapping[int, tuple[float, float]] | None = None,
) -> Cohort:
    """Every tracee strace covered, as lifetimes, from the trace in order.

    The roots strace was attached to, their threads from the census read
    after the attach (each timed by *census_times*), every tid a traced
    ``clone``, ``fork`` or ``vfork`` returned (a thread of its parent's
    process with ``CLONE_THREAD``, a process of its own without), every pid
    strace reported attaching (*attached*, once per announcement), and every
    tid a line was written for. A birth confirms a lifetime already on record
    only as ``_confirms`` allows; which process a thread belongs to is
    resolved once the whole trace is read, whatever order the returns came
    in. A tid that turns up again, born anew or writing after its end, one
    announced twice, and one whose observations cannot be placed, is
    recorded, and no lifetime of it is trusted.
    """
    members = Cohort(census_times=dict(census_times or {}))
    for pid in roots:
        members.add(Tracee(pid, "root", process_pid=pid))
    for tid, pid in threads.items():
        if tid not in members.current:
            members.add(Tracee(tid, "thread", process_pid=pid, census=True))
    announced = Counter(attached)
    for pid, count in announced.items():
        if count > 1:
            members.reused[pid] = (
                f"tid {pid} was announced attached {count} times; confirmation "
                f"and reuse are indistinguishable"
            )
        if pid not in members.current:
            members.add(Tracee(pid, "attached", process_pid=pid))
    #: Census threads a birth confirmed, and the process the census gave them.
    confirmed: list[tuple[Tracee, int]] = []
    for event in trace.events:
        if event[0] == "birth":
            birth: Birth = event[1]
            parent = members.current.get(birth.parent)
            known = members.current.get(birth.child)
            if known is None:
                members.add(
                    Tracee(
                        birth.child,
                        "thread" if birth.thread else "child",
                        first=birth.began,
                        parent=parent if birth.thread else None,
                        process_pid=None if birth.thread else birth.child,
                    )
                )
                continue
            problem = _confirms(known, birth, members.census_times)
            if problem is not None:
                members.reused[birth.child] = (
                    f"tid {birth.child} was born at {birth.began}, but {problem}"
                )
                continue
            if known.census and known.process_pid is not None:
                confirmed.append((known, known.process_pid))
            known.kind = "thread" if birth.thread else "child"
            known.parent = parent if birth.thread else None
            known.process_pid = None if birth.thread else birth.child
            known.census = False
            continue
        _, tid, t = event
        tracee = members.current.get(tid)
        if tracee is None:
            tracee = members.add(Tracee(tid, "unaccounted", first=t))
        elif tracee.ended:
            members.reused[tid] = f"tid {tid} wrote at {t} after its end"
            continue
        elif tracee.first is None and tracee.kind != "root":
            tracee.first = t
        if event[0] == "end":
            tracee.ended = True
            tracee.ended_at = t
    for tracee, pid in confirmed:
        process = tracee.process()
        if process != pid:
            members.reused[tracee.tid] = (
                f"tid {tracee.tid} was born a thread of {process}, but the census "
                f"had it in {pid}"
            )
    return members


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
        #: Tid -> when the census read its task directory, start to end: after
        #: the attach, so a thread born in between is in it and in the trace.
        self.census_times: dict[int, tuple[float, float]] = {}
        self.attach_failure: str | None = None
        self._process: subprocess.Popen[Any] | None = None
        self._run = run

    @property
    def available(self) -> bool:
        return self.unavailable is None

    def command(self, pids: Sequence[int]) -> list[str]:
        # No ``-e signal=``: strace then also writes ``+++ killed by ... +++``
        # for a tracee a signal ended, which is its end in the trace. The
        # followed syscalls name every tracee strace takes on after the attach.
        command = [
            "sudo",
            "-n",
            "strace",
            "-f",
            "-ttt",
            "-yy",
            "-T",
            "-e",
            "trace=" + ",".join(TRACED_SYSCALLS + FOLLOWED_SYSCALLS),
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
            attached = {int(found) for found in _ATTACHED.findall(text)}
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
        """Remember which threads belong to which traced process, while they run.

        Read after the attach, so a thread born after it can be in the census
        as well as in the trace: each entry keeps when its task directory was
        read, from the start of that listing to its end.
        """
        for pid in self.pids:
            with contextlib.suppress(OSError):
                began = self._now()
                tasks = self._tasks(pid)
                read = self._now()
                for name in tasks:
                    if name.isdigit():
                        self.threads[int(name)] = pid
                        self.census_times[int(name)] = (began, read)

    @staticmethod
    def _now() -> float:
        return time.time()

    @staticmethod
    def _tasks(pid: int) -> list[str]:
        """The names in ``/proc/<pid>/task``: one per thread of *pid*."""
        return [task.name for task in Path(f"/proc/{pid}/task").iterdir()]

    def _stderr(self) -> str:
        try:
            return self.err.read_text(errors="replace")
        except OSError:
            return ""

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
        which detaches it without touching the tracee; that is then each
        remaining tracee's boundary. A tracee strace let go before that is lost
        evidence. *confirmed_dead* are roots the harness killed and saw gone.
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
        # Detaches strace reported before any stop of ours were its own doing.
        before_stop: str | None = None
        stopped = False
        try:
            outcome.returncode = process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            before_stop = self._stderr()
            failure = self._signal_helper(process.pid, "INT")
            if failure is not None:
                outcome.reasons.append(f"strace could not be interrupted: {failure}")
            try:
                outcome.returncode = process.wait(timeout=15)
                stopped = failure is None
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
            stderr = self._stderr()
            let_go = {
                int(pid)
                for pid in _DETACHED.findall(
                    before_stop if before_stop is not None else stderr
                )
            }
            members = cohort(
                self.pids,
                self.threads,
                trace,
                [int(pid) for pid in _ATTACHED.findall(stderr)],
                census_times=self.census_times,
            )
            self._account(outcome, members, let_go, set(confirmed_dead), stopped)
        if outcome.reasons:
            outcome.status = INCOMPLETE
        return outcome

    def _account(
        self,
        outcome: OracleOutcome,
        members: Cohort,
        let_go: set[int],
        dead: set[int],
        stopped: bool,
    ) -> None:
        """How each tracee lifetime's coverage ended; a reason for each that did not.

        A reused tid is a reason of its own, and none of its lifetimes speaks
        for a thread's process: its association from ``/proc`` is dropped.
        """
        for tid, reason in members.reused.items():
            outcome.reasons.append(f"{reason}: a reused id the trace cannot place")
            outcome.threads.pop(tid, None)

        def ended(tracee: Tracee | None) -> bool:
            if tracee is None or tracee.tid in let_go or tracee.tid in members.reused:
                return False
            return tracee.ended or (tracee.kind == "root" and tracee.tid in dead)

        for tracee in members.lifetimes:
            tid = tracee.tid
            process = tracee.process()
            if tid in members.reused:
                tracee.end = None
                continue
            if tracee.kind == "thread" and process is not None:
                outcome.threads[tid] = process
            if tid in let_go:
                tracee.end = None
                outcome.reasons.append(f"strace let {tid} go before it ended")
            elif tracee.ended:
                tracee.end = "its exit line"
            elif tracee.kind == "root" and tid in dead:
                tracee.end = "killed by the harness"
            elif tracee.kind == "thread" and ended(members.current.get(process or -1)):
                tracee.end = "its process ended"
            elif stopped:
                tracee.end = f"detached at the stop, {outcome.stopped_at}"
            else:
                tracee.end = None
                outcome.reasons.append(
                    f"strace stopped following {tid} ({tracee.kind}) before it ended"
                )
        outcome.cohort = {
            tid: {
                "kind": tracee.kind,
                "process": tracee.process(),
                "end": tracee.end,
                "lifetimes": sum(1 for x in members.lifetimes if x.tid == tid),
                "reused": tid in members.reused,
            }
            for tid, tracee in members.current.items()
        }

    def _end(self, process: subprocess.Popen[Any], outcome: OracleOutcome) -> None:
        """Bounded cleanup of the oracle's own helper, whatever state it is in."""
        if process.poll() is not None:
            return
        failure = self._signal_helper(process.pid, "KILL")
        if failure is not None:
            outcome.reasons.append(f"strace could not be ended: {failure}")
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)

    def settled(self) -> bool:
        """Whether the tracer this oracle started, if any, has been reaped.

        Asked of its own ``Popen``: ``stop`` returning is not this answer, since
        its last bounded wait can end with the tracer still running.
        """
        return self._process is None or self._process.poll() is not None

    def end(self) -> bool:
        """One more bounded attempt to end the tracer; whether it is settled."""
        if self._process is not None and self._process.poll() is None:
            self._end(self._process, OracleOutcome(status=INCOMPLETE))
        return self.settled()


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
