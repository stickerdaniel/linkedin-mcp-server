"""Row H-R1 in K1, K3 and K0: one host starts, reads once, and quits.

K1 here is the **same-revision Direct reference**: this checkout's server with
``DAEMON_ENABLED=false``. It is not the plan's frozen-baseline K1, which, with
K2, is a later stage. K3 runs the same checkout through the shared owner
(``true``), and K0 runs K3 a second time. K0 passes only when the repeat meets
every expectation of its own row *and* reads exactly as K3 did, mode and health
included, which is what says the harness measures the product rather than its
own noise. K3 is then held to the Direct reference on O1 and O4.

The cases share one module-level record of vectors and run in file order, so
the comparisons read what the earlier cases measured in this same process.
Only a result with no failures is recorded, and a comparison whose input is
missing fails rather than passing on nothing.

Each case gets a fresh temporary auth root from the suite's own
``isolate_profile_dir`` and a fresh synthetic origin, and checks the hosts-file
fence from outside any browser before it stages anything. The feasibility row
in ``test_synthetic_origin.py`` runs first in CI and proves the unproxied
browser honours that fence; this row relies on it and re-checks only the
system's answer.

Runs only where CI opted in after trusting the CA, and never under xdist: the
evidence packet of a run is one process's. See ``synthetic_origin``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from differential.events import EventLog
from differential.harness import (
    ROW_H_R1,
    RowResult,
    RowVector,
    compare_to_direct,
    default_browsers_path,
    measure_host_quit_row,
    repeat_verdict,
)
from differential.synthetic_origin import (
    OPT_IN_ENV,
    EgressProxy,
    SyntheticOrigin,
    fence_breaches,
)
from linkedin_mcp_server.config import reset_config

pytestmark = [
    pytest.mark.differential_browser,
    pytest.mark.xdist_group("browser_runtime"),
    pytest.mark.skipif(
        os.environ.get(OPT_IN_ENV) != "1",
        reason=(
            f"native differential row: needs a per-run test CA that only a "
            f"disposable CI runner trusts, so it runs only where the CI step "
            f"sets {OPT_IN_ENV}=1 after installing that CA. Do not set it "
            f"locally."
        ),
    ),
]

#: Valid results measured so far in this process, by experiment.
_VECTORS: dict[str, RowVector] = {}


async def _run(
    experiment: str,
    *,
    daemon: bool,
    profile: Path,
    egress: tuple[SyntheticOrigin, EgressProxy],
    log: EventLog,
    monkeypatch: pytest.MonkeyPatch,
) -> RowResult:
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.fail("the native rows run without xdist; one process owns a packet")
    breaches = fence_breaches()
    if breaches:
        pytest.fail(
            f"the hosts file does not send these names to loopback only: "
            f"{breaches}; stopping before a browser starts"
        )
    _, proxy = egress
    # The harness process stages the session through the product's import
    # path, which reads its configuration the way the server does.
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(default_browsers_path()))
    monkeypatch.setenv("PROXY_SERVER", proxy.url)
    reset_config()
    result = await measure_host_quit_row(
        profile=profile,
        experiment=experiment,
        daemon=daemon,
        egress=egress,
        log=log,
        work_dir=log.directory / "rows" / experiment,
    )
    print(f"H-R1 {result.label}: {result.vector}")
    return result


@pytest.mark.differential_row(row=ROW_H_R1, experiment="K1", column="integrated")
async def test_direct_reference_host_starts_reads_and_quits(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    result = await _run(
        "K1",
        daemon=False,
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    assert not result.failures, result.report()
    assert result.vector is not None
    _VECTORS["K1"] = result.vector


@pytest.mark.differential_row(row=ROW_H_R1, experiment="K3", column="integrated")
async def test_daemon_host_starts_reads_and_quits(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    result = await _run(
        "K3",
        daemon=True,
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    assert not result.failures, result.report()
    assert result.vector is not None
    _VECTORS["K3"] = result.vector


@pytest.mark.differential_row(row=ROW_H_R1, experiment="K0", column="integrated")
async def test_the_daemon_row_repeats_identically(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    reference = _VECTORS.get("K3")
    if reference is None:
        pytest.fail("K3 produced no valid result in this run, so nothing to repeat")
    result = await _run(
        "K0",
        daemon=True,
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    problems = repeat_verdict(reference, result)
    assert not problems, f"K0: {problems}\n{result.report()}"


def test_the_daemon_is_no_worse_than_the_direct_reference_on_this_row():
    direct, daemon = _VECTORS.get("K1"), _VECTORS.get("K3")
    if direct is None or daemon is None:
        pytest.fail(
            f"K1 and K3 must both have passed in this process first; have "
            f"{sorted(_VECTORS)}"
        )
    differences = compare_to_direct(direct, daemon)
    assert not differences, f"K3 differs from the Direct reference: {differences}"
