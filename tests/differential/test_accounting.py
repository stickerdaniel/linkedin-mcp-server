"""counts.json is written from pytest's own reports, whatever the cases did.

Each case runs a separate pytest process over a small generated suite with the
accounting plugin loaded, and reads the counts it published. A separate process
because the plugin is session-scoped state: an in-process run would share it
with the suite running this test. Under xdist the counts come from the
controller alone, from the reports the workers forward.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from differential.events import COUNTS_FILE, OUT_ENV

pytest_plugins = ["pytester"]

TESTS_DIR = Path(__file__).resolve().parents[1]

_CASES = """
import pytest


@pytest.fixture
def broken_setup():
    raise RuntimeError("setup failed")


@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown failed")


@pytest.mark.differential_row(row="R", experiment="K1", column="integrated")
def test_passes():
    pass


@pytest.mark.differential_row(row="R", experiment="K3", column="integrated")
def test_setup_fails(broken_setup):
    pass


@pytest.mark.differential_row(row="R", experiment="K0", column="integrated")
def test_passes_then_teardown_fails(broken_teardown):
    pass


def test_unmarked():
    pass


@pytest.mark.differential_row(row="R", experiment="K2", column="integrated")
def test_last_case_teardown_fails(broken_teardown):
    pass
"""

_SKIPPED = """
import pytest

pytestmark = pytest.mark.skipif(True, reason="not opted in")


@pytest.mark.differential_row(row="R", experiment="K1", column="integrated")
def test_one():
    pass


@pytest.mark.differential_row(row="R", experiment="K3", column="integrated")
def test_two():
    pass
"""


def _counts(pytester: pytest.Pytester, monkeypatch, source: str, *extra: str) -> dict:
    out = pytester.path / "evidence"
    monkeypatch.setenv(OUT_ENV, str(out))
    monkeypatch.setenv("PYTHONPATH", str(TESTS_DIR))
    pytester.makeini("[pytest]\n")
    pytester.makepyfile(test_cases=source)
    pytester.runpytest_subprocess(
        "-p", "differential.accounting", "-p", "no:cacheprovider", *extra
    )
    written = sorted(out.glob(f"*/{COUNTS_FILE}"))
    assert len(written) == 1, sorted(p.name for p in out.glob("**/*"))
    cells = json.loads(written[0].read_text())["cases"]
    return {
        cell["experiment"]: (cell["executed"], cell["skipped"], cell["failed"])
        for cell in cells
    }


def test_every_phase_decides_a_cases_verdict(pytester, monkeypatch):
    assert _counts(pytester, monkeypatch, _CASES) == {
        "K1": (1, 0, 0),
        "K3": (0, 0, 1),
        "K0": (0, 0, 1),
        "K2": (0, 0, 1),
    }


def test_an_all_skipped_selection_still_writes_counts(pytester, monkeypatch):
    assert _counts(pytester, monkeypatch, _SKIPPED) == {
        "K1": (0, 1, 0),
        "K3": (0, 1, 0),
    }


def test_under_xdist_the_controller_counts_every_workers_cases(pytester, monkeypatch):
    assert _counts(pytester, monkeypatch, _CASES, "-n", "2") == {
        "K1": (1, 0, 0),
        "K3": (0, 0, 1),
        "K0": (0, 0, 1),
        "K2": (0, 0, 1),
    }
