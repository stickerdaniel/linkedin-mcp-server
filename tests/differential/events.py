"""The differential harness's event log and its case counts.

One ``events.jsonl`` per pytest run, one record per line, every record carrying
``{t, run, experiment, row, platform, actor, kind}`` and whatever the kind adds.
Records come from three writers that share no memory: the harness itself, the
watcher process, and the synthetic origin's request log, which the harness
copies in after a row. ``t`` is wall-clock seconds for that reason, since the
watcher runs in another process and a monotonic clock is not comparable across
processes.

``counts.json`` says how many cases ran, skipped and failed per experiment, row,
platform and column. A gate that reads "no difference" from a row that never ran
is the failure it exists to expose, so a skipped case is counted, not dropped.
"""

from __future__ import annotations

import json
import platform as _platform
import shutil
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EVENTS_FILE = "events.jsonl"
COUNTS_FILE = "counts.json"

#: Where CI collects a run's evidence. Read by the conftest at import, because
#: the suite's autouse fixture deletes every ``LINKEDIN*`` variable per test.
OUT_ENV = "LINKEDIN_MCP_DIFFERENTIAL_OUT"

BASE_FIELDS = ("t", "run", "experiment", "row", "platform", "actor", "kind")

#: The plan's actors, plus ``driver`` (Patchright's Node process), ``harness``
#: (this process), and ``other`` for a process the watcher cannot attribute.
ACTORS = frozenset(
    {
        "host_stub",
        "frontend",
        "owner",
        "guardian",
        "browser",
        "installer",
        "watcher",
        "canary",
        "origin",
        "proxy",
        "cli",
        "driver",
        "harness",
        "other",
    }
)

KINDS = frozenset(
    {
        # From the watcher.
        "process.start",
        "process.exit",
        "browser.roots",
        "watcher.ready",
        "watcher.summary",
        # From the synthetic origin and its proxy.
        "browser.request",
        "proxy.decision",
        # From the harness.
        "profile.snapshot",
        "user.output",
        "owner.found",
        "owner.exit",
        "tool.result",
        "row.outcome",
    }
)

EXPERIMENTS = frozenset({"K0", "K1", "K2", "K3"})

#: ``unit`` for a model or unit control, ``integrated`` for a native row on a
#: real browser, ``manual`` for a step only a person can run. The plan counts
#: them apart so a unit pass never stands in for a missing native one.
COLUMNS = frozenset({"unit", "integrated", "manual"})

STATUSES = ("executed", "skipped", "failed")


def current_platform() -> str:
    return f"{sys.platform}-{_platform.machine().lower() or 'unknown'}"


def validate(record: Mapping[str, Any]) -> None:
    """Refuse a record the schema does not describe."""
    missing = [name for name in BASE_FIELDS if name not in record]
    if missing:
        raise ValueError(f"event lacks {missing}: {dict(record)}")
    if not isinstance(record["t"], (int, float)) or isinstance(record["t"], bool):
        raise ValueError(f"event time is not a number: {record['t']!r}")
    if record["experiment"] not in EXPERIMENTS:
        raise ValueError(f"unknown experiment {record['experiment']!r}")
    if record["actor"] not in ACTORS:
        raise ValueError(f"unknown actor {record['actor']!r}")
    if record["kind"] not in KINDS:
        raise ValueError(f"unknown event kind {record['kind']!r}")
    for name in ("run", "row", "platform"):
        if not isinstance(record[name], str) or not record[name]:
            raise ValueError(f"event field {name} is empty: {record[name]!r}")


class EventLog:
    """Appends validated records to one ``events.jsonl``."""

    def __init__(self, directory: Path, run: str, platform: str | None = None):
        self.directory = directory
        self.path = directory / EVENTS_FILE
        self.run = run
        self.platform = platform or current_platform()
        self._lock = threading.Lock()
        directory.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        *,
        experiment: str,
        row: str,
        actor: str,
        kind: str,
        t: float | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        record = {
            "t": time.time() if t is None else t,
            "run": self.run,
            "experiment": experiment,
            "row": row,
            "platform": self.platform,
            "actor": actor,
            "kind": kind,
            **fields,
        }
        self.append(record)
        return record

    def append(self, record: Mapping[str, Any]) -> None:
        validate(record)
        line = json.dumps(record, sort_keys=True, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def extend(self, records: Iterable[Mapping[str, Any]]) -> int:
        count = 0
        for record in records:
            self.append(record)
            count += 1
        return count

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every complete record in *path*. A torn last line is a writer that was
    stopped mid-write, and is dropped rather than failing the reader."""
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


@dataclass
class Counts:
    """Executed, skipped and failed cases per experiment, row, platform, column."""

    _cells: Counter[tuple[str, str, str, str, str]] = field(default_factory=Counter)

    def record(
        self,
        *,
        experiment: str,
        row: str,
        column: str,
        status: str,
        platform: str | None = None,
    ) -> None:
        if experiment not in EXPERIMENTS:
            raise ValueError(f"unknown experiment {experiment!r}")
        if column not in COLUMNS:
            raise ValueError(f"unknown column {column!r}")
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        key = (experiment, row, platform or current_platform(), column, status)
        self._cells[key] += 1

    def __bool__(self) -> bool:
        return bool(self._cells)

    def as_rows(self) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str, str], dict[str, int]] = {}
        for (experiment, row, platform, column, status), n in self._cells.items():
            cell = grouped.setdefault(
                (experiment, row, platform, column),
                {status_name: 0 for status_name in STATUSES},
            )
            cell[status] += n
        return [
            {
                "experiment": experiment,
                "row": row,
                "platform": platform,
                "column": column,
                **cell,
            }
            for (experiment, row, platform, column), cell in sorted(grouped.items())
        ]

    def write(self, directory: Path) -> Path:
        path = directory / COUNTS_FILE
        path.write_text(
            json.dumps({"cases": self.as_rows()}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path


def publish(directory: Path, out: str | None, run: str) -> Path | None:
    """Copy a run's evidence to *out*, for CI to collect. None when unset."""
    if not out:
        return None
    target = Path(out) / run
    shutil.copytree(directory, target, dirs_exist_ok=True)
    return target
