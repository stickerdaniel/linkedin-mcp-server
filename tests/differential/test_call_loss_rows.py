"""The call-loss rows, H-CAL, H-R4 and H-R5, in K1 frozen, K3 and K0.

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

**H-R4, the host lost mid-read**, in four cases, each with its own cells
(``call_loss.LOSS_CASES``): stdin EOF, abrupt pipe loss, the host process
killed, and EOF with a second read outstanding. **H-R5, the server or
frontend killed mid-read** with the host alive: K1 frozen kills the Direct
server, K3 and K0 only the frontend. Each loses the calibrated read once its
held page entered, releases it fifteen seconds after that entry, and is
judged by ``call_loss.loss_problems``: nothing went on after the loss, the
server or frontend ended by itself, Direct's profile settled before the
harness ended anything, the owner stayed the one identified and a fresh host
read through it inside the declared window, and a fresh read succeeded
after the loss in both. K3 is held to K1 on O1 to O4 (``compare_to_direct``)
and on what went on and the read after the loss (``call_loss.
loss_comparison``); K0 to K3 on every classification. K2 is not applicable
(``call_loss.LOSS_K2_NOT_APPLICABLE``). All of them run with
``call_loss.LOSS_IDLE_TIMEOUT_SECONDS``, the calibration's.

Native, like ``test_host_comparison_rows``: only where CI opted in after
trusting the CA, never under xdist, in file order, with only passing results
recorded for the comparisons. CI runs each row in a step of its own, chosen
by keyword: ``held_profile or held_read`` for H-CAL, a case's id for H-R4,
and ``killed_mid_read`` for H-R5.
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
    LOSS_K2_NOT_APPLICABLE,
    ROW_H_CAL,
    ROW_H_R4_EOF,
    ROW_H_R4_HOST,
    ROW_H_R4_PIPE,
    ROW_H_R4_TWO,
    ROW_H_R5,
    comparison_refusals,
    loss_comparison,
    loss_semantic_differences,
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
    row: str = ROW_H_CAL,
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
        f"record={(result.record or {}).get('problems')}"
    )
    return result


def _recorded(key: str, result: RowResult, k2: dict[str, Any] = K2_NOT_APPLICABLE):
    assert not result.failures, result.report()
    assert result.vector is not None and result.record is not None
    assert result.record["k2"] == k2
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


# --- H-R4 and H-R5: the read lost mid-call -------------------------------------------

#: Each H-R4 case by the id CI selects its step with.
_HOST_LOSSES = {
    "eof": ROW_H_R4_EOF,
    "pipe": ROW_H_R4_PIPE,
    "host_killed": ROW_H_R4_HOST,
    "two_requests": ROW_H_R4_TWO,
}


def _cases(experiment: str) -> list[Any]:
    """Each H-R4 case as a parameter counted as its own row's cell."""
    return [
        pytest.param(
            row,
            id=name,
            marks=pytest.mark.differential_row(
                row=row, experiment=experiment, column="integrated"
            ),
        )
        for name, row in _HOST_LOSSES.items()
    ]


async def _frozen_direct(row, baseline_runtime, profile, egress, log, monkeypatch):
    result = await _run(
        "K1-frozen",
        "K1",
        daemon=False,
        runtime=baseline_runtime,
        reference=f"frozen Direct, {baseline_runtime.short}",
        profile=profile,
        egress=egress,
        log=log,
        monkeypatch=monkeypatch,
        row=row,
    )
    _recorded(f"{row} K1 frozen", result, LOSS_K2_NOT_APPLICABLE)


async def _candidate(row, experiment, profile, egress, log, monkeypatch):
    result = await _run(
        experiment,
        experiment,
        daemon=True,
        profile=profile,
        egress=egress,
        log=log,
        monkeypatch=monkeypatch,
        row=row,
    )
    if experiment == "K0":
        reference = _VECTORS.get(f"{row} K3")
        problems = repeat_verdict(reference, result)
        assert not problems, f"K0: {problems}\n{result.report()}"
        assert result.record is not None
        _RECORDS[f"{row} K0"] = result.record
    else:
        _recorded(f"{row} K3", result, LOSS_K2_NOT_APPLICABLE)


def _no_worse(row: str) -> None:
    direct = _VECTORS.get(f"{row} K1 frozen")
    daemon = _VECTORS.get(f"{row} K3")
    refusals = loss_comparison(
        _RECORDS.get(f"{row} K1 frozen"), _RECORDS.get(f"{row} K3")
    )
    if direct is None or daemon is None:
        pytest.fail(
            f"{row} K1 frozen and K3 must both have passed in this process "
            f"first; have {sorted(_VECTORS)}"
        )
    differences = compare_to_direct(direct, daemon) + refusals
    assert not differences, f"{row} K3 differs from K1 frozen: {differences}"


def _repeats_as_candidate(row: str) -> None:
    differences = loss_semantic_differences(
        _RECORDS.get(f"{row} K3"), _RECORDS.get(f"{row} K0")
    )
    assert not differences, f"{row} K0 differs from K3: {differences}"


@pytest.mark.parametrize("row", _cases("K1"))
async def test_the_frozen_direct_server_loses_its_host(
    row,
    baseline_runtime,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    monkeypatch,
):
    await _frozen_direct(
        row,
        baseline_runtime,
        isolate_profile_dir,
        synthetic_egress,
        differential_run,
        monkeypatch,
    )


@pytest.mark.parametrize("row", _cases("K3"))
async def test_the_candidate_frontend_loses_its_host(
    row, isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    await _candidate(
        row, "K3", isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
    )


@pytest.mark.parametrize("row", _cases("K0"))
async def test_a_lost_host_repeats(
    row, isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    if _VECTORS.get(f"{row} K3") is None:
        pytest.fail(f"{row} K3 produced no valid result in this run to repeat")
    await _candidate(
        row, "K0", isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
    )


@pytest.mark.parametrize("row", list(_HOST_LOSSES.values()), ids=list(_HOST_LOSSES))
def test_a_lost_host_through_the_owner_is_no_worse_than_the_frozen_direct_server(
    row,
):
    _no_worse(row)


@pytest.mark.parametrize("row", list(_HOST_LOSSES.values()), ids=list(_HOST_LOSSES))
def test_a_lost_host_repeat_reads_as_the_candidate_did(row):
    _repeats_as_candidate(row)


@pytest.mark.differential_row(row=ROW_H_R5, experiment="K1", column="integrated")
async def test_the_frozen_direct_server_is_killed_mid_read(
    baseline_runtime,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    monkeypatch,
):
    await _frozen_direct(
        ROW_H_R5,
        baseline_runtime,
        isolate_profile_dir,
        synthetic_egress,
        differential_run,
        monkeypatch,
    )


@pytest.mark.differential_row(row=ROW_H_R5, experiment="K3", column="integrated")
async def test_the_candidate_frontend_is_killed_mid_read(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    await _candidate(
        ROW_H_R5,
        "K3",
        isolate_profile_dir,
        synthetic_egress,
        differential_run,
        monkeypatch,
    )


@pytest.mark.differential_row(row=ROW_H_R5, experiment="K0", column="integrated")
async def test_a_frontend_killed_mid_read_repeats(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    if _VECTORS.get(f"{ROW_H_R5} K3") is None:
        pytest.fail(f"{ROW_H_R5} K3 produced no valid result in this run to repeat")
    await _candidate(
        ROW_H_R5,
        "K0",
        isolate_profile_dir,
        synthetic_egress,
        differential_run,
        monkeypatch,
    )


def test_a_frontend_killed_mid_read_is_no_worse_than_the_server_killed_mid_read():
    _no_worse(ROW_H_R5)


def test_a_frontend_killed_mid_read_repeat_reads_as_the_candidate_did():
    _repeats_as_candidate(ROW_H_R5)
