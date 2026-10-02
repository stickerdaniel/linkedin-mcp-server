"""A process watcher that runs outside every actor it observes.

Started by the harness as its own process: its own session on POSIX, its own
process group on Windows, and never a descendant of the server, the owner or
the browser. What it reports therefore does not depend on anything those actors
say about themselves. It samples the whole process table and writes
``process.start`` and ``process.exit`` for every process that appeared after
its first sample, keyed by pid *and* create time, so a recycled pid reads as
one exit and one start rather than as the same process.

It also derives O1 on every sample: how many browser tree roots each
``--user-data-dir`` has. A browser is found by that flag in its command line,
which Patchright passes to every persistent-context launch; a Chromium child
carries ``--type=`` and is part of its parent's tree, never a root of its own.
Two roots with the same profile in one sample is the second concurrent browser
the default-on contract forbids.

For O2 it records each process's group and, on POSIX, a digest of the browser
marker the product sets per launch, read once from every process started after
the first sample that could be the row's browser. That marker is what ties
Chromium's crashpad handler, which leaves the browser's tree for a session of
its own, to the browser that started it.

**Nothing a row could still change is settled by age.** A process can exec at
any moment of its life, and a forked child shows its parent's command line
until it does, which is how the Node driver starts Chromium on POSIX. So every
row actor, and every process not established as unrelated, has its executable
and command line read again on every sample for as long as it lives, and a
change is reported as
``process.update``. Its identity is checked on every sample either way.
Windows differs: nothing there can exec, and the parent pid a process records
at creation never changes, so once its parent, executable and command
line have all been read they are carried for the rest of that lifetime rather
than read again (``Sampler.no_exec``). What stays is the per-sample identity
check, which is also what reports its exit. A field that could not be read is
read again on every sample, as anywhere else.

**What a lifetime is cannot change, so it is asked once.** Its owning user is
read once per pid and create time, and a lifetime a full read found gone
(``NoSuchProcess``) is not read again, however long its pid stays listed.
Every read is timed, and each sample's time is charged to its phases
(``SAMPLE_PHASES``). The summary names the slowest read, keeps every sample of
at least ``SLOW_SAMPLE_SECONDS`` with its phases, and keeps the largest gap
between two samples with what the time outside sampling went to
(``BETWEEN_STEPS``) and the phases of the sample that closed it.

**When each sample was taken is part of the evidence.** The summary's
``sample_log`` gives every sample's start, its end (the time its events
carry) and, on Linux, the last pid the kernel had allocated as it began; the
ready line lists the process groups of the first sample. A process settled as
unrelated still has its group read on every sample, and a change is reported.
A group that could not be read is published as unread (``pgid`` None, with
``pgid_error``) and listed in the summary; the last group read never stands in
for it. A process whose group read finds it gone is an exit. Group membership
is what the samples saw at their ends, not a continuous record: a process that
joined and left a group between two samples is not seen. The kernel's last
pid is a diagnostic only.

**Relevant means descended from the harness.** A process is a row actor when it
descends from the harness (``--root-pid``), whether it was already running at
the first sample, as a staging leftover would be, or appeared later.

**Unrelated has to be established; not being able to attribute is not it.** A
process is established as unrelated to the row when

* its owning user can be read and is not the harness's user (the real uid on
  POSIX, the user name on Windows), since no actor runs as anyone else; or
* it was running at the first sample and its whole ancestry, read then, ends
  at a child of pid 0 without passing the harness, every parent present and
  no younger than its child. A child of pid 1 is not enough, since an orphan
  of the harness is adopted there, and nothing but a wall-clock create time
  would say it is older than the harness; or
* on Windows, it was running at the first sample, its executable and command
  line were both read then, it names no profile and cannot be the browser.
  Windows has no exec, so that image is the process's for its whole lifetime
  (``Sampler.no_exec``); or
* on Windows, outside the row, its executable was read and cannot be the
  browser, even if its command line could not be read: that image can never
  be a browser root. The failed read is recorded once and not repeated (as
  for ``LsaIso.exe``, whose command line psutil retries for a second); or
* its executable, read in the same sample, is neither the row's browser
  (``--browser-exe``) nor anything under the managed browsers
  (``--browser-dir``). That is the setuid ``/bin/ps`` the product runs on
  macOS: psutil cannot read its arguments, and it cannot be a browser.

A process is never excluded merely because its calendar creation time precedes
the harness's: the calendar clock can be stepped, and a process born after the
harness can then read as older. Creation times still identify sampled lifetimes
and reject apparently younger parent records; passing that rejection is not an
independent proof of a historical parent-child relationship.

**An exclusion belongs to one lifetime.** The first three last: they are kept
by pid *and* create time, and apply only in a sample that read that same create
time, so a pid that cannot be identified now is not covered by what was
established about an earlier process there. The last holds for its sample
only, since a POSIX process can exec later. Evidence that ties a kept exclusion
to the harness withdraws it. A process already running at the first sample that
none of these settles is not excluded but read again on every sample, and
counts as a possible actor once it shows the browser.

Losing a process (``NoSuchProcess``, or a pid whose create time changed) is an
exit. Any other process whose identity, parent or arguments cannot be read, or
that cannot be opened at all, is an **unresolved possible actor**: it is kept in
the census, recorded, and leaves O1 unestablished for the row. That record stays
even if the process later becomes readable or exits, since a later reading
cannot show what it did while unreadable. Only a later reading of the same
lifetime, its create time equal to the one recorded, that shows another user
resolves it; a record without a create time has no lifetime to match and
stays. Every failed read is recorded in the summary, with the executable when
known, the fields, how long it lasted, how it ended and whether it could have
been a browser.

**A known browser root stays in its profile's count.** A lifetime read before
as a browser root on a profile keeps that reading, profile included, when a
later read of its arguments fails while its executable, read in the same
sample, is unchanged; macOS refuses the arguments of a process on its way out.
The profile is pinned rather than derived again from the old arguments, which
could now resolve elsewhere. Pinned there, the lifetime still counts toward
that profile, as a root or inside a same-profile parent's tree that is counted,
so it cannot hide a second root *on that profile*. It says nothing about any
other: a same-image exec with hidden arguments could have moved it onto one.
So the failed read is kept in ``read_failures`` as retained history with its
``retained_profile``, one note for each profile the lifetime was retained on,
not as a possible browser, and whoever judges a profile treats it as
unidentified unless that is the profile it names. Every other
failed argument read stays uncertain, including one of a lifetime read before
as a driver, helper or renderer: an exec since could have made it a root.

What sampling cannot see: a process that lives and dies between two samples.
The summary records when observation began and ended and the largest wall-clock
gap between two samples, so a claim built on it can state the window it had; an
overlap wholly between samples remains outside this oracle's resolution. On
Windows the watcher runs ahead of the row (``SCHEDULING_CLASS``), so a burst of
process starts does not widen that window by keeping it waiting to run; the
summary's ``priority`` says what it ran at.

Imports nothing from the repository, so it runs as a plain script:
``python watcher.py --out FILE --stop FILE ...``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any

import psutil

USER_DATA_DIR_FLAG = "--user-data-dir="
CHILD_TYPE_FLAG = "--type="

#: Target interval between samples.
SAMPLE_SECONDS = 0.05

#: Whether this platform can replace a process's program in place. Windows
#: cannot: ``CreateProcess`` fixes a process's image and command line for its
#: whole lifetime, and becoming another program means a new process, which is a
#: new (pid, create time) the watcher reads in full as new. A process rewriting
#: its own PEB command line is adversarial and is not a browser launch; it is
#: outside this oracle.
NO_EXEC = os.name == "nt"

#: The scheduling class the watcher asks for, on Windows only: POSIX lets an
#: unprivileged process lower its priority but not raise it. A burst of process
#: starts on a Windows runner, such as the Node driver's launch, has kept a
#: normal-class watcher waiting to run for 1.17s between two samples of 4ms
#: each, while the owner in the same seconds logged that it had not been
#: scheduled for 1.1s. A browser can live wholly inside such a gap. A sample
#: costs a few milliseconds per interval, so running ahead of the row costs the
#: row almost nothing. Asked for by the watcher itself, because a venv's
#: ``python.exe`` is a launcher and a class given to it at creation is not
#: passed on to the interpreter it starts.
SCHEDULING_CLASS: int | None = getattr(psutil, "HIGH_PRIORITY_CLASS", None)

#: A sample at least this long is recorded with its phases and its largest
#: timed read. A quarter of the gap budget the rows accept
#: (``harness.MAX_WATCHER_GAP_SECONDS``).
SLOW_SAMPLE_SECONDS = 0.25
#: What a sample's time is charged to. Every instant of a sample belongs to
#: the phase current then, so the phases add up to the sample's duration:
#: ``last_pid`` and ``enumeration`` open it, ``reads`` is every timed read of
#: one process (by kind in ``read_kinds``), ``canonicalization`` every path
#: resolved to name a profile or compare an executable, and ``bookkeeping``
#: everything else: the loop around the reads, classification and judgement.
SAMPLE_PHASES = ("last_pid", "enumeration", "reads", "canonicalization", "bookkeeping")
#: What the time between one sample's end and the next one's start is
#: charged to, in the order it passes: turning the sample into events,
#: serializing and writing them, the flush, the sleep asked for, how much
#: later than asked it returned, and the stop-file check. Whatever none of
#: them took is the record's ``unaccounted``.
BETWEEN_STEPS = ("tracker", "write", "flush", "sleep", "wakeup_delay", "stop_check")
#: At most this many slow samples are kept, the first ones.
_SLOW_SAMPLES_KEPT = 100
#: At most this many failed group reads are kept, the first ones.
_GROUP_FAILURES_KEPT = 200

#: What the product sets in the environment of every browser it launches, a
#: random value per launch, and what its guardian drains browser groups by.
#: The same name at the frozen baseline.
BROWSER_MARKER_ENV = "LINKEDIN_MCP_BROWSER_PROCESS_MARKER"

#: Whether markers are read: on POSIX, where the guardian drains by them and
#: where Chromium's crashpad handler leaves the row's tree for a session of its
#: own. Windows starts no guardian.
READ_MARKERS = os.name != "nt"


def canonical_user_data_dir(value: str) -> str:
    """One spelling per profile directory, so two routes to it count as one.

    ``realpath`` because macOS reaches the same temporary directory through
    ``/var`` and ``/private/var``, and ``normcase`` for Windows.
    """
    value = value.strip().strip('"').strip("'")
    return os.path.normcase(os.path.realpath(value))


def user_data_dir(cmdline: Sequence[str]) -> str | None:
    """The profile a browser *root* runs on, or None for anything else.

    None for a Chromium child (``--type=``), which belongs to its parent's
    tree, and for anything without the flag.
    """
    found: str | None = None
    for argument in cmdline:
        if argument.startswith(CHILD_TYPE_FLAG):
            return None
        if argument.startswith(USER_DATA_DIR_FLAG):
            found = argument[len(USER_DATA_DIR_FLAG) :]
    return None if not found else canonical_user_data_dir(found)


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    ppid: int
    #: Create time. With the pid, this is the process's identity.
    start: float
    exe: str | None
    cmdline: tuple[str, ...]
    #: The canonical profile when this is a browser root, else None.
    profile: str | None = None
    #: Descended from the harness, so one of the row's actors.
    in_row: bool = False
    #: The venv interpreter a framework build was started as (``LAUNCHER_ENV``):
    #: a cached initial-image observation for this PID, create time and command
    #: line; same-command re-exec is outside this identity oracle.
    launcher: str | None = None
    #: POSIX process group, read on every sample; None on Windows, or when it
    #: could not be read, which ``pgid_error`` then says. Never the last
    #: group read in place of one that failed.
    pgid: int | None = None
    #: A digest of the process's browser marker (``BROWSER_MARKER_ENV``), read
    #: once per lifetime for a process that could be the row's browser. What
    #: ties a crashpad handler, which leaves the row's tree, to its browser.
    browser_marker: str | None = None
    #: Why this sample could not read the group, when it could not.
    pgid_error: str | None = None

    @property
    def identity(self) -> tuple[int, float]:
        return (self.pid, self.start)

    def as_event_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "pid": self.pid,
            "ppid": self.ppid,
            "pgid": self.pgid,
            "start_identity": self.start,
            "exe": self.exe,
            "cmdline": list(self.cmdline),
            "in_row": self.in_row,
        }
        if self.launcher is not None:
            fields["launcher"] = self.launcher
        if self.browser_marker is not None:
            fields["browser_marker"] = self.browser_marker
        if self.pgid_error is not None:
            fields["pgid_error"] = self.pgid_error
        if not self.in_row:
            # These events are published as CI evidence, and a process that is
            # not the row's own may carry anything in its arguments, credentials
            # included. Only the profile it names, if any, is kept.
            fields["cmdline"] = []
            fields["cmdline_withheld"] = True
            fields["profile"] = self.profile
            fields.pop("launcher", None)
        return fields


def record(
    pid: int,
    ppid: int,
    start: float,
    exe: str | None,
    cmdline: Sequence[str],
    *,
    in_row: bool = False,
    launcher: str | None = None,
    pgid: int | None = None,
    browser_marker: str | None = None,
    pgid_error: str | None = None,
) -> ProcessRecord:
    cmdline = tuple(cmdline)
    return ProcessRecord(
        pid,
        ppid,
        start,
        exe,
        cmdline,
        user_data_dir(cmdline),
        in_row,
        launcher,
        pgid,
        browser_marker,
        pgid_error,
    )


#: The last pid the kernel allocated in this pid namespace (Linux). Logged as
#: a diagnostic only: the allocator's cursor can wrap past occupied pids and
#: come back higher, so two readings prove nothing about reuse in between.
NS_LAST_PID = Path("/proc/sys/kernel/ns_last_pid")


def read_last_pid() -> int | None:
    """The kernel's last allocated pid, or None off Linux or if unreadable."""
    try:
        return int(NS_LAST_PID.read_text().strip())
    except (OSError, ValueError):
        return None


#: What ``Sampler`` records for a group read that found the process gone.
GONE = "gone"


def posix_pgid(pid: int) -> int | None:
    """A process's group on POSIX, or None on Windows, which has none.

    Raises ``ProcessLookupError`` for a process that is gone, and any other
    ``OSError`` for one whose group could not be read: neither is a group.
    """
    if os.name == "nt":
        return None
    return os.getpgid(pid)


#: Set by a macOS framework build's ``bin/python`` stub to the path it was
#: started as before it re-executes ``Python.app``, whose binary is then both
#: the process's executable and its ``argv[0]``. For a venv that path is the
#: venv's interpreter, and it is the only place the venv still shows.
LAUNCHER_ENV = "__PYVENV_LAUNCHER__"

SERVER_MODULE = "linkedin_mcp_server"
OWNER_MODULE = "linkedin_mcp_server.daemon_owner"

#: CPython's short options (``Python/getopt.c``, ``bBc:dEhiIm:OPqRsStuvVW:xX:?``):
#: flags without a value; ``c``, ``m``, ``W`` and ``X`` take one, from the rest
#: of their cluster or from the next argument.
_SHORT_FLAGS = frozenset("bBdEiIOPqRsStuvx")
_SHORT_WITH_VALUE = frozenset("cmWX")
#: ``h``, ``V`` and ``?`` print and exit, running no module; they fall under
#: "anything else" below, with every option the interpreter would reject.
#: Long options, and whether each takes a value. Every one without a value
#: (help, version) ends the run without executing anything.
_LONG_OPTIONS = {
    "check-hash-based-pycs": True,
    "help": False,
    "help-all": False,
    "help-env": False,
    "help-xoptions": False,
    "version": False,
}
#: The values the interpreter accepts for ``--check-hash-based-pycs``; any
#: other one, or none, makes it exit with status 2 before running anything.
_HASH_CHECK_MODES = frozenset({"default", "always", "never"})


def invoked_module(cmdline: Sequence[str]) -> str | None:
    """The module a Python command line runs with ``-m``, or None.

    Parsed with the interpreter's own option grammar: short options clustered
    character by character, ``m``, ``c``, ``W`` and ``X`` taking the rest of
    their cluster or the next argument. Execution ends the options: ``-c``
    (attached, clustered or separate), a script path, ``-`` for stdin, or
    ``--``, after which the next word is a script. Help, version and any
    option the interpreter does not know run no module. A module name that
    appears anywhere else is not an invocation.
    """
    arguments = list(cmdline[1:])
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if argument == "--" or argument == "-" or not argument.startswith("-"):
            return None
        if argument.startswith("--"):
            # Matched whole: ``--check-hash-based-pycs=always`` is rejected.
            if not _LONG_OPTIONS.get(argument[2:]):
                return None
            if index >= len(arguments) or arguments[index] not in _HASH_CHECK_MODES:
                return None
            index += 1
            continue
        position = 1
        while position < len(argument):
            option = argument[position]
            position += 1
            if option in _SHORT_FLAGS:
                continue
            if option in _SHORT_WITH_VALUE:
                value = argument[position:]
                if not value:
                    if index >= len(arguments):
                        return None
                    value = arguments[index]
                    index += 1
                if option == "m":
                    return value or None
                if option == "c":
                    return None
                break
            # Help, version, or an option the interpreter would reject.
            return None
    return None


def read_launcher(process: Any) -> str | None:
    """The venv interpreter a process was started as, if it says so.

    psutil returns the whole environment and only this one value is kept. The
    sampler asks only row actors that run the server or the owner module,
    once per PID, create time and command line.
    """
    try:
        value = process.environ().get(LAUNCHER_ENV)
    except (psutil.Error, OSError, AttributeError):
        return None
    return value or None


def read_browser_marker(process: Any) -> str | None:
    """A digest of the process's browser marker, if it carries one.

    Only the digest is kept: it matches the same marker in another process,
    which is all it is for, and the published evidence holds no value the
    product's guardian acts on. A failed read raises ``psutil.Error`` or
    ``OSError``: that is not knowing, and the caller asks again.
    """
    value = process.environ().get(BROWSER_MARKER_ENV)
    if not value:
        return None
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def classify(process: ProcessRecord) -> str:
    """Which actor a process is, from its command line alone."""
    joined = " ".join(process.cmdline)
    if any(argument.startswith(USER_DATA_DIR_FLAG) for argument in process.cmdline):
        return "browser"
    if "linkedin_mcp_server.daemon_owner" in joined:
        return "owner"
    if "process_guardian" in joined:
        return "guardian"
    if "installer_supervisor" in joined or "installer_worker" in joined:
        return "installer"
    if "run-driver" in joined:
        return "driver"
    if "linkedin_mcp_server" in joined:
        return "frontend"
    if "watcher.py" in joined:
        return "watcher"
    return "other"


def browser_roots(sample: Mapping[int, ProcessRecord]) -> dict[str, tuple[int, ...]]:
    """Browser tree roots per profile in one sample.

    A browser process whose parent is a browser on the same profile is inside
    that tree, not a second one.
    """
    roots: dict[str, list[int]] = {}
    for pid, process in sample.items():
        if process.profile is None:
            continue
        parent = sample.get(process.ppid)
        if parent is not None and parent.profile == process.profile:
            continue
        roots.setdefault(process.profile, []).append(pid)
    return {profile: tuple(sorted(pids)) for profile, pids in sorted(roots.items())}


class Tracker:
    """Turns successive samples into events and the O1 verdict."""

    def __init__(self) -> None:
        self._known: dict[int, ProcessRecord] = {}
        self._baseline_taken = False
        self._roots: dict[str, tuple[int, ...]] = {}
        self.samples = 0
        #: The most roots any profile had in one sample.
        self.max_roots: dict[str, int] = {}
        #: Every sample where one profile had more than one root.
        self.violations: list[dict[str, Any]] = []

    def observe(
        self, sample: Mapping[int, ProcessRecord], t: float
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """Return ``(actor, kind, fields)`` for what changed since the last sample."""
        events: list[tuple[str, str, dict[str, Any]]] = []
        if self._baseline_taken:
            for pid, process in sample.items():
                previous = self._known.get(pid)
                if previous is not None and previous.start == process.start:
                    if (
                        previous.exe,
                        previous.cmdline,
                        previous.pgid,
                        previous.pgid_error,
                        previous.browser_marker,
                    ) != (
                        process.exe,
                        process.cmdline,
                        process.pgid,
                        process.pgid_error,
                        process.browser_marker,
                    ):
                        events.append(
                            (
                                classify(process),
                                "process.update",
                                process.as_event_fields(),
                            )
                        )
                    continue
                if previous is not None:
                    events.append(
                        (classify(previous), "process.exit", previous.as_event_fields())
                    )
                events.append(
                    (classify(process), "process.start", process.as_event_fields())
                )
            for pid, previous in self._known.items():
                if pid not in sample:
                    events.append(
                        (classify(previous), "process.exit", previous.as_event_fields())
                    )
        self._baseline_taken = True
        self._known = dict(sample)
        self.samples += 1

        roots = browser_roots(sample)
        if roots != self._roots:
            events.append(
                (
                    "watcher",
                    "browser.roots",
                    {"roots": {profile: list(pids) for profile, pids in roots.items()}},
                )
            )
        for profile, pids in roots.items():
            self.max_roots[profile] = max(self.max_roots.get(profile, 0), len(pids))
            if len(pids) > 1:
                self.violations.append({"t": t, "profile": profile, "pids": list(pids)})
        self._roots = roots
        return events


_UNREADABLE = (psutil.AccessDenied, OSError)


def _real(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def possible_browser(
    exe: str | None, browser_exe: str | None, browser_dir: str | None
) -> bool:
    """Whether an executable could be the row's browser. Unknown is yes."""
    if not exe:
        return True
    if browser_exe is None and browser_dir is None:
        return True
    real = _real(exe)
    if browser_exe is not None and real == _real(browser_exe):
        return True
    if browser_dir is not None:
        directory = _real(browser_dir)
        try:
            if os.path.commonpath([real, directory]) == directory:
                return True
        except ValueError:
            pass
    return False


def process_user(process: Any) -> object | None:
    """Who owns a process, or None if that cannot be read.

    The real uid on POSIX, which a setuid executable does not change, and the
    user name on Windows.
    """
    try:
        if os.name == "nt":
            return process.username()
        return process.uids().real
    except (psutil.Error, OSError, AttributeError):
        return None


def harness_user() -> object | None:
    if os.name == "nt":
        return process_user(psutil.Process())
    return os.getuid()


def another_user(found: object | None, harness: object | None) -> bool:
    """Whether a process's user is established as not the harness's.

    Only when both are known: an unknown on either side is not a difference.
    """
    return found is not None and harness is not None and found != harness


#: How a failed reading of one process was judged.
_UNRELATED = "unrelated"
_EVIDENCE = "evidence"
_POSSIBLE = "possible"


class Sampler:
    """Reads the process table, keeping what cannot be excluded as a possible actor.

    *pids*, *open_process*, *clock* and *user_of* are psutil's, the wall clock
    and ``process_user`` by default, and are replaced in tests to model a
    process table. *browser_exe* and *browser_dir* name what the row's browser
    runs. *user* is the harness's user, as *user_of* reports it. *no_exec*
    says whether a process's program is fixed for its lifetime, which is
    Windows's by default and set in tests to model either platform. *pgid_of*
    reads a process's group, ``os.getpgid`` by default. *read_markers* says
    whether browser markers are read (``READ_MARKERS``). *timer* times each
    read and phase, ``time.perf_counter`` by default, and *cpu* is the
    process's CPU time (``time.process_time``). *last_pid_of* reads the
    kernel's last allocated pid as each sample begins (``read_last_pid``).
    """

    def __init__(
        self,
        root_pid: int,
        *,
        own_pid: int | None = None,
        pids: Callable[[], Iterable[int]] = psutil.pids,
        open_process: Callable[[int], Any] = psutil.Process,
        clock: Callable[[], float] = time.time,
        user_of: Callable[[Any], object | None] = process_user,
        user: object | None = None,
        browser_exe: str | None = None,
        browser_dir: str | None = None,
        no_exec: bool = NO_EXEC,
        pgid_of: Callable[[int], int | None] = posix_pgid,
        read_markers: bool = READ_MARKERS,
        timer: Callable[[], float] = time.perf_counter,
        last_pid_of: Callable[[], int | None] = read_last_pid,
        cpu: Callable[[], float] = time.process_time,
    ) -> None:
        self.root_pid = root_pid
        self._timer = timer
        self._cpu = cpu
        self._last_pid = last_pid_of
        #: When the sample in progress began, and the kernel's last pid then.
        self.began_at: float | None = None
        self.last_pid_at_begin: int | None = None
        #: The process groups at the first sample.
        self.baseline_pgids: list[int] = []
        self.no_exec = no_exec
        self._pgid_of = pgid_of
        self.read_markers = read_markers
        self.own_pid = os.getpid() if own_pid is None else own_pid
        self._pids = pids
        self._open = open_process
        self._clock = clock
        self._user_of = user_of
        self.user = harness_user() if user is None else user
        self.browser_exe = browser_exe
        self.browser_dir = browser_dir
        self._known: dict[int, ProcessRecord] = {}
        self._baseline: set[tuple[int, float]] | None = None
        self._row: set[tuple[int, float]] = set()
        #: Identities established as unrelated; read once, identity-checked.
        self._unrelated: set[tuple[int, float]] = set()
        #: First-sample identities whose ancestry could not be completed.
        self._watched: set[tuple[int, float]] = set()
        #: (pid, create time, command line) whose launcher was already asked.
        self._launchers_read: set[tuple[int, float, tuple[str, ...]]] = set()
        #: Lifetimes whose browser marker was already asked.
        self._markers_read: set[tuple[int, float]] = set()
        #: What sampling cost: first-sample outcomes and full reads per sample.
        self.first_sample_cached = 0
        self.first_sample_watched = 0
        self.reads_per_sample: list[int] = []
        self._reads = 0
        #: Lifetimes whose parent, executable and command line were all read,
        #: carried rather than read again where nothing can exec (``no_exec``).
        self._complete: set[tuple[int, float]] = set()
        self.carried_per_sample: list[int] = []
        self._carried = 0
        #: Lifetimes a full read found gone: a pid and create time do not come
        #: back, however long the pid stays listed.
        self._vanished: set[tuple[int, float]] = set()
        self.vanished = 0
        #: Each lifetime's owning user, once read: a process cannot change it.
        self._users: dict[tuple[int, float], object] = {}
        #: The slowest single read of the sample in progress, and of the run.
        self._slowest: dict[str, Any] | None = None
        self.slowest_read: dict[str, Any] | None = None
        #: Every sample of at least ``SLOW_SAMPLE_SECONDS``, up to a bound.
        self.slow_samples: list[dict[str, Any]] = []
        self.slow_sample_count = 0
        #: The phase the sample in progress is in, since when, and what each
        #: phase, read kind and canonicalization has taken so far.
        self._phase = "bookkeeping"
        self._phase_mark = 0.0
        self._phase_seconds = dict.fromkeys(SAMPLE_PHASES, 0.0)
        self._read_kinds: dict[str, dict[str, Any]] = {}
        self._canonicalizations = 0
        self._canonical_max = 0.0
        #: The last sample's phases and largest timed read (``_record_cost``).
        self.breakdown: dict[str, Any] | None = None
        #: Every group read that failed for a process still there.
        self.group_read_failures: list[dict[str, Any]] = []
        self.group_read_failure_count = 0
        self._episodes: dict[tuple[int, float | None], dict[str, Any]] = {}
        #: Failed argument reads of lifetimes already counted as a browser
        #: root, which keep their earlier reading (``_keeps_its_root``): one
        #: note per lifetime and profile retained, never replaced or cleared.
        self._retained: dict[tuple[int, float, str], dict[str, Any]] = {}
        self._closed: list[dict[str, Any]] = []

    def stats(self) -> dict[str, Any]:
        """What sampling cost, for the summary."""
        reads = self.reads_per_sample
        return {
            "no_exec": self.no_exec,
            "first_sample_cached": self.first_sample_cached,
            "first_sample_watched": self.first_sample_watched,
            "reads_per_sample_mean": (
                round(sum(reads) / len(reads), 1) if reads else None
            ),
            "reads_per_sample_max": max(reads) if reads else None,
            "carried_per_sample_mean": (
                round(sum(self.carried_per_sample) / len(self.carried_per_sample), 1)
                if self.carried_per_sample
                else None
            ),
            "vanished_reads": self.vanished,
            "slowest_read": self.slowest_read,
            "slow_sample_count": self.slow_sample_count,
            "slow_samples": self.slow_samples,
            "group_read_failure_count": self.group_read_failure_count,
            "group_read_failures": self.group_read_failures,
        }

    def possible_browser(self, exe: str | None) -> bool:
        return self._canonical(
            lambda: possible_browser(exe, self.browser_exe, self.browser_dir)
        )

    def _enter(self, phase: str) -> tuple[str, float]:
        """Charge the time since the last switch to the current phase, then
        switch to *phase*; return the phase left and when."""
        now = self._timer()
        self._phase_seconds[self._phase] += now - self._phase_mark
        self._phase_mark = now
        previous, self._phase = self._phase, phase
        return previous, now

    def _leave(self, previous: str, entered: float) -> float:
        """Charge the time since the last switch to the current phase, then
        switch back to *previous*; return the seconds since *entered*."""
        now = self._timer()
        self._phase_seconds[self._phase] += now - self._phase_mark
        self._phase_mark = now
        self._phase = previous
        return now - entered

    def _in_phase(self, phase: str, call: Callable[[], Any]) -> Any:
        previous, entered = self._enter(phase)
        try:
            return call()
        finally:
            self._leave(previous, entered)

    def _canonical(self, call: Callable[[], Any]) -> Any:
        """Run a call that resolves paths, charged to ``canonicalization``."""
        previous, entered = self._enter("canonicalization")
        try:
            return call()
        finally:
            seconds = self._leave(previous, entered)
            self._canonicalizations += 1
            self._canonical_max = max(self._canonical_max, seconds)

    def _group(self, pid: int, start: float) -> tuple[int | None, str | None]:
        """The process's group now, or None and why not: ``GONE``, or unread.

        An unread group is recorded, and is never the last one read.
        """
        try:
            return self._timed("pgid", pid, lambda: self._pgid_of(pid)), None
        except ProcessLookupError:
            return None, GONE
        except OSError as exc:
            error = f"unread: {type(exc).__name__}"
            if len(self.group_read_failures) < _GROUP_FAILURES_KEPT:
                self.group_read_failures.append(
                    {
                        "pid": pid,
                        "start_identity": start,
                        "t": self._clock(),
                        "error": error,
                    }
                )
            self.group_read_failure_count += 1
            return None, error

    def _timed(self, kind: str, pid: int, call: Callable[[], Any]) -> Any:
        """Run one read, charged to ``reads`` and counted by *kind*, keeping
        the sample's slowest with its kind and pid."""
        previous, began = self._enter("reads")
        try:
            return call()
        finally:
            seconds = self._leave(previous, began)
            stats = self._read_kinds.setdefault(
                kind, {"count": 0, "seconds": 0.0, "max_seconds": 0.0, "max_pid": pid}
            )
            stats["count"] += 1
            stats["seconds"] += seconds
            if seconds > stats["max_seconds"]:
                stats["max_seconds"], stats["max_pid"] = seconds, pid
            if self._slowest is None or seconds > self._slowest["seconds"]:
                self._slowest = {"kind": kind, "pid": pid, "seconds": seconds}

    def _another_user(
        self, process: Any, lifetime: tuple[int, float] | None = None
    ) -> bool:
        """Whether *process* is another user's; read once per *lifetime*."""
        found = self._users.get(lifetime) if lifetime is not None else None
        if found is None:
            found = self._timed(
                "username", getattr(process, "pid", -1), lambda: self._user_of(process)
            )
            if found is not None and lifetime is not None:
                self._users[lifetime] = found
        return another_user(found, self.user)

    @property
    def read_failures(self) -> list[dict[str, Any]]:
        """Every failed-read episode that was not established as unrelated.

        ``resolution`` says how an episode ended: ``readable``, ``exited``, or
        ``open`` when it was still unreadable at the last sample. A failed
        argument read of a known browser root (``_keeps_its_root``) is listed
        as retained history instead, with the profile its earlier reading
        named, never as a fresh reading.
        ``possible_browser`` says whether it leaves O1 unestablished; for a
        retained note it is False, and only for ``retained_profile`` is that
        true.
        """
        open_ = [
            dict(e, failures=sorted(e["failures"]), resolution="open")
            for e in self._episodes.values()
        ]
        retained = [
            dict(e, failures=sorted(e["failures"])) for e in self._retained.values()
        ]
        return [*self._closed, *open_, *retained]

    @property
    def relevant_read_failures(self) -> list[dict[str, Any]]:
        """The episodes that could have hidden a browser root."""
        return [e for e in self.read_failures if e["possible_browser"]]

    def _read(
        self, process: Any, known: ProcessRecord | None, *, arguments: bool = True
    ) -> tuple[int | None, str | None, tuple[str, ...], list[str], bool]:
        """Parent, executable and, unless *arguments* is False, command line."""
        self._reads += 1
        failures: list[str] = []
        ppid = known.ppid if known is not None else None
        exe = known.exe if known is not None else None
        cmdline = known.cmdline if known is not None else ()
        exe_read = False
        pid = process.pid
        try:
            ppid = self._timed("ppid", pid, process.ppid)
        except _UNREADABLE as exc:
            failures.append(f"ppid: {type(exc).__name__}")
        try:
            exe = self._timed("exe", pid, process.exe)
            exe_read = True
        except _UNREADABLE as exc:
            failures.append(f"exe: {type(exc).__name__}")
        if not arguments:
            return ppid, exe, cmdline, failures, exe_read
        try:
            cmdline = tuple(self._timed("cmdline", pid, process.cmdline))
        except _UNREADABLE as exc:
            failures.append(f"cmdline: {type(exc).__name__}")
        return ppid, exe, cmdline, failures, exe_read

    def sample(self) -> dict[int, ProcessRecord]:
        began = self._timer()
        cpu = {"began": self._cpu()}
        self._phase, self._phase_mark = "bookkeeping", began
        self._phase_seconds = dict.fromkeys(SAMPLE_PHASES, 0.0)
        self._read_kinds = {}
        self._canonicalizations, self._canonical_max = 0, 0.0
        self.began_at = self._clock()
        self.last_pid_at_begin = self._in_phase("last_pid", self._last_pid)
        cpu["last_pid"] = self._cpu()
        pids = self._in_phase("enumeration", lambda: list(self._pids()))
        cpu["enumeration"] = self._cpu()
        self._slowest = None
        first = self._baseline is None
        sample: dict[int, ProcessRecord] = {}
        # pid -> (episode key, failed fields, exe, exe read now, process or None,
        #         parent read)
        failures: dict[int, tuple[Any, list[str], str | None, bool, Any, bool]] = {}
        # pid -> the known root whose failed read may keep its reading.
        retaining: dict[int, tuple[ProcessRecord, list[str]]] = {}
        # pid -> the process whose create time this sample read.
        identified: dict[int, Any] = {}
        for pid in pids:
            known = self._known.get(pid)
            # When opening or identifying fails, whatever runs at the pid now is
            # unverified: the failure is keyed without a create time, and no
            # exclusion established for an earlier process there applies.
            try:
                process = self._timed("open", pid, lambda: self._open(pid))
            except psutil.NoSuchProcess:
                continue
            except _UNREADABLE as exc:
                if known is not None:
                    sample[pid] = known
                failures[pid] = (
                    (pid, None),
                    [f"open: {type(exc).__name__}"],
                    known.exe if known is not None else None,
                    False,
                    None,
                    False,
                )
                continue
            try:
                # The create time is what separates a recycled pid from the
                # process already known under it.
                start = self._timed("create_time", pid, process.create_time)
            except psutil.NoSuchProcess:
                continue
            except _UNREADABLE as exc:
                if self._another_user(process):
                    # Established for this sample only: there is no create
                    # time to keep it by.
                    continue
                if known is not None:
                    sample[pid] = known
                failures[pid] = (
                    (pid, None),
                    [f"identity: {type(exc).__name__}"],
                    known.exe if known is not None else None,
                    False,
                    process,
                    False,
                )
                continue
            identified[pid] = process
            if known is not None and known.start != start:
                known = None
            if known is not None and known.identity in self._unrelated:
                # Settled as unrelated, but a group can still be joined, and a
                # group signal's members must include it if it did.
                group, error = self._group(pid, start)
                if error == GONE:
                    # Gone since it was opened: an exit, not a change of group.
                    continue
                if (group, error) != (known.pgid, known.pgid_error):
                    known = replace(known, pgid=group, pgid_error=error)
                sample[pid] = known
                continue
            if (pid, start) in self._vanished:
                continue
            if self.no_exec and known is not None and known.identity in self._complete:
                # Its image, command line and parent are its own for good.
                ppid, exe, cmdline, failed, exe_read = (
                    known.ppid,
                    known.exe,
                    known.cmdline,
                    [],
                    True,
                )
                self._carried += 1
            else:
                # Windows, first sample: another user's process is settled by
                # its user before its arguments cost a read. Its parent and
                # executable are still read, for the ancestry of the others.
                foreign = (
                    first and self.no_exec and self._another_user(process, (pid, start))
                )
                if foreign:
                    self._unrelated.add((pid, start))
                try:
                    ppid, exe, cmdline, failed, exe_read = self._read(
                        process, known, arguments=not foreign
                    )
                except psutil.NoSuchProcess:
                    self._vanished.add((pid, start))
                    self.vanished += 1
                    continue
                if not failed:
                    self._complete.add((pid, start))
            group, error = self._group(pid, start)
            if error == GONE:
                continue
            # Carried while the command line holds; read, if at all, only once
            # the process is classified (``_read_launchers``).
            launcher = (
                known.launcher
                if known is not None and known.cmdline == cmdline
                else None
            )
            # Canonicalizes the profile its arguments name, if any.
            sample[pid] = self._canonical(
                lambda: record(
                    pid,
                    -1 if ppid is None else ppid,
                    start,
                    exe,
                    cmdline,
                    in_row=known is not None and known.in_row,
                    launcher=launcher,
                    pgid=group,
                    pgid_error=error,
                    browser_marker=known.browser_marker if known is not None else None,
                )
            )
            if failed and self._keeps_its_root(known, failed, exe):
                # Decided once the sample is complete: whether it parents
                # another browser record is known only then.
                assert known is not None
                retaining[pid] = (known, failed)
            if failed:
                parent_read = not any(f.startswith("ppid") for f in failed)
                failures[pid] = (
                    (pid, start),
                    failed,
                    exe,
                    exe_read,
                    process,
                    parent_read,
                )
        cpu["per_process"] = self._cpu()
        # Already counted as its profile's browser root: the record keeps that
        # reading, with the profile it named rather than one the old arguments
        # resolve to now, and the failure is kept as an audit note. Not for a
        # parent of another browser record (``_keeps_its_root``).
        parents = {p.ppid for p in sample.values() if p.profile is not None}
        for pid, (known, failed) in retaining.items():
            if pid in parents:
                continue
            sample[pid] = replace(sample[pid], profile=known.profile)
            self._note_retained(known, failed)
            del failures[pid]
        if first:
            self._baseline = {process.identity for process in sample.values()}
            self.baseline_pgids = sorted(
                {p.pgid for p in sample.values() if p.pgid is not None}
            )
            root = sample.get(self.root_pid)
            if root is not None:
                self._row.add(root.identity)
        self._classify(sample)
        self._read_launchers(sample, identified)
        if not first:
            self._read_markers(sample, identified)
        # Whatever the harness turned out to own is no longer excluded.
        row = {process.identity for process in sample.values() if process.in_row}
        self._unrelated -= row
        self._watched -= row
        settled: set[tuple[int, float]] = set()
        if first:
            settled = self._settled_ancestry(sample)
            if self.no_exec:
                settled |= self._settled_image(sample, failures)
            for pid in identified:
                process = sample.get(pid)
                if process is None or process.in_row or pid == self.own_pid:
                    continue
                if process.identity not in settled:
                    self._watched.add(process.identity)
        verdicts = self._judge(sample, failures, settled)
        # Running before any actor, and its whole ancestry read now not leading
        # to the harness, or on Windows its whole image read now and no
        # browser: established unrelated.
        self._unrelated |= settled
        if first:
            # Everything cached so far was established in this first sample,
            # by ancestry, image or user.
            self.first_sample_cached = len(self._unrelated)
            # Not those another rule established unrelated in the same sample.
            self.first_sample_watched = len(self._watched - self._unrelated)
        self.reads_per_sample.append(self._reads)
        self._reads = 0
        self.carried_per_sample.append(self._carried)
        self._carried = 0
        self._track(sample, verdicts)
        self._resolve_by_user(sample, identified)
        self._known = sample
        # The rest of the sample is the bookkeeping it is in; the phases now
        # add up to its duration.
        self._enter("bookkeeping")
        ended = self._phase_mark
        cpu["classification"] = self._cpu()
        self._record_cost(sample, ended - began, cpu)
        return sample

    def _record_cost(
        self, sample: dict[int, ProcessRecord], seconds: float, cpu: dict[str, float]
    ) -> None:
        """Keep the slowest read, this sample's breakdown, and every slow
        sample's.

        ``cpu`` holds the process's CPU time where the sample began and
        after each of its stretches, which run in order: its last-pid read,
        the enumeration, the loop over every process (reads, the
        canonicalization of each record and the loop's own bookkeeping), and
        the classification and judgement after it.
        """
        now = self._clock()
        slowest = self._slowest
        if slowest is not None:
            known = sample.get(slowest["pid"])
            slowest = dict(
                slowest,
                seconds=round(slowest["seconds"], 4),
                exe=known.exe if known is not None else None,
                t=now,
            )
            if (
                self.slowest_read is None
                or slowest["seconds"] > self.slowest_read["seconds"]
            ):
                self.slowest_read = slowest
        marks = list(cpu.values())
        self.breakdown = {
            "t": now,
            "seconds": round(seconds, 4),
            "reads": self.reads_per_sample[-1],
            "slowest": slowest,
            "phases": {
                phase: round(spent, 4) for phase, spent in self._phase_seconds.items()
            },
            "read_kinds": {
                kind: dict(
                    stats,
                    seconds=round(stats["seconds"], 4),
                    max_seconds=round(stats["max_seconds"], 4),
                )
                for kind, stats in sorted(self._read_kinds.items())
            },
            "canonicalization": {
                "count": self._canonicalizations,
                "max_seconds": round(self._canonical_max, 4),
            },
            "cpu_seconds": round(marks[-1] - marks[0], 4),
            "cpu": {
                stretch: round(mark - before, 4)
                for (stretch, mark), before in zip(list(cpu.items())[1:], marks)
            },
        }
        if seconds < SLOW_SAMPLE_SECONDS:
            return
        self.slow_sample_count += 1
        if len(self.slow_samples) < _SLOW_SAMPLES_KEPT:
            self.slow_samples.append(self.breakdown)

    @staticmethod
    def _keeps_its_root(
        known: ProcessRecord | None, failed: list[str], exe: str | None
    ) -> bool:
        """Whether a failed read leaves a known browser root's reading standing.

        Only when this lifetime was read before as a browser root (a profile,
        no ``--type=``), so that profile's count already includes it; only
        its arguments failed, so its executable was read in this sample; and
        that executable is the one it had. Pinned to that profile, a failed
        read of it cannot hide a second root there. For any other profile it
        stays unidentified, which the note's ``retained_profile`` lets the
        judge of that profile see. Anything else, a helper or driver read
        before included, stays a failure: an exec since the last reading
        could have made it a root.

        Nor in a sample where it is the parent of another record with a
        profile: pinned, it would fold those roots into its tree, while its
        hidden arguments may name another profile and leave each of them a
        root of its own.
        """
        return (
            known is not None
            and known.profile is not None
            and {failure.split(":", 1)[0] for failure in failed} == {"cmdline"}
            and exe == known.exe
        )

    def _note_retained(self, known: ProcessRecord, failed: list[str]) -> None:
        """Record the failed read of a known root, once per lifetime and
        profile.

        A lifetime read on one profile, then on another, can be retained on
        each: each note is what the judge of another profile must see, so a
        later one never stands in for an earlier.
        """
        now = self._clock()
        assert known.profile is not None
        key = (*known.identity, known.profile)
        note: dict[str, Any] | None = self._retained.get(key)
        if note is None:
            note = {
                "pid": known.pid,
                "start_identity": known.start,
                "exe": known.exe,
                "failures": set(),
                "first": now,
                "possible_browser": False,
                "resolution": "a known browser root's earlier reading retained",
                "retained_profile": known.profile,
            }
            self._retained[key] = note
        note["failures"].update(failed)
        note["last"] = now
        note["seconds"] = round(now - note["first"], 4)

    def _read_launchers(
        self, sample: dict[int, ProcessRecord], identified: dict[int, Any]
    ) -> None:
        """Record the launcher of the row's server and owner processes.

        Only for a row actor, established by ancestry in this sample, whose
        command line runs the server or the owner module, and only when this
        sample read its create time. The launcher is a cached initial-image
        observation for this PID, create time and command line; same-command
        re-exec is outside this identity oracle.
        """
        for pid, process in list(sample.items()):
            if not process.in_row or pid not in identified:
                continue
            if invoked_module(process.cmdline) not in (SERVER_MODULE, OWNER_MODULE):
                continue
            key = (process.pid, process.start, process.cmdline)
            if key in self._launchers_read:
                continue
            self._launchers_read.add(key)
            launcher = self._timed(
                "environ", pid, lambda: read_launcher(identified[pid])
            )
            if launcher is not None:
                sample[pid] = replace(process, launcher=launcher)

    def _read_markers(
        self, sample: dict[int, ProcessRecord], identified: dict[int, Any]
    ) -> None:
        """Record the browser marker of a process that could be the browser.

        Once per lifetime, when a sample after the first read its create time
        and its executable could be the row's browser, whether or not it is in
        the row's tree: Chromium's crashpad handler is started by the browser
        and then leaves it, parented to pid 1 in a session of its own, carrying
        the browser's environment. The first sample's processes predate every
        actor and are not asked. A marker is set before the browser starts, so
        a process that shows none when first read has none.
        """
        if not self.read_markers:
            return
        for pid, process in list(sample.items()):
            if pid not in identified or process.identity in self._markers_read:
                continue
            if pid == self.own_pid or process.identity in (self._baseline or ()):
                continue
            if not process.exe or not self.possible_browser(process.exe):
                continue
            try:
                marker = self._timed(
                    "environ", pid, lambda: read_browser_marker(identified[pid])
                )
            except psutil.NoSuchProcess:
                continue
            except _UNREADABLE:
                # Asked again on the next sample, for as long as it lives.
                continue
            self._markers_read.add(process.identity)
            if marker is not None:
                sample[pid] = replace(process, browser_marker=marker)

    def _settled_ancestry(
        self, sample: dict[int, ProcessRecord]
    ) -> set[tuple[int, float]]:
        """The identities whose ancestry, read in this sample, ends away from the harness.

        Every parent has to be in the sample and no younger than its child: a
        parent that could not be read, or a younger process at the parent's
        pid, leaves the ancestry open. It ends at a child of pid 0 (or a pid
        that is its own parent). A child of pid 1 stays open whatever its create
        time says: pid 1 adopts every orphan, the harness's included, and a
        wall-clock create time cannot show that it was born before the harness.
        """
        memo: dict[int, bool] = {}
        for pid in sample:
            chain: list[int] = []
            current = pid
            while True:
                if current in memo:
                    settled = memo[current]
                    break
                process = sample[current]
                if process.in_row or current == self.own_pid or current in chain:
                    settled = False
                    break
                chain.append(current)
                if process.ppid in (0, current):
                    settled = True
                    break
                if process.ppid == 1:
                    settled = False
                    break
                parent = sample.get(process.ppid)
                if parent is None or parent.start > process.start:
                    settled = False
                    break
                current = process.ppid
            for member in chain:
                memo[member] = settled
        return {
            process.identity
            for pid, process in sample.items()
            if memo.get(pid) and pid != self.own_pid
        }

    def _settled_image(
        self,
        sample: dict[int, ProcessRecord],
        failures: dict[int, tuple[Any, list[str], str | None, bool, Any, bool]],
    ) -> set[tuple[int, float]]:
        """First-sample processes whose whole image is read now and is no browser.

        Only where a process cannot exec (``no_exec``, Windows): its executable
        and command line, both read in this sample, are then its own for the
        rest of its lifetime, so one that names no profile and cannot be the
        browser never will, whatever its ancestry says. A process whose
        executable or arguments could not be read is not judged here.
        """
        settled = set()
        for pid, process in sample.items():
            if process.in_row or pid == self.own_pid:
                continue
            if pid in failures:
                fields = {failure.split(":", 1)[0] for failure in failures[pid][1]}
                if fields & {"open", "identity", "exe", "cmdline"}:
                    continue
            if not process.exe or self.possible_browser(process.exe):
                continue
            if process.profile is not None or any(
                argument.startswith(USER_DATA_DIR_FLAG) for argument in process.cmdline
            ):
                continue
            settled.add(process.identity)
        return settled

    def _resolve_by_user(
        self, sample: dict[int, ProcessRecord], identified: dict[int, Any]
    ) -> None:
        """Resolve a possible-actor record once a reading shows another user owns it.

        The only later reading that resolves one, and only for the lifetime the
        record names: a process cannot change its real owner, so this says
        what it was while it was unreadable too. A record without a create
        time cannot be matched to any later process at its pid. Becoming
        readable, or exiting, says nothing of the kind.
        """
        records = [*self._episodes.values(), *self._closed]
        for episode in records:
            if not episode["possible_browser"]:
                continue
            start = episode["start_identity"]
            if start is None:
                continue
            pid = episode["pid"]
            process = identified.get(pid)
            current = sample.get(pid)
            if process is None or current is None or current.start != start:
                continue
            if self._another_user(process, current.identity):
                episode["possible_browser"] = False
                episode["resolved_by"] = "another user"
                self._unrelated.add(current.identity)

    def _judge(
        self,
        sample: dict[int, ProcessRecord],
        failures: dict[int, tuple[Any, list[str], str | None, bool, Any, bool]],
        settled: set[tuple[int, float]],
    ) -> dict[Any, tuple[str, int, str | None, list[str]]]:
        """Judge each failed reading: unrelated, evidence only, or a possible actor."""
        verdicts: dict[Any, tuple[str, int, str | None, list[str]]] = {}
        for pid, (key, failed, exe, exe_read, process, parent_read) in failures.items():
            current = sample.get(pid)
            # Set only when this sample read the create time of *current*.
            lifetime = current.identity if key[1] is not None and current else None
            in_row = current is not None and current.in_row
            fields = {failure.split(":", 1)[0] for failure in failed}
            # A baseline process whose ancestry never resolved is judged like
            # any other: being watched is a reason to read it again, never a
            # reason to discount what cannot be seen.
            if lifetime is not None and lifetime in settled:
                verdict = _UNRELATED
            elif process is not None and self._another_user(process, lifetime):
                verdict = _UNRELATED
                if lifetime is not None:
                    self._unrelated.add(lifetime)
            elif (
                self.no_exec
                and exe_read
                and not self.possible_browser(exe)
                and not in_row
                and parent_read
                and lifetime is not None
            ):
                # Windows: its image, read now, is its own for its lifetime
                # and cannot be the browser, so it can never be a browser
                # root, whatever its arguments. Established unrelated for this
                # lifetime and never read again; the failed read is recorded
                # once. Like LsaIso.exe, whose arguments psutil retries for a
                # second before it gives up.
                verdict = _UNRELATED
                self._unrelated.add(lifetime)
                now = self._clock()
                self._closed.append(
                    {
                        "pid": pid,
                        "start_identity": lifetime[1],
                        "exe": exe,
                        "failures": sorted(failed),
                        "first": now,
                        "last": now,
                        "seconds": 0.0,
                        "possible_browser": False,
                        "resolution": "settled by its image",
                    }
                )
            elif exe_read and not self.possible_browser(exe):
                # Not a browser, whatever its arguments. Kept as evidence when
                # it belongs to the row or its ancestry could not be read.
                verdict = _EVIDENCE if (in_row or not parent_read) else _UNRELATED
            elif fields <= {"exe"}:
                # Parent and arguments were read, so the census sees it whole.
                verdict = _EVIDENCE if in_row else _UNRELATED
            else:
                verdict = _POSSIBLE
            verdicts[key] = (verdict, pid, exe, failed)
        return verdicts

    def _track(
        self,
        sample: dict[int, ProcessRecord],
        verdicts: dict[Any, tuple[str, int, str | None, list[str]]],
    ) -> None:
        now = self._clock()
        failing: set[Any] = set()
        for key, (verdict, pid, exe, failed) in verdicts.items():
            if verdict == _UNRELATED:
                continue
            failing.add(key)
            episode = self._episodes.setdefault(
                key,
                {
                    "pid": pid,
                    "start_identity": key[1],
                    "exe": exe,
                    "failures": set(),
                    "first": now,
                    "possible_browser": False,
                },
            )
            episode["failures"].update(failed)
            if exe:
                episode["exe"] = exe
            episode["last"] = now
            episode["seconds"] = round(now - episode["first"], 4)
            if verdict == _POSSIBLE:
                episode["possible_browser"] = True
        for key in [key for key in self._episodes if key not in failing]:
            episode = self._episodes.pop(key)
            present = sample.get(key[0])
            alive = present is not None and key[1] in (None, present.start)
            self._closed.append(
                dict(
                    episode,
                    failures=sorted(episode["failures"]),
                    resolution="readable" if alive else "exited",
                )
            )

    def _classify(self, sample: dict[int, ProcessRecord]) -> None:
        """Mark row actors: the harness, and whatever descends from it.

        At the first sample too: a descendant already running then, such as a
        staging leftover, is a row actor like any other and is never cached.
        """
        changed = True
        while changed:
            changed = False
            for pid, process in sample.items():
                if process.in_row or pid == self.own_pid:
                    continue
                if process.identity in self._row:
                    sample[pid] = replace(process, in_row=True)
                    changed = True
                    continue
                parent = sample.get(process.ppid)
                if parent is not None and (
                    parent.in_row or parent.identity in self._row
                ):
                    self._row.add(process.identity)
                    sample[pid] = replace(process, in_row=True)
                    changed = True


def duration_stats(durations: Sequence[float]) -> dict[str, float | None]:
    """Mean and 95th percentile of the sample durations, in seconds."""
    if not durations:
        return {"sample_seconds_mean": None, "sample_seconds_p95": None}
    ordered = sorted(durations)
    p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
    return {
        "sample_seconds_mean": round(sum(ordered) / len(ordered), 4),
        "sample_seconds_p95": round(p95, 4),
    }


def run_ahead() -> dict[str, Any]:
    """Ask for ``SCHEDULING_CLASS`` where there is one; say what this runs at.

    A refusal is recorded, never raised: the gaps the summary reports are
    judged either way.
    """
    process = psutil.Process()
    fields: dict[str, Any] = {}
    if SCHEDULING_CLASS is not None:
        try:
            process.nice(SCHEDULING_CLASS)
        except (psutil.Error, OSError) as exc:
            fields["priority_error"] = type(exc).__name__
    try:
        now = process.nice()
    except (psutil.Error, OSError):
        now = None
    # Windows answers with a class, POSIX with a nice value.
    fields["priority"] = getattr(now, "name", now)
    return fields


def write_event(
    out: IO[str],
    base: Mapping[str, Any],
    actor: str,
    kind: str,
    fields: Mapping[str, Any],
    t: float,
) -> None:
    out.write(
        json.dumps(
            {"t": t, **base, "actor": actor, "kind": kind, **fields}, sort_keys=True
        )
        + "\n"
    )


def observe(
    sampler: Sampler,
    tracker: Tracker,
    out: IO[str],
    stop_requested: Callable[[], bool],
    *,
    base: Mapping[str, Any],
    interval: float,
    deadline: float,
    timer: Callable[[], float] = time.perf_counter,
    monotonic: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    cpu: Callable[[], float] = time.process_time,
) -> dict[str, Any]:
    """Sample until *stop_requested* or *deadline*; return the loop's summary.

    Besides the largest gap between two samples it keeps that gap's
    breakdown (``largest_gap``): the wall time outside sampling, from one
    sample's end to the next one's start, with what it went to
    (``BETWEEN_STEPS``), and the wall time in the sample that closed it, with
    that sample's phases (``Sampler.breakdown``). Each part's ``unaccounted``
    is what none of its steps or phases took, so a stall that falls between
    them, or a stepped wall clock, shows there rather than in a step.
    """
    began = monotonic()
    observation_start: float | None = None
    last_sample: float | None = None
    max_gap = 0.0
    largest_gap: dict[str, Any] | None = None
    #: Wall time of each ``sampler.sample()``, the watcher's own cost.
    durations: list[float] = []
    #: ``[began, ended, kernel's last pid as it began]`` for every sample;
    #: ``ended`` is the time every event of that sample carries.
    sample_log: list[list[Any]] = []
    stopped_by = "deadline"
    #: What the time since the last sample ended went to (``BETWEEN_STEPS``),
    #: and the process's CPU time when it began.
    between: dict[str, float] = {}
    between_cpu = cpu()
    sleep_requested = 0.0

    def step(name: str, call: Callable[[], Any]) -> Any:
        began_step = timer()
        try:
            return call()
        finally:
            between[name] = between.get(name, 0.0) + timer() - began_step

    def take_sample() -> None:
        nonlocal observation_start, last_sample, max_gap, largest_gap, between_cpu
        cpu_outside = cpu() - between_cpu
        began_sample = monotonic()
        sample = sampler.sample()
        durations.append(monotonic() - began_sample)
        now = wall()
        if last_sample is not None:
            gap = now - last_sample
            # The gap and the sample that closed it, never the run's slowest
            # sample, which can sit anywhere.
            if largest_gap is None or gap > max_gap:
                largest_gap = _gap_record(
                    gap,
                    now,
                    last_sample,
                    sampler,
                    between,
                    sleep_requested,
                    cpu_outside,
                )
            max_gap = max(max_gap, gap)
        last_sample = now
        sample_log.append([sampler.began_at, now, sampler.last_pid_at_begin])
        between.clear()
        between_cpu = cpu()
        events = step("tracker", lambda: tracker.observe(sample, now))

        def write_all() -> None:
            nonlocal observation_start
            for actor, kind, fields in events:
                write_event(out, base, actor, kind, fields, now)
            if observation_start is None:
                observation_start = now
                # The baseline is taken. The harness waits for this line before
                # it starts an actor, so no actor is mistaken for background.
                write_event(
                    out,
                    base,
                    "watcher",
                    "watcher.ready",
                    {
                        "pid": os.getpid(),
                        "baseline_processes": len(sample),
                        "baseline_pgids": sampler.baseline_pgids,
                    },
                    now,
                )

        step("write", write_all)
        step("flush", out.flush)

    while monotonic() - began < deadline:
        if step("stop_check", stop_requested):
            stopped_by = "stop file"
            # One more sample after the request, so the observation
            # provably ends after whatever the harness waited for.
            take_sample()
            break
        tick = monotonic()
        take_sample()
        elapsed = monotonic() - tick
        sleep_requested = max(0.0, interval - elapsed)
        slept_from = timer()
        sleep(sleep_requested)
        slept = timer() - slept_from
        between["sleep"] = min(slept, sleep_requested)
        between["wakeup_delay"] = max(0.0, slept - sleep_requested)

    return {
        "observation_start": observation_start,
        "observation_end": last_sample,
        "max_gap_seconds": round(max_gap, 4),
        "largest_gap": largest_gap,
        **duration_stats(durations),
        "sample_log": sample_log,
        "stopped_by": stopped_by,
    }


def _gap_record(
    gap: float,
    now: float,
    last_sample: float,
    sampler: Sampler,
    between: Mapping[str, float],
    sleep_requested: float,
    cpu_outside: float,
) -> dict[str, Any]:
    """The breakdown of a gap that ended at *now*, by the sample closing it."""
    breakdown = sampler.breakdown or {}
    began = sampler.began_at if sampler.began_at is not None else now
    outside, inside = began - last_sample, now - began
    phases = breakdown.get("phases") or {}
    return {
        "seconds": round(gap, 4),
        "t": now,
        "outside_sampling": {
            "seconds": round(outside, 4),
            "steps": {name: round(between.get(name, 0.0), 4) for name in BETWEEN_STEPS},
            "sleep_requested": round(sleep_requested, 4),
            "unaccounted": round(outside - sum(between.values()), 4),
            "cpu_seconds": round(cpu_outside, 4),
        },
        "in_sample": {
            "seconds": round(inside, 4),
            "unaccounted": round(inside - sum(phases.values()), 4),
        },
        "sample": breakdown,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--stop", required=True, type=Path)
    parser.add_argument("--run", required=True)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--row", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--interval", type=float, default=SAMPLE_SECONDS)
    # The process whose descendants are the row's actors. The harness passes
    # its own pid; by default, whoever started this watcher.
    parser.add_argument("--root-pid", type=int, default=os.getppid())
    # Its own deadline, so a harness that dies without writing the stop file
    # cannot leave this sampling for the rest of the runner's life.
    parser.add_argument("--deadline", type=float, default=900.0)
    # What the row's browser runs: an unreadable actor running either could be
    # a browser root, and one running neither cannot.
    parser.add_argument("--browser-exe")
    parser.add_argument("--browser-dir")
    args = parser.parse_args(argv)

    base = {
        "run": args.run,
        "experiment": args.experiment,
        "row": args.row,
        "platform": args.platform,
    }
    # Before the baseline, so every sample the rows are judged by runs at it.
    scheduling = run_ahead()
    tracker = Tracker()
    sampler = Sampler(
        args.root_pid,
        browser_exe=args.browser_exe,
        browser_dir=args.browser_dir,
    )
    with args.out.open("a", encoding="utf-8") as out:
        loop = observe(
            sampler,
            tracker,
            out,
            args.stop.exists,
            base=base,
            interval=args.interval,
            deadline=args.deadline,
        )
        write_event(
            out,
            base,
            "watcher",
            "watcher.summary",
            {
                "pid": os.getpid(),
                "root_pid": args.root_pid,
                "samples": tracker.samples,
                "interval_seconds": args.interval,
                **scheduling,
                **loop,
                **sampler.stats(),
                "browser_exe": args.browser_exe,
                "browser_dir": args.browser_dir,
                "read_failures": sampler.read_failures,
                "relevant_read_failures": sampler.relevant_read_failures,
                "max_roots": tracker.max_roots,
                "violations": tracker.violations,
            },
            time.time(),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
