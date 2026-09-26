"""Case accounting for the differential harness, from pytest's own reports.

A plugin rather than a fixture, because a fixture is only set up for a case
that gets that far: a run where every native row skips before setup never
instantiates one, and its counts would never be written. Here the counts are
built from the report hooks and written at session finish, whatever ran.

**A case's verdict is all of its phases.** Pytest reports setup, call and
teardown separately, and a finalizer that fails runs after the call already
passed. So a case is ``failed`` if any phase failed, ``skipped`` if setup or
call skipped, and ``executed`` only when its call passed and nothing failed
around it. The verdict is taken at session finish, after the last teardown.

**Under xdist the controller counts.** Workers attach the case's marker to each
report as a user property, which xdist forwards to the controller with the
report; the controller aggregates and writes one ``counts.json``. Workers write
no counts of their own. The native rows refuse to run under xdist at all, so
their event log and counts always come from one process.

The evidence directory is created lazily, under pytest's base temporary
directory, and copied to ``LINKEDIN_MCP_DIFFERENTIAL_OUT`` at the end when that
is set (``events.publish`` decides which files go).
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from differential.events import OUT_ENV, Counts, publish

PROPERTY = "differential_row"

_PHASE_ORDER = ("setup", "call", "teardown")


@dataclass
class _Case:
    row: str
    experiment: str
    column: str
    phases: dict[str, str] = field(default_factory=dict)

    def status(self) -> str:
        outcomes = self.phases
        if "failed" in outcomes.values():
            return "failed"
        if outcomes.get("setup") == "skipped" or outcomes.get("call") == "skipped":
            return "skipped"
        if outcomes.get("call") == "passed":
            return "executed"
        # Started and never finished its call: not a pass.
        return "failed"


class Accounting:
    """One run's evidence directory and the verdicts of its marked cases."""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.worker = getattr(config, "workerinput", None)
        run = self.worker.get("testrunuid") if self.worker else None
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self.run = str(run) if run else f"{stamp}-{uuid.uuid4().hex[:8]}"
        # Read now, before the suite's autouse fixture deletes every LINKEDIN*
        # variable for the length of each test.
        self.out = os.environ.get(OUT_ENV)
        self.cases: dict[str, _Case] = {}
        self._directory: Path | None = None

    @property
    def is_worker(self) -> bool:
        return self.worker is not None

    @property
    def directory(self) -> Path:
        if self._directory is None:
            factory = getattr(self.config, "_tmp_path_factory", None)
            if factory is not None:
                self._directory = factory.mktemp("differential")
            else:
                self._directory = Path(tempfile.mkdtemp(prefix="differential-"))
        return self._directory

    def note(self, nodeid: str, marker: dict[str, Any], when: str, outcome: str):
        case = self.cases.setdefault(
            nodeid,
            _Case(
                row=marker["row"],
                experiment=marker["experiment"],
                column=marker.get("column", "integrated"),
            ),
        )
        case.phases[when] = outcome

    def finish(self) -> None:
        if self.is_worker:
            # Its reports are counted by the controller; its own event log, if a
            # row wrote one, is kept apart under the worker's name.
            if self._directory is not None and self.worker is not None:
                worker = str(self.worker.get("workerid", "worker"))
                publish(self._directory, self.out, f"{self.run}/{worker}")
            return
        if self.cases:
            counts = Counts()
            for case in self.cases.values():
                counts.record(
                    experiment=case.experiment,
                    row=case.row,
                    column=case.column,
                    status=case.status(),
                )
            counts.write(self.directory)
            print(f"\ndifferential evidence for run {self.run}: {self.directory}")
        if self._directory is not None:
            publish(self._directory, self.out, self.run)


_ACTIVE: list[Accounting] = []


def current(config: pytest.Config) -> Accounting:
    for accounting in reversed(_ACTIVE):
        if accounting.config is config:
            return accounting
    raise RuntimeError("the differential accounting plugin is not active")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "differential_row(row, experiment, column): a differential harness "
        "case, counted per row, experiment and platform in counts.json",
    )
    _ACTIVE.append(Accounting(config))


def pytest_unconfigure(config: pytest.Config) -> None:
    _ACTIVE[:] = [a for a in _ACTIVE if a.config is not config]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]):
    outcome = yield
    marker = item.get_closest_marker("differential_row")
    if marker is None:
        return
    report = outcome.get_result()
    if not any(name == PROPERTY for name, _ in report.user_properties):
        report.user_properties.append((PROPERTY, dict(marker.kwargs)))


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if not _ACTIVE:
        return
    accounting = _ACTIVE[-1]
    if accounting.is_worker:
        return
    marker = dict(report.user_properties).get(PROPERTY)
    if not isinstance(marker, dict) or report.when not in _PHASE_ORDER:
        return
    accounting.note(report.nodeid, marker, report.when, report.outcome)


def pytest_sessionfinish(session: pytest.Session) -> None:
    for accounting in reversed(_ACTIVE):
        if accounting.config is session.config:
            accounting.finish()
            return
