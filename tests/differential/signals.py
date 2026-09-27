"""O2: every signal a row's actors send, whom it reached, and the canaries.

**The oracle is strace, on Linux only.** ``strace -f -ttt -yy -e
trace=kill,tkill,tgkill,pidfd_send_signal -e signal=none -p <server|owner> -p
<guardian>`` records every signal those processes send, with its target and
the time. It is attached from outside the actors, as soon as each is
identified. Ubuntu's default ``kernel.yama.ptrace_scope`` is 1, which lets a
tracer attach only to its own descendants, and strace is not an ancestor of
the owner, so the oracle runs ``sudo -n strace``: only on a disposable
GitHub-hosted runner, behind the same guard as the trust step, and it records
the scope it read. Anywhere else, and on macOS and Windows, which have no
strace, the oracle is *unavailable* and says why; nothing stands in for it.

**Targets are resolved against the watcher's own records.** A pid target is the
lifetime the watcher had at that pid at the send time. A group target is the
set of lifetimes in that group then, and it can be resolved only for a group
whose leader the watcher saw start: a group that already existed at its first
sample may hold processes it never reported. What cannot be resolved makes O2
*unknown*, never *held*.

**O2 per row** holds when every delivered signal reached only the sender's
launched set (its principal, the server or owner it belongs to, and that
principal's descendants, at the identity recorded when the watcher first saw
them) and no canary died. A signal to a pid or group that no longer exists
(``ESRCH``) reached nobody; a signal 0 is a liveness probe and delivers
nothing.

**O2 across experiments** is about signals Direct would not send, which the
per-row rule cannot see: the pre-Path-A guardian's ``killpg`` of the owner's
group reaches processes the owner launched, yet no Direct guardian sends it.
Each signal is therefore also given a class, sender role and target kind, and
a row's classes are compared with those Direct's actors send by construction
(``DIRECT_CLASSES``) and those the Direct reference actually sent.

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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The syscalls that send a signal. ``killpg`` is ``kill`` with a negative pid.
TRACED_SYSCALLS = ("kill", "tkill", "tgkill", "pidfd_send_signal")

HELD = "held"
VIOLATED = "violated"
UNKNOWN = "unknown"
#: No oracle on this platform or runner; canaries alone cannot establish O2.
UNOBSERVED = "unobserved"

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
_COMPLETE = re.compile(
    r"^(?P<name>" + "|".join(TRACED_SYSCALLS) + r")\((?P<args>.*)\)\s+=\s+(?P<ret>.*)$"
)
_UNFINISHED = re.compile(
    r"^(?P<name>" + "|".join(TRACED_SYSCALLS) + r")\((?P<args>.*)\s<unfinished \.\.\.>$"
)
_RESUMED = re.compile(
    r"^<\.\.\. (?P<name>" + "|".join(TRACED_SYSCALLS) + r") resumed>(?P<args>.*)\)"
    r"\s+=\s+(?P<ret>.*)$"
)
_PIDFD = re.compile(r"<pid:(?P<pid>\d+)>")


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
    #: A process target (``kill`` with a pid, ``tkill``, ``tgkill``, a pidfd).
    target_pid: int | None = None
    #: A group target: ``kill`` with a negative pid, or 0 for the sender's own.
    target_group: int | None = None
    #: ``kill(-1, ...)``: every process the sender may signal.
    everyone: bool = False
    raw: str = ""

    @property
    def probe(self) -> bool:
        """Signal 0 checks that a target exists and delivers nothing."""
        return self.signal == "0"

    @property
    def reached_nobody(self) -> bool:
        return "ESRCH" in self.result

    def as_event_fields(self) -> dict[str, Any]:
        return {
            "source": "strace",
            "sender_tid": self.tid,
            "sent_at": self.t,
            "syscall": self.syscall,
            "signal": self.signal,
            "result": self.result,
            "target_pid": self.target_pid,
            "target_group": self.target_group,
            "everyone": self.everyone,
        }


def _arguments(text: str) -> list[str]:
    return [part.strip() for part in text.split(",")]


def _call(tid: int, t: float, name: str, args: str, ret: str, raw: str) -> SignalCall:
    parts = _arguments(args)
    if name == "kill":
        target = int(parts[0])
        signal = parts[1]
        if target > 0:
            return SignalCall(tid, t, name, signal, ret, target_pid=target, raw=raw)
        if target == -1:
            return SignalCall(tid, t, name, signal, ret, everyone=True, raw=raw)
        return SignalCall(tid, t, name, signal, ret, target_group=-target, raw=raw)
    if name == "tkill":
        return SignalCall(
            tid, t, name, parts[1], ret, target_pid=int(parts[0]), raw=raw
        )
    if name == "tgkill":
        return SignalCall(
            tid, t, name, parts[2], ret, target_pid=int(parts[0]), raw=raw
        )
    # pidfd_send_signal(fd<pid:N>, SIG, info, flags); ``-yy`` names the pid.
    found = _PIDFD.search(parts[0])
    return SignalCall(
        tid,
        t,
        name,
        parts[1],
        ret,
        target_pid=int(found["pid"]) if found else None,
        raw=raw,
    )


def parse_strace(text: str) -> list[SignalCall]:
    """Every signal syscall in strace's ``-f -ttt`` output, in order.

    A call split by another thread (``<unfinished ...>`` then ``<... resumed>``)
    is joined. Exit lines (``+++``), signal lines (``---``) and anything else
    are skipped.
    """
    calls: list[SignalCall] = []
    pending: dict[tuple[int, str], tuple[float, str]] = {}
    for raw in text.splitlines():
        line = _LINE.match(raw.strip())
        if line is None:
            continue
        tid, t, rest = int(line["tid"]), float(line["t"]), line["rest"]
        if (found := _COMPLETE.match(rest)) is not None:
            calls.append(_call(tid, t, found["name"], found["args"], found["ret"], raw))
        elif (found := _UNFINISHED.match(rest)) is not None:
            pending[(tid, found["name"])] = (t, found["args"])
        elif (found := _RESUMED.match(rest)) is not None:
            began = pending.pop((tid, found["name"]), None)
            if began is None:
                continue
            calls.append(
                _call(
                    tid,
                    began[0],
                    found["name"],
                    began[1] + found["args"],
                    found["ret"],
                    raw,
                )
            )
    return calls


# --- What the watcher saw ---------------------------------------------------------


@dataclass
class Lifetime:
    """One process lifetime, from the watcher's records."""

    pid: int
    start: float
    ppid: int
    pgid: int | None
    in_row: bool
    actor: str
    #: The sample that first reported it, and the one that reported its exit.
    first_t: float
    exit_t: float | None = None

    @property
    def identity(self) -> tuple[int, float]:
        return (self.pid, self.start)

    def alive_at(self, t: float) -> bool:
        return self.first_t <= t and (self.exit_t is None or t < self.exit_t)


class ProcessHistory:
    """Every lifetime the watcher reported, to resolve a target at a send time.

    Only processes that appeared after the watcher's first sample are
    reported, so a pid it never reported cannot be resolved.
    """

    def __init__(
        self, records: Iterable[Mapping[str, Any]], *, outside: Iterable[int] = ()
    ) -> None:
        #: Pids known to be no actor's launch: the harness itself, whose
        #: children (the host's server, the canaries) are in the row without
        #: being any actor's.
        self.outside = frozenset(outside)
        self.lifetimes: list[Lifetime] = []
        current: dict[tuple[int, float], Lifetime] = {}
        for entry in records:
            kind = entry.get("kind")
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
                    pgid=entry.get("pgid"),
                    in_row=entry.get("in_row") is True,
                    actor=str(entry.get("actor", "other")),
                    first_t=t,
                )
                current[key] = known
                self.lifetimes.append(known)
            else:
                # An update: exec or a new group. The newest reading holds.
                known.pgid = entry.get("pgid", known.pgid)
                known.actor = str(entry.get("actor", known.actor))
                known.in_row = known.in_row or entry.get("in_row") is True

    def at(self, pid: int, t: float) -> Lifetime | None:
        """The lifetime at *pid* at time *t*, or None if the watcher cannot say."""
        alive = [
            life for life in self.lifetimes if life.pid == pid and life.alive_at(t)
        ]
        return alive[-1] if len(alive) == 1 else None

    def first(self, pid: int) -> Lifetime | None:
        seen = [life for life in self.lifetimes if life.pid == pid]
        return seen[0] if seen else None

    def group(self, pgid: int, t: float) -> list[Lifetime] | None:
        """The lifetimes in group *pgid* at *t*, or None when that is unknowable.

        Knowable only for a group whose leader the watcher saw start: every
        member of it was then born or moved into it while the watcher looked.
        """
        if self.leader(pgid, t) is None:
            return None
        return [
            life for life in self.lifetimes if life.pgid == pgid and life.alive_at(t)
        ]

    def leader(self, pgid: int, t: float) -> Lifetime | None:
        """The last lifetime at pid *pgid* the watcher saw start by *t*."""
        leaders = [
            life for life in self.lifetimes if life.pid == pgid and life.first_t <= t
        ]
        return leaders[-1] if leaders else None

    def descends(self, life: Lifetime, ancestor: Lifetime) -> bool | None:
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


# --- O2 ---------------------------------------------------------------------------


@dataclass
class O2Result:
    state: str
    classes: tuple[str, ...] = ()
    violations: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    #: Each delivered signal with the identities it reached.
    resolved: list[dict[str, Any]] = field(default_factory=list)


def _principal(sender: Lifetime, history: ProcessHistory) -> Lifetime | None:
    """Whom a sender acts for: a guardian for the process that started it."""
    if sender.actor == "guardian":
        return history.at(sender.ppid, sender.first_t)
    return sender


def _target_kind(
    targets: Sequence[Lifetime],
    sender: Lifetime,
    principal: Lifetime,
    pgid: int | None,
    leader: Lifetime | None,
) -> str:
    """What a signal was aimed at, in the terms Direct's construction uses.

    *pgid* is the group signalled, or None for a single process. A group the
    principal leads is its own group whatever is left in it: that is the
    pre-Path-A guardian's ``killpg(owner_group)``. A group a browser leads is a
    browser group, whatever helpers such as crashpad it also holds: the driver
    starts Chromium in a group of its own.
    """
    if pgid is not None:
        if pgid == principal.pid:
            return "principal-group"
        if leader is not None and leader.actor == "browser":
            return "browser-group"
        return "descendant-group"
    if len(targets) == 1 and targets[0].identity == sender.identity:
        return "self"
    if targets and all(life.actor == "browser" for life in targets):
        return "browser"
    return "descendant"


def derive_o2(
    calls: Sequence[SignalCall],
    history: ProcessHistory,
    *,
    threads: Mapping[int, int],
    oracle_available: bool,
    canary_deaths: Sequence[Mapping[str, Any]] = (),
) -> O2Result:
    """O2 for one row, from its traced signals, the watcher and the canaries.

    *threads* maps a traced thread id to its process id, as read from
    ``/proc`` while the tracees were alive; a sender named only by a thread
    the map does not know cannot be attributed.
    """
    result = O2Result(state=HELD)
    for death in canary_deaths:
        result.violations.append(f"canary {death.get('pid')} died during the row")
    classes: set[str] = set()
    for call in calls:
        if call.probe or call.reached_nobody:
            continue
        where = f"{call.syscall} at {call.t} ({call.raw.strip()})"
        pid = threads.get(call.tid, call.tid)
        sender = history.at(pid, call.t)
        if sender is None:
            result.unknowns.append(f"sender {call.tid} unknown: {where}")
            continue
        principal = _principal(sender, history)
        if principal is None:
            result.unknowns.append(f"whom {sender.pid} acts for is unknown: {where}")
            continue
        if call.everyone:
            result.violations.append(f"a signal to every process: {where}")
            continue
        pgid: int | None = None
        if call.target_group is not None:
            # ``kill(0, ...)`` is the sender's own group.
            pgid = call.target_group or sender.pgid
            targets = history.group(pgid, call.t) if pgid else None
        elif call.target_pid is not None:
            one = history.at(call.target_pid, call.t)
            targets = [one] if one is not None else None
        else:
            # A pidfd strace could not name.
            targets = None
        if targets is None:
            result.unknowns.append(f"target unresolved: {where}")
            continue
        outside = []
        undecided = []
        for target in targets:
            inside = history.descends(target, principal)
            if inside is False:
                outside.append(target)
            elif inside is None:
                undecided.append(target)
        leader = history.leader(pgid, call.t) if pgid is not None else None
        kind = (
            f"{sender.actor}:{_target_kind(targets, sender, principal, pgid, leader)}"
        )
        classes.add(kind)
        result.resolved.append(
            {
                "sent_at": call.t,
                "sender": [sender.pid, sender.start],
                "principal": [principal.pid, principal.start],
                "class": kind,
                "targets": [[life.pid, life.start] for life in targets],
            }
        )
        if outside:
            result.violations.append(
                f"reached {[life.identity for life in outside]} outside the "
                f"launched set of {principal.identity}: {where}"
            )
        if undecided:
            result.unknowns.append(
                f"could not place {[life.identity for life in undecided]}: {where}"
            )
    result.classes = tuple(sorted(classes))
    if result.violations:
        result.state = VIOLATED
    elif not oracle_available:
        result.state = UNOBSERVED
    elif result.unknowns:
        result.state = UNKNOWN
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


class SignalOracle:
    """``sudo strace`` on the row's server or owner and its guardian."""

    def __init__(self, directory: Path) -> None:
        self.out = directory / "strace.txt"
        self.err = directory / "strace.stderr"
        self.scope = ptrace_scope()
        self.unavailable = oracle_unavailable(scope=self.scope)
        self.pids: list[int] = []
        #: Thread id -> process id, read from ``/proc`` while the tracees ran.
        self.threads: dict[int, int] = {}
        self._process: subprocess.Popen[Any] | None = None

    @property
    def available(self) -> bool:
        return self.unavailable is None

    def command(self, pids: Sequence[int]) -> list[str]:
        command = [
            "sudo",
            "-n",
            "strace",
            "-f",
            "-ttt",
            "-yy",
            "-e",
            "trace=" + ",".join(TRACED_SYSCALLS),
            "-e",
            "signal=none",
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
                self.read_threads()
                return None
            if self._process.poll() is not None:
                break
            time.sleep(0.05)
        self.unavailable = (
            f"strace did not attach to {list(pids)}: "
            f"{self.err.read_text(errors='replace')[-500:]}"
        )
        return self.unavailable

    def read_threads(self) -> None:
        """Remember which threads belong to which traced process, while they run."""
        for pid in self.pids:
            with contextlib.suppress(OSError):
                for task in Path(f"/proc/{pid}/task").iterdir():
                    if task.name.isdigit():
                        self.threads[int(task.name)] = pid

    def stop(self, *, seconds: float = 30.0) -> list[SignalCall]:
        """Wait for strace to finish with its tracees, and read what it saw.

        It ends by itself once every tracee has exited. If one is still running
        at the deadline, strace (the harness's own helper) is interrupted,
        which detaches it without touching the tracee.
        """
        process = self._process
        if process is None:
            return []
        try:
            process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["sudo", "-n", "kill", "-INT", str(process.pid)],
                check=False,
                capture_output=True,
                timeout=15,
            )
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=15)
        try:
            text = self.out.read_text(errors="replace")
        except OSError:
            return []
        return parse_strace(text)


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
    start: float
    #: POSIX: its own session and group. Windows: whether it is in any Job.
    session: int | None
    group: int | None
    in_job: bool | None
    broke_away: bool

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
        import psutil

        # No stream of the harness's: a canary that outlived its row must not
        # hold the output of whatever ran the harness open behind it.
        quiet: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        for _ in range(self.count):
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
                    # The harness's own Job forbids breakaway: the canary
                    # shares that Job, which is no actor's.
                    process = subprocess.Popen(
                        command, creationflags=_CREATE_NEW_PROCESS_GROUP, **quiet
                    )
                session = group = None
            else:
                process = subprocess.Popen(command, start_new_session=True, **quiet)
                session, group = os.getsid(process.pid), os.getpgid(process.pid)
            start = psutil.Process(process.pid).create_time()
            self.canaries.append(
                Canary(
                    process=process,
                    pid=process.pid,
                    start=start,
                    session=session,
                    group=group,
                    in_job=_in_any_job(process.pid),
                    broke_away=broke_away,
                )
            )
        return list(self.canaries)

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
