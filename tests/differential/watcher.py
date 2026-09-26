"""A process watcher that runs outside every actor it observes.

Started by the harness as its own process: its own session on POSIX, its own
process group on Windows, and never a child of the server, the owner or the
browser. What it reports therefore does not depend on anything those actors
say about themselves. It samples the whole process table every 50 ms and
writes ``process.start`` and ``process.exit`` for every process that appeared
after its first sample, keyed by pid *and* create time, so a recycled pid reads
as one exit and one start rather than as the same process.

It also derives O1 on every sample: how many browser tree roots each
``--user-data-dir`` has. A browser is found by that flag in its command line,
which Patchright passes to every persistent-context launch; a Chromium child
carries ``--type=`` and is part of its parent's tree, never a root of its own.
Two roots with the same profile in one sample is the second concurrent browser
the default-on contract forbids.

What sampling cannot see: a process that lives and dies between two samples.
The summary records the slowest sample, so a claim built on it can state the
window it actually had.

Imports nothing from the repository, so it runs as a plain script:
``python watcher.py --out FILE --stop FILE ...``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

USER_DATA_DIR_FLAG = "--user-data-dir="
CHILD_TYPE_FLAG = "--type="

#: Target interval between samples.
SAMPLE_SECONDS = 0.05

#: How long a new process's command line keeps being re-read. A forked child
#: shows its parent's command line until it execs, which is exactly how the
#: Node driver starts Chromium on POSIX, so the first reading can be a stale one.
FRESH_SECONDS = 2.0


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

    def as_event_fields(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "start_identity": self.start,
            "exe": self.exe,
            "cmdline": list(self.cmdline),
        }


def record(
    pid: int, ppid: int, start: float, exe: str | None, cmdline: Sequence[str]
) -> ProcessRecord:
    cmdline = tuple(cmdline)
    return ProcessRecord(pid, ppid, start, exe, cmdline, user_data_dir(cmdline))


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


@dataclass
class _Seen:
    record: ProcessRecord
    first_seen: float


def _read(process: psutil.Process) -> tuple[int, str | None, tuple[str, ...]]:
    with process.oneshot():
        ppid = process.ppid()
        try:
            exe: str | None = process.exe()
        except (psutil.AccessDenied, OSError):
            exe = None
        try:
            cmdline = tuple(process.cmdline())
        except (psutil.AccessDenied, OSError):
            cmdline = ()
    return ppid, exe, cmdline


def sample_processes(seen: dict[int, _Seen]) -> dict[int, ProcessRecord]:
    """Read the process table once, reusing what is already known and settled."""
    now = time.monotonic()
    sample: dict[int, ProcessRecord] = {}
    alive: set[int] = set()
    for pid in psutil.pids():
        try:
            # The constructor reads the create time, which is what separates a
            # recycled pid from the process the cache already holds.
            process = psutil.Process(pid)
            start = process.create_time()
            cached = seen.get(pid)
            if cached is not None and cached.record.start == start:
                if now - cached.first_seen < FRESH_SECONDS:
                    ppid, exe, cmdline = _read(process)
                    cached.record = record(pid, ppid, start, exe, cmdline)
            else:
                ppid, exe, cmdline = _read(process)
                cached = _Seen(record(pid, ppid, start, exe, cmdline), now)
                seen[pid] = cached
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
        alive.add(pid)
        sample[pid] = cached.record
    for pid in list(seen):
        if pid not in alive:
            del seen[pid]
    return sample


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--stop", required=True, type=Path)
    parser.add_argument("--run", required=True)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--row", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--interval", type=float, default=SAMPLE_SECONDS)
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
    seen: dict[int, _Seen] = {}
    began = time.monotonic()
    slowest = 0.0
    late = 0
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

        while time.monotonic() - began < args.deadline:
            if args.stop.exists():
                stopped_by = "stop file"
                break
            tick = time.monotonic()
            sample = sample_processes(seen)
            now = time.time()
            for actor, kind, fields in tracker.observe(sample, now):
                write(actor, kind, fields, now)
            if tracker.samples == 1:
                # The baseline is taken. The harness waits for this line before
                # it starts an actor, so no actor is mistaken for background.
                write(
                    "watcher",
                    "watcher.ready",
                    {"pid": os.getpid(), "baseline_processes": len(sample)},
                    now,
                )
            out.flush()
            elapsed = time.monotonic() - tick
            slowest = max(slowest, elapsed)
            if elapsed > 2 * args.interval:
                late += 1
            time.sleep(max(0.0, args.interval - elapsed))

        write(
            "watcher",
            "watcher.summary",
            {
                "pid": os.getpid(),
                "samples": tracker.samples,
                "interval_seconds": args.interval,
                "slowest_sample_seconds": round(slowest, 4),
                "late_samples": late,
                "max_roots": tracker.max_roots,
                "violations": tracker.violations,
                "stopped_by": stopped_by,
            },
            time.time(),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
