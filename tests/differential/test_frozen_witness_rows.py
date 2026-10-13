"""K2's source-model witnesses at the frozen pin (``frozen_witnesses``).

One case per witness, counted K2 in column ``unit``: executed when the pin's
own strict-xfail test failed on its intended assertion, failed otherwise. The
pin's tests run once for the module, from its checkout and its locked dev
venv, and the judgement lands in the packet as ``witnesses.json``.

No browser and no trust store: it opts in with the native rows only because it
syncs the pin's dev dependencies, which a unit run should not pay for.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from differential.accounting import current
from differential.baseline import (
    BASELINE_DIR_ENV,
    REPO_ROOT,
    prepare_frozen_tests,
    remove_baseline,
    verify_checkout,
)
from differential.frozen_witnesses import (
    REPRODUCED,
    WITNESSES,
    judge,
    run_witnesses,
    write_witnesses,
)
from differential.synthetic_origin import OPT_IN_ENV

pytestmark = pytest.mark.skipif(
    os.environ.get(OPT_IN_ENV) != "1",
    reason=(
        f"source-model witnesses at the frozen pin: they sync the pin's locked "
        f"dev dependencies, so they run where the evidence workflow sets "
        f"{OPT_IN_ENV}=1"
    ),
)


@pytest.fixture(scope="module")
def verdicts(request, tmp_path_factory) -> Iterator[dict[str, Any]]:
    configured = os.environ.get(BASELINE_DIR_ENV)
    directory = Path(configured) if configured else tmp_path_factory.mktemp("baseline")
    # Pointed at the candidate on purpose: the sync must still bind to the
    # pin's lock, and `prepare_frozen_tests` refuses a venv that does not
    # match it, so every run of these rows shows the override was ignored.
    with pytest.MonkeyPatch.context() as hostile:
        hostile.setenv("UV_PROJECT", str(REPO_ROOT))
        runtime = prepare_frozen_tests(directory)
    packet = current(request.config).directory
    recorded = run_witnesses(runtime, packet / "witness-run" / "report.json")
    # The run must not have moved or dirtied the pin it is named after.
    verify_checkout(runtime.checkout, runtime.pinned or "")
    judged = judge(recorded, runtime.checkout)
    write_witnesses(packet, runtime, recorded, judged)
    yield judged
    if not configured:
        remove_baseline(directory)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(
            name,
            marks=pytest.mark.differential_row(
                row=name, experiment="K2", column="unit"
            ),
        )
        for name in WITNESSES
    ],
)
def test_the_pin_reproduces_its_witness(verdicts: dict[str, Any], name: str):
    verdict = verdicts[name]
    print(f"{name} at the pin: {verdict['verdict']}")
    assert verdict["verdict"] == REPRODUCED, verdict["nodes"]
