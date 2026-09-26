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
row actor, and every process that appeared after the first sample and was not
established as unrelated, has its executable and command line read again on
every sample for as long as it lives, and a change is reported as
``process.update``. Its identity is checked on every sample either way.

**Relevant means descended from the harness.** A process is a row actor when it
descends from the harness (``--root-pid``), whether it was already running at
the first sample, as a staging leftover would be, or appeared later.

**Unrelated has to be established; not being able to attribute is not it.** A
process is established as unrelated to the row when

* its owning user can be read and is not the harness's user (the real uid on
  POSIX, the user name on Windows), since no actor runs as anyone else; or
* it was running at the first sample and its ancestry, read then, does not lead
  to the harness; or
* its executable, read in the same sample, is neither the row's browser
  (``--browser-exe``) nor anything under the managed browsers
  (``--browser-dir``). That is the setuid ``/bin/ps`` the product runs on
  macOS: psutil cannot read its arguments, and it cannot be a browser.

Losing a process (``NoSuchProcess``, or a pid whose create time changed) is an
exit. Any other process whose identity, parent or arguments cannot be read, or
that cannot be opened at all, is an **unresolved possible actor**: it is kept in
the census, recorded, and leaves O1 unestablished for the row. That record stays
even if the process later becomes readable or exits, since a later reading
cannot show what it did while unreadable; only a later reading that establishes
it as unrelated by its user resolves it. Every failed read is recorded in the
summary, with the executable when known, the fields, how long it lasted, how it
ended and whether it could have been a browser.

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

    @property
    def identity(self) -> tuple[int, float]:
        return (self.pid, self.start)

    def as_event_fields(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "start_identity": self.start,
            "exe": self.exe,
            "cmdline": list(self.cmdline),
            "in_row": self.in_row,
        }


def record(
    pid: int,
    ppid: int,
    start: float,
    exe: str | None,
    cmdline: Sequence[str],
    *,
    in_row: bool = False,
) -> ProcessRecord:
    cmdline = tuple(cmdline)
    return ProcessRecord(pid, ppid, start, exe, cmdline, user_data_dir(cmdline), in_row)


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


#: How a failed reading of one process was judged.
_UNRELATED = "unrelated"
_EVIDENCE = "evidence"
_POSSIBLE = "possible"


class Sampler:
    """Reads the process table, keeping what cannot be excluded as a possible actor.

    *pids*, *open_process*, *clock* and *user_of* are psutil's, the wall clock
    and ``process_user`` by default, and are replaced in tests to model a
    process table. *browser_exe* and *browser_dir* name what the row's browser
    runs. *user* is the harness's user, as *user_of* reports it.
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
    ) -> None:
        self.root_pid = root_pid
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
        self._episodes: dict[tuple[int, float | None], dict[str, Any]] = {}
        self._closed: list[dict[str, Any]] = []

    def possible_browser(self, exe: str | None) -> bool:
        return possible_browser(exe, self.browser_exe, self.browser_dir)

    def _another_user(self, process: Any) -> bool:
        owner = self._user_of(process)
        return owner is not None and self.user is not None and owner != self.user

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
        opened: dict[int, Any] = {}
        for pid in self._pids():
            known = self._known.get(pid)
            try:
                process = self._open(pid)
            except psutil.NoSuchProcess:
                continue
            except _UNREADABLE as exc:
                if known is not None and known.identity in self._unrelated:
                    sample[pid] = known
                    continue
                if known is not None:
                    sample[pid] = known
                key = known.identity if known is not None else (pid, None)
                exe = known.exe if known is not None else None
                failures[pid] = (
                    key,
                    [f"open: {type(exc).__name__}"],
                    exe,
                    False,
                    None,
                    False,
                )
                continue
            opened[pid] = process
            try:
                # The create time is what separates a recycled pid from the
                # process already known under it.
                start = process.create_time()
            except psutil.NoSuchProcess:
                continue
            except _UNREADABLE as exc:
                if known is not None and known.identity in self._unrelated:
                    sample[pid] = known
                    continue
                if self._another_user(process):
                    continue
                if known is not None:
                    sample[pid] = known
                key = known.identity if known is not None else (pid, None)
                exe = known.exe if known is not None else None
                failures[pid] = (
                    key,
                    [f"identity: {type(exc).__name__}"],
                    exe,
                    False,
                    process,
                    False,
                )
                continue
            if known is not None and known.start != start:
                known = None
            if known is not None and known.identity in self._unrelated:
                sample[pid] = known
                continue
            try:
                ppid, exe, cmdline, failed, exe_read = self._read(process, known)
            except psutil.NoSuchProcess:
                continue
            sample[pid] = record(
                pid,
                -1 if ppid is None else ppid,
                start,
                exe,
                cmdline,
                in_row=known is not None and known.in_row,
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
        verdicts = self._judge(sample, failures, first)
        if first:
            # Running before any actor, not descended from the harness, and
            # judged by a parent read now: established unrelated.
            for pid, process in sample.items():
                if process.in_row or pid == self.own_pid:
                    continue
                if pid not in failures or failures[pid][5]:
                    self._unrelated.add(process.identity)
        self._track(sample, verdicts)
        self._resolve_by_user(sample, opened)
        self._known = sample
        return sample

    def _resolve_by_user(
        self, sample: dict[int, ProcessRecord], opened: dict[int, Any]
    ) -> None:
        """Resolve a possible-actor record once a reading shows another user owns it.

        The only later reading that resolves one: a process cannot change its
        real owner, so this says what it was while it was unreadable too.
        Becoming readable, or exiting, says nothing of the kind.
        """
        records = [*self._episodes.items(), *((None, e) for e in self._closed)]
        for key, episode in records:
            if not episode["possible_browser"]:
                continue
            pid = episode["pid"]
            process = opened.get(pid)
            current = sample.get(pid)
            if process is None or current is None:
                continue
            start = episode["start_identity"]
            if start is not None and start != current.start:
                continue
            if self._another_user(process):
                episode["possible_browser"] = False
                episode["resolved_by"] = "another user"
                self._unrelated.add(current.identity)

    def _judge(
        self,
        sample: dict[int, ProcessRecord],
        failures: dict[int, tuple[Any, list[str], str | None, bool, Any, bool]],
        first: bool,
    ) -> dict[Any, tuple[str, int, str | None, list[str]]]:
        """Judge each failed reading: unrelated, evidence only, or a possible actor."""
        verdicts: dict[Any, tuple[str, int, str | None, list[str]]] = {}
        for pid, (key, failed, exe, exe_read, process, parent_read) in failures.items():
            current = sample.get(pid)
            in_row = current is not None and current.in_row
            fields = {failure.split(":", 1)[0] for failure in failed}
            if first and current is not None and not in_row and parent_read:
                # Running before any actor, and its ancestry, read now, does
                # not lead to the harness.
                verdict = _UNRELATED
            elif process is not None and self._another_user(process):
                verdict = _UNRELATED
                if current is not None:
                    self._unrelated.add(current.identity)
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
        args.root_pid, browser_exe=args.browser_exe, browser_dir=args.browser_dir
    )
    began = time.monotonic()
    observation_start: float | None = None
    last_sample: float | None = None
    max_gap = 0.0
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
            sample = sampler.sample()
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
