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

**Nothing new is ever settled by age.** A process can exec at any moment of its
life, and a forked child shows its parent's command line until it does, which
is how the Node driver starts Chromium on POSIX. So every process that appeared
after the first sample has its executable and command line read again on every
sample for as long as it lives, and a change is reported as ``process.update``.
Only the processes already running at the first sample are read once: they
existed before any actor, nothing the row starts is among them, and none of
them was ever told the row's temporary profile, so none can become a browser on
it. Their identity is still checked on every sample.

**Relevant means descended from the harness.** A process is a row actor when its
parent, at first sight, is the harness (``--root-pid``) or another actor. A
metadata read that fails for an actor is recorded in the summary, since a
command line nobody could read is not a command line without the flag. The same
failure for an unrelated process, say a protected system service, is not.

What sampling cannot see: a process that lives and dies between two samples.
The summary records when observation began and ended and the largest wall-clock
gap between two samples, so a claim built on it can state the window it had.

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


class Sampler:
    """Reads the process table, re-reading every process that could still change.

    *pids* and *open_process* are psutil's by default and are replaced in tests
    to model a process table.
    """

    def __init__(
        self,
        root_pid: int,
        *,
        own_pid: int | None = None,
        pids: Callable[[], Iterable[int]] = psutil.pids,
        open_process: Callable[[int], Any] = psutil.Process,
    ) -> None:
        self.root_pid = root_pid
        self.own_pid = os.getpid() if own_pid is None else own_pid
        self._pids = pids
        self._open = open_process
        self._known: dict[int, ProcessRecord] = {}
        self._baseline: set[tuple[int, float]] | None = None
        self._row: set[tuple[int, float]] = set()
        self._failed: set[tuple[int, float, str]] = set()
        #: Failed metadata reads of row actors, once per process and field.
        self.relevant_read_failures: list[dict[str, Any]] = []

    def _read(
        self, process: Any, known: ProcessRecord | None
    ) -> tuple[int, str | None, tuple[str, ...], list[str]]:
        failures: list[str] = []
        ppid = known.ppid if known is not None else -1
        exe = known.exe if known is not None else None
        cmdline = known.cmdline if known is not None else ()
        try:
            ppid = process.ppid()
        except _UNREADABLE as exc:
            failures.append(f"ppid: {type(exc).__name__}")
        try:
            exe = process.exe()
        except _UNREADABLE as exc:
            failures.append(f"exe: {type(exc).__name__}")
        try:
            cmdline = tuple(process.cmdline())
        except _UNREADABLE as exc:
            failures.append(f"cmdline: {type(exc).__name__}")
        return ppid, exe, cmdline, failures

    def sample(self) -> dict[int, ProcessRecord]:
        first = self._baseline is None
        sample: dict[int, ProcessRecord] = {}
        failures: dict[int, list[str]] = {}
        for pid in self._pids():
            try:
                # Reads the create time, which is what separates a recycled pid
                # from the process already known under it.
                process = self._open(pid)
                start = process.create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
            known = self._known.get(pid)
            if known is not None and known.start != start:
                known = None
            if (
                known is not None
                and self._baseline is not None
                and known.identity in self._baseline
            ):
                sample[pid] = known
                continue
            try:
                ppid, exe, cmdline, failed = self._read(process, known)
            except psutil.NoSuchProcess:
                continue
            sample[pid] = record(
                pid,
                ppid,
                start,
                exe,
                cmdline,
                in_row=known is not None and known.in_row,
            )
            if failed:
                failures[pid] = failed
        if first:
            self._baseline = {process.identity for process in sample.values()}
            root = sample.get(self.root_pid)
            if root is not None:
                self._row.add(root.identity)
        self._classify(sample)
        for pid, failed in failures.items():
            process = sample[pid]
            if not process.in_row:
                continue
            for field in failed:
                key = (pid, process.start, field)
                if key in self._failed:
                    continue
                self._failed.add(key)
                self.relevant_read_failures.append(
                    {"pid": pid, "start_identity": process.start, "failure": field}
                )
        self._known = sample
        return sample

    def _classify(self, sample: dict[int, ProcessRecord]) -> None:
        """Mark row actors: the harness, and whatever descends from it."""
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
                if self._baseline is not None and process.identity in self._baseline:
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
    args = parser.parse_args(argv)

    base = {
        "run": args.run,
        "experiment": args.experiment,
        "row": args.row,
        "platform": args.platform,
    }
    tracker = Tracker()
    sampler = Sampler(args.root_pid)
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
