"""H-R13 and the turnover lanes, native: a read racing the owner's retirement.

**H-R13** (``retirement_race``), with its own idle timeout
(``retirement_race.IDLE_RACE_TIMEOUT_SECONDS``), the same in every column:

* **Admission wins**: the read is admitted and its profile page held past
  the idle threshold and the owner's connection grace, then released; it
  completes on the original owner and browser, which retire only afterwards.
* **Retirement wins**: the owner's idle exit is seen first, then a call
  through the still-live frontend reaches a verified successor and reads, or
  fails explicitly. K1: Direct's browser idled closed and the call reopens it.

Each in K1 frozen (the pinned baseline, Direct), K3 (this checkout through
the shared owner) and K0 (K3 again, valid on its own and reading as K3 did,
with both safe branches of retirement-wins projected alike). K3 is held to
K1 on O1 to O4 (``compare_to_direct``) from two valid records
(``retirement_race.comparison_refusals``). K2 is not applicable
(``retirement_race.K2_NOT_APPLICABLE``). The margins the idle timeout left
are printed from each record (``retirement_race.idle_margins``).

**Turnover lanes**, daemon only: the row asks the identified owner to stand
down with the product's bodyless request, then judges the drain, new work
refused, a read cut past the drain and a queued read cut before it began
(``retirement_race.turnover_problems``). K3 and K0 run; K1 is a counted skip
with the contract's reason, a policy and not parity
(``retirement_race.K1_NOT_APPLICABLE``), and K2 is not applicable. These
lanes do not discharge W6.

Native, like ``test_call_loss_rows``: only where CI opted in after trusting
the CA, never under xdist, in file order, with only passing results recorded
for the comparisons. CI runs each row in a step of its own, chosen by
keyword: ``admitted``, ``idled_out``, and ``turns_over and <lane>``.
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
from differential.retirement_race import (
    K1_NOT_APPLICABLE,
    K2_NOT_APPLICABLE,
    ROW_ADMISSION,
    ROW_CUT,
    ROW_DRAIN,
    ROW_QUEUED,
    ROW_REFUSED,
    ROW_RETIREMENT,
    comparison_refusals,
    idle_margins,
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
    record = result.record or {}
    print(
        f"{row} {result.label}: {result.vector} margins={idle_margins(record)} "
        f"record={record.get('problems')}"
    )
    return result


def _recorded(key: str, result: RowResult) -> None:
    assert not result.failures, result.report()
    assert result.vector is not None and result.record is not None
    assert result.record["k2"] == K2_NOT_APPLICABLE
    _VECTORS[key] = result.vector
    _RECORDS[key] = result.record


async def _frozen_direct(row, baseline_runtime, profile, egress, log, monkeypatch):
    result = await _run(
        row,
        "K1",
        daemon=False,
        runtime=baseline_runtime,
        profile=profile,
        egress=egress,
        log=log,
        monkeypatch=monkeypatch,
    )
    _recorded(f"{row} K1 frozen", result)


async def _candidate(row, experiment, profile, egress, log, monkeypatch):
    if experiment == "K0" and _VECTORS.get(f"{row} K3") is None:
        pytest.fail(f"{row} K3 produced no valid result in this run to repeat")
    result = await _run(
        row,
        experiment,
        daemon=True,
        profile=profile,
        egress=egress,
        log=log,
        monkeypatch=monkeypatch,
    )
    if experiment == "K0":
        problems = repeat_verdict(_VECTORS.get(f"{row} K3"), result)
        assert not problems, f"K0: {problems}\n{result.report()}"
        assert result.record is not None
        _RECORDS[f"{row} K0"] = result.record
    else:
        _recorded(f"{row} K3", result)


def _no_worse(row: str) -> None:
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


def _repeats_as_candidate(row: str) -> None:
    differences = semantic_differences(
        _RECORDS.get(f"{row} K3"), _RECORDS.get(f"{row} K0")
    )
    assert not differences, f"{row} K0 differs from K3: {differences}"


# --- H-R13, admission wins ------------------------------------------------------------


@pytest.mark.differential_row(row=ROW_ADMISSION, experiment="K1", column="integrated")
async def test_the_frozen_direct_server_keeps_its_browser_under_an_admitted_read(
    baseline_runtime,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    monkeypatch,
):
    await _frozen_direct(
        ROW_ADMISSION,
        baseline_runtime,
        isolate_profile_dir,
        synthetic_egress,
        differential_run,
        monkeypatch,
    )


@pytest.mark.differential_row(row=ROW_ADMISSION, experiment="K3", column="integrated")
async def test_the_candidate_owner_serves_an_admitted_read_before_it_retires(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    await _candidate(
        ROW_ADMISSION,
        "K3",
        isolate_profile_dir,
        synthetic_egress,
        differential_run,
        monkeypatch,
    )


@pytest.mark.differential_row(row=ROW_ADMISSION, experiment="K0", column="integrated")
async def test_an_admitted_read_repeats(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    await _candidate(
        ROW_ADMISSION,
        "K0",
        isolate_profile_dir,
        synthetic_egress,
        differential_run,
        monkeypatch,
    )


def test_an_admitted_read_through_the_owner_is_no_worse_than_the_frozen_server():
    _no_worse(ROW_ADMISSION)


def test_an_admitted_read_repeat_reads_as_the_candidate_did():
    _repeats_as_candidate(ROW_ADMISSION)


# --- H-R13, retirement wins -----------------------------------------------------------


@pytest.mark.differential_row(row=ROW_RETIREMENT, experiment="K1", column="integrated")
async def test_the_frozen_direct_server_reopens_a_browser_that_idled_out(
    baseline_runtime,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    monkeypatch,
):
    await _frozen_direct(
        ROW_RETIREMENT,
        baseline_runtime,
        isolate_profile_dir,
        synthetic_egress,
        differential_run,
        monkeypatch,
    )


@pytest.mark.differential_row(row=ROW_RETIREMENT, experiment="K3", column="integrated")
async def test_the_candidate_frontend_recovers_from_an_owner_that_idled_out(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    await _candidate(
        ROW_RETIREMENT,
        "K3",
        isolate_profile_dir,
        synthetic_egress,
        differential_run,
        monkeypatch,
    )


@pytest.mark.differential_row(row=ROW_RETIREMENT, experiment="K0", column="integrated")
async def test_a_call_after_the_owner_idled_out_repeats(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    await _candidate(
        ROW_RETIREMENT,
        "K0",
        isolate_profile_dir,
        synthetic_egress,
        differential_run,
        monkeypatch,
    )


def test_a_call_after_the_owner_idled_out_is_no_worse_than_the_frozen_server():
    _no_worse(ROW_RETIREMENT)


def test_a_call_after_the_owner_idled_out_repeat_reads_as_the_candidate_did():
    _repeats_as_candidate(ROW_RETIREMENT)


# --- Turnover lanes, daemon only --------------------------------------------------------

#: Each lane by the id CI selects its step with.
_LANES = {
    "drain": ROW_DRAIN,
    "refused": ROW_REFUSED,
    "cut": ROW_CUT,
    "queued": ROW_QUEUED,
}


def _lanes(experiment: str) -> list[Any]:
    """Each lane as a parameter counted as its own row's cell."""
    return [
        pytest.param(
            row,
            id=name,
            marks=pytest.mark.differential_row(
                row=row, experiment=experiment, column="integrated"
            ),
        )
        for name, row in _LANES.items()
    ]


@pytest.mark.parametrize("row", _lanes("K1"))
def test_the_frozen_direct_server_turns_over_nothing(row):
    pytest.skip(f"{row} K1: {K1_NOT_APPLICABLE['reason']}")


@pytest.mark.parametrize("row", _lanes("K3"))
async def test_the_candidate_owner_turns_over(
    row, isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    await _candidate(
        row, "K3", isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
    )


@pytest.mark.parametrize("row", _lanes("K0"))
async def test_a_turnover_repeats_as_the_owner_turns_over(
    row, isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    await _candidate(
        row, "K0", isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
    )


@pytest.mark.parametrize("row", list(_LANES.values()), ids=list(_LANES))
def test_a_turnover_repeat_reads_as_the_owner_turns_over(row):
    _repeats_as_candidate(row)
