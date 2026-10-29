"""A witness counts as reproduced only when its intended assertion failed.

Each case writes a small checkout shaped like the pin's: a strict-xfail test at
``frozen_witnesses.WITNESS_FILE`` and a ``linkedin_mcp_server`` package of its
own, runs it through ``run_witnesses`` with this interpreter's pytest, and
judges what the probe recorded. Variants plant what the judgement must not be
fooled by: a body that passes (strict XPASS), an ``AssertionError`` raised
somewhere else, which pytest still reports as the xfail, a node that never
ran, and a run that imported another checkout's package.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from differential.baseline import Runtime
from differential.frozen_witnesses import (
    NOT_REPRODUCED,
    REPRODUCED,
    WITNESS_FILE,
    Witness,
    judge,
    run_witnesses,
)

_SOURCE = """\
import pytest

import linkedin_mcp_server


def _forward():
    {forward}


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="W-PLANTED")
@pytest.mark.parametrize("preflight", ["raises", "404"])
def test_no_unmarked_dispatch(preflight):
    dispatched = {dispatched}
    _forward()
    assert dispatched == []
"""

_INTENDED = "assert dispatched == []"


def _checkout(
    root: Path,
    *,
    dispatched: str = "['tool call']",
    forward: str = "pass",
    package: bool = True,
) -> Path:
    checkout = root / "checkout"
    (checkout / "tests").mkdir(parents=True)
    (checkout / "pytest.ini").write_text("[pytest]\n")
    (checkout / WITNESS_FILE).write_text(
        _SOURCE.format(dispatched=dispatched, forward=forward)
    )
    if package:
        (checkout / "linkedin_mcp_server").mkdir()
        (checkout / "linkedin_mcp_server" / "__init__.py").write_text("")
    return checkout


def _witness(checkout: Path, *parameters: str) -> Witness:
    lines = (checkout / WITNESS_FILE).read_text().splitlines()
    base = f"{WITNESS_FILE}::test_no_unmarked_dispatch"
    return Witness(
        name="W-PLANTED",
        test="test_no_unmarked_dispatch",
        nodes=tuple(f"{base}[{p}]" for p in parameters or ("raises", "404")),
        statement=_INTENDED,
        line=next(i for i, line in enumerate(lines, 1) if line.strip() == _INTENDED),
    )


def _verdict(tmp_path: Path, checkout: Path, witness: Witness) -> dict[str, Any]:
    runtime = Runtime(sys.executable, checkout, checkout / "browsers", "pin")
    recorded = run_witnesses(runtime, tmp_path / "report.json", [witness])
    return judge(recorded, checkout, [witness])[witness.name]


def test_the_intended_assertion_failing_is_a_reproduction(tmp_path):
    checkout = _checkout(tmp_path)
    verdict = _verdict(tmp_path, checkout, _witness(checkout))
    assert verdict["verdict"] == REPRODUCED, verdict["nodes"]
    assert verdict["evidence"] == "source-model"


def test_a_body_that_passes_is_not_a_reproduction(tmp_path):
    checkout = _checkout(tmp_path, dispatched="[]")
    verdict = _verdict(tmp_path, checkout, _witness(checkout))
    assert [n["verdict"] for n in verdict["nodes"]] == [NOT_REPRODUCED] * 2


def test_an_assertion_error_raised_elsewhere_is_not_a_reproduction(tmp_path):
    # Pytest reports this as the declared xfail, and exits 0.
    checkout = _checkout(tmp_path, forward="assert False, 'not the witness'")
    verdict = _verdict(tmp_path, checkout, _witness(checkout))
    assert [n["verdict"] for n in verdict["nodes"]] == [NOT_REPRODUCED] * 2


def test_a_witness_with_a_node_that_never_ran_is_not_reproduced(tmp_path):
    # Pytest refuses the whole selection when one node does not exist.
    checkout = _checkout(tmp_path)
    verdict = _verdict(tmp_path, checkout, _witness(checkout, "raises", "missing"))
    assert [n["verdict"] for n in verdict["nodes"]] == [NOT_REPRODUCED] * 2


def test_a_run_of_another_checkouts_package_is_not_a_reproduction(tmp_path):
    # Without a package of its own, the import finds the one this venv has.
    checkout = _checkout(tmp_path, package=False)
    verdict = _verdict(tmp_path, checkout, _witness(checkout))
    assert verdict["verdict"] == NOT_REPRODUCED
