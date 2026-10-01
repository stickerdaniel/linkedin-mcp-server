"""Row H-CAL, the person read the call-loss rows hold, in K1 frozen, K3 and K0.

**H-CAL, an unfaulted held read.** The host starts and reads the feed, which
starts the browser outside anything held. The row's script then arms a gate
on ``details/experience/`` of a row-chosen username, reads
``get_person_profile`` with ``sections="experience,education"``, releases the
held page as soon as it entered, and the host quits normally
(``call_loss.calibration_problems``). It establishes, before any loss is
measured on it, that the pages, the gate and the ordering it relies on hold
on each runtime and platform.

* **K1 frozen**: the pinned baseline, Direct. Its own record has to show the
  three section requests; that the baseline navigates the same paths is not
  assumed from its source.
* **K3**: this checkout through the shared owner.
* **K0**: K3 again, valid on its own and reading as K3 did.

All three run with ``call_loss.CALIBRATION_IDLE_TIMEOUT_SECONDS``.

**K2 is not applicable.** The plan names no historical-daemon regression
witness for a calibration, and the contract forbids inventing one; the record
says so (``call_loss.K2_NOT_APPLICABLE``) and no K2 case runs.

Two comparisons counted as no cell: K3 held to K1 on O1 to O4, from two
valid records, and K0 held to K3 on every classification.

Native, like ``test_host_comparison_rows``: only where CI opted in after
trusting the CA, never under xdist, in file order, with only passing results
recorded for the comparisons.
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
from differential.call_loss import (
    K2_NOT_APPLICABLE,
    ROW_H_CAL,
    comparison_refusals,
    semantic_differences,
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
        work_dir=log.directory / "rows" / f"{ROW_H_CAL}-{key}",
        runtime=runtime,
        row=ROW_H_CAL,
        reference=reference,
    )
    print(
        f"{ROW_H_CAL} {result.label}: {result.vector} "
        f"record={(result.record or {}).get('problems')}"
    )
    return result


def _recorded(key: str, result: RowResult) -> None:
    assert not result.failures, result.report()
    assert result.vector is not None and result.record is not None
    assert result.record["k2"] == K2_NOT_APPLICABLE
    _VECTORS[key] = result.vector
    _RECORDS[key] = result.record


@pytest.mark.differential_row(row=ROW_H_CAL, experiment="K1", column="integrated")
async def test_the_frozen_direct_server_reads_a_held_profile_section_by_section(
    baseline_runtime,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    monkeypatch,
):
    result = await _run(
        "K1-frozen",
        "K1",
        daemon=False,
        runtime=baseline_runtime,
        reference=f"frozen Direct, {baseline_runtime.short}",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    _recorded(f"{ROW_H_CAL} K1 frozen", result)


@pytest.mark.differential_row(row=ROW_H_CAL, experiment="K3", column="integrated")
async def test_the_candidate_owner_reads_a_held_profile_section_by_section(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    result = await _run(
        "K3",
        "K3",
        daemon=True,
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    _recorded(f"{ROW_H_CAL} K3", result)


@pytest.mark.differential_row(row=ROW_H_CAL, experiment="K0", column="integrated")
async def test_the_held_read_repeats(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    reference = _VECTORS.get(f"{ROW_H_CAL} K3")
    if reference is None:
        pytest.fail("K3 produced no valid result in this run, so nothing to repeat")
    result = await _run(
        "K0",
        "K0",
        daemon=True,
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    problems = repeat_verdict(reference, result)
    assert not problems, f"K0: {problems}\n{result.report()}"
    assert result.record is not None
    _RECORDS[f"{ROW_H_CAL} K0"] = result.record


def test_the_held_read_through_the_owner_is_no_worse_than_the_frozen_direct_server():
    direct = _VECTORS.get(f"{ROW_H_CAL} K1 frozen")
    daemon = _VECTORS.get(f"{ROW_H_CAL} K3")
    refusals = comparison_refusals(
        _RECORDS.get(f"{ROW_H_CAL} K1 frozen"), _RECORDS.get(f"{ROW_H_CAL} K3")
    )
    if direct is None or daemon is None or refusals:
        pytest.fail(
            f"{ROW_H_CAL} K1 frozen and K3 must both have passed in this process "
            f"first; have {sorted(_VECTORS)}: {refusals}"
        )
    differences = compare_to_direct(direct, daemon)
    assert not differences, f"{ROW_H_CAL} K3 differs from K1 frozen: {differences}"


def test_the_held_read_repeat_reads_as_the_candidate_did():
    differences = semantic_differences(
        _RECORDS.get(f"{ROW_H_CAL} K3"), _RECORDS.get(f"{ROW_H_CAL} K0"), daemon=True
    )
    assert not differences, f"{ROW_H_CAL} K0 differs from K3: {differences}"
