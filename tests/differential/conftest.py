"""Opt-in and fixtures shared by the differential harness."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from differential.events import OUT_ENV, Counts, EventLog, publish
from differential.synthetic_origin import (
    ALLOWED_HOSTS,
    CA_DIR_ENV,
    CA_FILE,
    LEAF_FILE,
    LEAF_KEY_FILE,
    OPT_IN_ENV,
    EgressProxy,
    SyntheticOrigin,
)

#: Read at import, because ``ignore_the_developers_environment`` deletes every
#: ``LINKEDIN*`` variable before each test runs.
_CA_DIR = os.environ.get(CA_DIR_ENV)
_OUT = os.environ.get(OUT_ENV)

#: Filled by the report hook below from every case marked ``differential_row``,
#: skipped ones included.
_COUNTS = Counts()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Count a marked case as executed, skipped or failed, once.

    From pytest's own verdict rather than from the test body, because a case
    skipped at setup never reaches its body, and a count that leaves skips out
    reads the same as a platform where the row was never collected.
    """
    outcome = yield
    marker = item.get_closest_marker("differential_row")
    if marker is None:
        return
    report = outcome.get_result()
    status = None
    if report.skipped and call.when in ("setup", "call"):
        status = "skipped"
    elif report.failed:
        status = "failed"
    elif call.when == "call" and report.passed:
        status = "executed"
    if status is None or getattr(item, "_differential_counted", False):
        return
    item._differential_counted = True
    _COUNTS.record(
        experiment=marker.kwargs["experiment"],
        row=marker.kwargs["row"],
        column=marker.kwargs.get("column", "integrated"),
        status=status,
    )


@pytest.fixture(scope="session")
def differential_run(tmp_path_factory) -> Iterator[EventLog]:
    """This run's event log. At the end, ``counts.json`` beside it, and a copy
    of both under ``LINKEDIN_MCP_DIFFERENTIAL_OUT`` when that is set."""
    run = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    directory = tmp_path_factory.mktemp("differential")
    log = EventLog(directory, run)
    yield log
    if _COUNTS:
        _COUNTS.write(directory)
        print(f"\ndifferential evidence for run {run}: {directory}")
    publish(directory, _OUT, run)


@pytest.fixture(autouse=True, scope="session")
def _differential_evidence(differential_run: EventLog) -> EventLog:
    """Instantiated by any test in this directory, so counts are written even
    for a run where every native row skipped."""
    return differential_run


@pytest.fixture
def certificates() -> Path:
    """The run's issued certificates. A missing one fails, since opting in
    without them is a broken CI step rather than a reason to skip."""
    raw = _CA_DIR
    if not raw:
        pytest.fail(f"{OPT_IN_ENV} is set but {CA_DIR_ENV} is not")
    directory = Path(raw)
    missing = [
        name
        for name in (CA_FILE, LEAF_FILE, LEAF_KEY_FILE)
        if not (directory / name).is_file()
    ]
    if missing:
        pytest.fail(f"{CA_DIR_ENV}={directory} lacks {', '.join(missing)}")
    return directory


@pytest.fixture
def synthetic_egress(
    certificates: Path,
) -> Iterator[tuple[SyntheticOrigin, EgressProxy]]:
    """The synthetic origin, and the fail-closed proxy that is the only way to it."""
    origin = SyntheticOrigin(certificates)
    origin.start()
    try:
        proxy = EgressProxy({host: origin.port for host in ALLOWED_HOSTS})
        proxy.start()
        try:
            yield origin, proxy
        finally:
            proxy.stop()
    finally:
        origin.stop()
