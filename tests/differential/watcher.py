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

**Nothing a row could still change is settled by age.** A process can exec at
any moment of its life, and a forked child shows its parent's command line
until it does, which is how the Node driver starts Chromium on POSIX. So every
row actor, and every process not established as unrelated, has its executable
and command line read again on every sample for as long as it lives, and a
change is reported as
``process.update``. Its identity is checked on every sample either way.

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
* its executable, read in the same sample, is neither the row's browser
  (``--browser-exe``) nor anything under the managed browsers
  (``--browser-dir``). That is the setuid ``/bin/ps`` the product runs on
  macOS: psutil cannot read its arguments, and it cannot be a browser.

Wall-clock create times are never a birth-order key: the calendar clock can be
stepped, and a process born after the harness can then read as older.

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

What sampling cannot see: a process that lives and dies between two samples.
The summary records when observation began and ended and the largest wall-clock
gap between two samples, so a claim built on it can state the window it had; an
overlap wholly between samples remains outside this oracle's resolution.

Imports nothing from the repository, so it runs as a plain script:
``python watcher.py --out FILE --stop FILE ...``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

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

    @property
    def identity(self) -> tuple[int, float]:
        return (self.pid, self.start)

    def as_event_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "pid": self.pid,
            "ppid": self.ppid,
            "start_identity": self.start,
            "exe": self.exe,
            "cmdline": list(self.cmdline),
            "in_row": self.in_row,
        }
        if self.launcher is not None:
            fields["launcher"] = self.launcher
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
) -> ProcessRecord:
    cmdline = tuple(cmdline)
    return ProcessRecord(
        pid, ppid, start, exe, cmdline, user_data_dir(cmdline), in_row, launcher
    )


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
                    if (previous.exe, previous.cmdline) != (
                        process.exe,
                        process.cmdline,
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
    Windows's by default and set in tests to model either platform.
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
    ) -> None:
        self.root_pid = root_pid
        self.no_exec = no_exec
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
        #: What sampling cost: first-sample outcomes and full reads per sample.
        self.first_sample_cached = 0
        self.first_sample_watched = 0
        self.reads_per_sample: list[int] = []
        self._reads = 0
        self._episodes: dict[tuple[int, float | None], dict[str, Any]] = {}
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
        }

    def possible_browser(self, exe: str | None) -> bool:
        return possible_browser(exe, self.browser_exe, self.browser_dir)

    def _another_user(self, process: Any) -> bool:
        return another_user(self._user_of(process), self.user)

    @property
    def read_failures(self) -> list[dict[str, Any]]:
        """Every failed-read episode that was not established as unrelated.

        ``resolution`` says how an episode ended: ``readable``, ``exited``, or
        ``open`` when it was still unreadable at the last sample.
        ``possible_browser`` says whether it leaves O1 unestablished.
        """
        open_ = [
            dict(e, failures=sorted(e["failures"]), resolution="open")
            for e in self._episodes.values()
        ]
        return [*self._closed, *open_]

    @property
    def relevant_read_failures(self) -> list[dict[str, Any]]:
        """The episodes that could have hidden a browser root."""
        return [e for e in self.read_failures if e["possible_browser"]]

    def _read(
        self, process: Any, known: ProcessRecord | None
    ) -> tuple[int | None, str | None, tuple[str, ...], list[str], bool]:
        self._reads += 1
        failures: list[str] = []
        ppid = known.ppid if known is not None else None
        exe = known.exe if known is not None else None
        cmdline = known.cmdline if known is not None else ()
        exe_read = False
        try:
            ppid = process.ppid()
        except _UNREADABLE as exc:
            failures.append(f"ppid: {type(exc).__name__}")
        try:
            exe = process.exe()
            exe_read = True
        except _UNREADABLE as exc:
            failures.append(f"exe: {type(exc).__name__}")
        try:
            cmdline = tuple(process.cmdline())
        except _UNREADABLE as exc:
            failures.append(f"cmdline: {type(exc).__name__}")
        return ppid, exe, cmdline, failures, exe_read

    def sample(self) -> dict[int, ProcessRecord]:
        first = self._baseline is None
        sample: dict[int, ProcessRecord] = {}
        # pid -> (episode key, failed fields, exe, exe read now, process or None,
        #         parent read)
        failures: dict[int, tuple[Any, list[str], str | None, bool, Any, bool]] = {}
        # pid -> the process whose create time this sample read.
        identified: dict[int, Any] = {}
        for pid in self._pids():
            known = self._known.get(pid)
            # When opening or identifying fails, whatever runs at the pid now is
            # unverified: the failure is keyed without a create time, and no
            # exclusion established for an earlier process there applies.
            try:
                process = self._open(pid)
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
                start = process.create_time()
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
                sample[pid] = known
                continue
            try:
                ppid, exe, cmdline, failed, exe_read = self._read(process, known)
            except psutil.NoSuchProcess:
                continue
            # Carried while the command line holds; read, if at all, only once
            # the process is classified (``_read_launchers``).
            launcher = (
                known.launcher
                if known is not None and known.cmdline == cmdline
                else None
            )
            sample[pid] = record(
                pid,
                -1 if ppid is None else ppid,
                start,
                exe,
                cmdline,
                in_row=known is not None and known.in_row,
                launcher=launcher,
            )
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
        if first:
            self._baseline = {process.identity for process in sample.values()}
            root = sample.get(self.root_pid)
            if root is not None:
                self._row.add(root.identity)
        self._classify(sample)
        self._read_launchers(sample, identified)
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
        self._track(sample, verdicts)
        self._resolve_by_user(sample, identified)
        self._known = sample
        return sample

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
            launcher = read_launcher(identified[pid])
            if launcher is not None:
                sample[pid] = replace(process, launcher=launcher)

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
            if self._another_user(process):
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
            elif process is not None and self._another_user(process):
                verdict = _UNRELATED
                if lifetime is not None:
                    self._unrelated.add(lifetime)
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
    tracker = Tracker()
    sampler = Sampler(
        args.root_pid,
        browser_exe=args.browser_exe,
        browser_dir=args.browser_dir,
    )
    began = time.monotonic()
    observation_start: float | None = None
    last_sample: float | None = None
    max_gap = 0.0
    #: Wall time of each ``sampler.sample()``, the watcher's own cost.
    durations: list[float] = []
    stopped_by = "deadline"
    with args.out.open("a", encoding="utf-8") as out:

        def write(actor: str, kind: str, fields: dict[str, Any], t: float) -> None:
            out.write(
                json.dumps(
                    {"t": t, **base, "actor": actor, "kind": kind, **fields},
                    sort_keys=True,
                )
                + "\n"
            )

        def take_sample() -> None:
            nonlocal observation_start, last_sample, max_gap
            began_sample = time.monotonic()
            sample = sampler.sample()
            durations.append(time.monotonic() - began_sample)
            now = time.time()
            if last_sample is not None:
                max_gap = max(max_gap, now - last_sample)
            last_sample = now
            for actor, kind, fields in tracker.observe(sample, now):
                write(actor, kind, fields, now)
            if observation_start is None:
                observation_start = now
                # The baseline is taken. The harness waits for this line before
                # it starts an actor, so no actor is mistaken for background.
                write(
                    "watcher",
                    "watcher.ready",
                    {"pid": os.getpid(), "baseline_processes": len(sample)},
                    now,
                )
            out.flush()

        while time.monotonic() - began < args.deadline:
            if args.stop.exists():
                stopped_by = "stop file"
                # One more sample after the request, so the observation
                # provably ends after whatever the harness waited for.
                take_sample()
                break
            tick = time.monotonic()
            take_sample()
            elapsed = time.monotonic() - tick
            time.sleep(max(0.0, args.interval - elapsed))

        write(
            "watcher",
            "watcher.summary",
            {
                "pid": os.getpid(),
                "root_pid": args.root_pid,
                "samples": tracker.samples,
                "interval_seconds": args.interval,
                "observation_start": observation_start,
                "observation_end": last_sample,
                "max_gap_seconds": round(max_gap, 4),
                **duration_stats(durations),
                **sampler.stats(),
                "browser_exe": args.browser_exe,
                "browser_dir": args.browser_dir,
                "read_failures": sampler.read_failures,
                "relevant_read_failures": sampler.relevant_read_failures,
                "max_roots": tracker.max_roots,
                "violations": tracker.violations,
                "stopped_by": stopped_by,
            },
            time.time(),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
