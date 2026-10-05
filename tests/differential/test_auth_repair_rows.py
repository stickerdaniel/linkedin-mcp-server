"""H-R16 and H-R10a-login, native: a sign-in staged at the synthetic origin.

Each row stages its sign-in at the origin (``auth_repair``), with the
calibration's idle timeout and the cell's declared bounds
(``auth_repair.ENVIRONMENT``), in the columns it has:

* **Cold** (``H-R16-cold``): the browser closed, the staged session rejected,
  a cold read that meets the wall; K3 repairs and replays once, K1 frozen
  starts its own login and the host reads again once it completed.
* **Second frontend** (``H-R16-second``): a second host during the repair;
  daemon only, K1 a counted skip (``auth_repair.K1_NOT_APPLICABLE``).
* **Failed login** (``H-R16-failed``): the completion never released.
* **Login** (``H-R10a-login``, terminal): ``--login`` after the host quit,
  beside the idle owner in K3; a counted skip on Windows
  (``profile_commands.NO_TERMINAL_ON_WINDOWS``).

Three lanes are counted and not run: the response loss
(``auth_repair.RESPONSE_LOSS_OPEN``), the ``browser_open`` marker, and the
import beside an idle owner (``auth_repair.IMPORT_NOT_NATIVE``); each is
mapped to its models (``auth_repair.MODEL_COVERAGE``).

The login is headed. Linux runs it on the job's display; macOS and Windows
run it in the runner's own desktop session, which nothing before these rows
measured: a login that never opens its wall there leaves the cell invalid
(``the login never asked for its completion``), never a finding.

K3 is held to K1 on O1 to O4, the lineage among them (``compare_to_direct``),
from two valid records (``auth_repair.comparison_refusals``); K0 to K3 on
every classification (``auth_repair.semantic_differences``). K2 is not
applicable (``auth_repair.K2_NOT_APPLICABLE``).

Native, like ``test_profile_command_rows``: only where CI opted in after
trusting the CA, never under xdist, in file order, with only passing results
recorded for the comparisons. CI runs each group in a step of its own, chosen
by keyword: ``cold or second``, ``failed``, ``login``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from differential.auth_repair import (
    CASES,
    IMPORT_NOT_NATIVE,
    K1_NOT_APPLICABLE,
    K2_NOT_APPLICABLE,
    RESPONSE_LOSS_OPEN,
    ROW_COLD,
    ROW_FAILED,
    ROW_LOGIN,
    ROW_SECOND,
    comparison_refusals,
    semantic_differences,
)
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
from differential.profile_commands import NO_TERMINAL_ON_WINDOWS
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

#: Each row by the id CI selects its step with.
_ROWS = {
    "cold": ROW_COLD,
    "second": ROW_SECOND,
    "failed": ROW_FAILED,
    "login": ROW_LOGIN,
}


@pytest.fixture(scope="module")
def baseline_runtime(tmp_path_factory) -> Iterator[Runtime]:
    configured = os.environ.get(BASELINE_DIR_ENV)
    directory = Path(configured) if configured else tmp_path_factory.mktemp("baseline")
    yield prepare_baseline(directory)
    if not configured:
        remove_baseline(directory)


async def _run(
    row: str,
    experiment: str,
    *,
    daemon: bool,
    profile: Path,
    egress: tuple[SyntheticOrigin, EgressProxy],
    log: EventLog,
    monkeypatch: pytest.MonkeyPatch,
    runtime: Runtime | None = None,
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
    key = "K1-frozen" if runtime is not None else experiment
    result = await measure_host_quit_row(
        profile=profile,
        experiment=experiment,
        daemon=daemon,
        egress=egress,
        log=log,
        work_dir=log.directory / "rows" / f"{row}-{key}",
        runtime=runtime,
        row=row,
        reference=f"frozen Direct, {runtime.short}" if runtime is not None else None,
    )
    print(
        f"{row} {result.label}: {result.vector} "
        f"record={(result.record or {}).get('problems')}"
    )
    return result


def _recorded(key: str, result: RowResult) -> None:
    assert not result.failures, result.report()
    assert result.vector is not None and result.record is not None
    assert result.record["k2"] == K2_NOT_APPLICABLE
    _VECTORS[key] = result.vector
    _RECORDS[key] = result.record


def _skips(row: str) -> list[Any]:
    if CASES[row].terminal:
        return [
            pytest.mark.skipif(sys.platform == "win32", reason=NO_TERMINAL_ON_WINDOWS)
        ]
    return []


def _cells(experiment: str | None, *, direct: bool | None = None) -> list[Any]:
    """Each row as a parameter counted as its own row's cell, or, with no
    experiment, as a comparison counted as no cell; *direct* keeps only the
    rows that have (or have not) a K1 column."""
    return [
        pytest.param(
            row,
            id=name,
            marks=[
                *_skips(row),
                *(
                    [
                        pytest.mark.differential_row(
                            row=row, experiment=experiment, column="integrated"
                        )
                    ]
                    if experiment is not None
                    else []
                ),
            ],
        )
        for name, row in _ROWS.items()
        if direct is None or CASES[row].direct is direct
    ]


@pytest.mark.parametrize("row", _cells("K1", direct=True))
async def test_the_frozen_direct_server_meets_the_sign_in(
    row,
    baseline_runtime,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    monkeypatch,
):
    result = await _run(
        row,
        "K1",
        daemon=False,
        runtime=baseline_runtime,
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    _recorded(f"{row} K1 frozen", result)


@pytest.mark.parametrize("row", _cells("K1", direct=False))
def test_the_frozen_direct_server_has_no_frontend_repairing_for_it(row):
    pytest.skip(f"{row} K1: {K1_NOT_APPLICABLE['reason']}")


@pytest.mark.parametrize("row", _cells("K3"))
async def test_the_candidate_meets_the_sign_in_through_its_owner(
    row, isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    result = await _run(
        row,
        "K3",
        daemon=True,
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    _recorded(f"{row} K3", result)


@pytest.mark.parametrize("row", _cells("K0"))
async def test_the_sign_in_through_an_owner_repeats(
    row, isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    if _VECTORS.get(f"{row} K3") is None:
        pytest.fail(f"{row} K3 produced no valid result in this run to repeat")
    result = await _run(
        row,
        "K0",
        daemon=True,
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    problems = repeat_verdict(_VECTORS.get(f"{row} K3"), result)
    assert not problems, f"K0: {problems}\n{result.report()}"
    assert result.record is not None
    _RECORDS[f"{row} K0"] = result.record


@pytest.mark.parametrize("row", _cells(None, direct=True))
def test_the_sign_in_through_an_owner_is_no_worse_than_the_frozen_server(row):
    direct = _VECTORS.get(f"{row} K1 frozen")
    daemon = _VECTORS.get(f"{row} K3")
    if direct is None or daemon is None:
        pytest.fail(
            f"{row} K1 frozen and K3 must both have passed in this process first; "
            f"have {sorted(_VECTORS)}"
        )
    differences = compare_to_direct(direct, daemon) + comparison_refusals(
        _RECORDS.get(f"{row} K1 frozen"), _RECORDS.get(f"{row} K3")
    )
    assert not differences, f"{row} K3 differs from K1 frozen: {differences}"


@pytest.mark.parametrize("row", _cells(None))
def test_the_sign_in_repeat_reads_as_the_candidate_did(row):
    differences = semantic_differences(
        _RECORDS.get(f"{row} K3"), _RECORDS.get(f"{row} K0")
    )
    assert not differences, f"{row} K0 differs from K3: {differences}"


@pytest.mark.differential_row(row="H-R16-response-loss", experiment="K3")
def test_a_lost_marker_response_is_not_observed_natively():
    pytest.skip(RESPONSE_LOSS_OPEN)


@pytest.mark.differential_row(row="H-R10a-import", experiment="K3")
def test_an_import_beside_an_idle_owner_is_not_run_natively():
    pytest.skip(IMPORT_NOT_NATIVE)
