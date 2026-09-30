"""Rows H-R3 and H-R2, each in K1 frozen, K3 and K0.

**H-R3, host quit with an idle browser.** The host actions are H-R1's: start,
one ``get_feed`` call, stdin EOF. The cells are H-R3's own because what they
add is new instrumentation of those actions, three checkpoints judged by
``host_comparison.r3_problems``, and the accounting credits each case to one
row. They run the same driver as H-R1, ``measure_host_quit_row``, selected
by the row.

* **K1 frozen**: the pinned baseline, Direct. At the quit the server closes
  its browser; its first post-exit reading is kept as read, and settlement
  is a bounded passive wait for an empty profile and a free lock.
* **K3**: this checkout through the shared owner. The owner keeps its root
  and the lease past the host's exit, in a window that ends before its idle
  timeout could have run out, then leaves by itself and the profile settles.
* **K0**: K3 again, valid on its own and reading as K3 did.

**H-R2, a second host while the first is open.** Host A reads, host B starts
on the same profile with the same launch, reads and quits, and A reads again
and quits (``host_comparison.r2_problems``).

* **K1 frozen**: Direct. B waits for the profile and A's server hands it over
  once its minimum hold is up, or has already released it idle; which one is
  evidence only. B's browser is gone before A2, and A2 launches again.
* **K3**: the owner A1 reached serves B and A2: one lifetime and instance,
  B forwarding to it, no second owner, each gap fresh from the last send.
* **K0**: K3 again.

Both rows run with ``harness.COMPARISON_IDLE_TIMEOUT_SECONDS`` in every column.

**K2 is not applicable to either row.** The plan names no historical-daemon
regression witness for them, and the contract forbids inventing one; each
record says so (``host_comparison.K2_NOT_APPLICABLE``) and no K2 case runs.

For each row, two comparisons counted as no cell: K3 held to K1 on O1 and
O4, from two valid records, and K0 held to K3 on every classification.

Native, like ``test_frozen_rows``: only where CI opted in after trusting the
CA, never under xdist, in file order, with only passing results recorded for
the comparisons.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from differential.baseline import (
    BASELINE_DIR_ENV,
    Runtime,
    prepare_baseline,
    remove_baseline,
)
from differential.events import EventLog
from differential.harness import (
    RowResult,
    RowVector,
    compare_to_direct,
    default_browsers_path,
    measure_host_quit_row,
    repeat_verdict,
)
from differential.host_comparison import (
    K2_NOT_APPLICABLE,
    ROW_H_R2,
    ROW_H_R3,
    comparison_refusals,
    semantic_differences,
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
_RECORDS: dict[str, dict[str, Any]] = {}


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
        reference=reference,
    )
    print(
        f"{row} {result.label}: {result.vector} "
        f"checkpoints={(result.comparison or {}).get('problems')}"
    )
    return result


def _recorded(key: str, result: RowResult) -> None:
    assert not result.failures, result.report()
    assert result.vector is not None and result.comparison is not None
    assert result.comparison["k2"] == K2_NOT_APPLICABLE
    _VECTORS[key] = result.vector
    _RECORDS[key] = result.comparison


async def _repeat(row: str, **cell: Any) -> None:
    reference = _VECTORS.get(f"{row} K3")
    if reference is None:
        pytest.fail("K3 produced no valid result in this run, so nothing to repeat")
    result = await _run("K0", "K0", row=row, daemon=True, **cell)
    problems = repeat_verdict(reference, result)
    assert not problems, f"K0: {problems}\n{result.report()}"
    assert result.comparison is not None
    _RECORDS[f"{row} K0"] = result.comparison


def _compare(row: str) -> None:
    direct, daemon = _VECTORS.get(f"{row} K1 frozen"), _VECTORS.get(f"{row} K3")
    refusals = comparison_refusals(
        _RECORDS.get(f"{row} K1 frozen"), _RECORDS.get(f"{row} K3")
    )
    if direct is None or daemon is None or refusals:
        pytest.fail(
            f"{row} K1 frozen and K3 must both have passed in this process "
            f"first; have {sorted(_VECTORS)}: {refusals}"
        )
    differences = compare_to_direct(direct, daemon)
    assert not differences, f"{row} K3 differs from K1 frozen: {differences}"


def _compare_repeat(row: str) -> None:
    differences = semantic_differences(
        _RECORDS.get(f"{row} K3"), _RECORDS.get(f"{row} K0"), daemon=True
    )
    assert not differences, f"{row} K0 differs from K3: {differences}"


# --- H-R3 ---------------------------------------------------------------------


@pytest.mark.differential_row(row=ROW_H_R3, experiment="K1", column="integrated")
async def test_the_frozen_direct_server_leaves_with_the_host(
    baseline_runtime,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    monkeypatch,
):
    result = await _run(
        "K1-frozen",
        "K1",
        row=ROW_H_R3,
        daemon=False,
        runtime=baseline_runtime,
        reference=f"frozen Direct, {baseline_runtime.short}",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    _recorded(f"{ROW_H_R3} K1 frozen", result)


@pytest.mark.differential_row(row=ROW_H_R3, experiment="K3", column="integrated")
async def test_the_candidate_owner_keeps_its_browser_past_the_host_and_then_leaves(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    result = await _run(
        "K3",
        "K3",
        row=ROW_H_R3,
        daemon=True,
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    _recorded(f"{ROW_H_R3} K3", result)


@pytest.mark.differential_row(row=ROW_H_R3, experiment="K0", column="integrated")
async def test_the_candidate_row_repeats(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    await _repeat(
        ROW_H_R3,
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )


def test_the_candidate_is_no_worse_than_the_frozen_direct_server():
    _compare(ROW_H_R3)


def test_the_repeat_reads_every_checkpoint_as_the_candidate_did():
    _compare_repeat(ROW_H_R3)


# --- H-R2 ---------------------------------------------------------------------


@pytest.mark.differential_row(row=ROW_H_R2, experiment="K1", column="integrated")
async def test_the_frozen_direct_server_hands_the_profile_to_a_second_host(
    baseline_runtime,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    monkeypatch,
):
    result = await _run(
        "K1-frozen",
        "K1",
        row=ROW_H_R2,
        daemon=False,
        runtime=baseline_runtime,
        reference=f"frozen Direct, {baseline_runtime.short}",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    _recorded(f"{ROW_H_R2} K1 frozen", result)


@pytest.mark.differential_row(row=ROW_H_R2, experiment="K3", column="integrated")
async def test_the_candidate_owner_serves_a_second_host_and_the_first_again(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    result = await _run(
        "K3",
        "K3",
        row=ROW_H_R2,
        daemon=True,
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    _recorded(f"{ROW_H_R2} K3", result)


@pytest.mark.differential_row(row=ROW_H_R2, experiment="K0", column="integrated")
async def test_the_second_host_row_repeats(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    await _repeat(
        ROW_H_R2,
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )


def test_the_candidate_with_a_second_host_is_no_worse_than_the_frozen_direct_server():
    _compare(ROW_H_R2)


def test_the_second_host_repeat_reads_as_the_candidate_did():
    _compare_repeat(ROW_H_R2)
