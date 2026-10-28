"""A pytest plugin that records how each selected test ended, for a judge
elsewhere (``frozen_witnesses``).

It runs inside the frozen pin's own pytest, from that pin's interpreter, so it
imports nothing but the standard library and pytest. Loaded with
``-p witness_probe --witness-report=PATH`` from a directory on ``PYTHONPATH``,
it writes one JSON file at session end: every phase of every test, and for a
phase that raised, the exception's type and the innermost frame it was raised
from, by file, line, function and source statement. What that statement
means is not decided here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_PHASES: list[dict[str, Any]] = []


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--witness-report", required=True, help="where to write")


def _raised(call: pytest.CallInfo[Any], rootdir: Path) -> dict[str, Any] | None:
    excinfo = call.excinfo
    if excinfo is None:
        return None
    entry = excinfo.traceback[-1]
    path = Path(entry.path)
    try:
        shown = path.resolve().relative_to(rootdir.resolve()).as_posix()
    except ValueError:
        shown = str(path)
    return {
        "type": f"{excinfo.type.__module__}.{excinfo.type.__qualname__}",
        "message": str(excinfo.value)[:500],
        "path": shown,
        # One-based, as an editor and a traceback print it.
        "line": entry.lineno + 1,
        "function": entry.name,
        "statement": str(entry.statement).strip(),
    }


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]):
    outcome = yield
    report = outcome.get_result()
    _PHASES.append(
        {
            "nodeid": item.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "wasxfail": getattr(report, "wasxfail", None),
            "raised": _raised(call, item.config.rootpath),
        }
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    package = sys.modules.get("linkedin_mcp_server")
    record = {
        "pytest": pytest.__version__,
        "python": sys.executable,
        "rootdir": str(session.config.rootpath),
        # Which checkout's code the tests imported.
        "package_file": getattr(package, "__file__", None),
        "exitstatus": int(exitstatus),
        "phases": _PHASES,
    }
    Path(session.config.getoption("--witness-report")).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
