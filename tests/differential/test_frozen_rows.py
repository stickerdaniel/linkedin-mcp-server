"""The frozen baseline's columns: K1 frozen on H-R1, and H-R12 in K1, K2 and K3.

**K1 frozen** is the plan's reference column: the server at the pinned
baseline (``baseline.BASELINE_SHA``), run Direct from its own venv, staged by
its own code and driving its own browser. It is labelled with its short SHA and
kept apart from the same-revision Direct reference in ``test_host_quit_row``.
K3, this checkout through the shared owner, is run here again and held to it
on O1 and O4.

**H-R12**, a custom browser: ``CHROME_PATH`` names the runtime's own bundled
Chromium, so the binary is the one the row would have run anyway and only the
setting differs, and the daemon is enabled. The candidate (K3) must show no
coordination effect: no owner, no forwarding, no daemon state, and the O1/O4
of the frozen Direct run with the same setting. The baseline (K2) elects an
owner regardless (W-CHROME-PATH), so K2 must read ``!``; reading ``=`` there
is the harness missing a known difference and fails as a harness defect.

Native, like ``test_host_quit_row``: only where CI opted in after trusting the
CA, never under xdist, in file order, with only passing results recorded for
the comparisons.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from differential.baseline import (
    BASELINE_DIR_ENV,
    Runtime,
    prepare_baseline,
    remove_baseline,
)
from differential.events import EventLog
from differential.harness import (
    ROW_H_R1,
    ROW_H_R12,
    RowResult,
    RowVector,
    compare_to_direct,
    coordination_reading,
    default_browsers_path,
    k2_r12_verdict,
    measure_host_quit_row,
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

#: Valid results measured so far in this process, by row and column.
_VECTORS: dict[str, RowVector] = {}


@pytest.fixture(scope="module")
def baseline_runtime(tmp_path_factory) -> Iterator[Runtime]:
    configured = os.environ.get(BASELINE_DIR_ENV)
    directory = Path(configured) if configured else tmp_path_factory.mktemp("baseline")
    yield prepare_baseline(directory)
    if not configured:
        remove_baseline(directory)


async def _run(
    key: str,
    experiment: str,
    *,
    row: str,
    daemon: bool,
    profile: Path,
    egress: tuple[SyntheticOrigin, EgressProxy],
    log: EventLog,
    monkeypatch: pytest.MonkeyPatch,
    runtime: Runtime | None = None,
    custom_browser: bool = False,
    expect_owner: bool | None = None,
    reference: str | None = None,
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
    # A candidate row stages in this process through the product's import
    # path; a frozen one stages in the baseline's interpreter instead.
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(default_browsers_path()))
    monkeypatch.setenv("PROXY_SERVER", proxy.url)
    reset_config()
    result = await measure_host_quit_row(
        profile=profile,
        experiment=experiment,
        daemon=daemon,
        egress=egress,
        log=log,
        work_dir=log.directory / "rows" / f"{row}-{key}",
        runtime=runtime,
        row=row,
        custom_browser=custom_browser,
        expect_owner=expect_owner,
        reference=reference,
    )
    print(
        f"{row} {result.label}: {result.vector} "
        f"coordination={coordination_reading(result.vector) if result.vector else None}"
    )
    return result


def _recorded(key: str, result: RowResult) -> None:
    assert not result.failures, result.report()
    assert result.vector is not None
    _VECTORS[key] = result.vector


def _compare(reference_key: str, candidate_key: str) -> None:
    reference, candidate = _VECTORS.get(reference_key), _VECTORS.get(candidate_key)
    if reference is None or candidate is None:
        pytest.fail(
            f"{reference_key} and {candidate_key} must both have passed in this "
            f"process first; have {sorted(_VECTORS)}"
        )
    differences = compare_to_direct(reference, candidate)
    assert not differences, (
        f"{candidate_key} differs from {reference_key}: {differences}"
    )


@pytest.mark.differential_row(row=ROW_H_R1, experiment="K1", column="integrated")
async def test_frozen_direct_reference_host_starts_reads_and_quits(
    baseline_runtime,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    monkeypatch,
):
    result = await _run(
        "K1-frozen",
        "K1",
        row=ROW_H_R1,
        daemon=False,
        runtime=baseline_runtime,
        reference=f"frozen Direct, {baseline_runtime.short}",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    _recorded("H-R1 K1 frozen", result)


@pytest.mark.differential_row(row=ROW_H_R1, experiment="K3", column="integrated")
async def test_candidate_daemon_host_starts_reads_and_quits(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    result = await _run(
        "K3",
        "K3",
        row=ROW_H_R1,
        daemon=True,
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    _recorded("H-R1 K3", result)


def test_the_candidate_daemon_is_no_worse_than_the_frozen_direct_reference():
    _compare("H-R1 K1 frozen", "H-R1 K3")


@pytest.mark.differential_row(row=ROW_H_R12, experiment="K1", column="integrated")
async def test_frozen_direct_reference_with_a_custom_browser(
    baseline_runtime,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    monkeypatch,
):
    result = await _run(
        "K1-frozen",
        "K1",
        row=ROW_H_R12,
        daemon=False,
        runtime=baseline_runtime,
        custom_browser=True,
        reference=f"frozen Direct, {baseline_runtime.short}, CHROME_PATH",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    _recorded("H-R12 K1 frozen", result)


@pytest.mark.differential_row(row=ROW_H_R12, experiment="K2", column="integrated")
async def test_the_baseline_coordinates_despite_a_custom_browser(
    baseline_runtime,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    monkeypatch,
):
    result = await _run(
        "K2",
        "K2",
        row=ROW_H_R12,
        daemon=True,
        runtime=baseline_runtime,
        custom_browser=True,
        expect_owner=True,
        reference=f"baseline daemon, {baseline_runtime.short}, CHROME_PATH",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    # The rest of K2's outcome is the baseline's, recorded in the packet.
    problems = k2_r12_verdict(result)
    assert not problems, f"K2: {problems}\n{result.report()}"


@pytest.mark.differential_row(row=ROW_H_R12, experiment="K3", column="integrated")
async def test_the_candidate_stays_direct_with_a_custom_browser(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    result = await _run(
        "K3",
        "K3",
        row=ROW_H_R12,
        daemon=True,
        custom_browser=True,
        expect_owner=False,
        reference="candidate daemon, CHROME_PATH",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    _recorded("H-R12 K3", result)
    assert result.vector is not None
    assert coordination_reading(result.vector) == "="


def test_the_candidate_with_a_custom_browser_matches_the_frozen_direct_run():
    _compare("H-R12 K1 frozen", "H-R12 K3")
