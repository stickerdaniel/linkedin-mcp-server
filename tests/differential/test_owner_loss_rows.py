"""H-R8 and H-R9, native: the owner lost before dispatch, and after the
dispatch of a mutating tool.

**H-R8** (``owner_loss``), each lane with its own username, the
calibration's idle timeout and K1 frozen, K3 and K0:

* **Unreachable**: the owner killed between calls; the preflight goes
  unanswered and a verified successor serves the read.
* **Stopped** (owner error, POSIX only): the owner stopped between calls and
  resumed ``owner_loss.STOP_SECONDS`` later, on every path; a bounded failed
  preflight, nothing run on the stopped owner, a later read through it.
  Windows has no portable stop: every cell of the lane is a counted skip.
* **Responder 404 and 500**: the owner killed and a declared responder bound
  on its old address; the frontend classifies the answer, nothing is
  dispatched to it, and a successor serves the read.

K1 frozen makes the same host requests through Direct, with no hop and no
fault; in a lane that kills, its server is traced over the same interval, so
both columns' O2 rest on the same tracing. Retiring is H-R13's.

**H-R9**: ``send_message`` to a synthetic recipient, its first navigation
held; once it entered, K1 frozen kills the Direct server and K3 kills the
owner. K3 must answer ``outcome_unknown`` with ``retry_safe`` false and never
navigate to the recipient again, then read through a verified successor; K1
loses the connection, settles and reads from a fresh host.

K3 is held to K1 on O1 to O4 (``compare_to_direct``) from two valid records
(``owner_loss.comparison_refusals``); K0 to K3 on every classification
(``owner_loss.semantic_differences``). K2 is not applicable
(``owner_loss.K2_NOT_APPLICABLE``).

Native, like ``test_call_loss_rows``: only where CI opted in after trusting
the CA, never under xdist, in file order, with only passing results recorded
for the comparisons. CI runs each lane in a step of its own, chosen by
keyword: ``unreachable``, ``stopped``, ``responder_404``, ``responder_500``
and ``message``.
"""

from __future__ import annotations

import os
import sys
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
from differential.owner_loss import (
    K2_NOT_APPLICABLE,
    ROW_H_R9,
    ROW_OWNER_ERROR,
    ROW_RESPONDER_404,
    ROW_RESPONDER_500,
    ROW_UNREACHABLE,
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

#: Why the stopped-owner lane does not run on Windows.
NO_PORTABLE_STOP = (
    "Windows has no portable stop of a process: SIGSTOP and SIGCONT are POSIX, "
    "and suspending every thread is a different fault with no matching resume "
    "contract, so the owner-error lane stays with the unit matrix there"
)


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


# --- H-R8 ---------------------------------------------------------------------------

#: Each lane by the id CI selects its step with.
_LANES = {
    "unreachable": ROW_UNREACHABLE,
    "stopped": ROW_OWNER_ERROR,
    "responder_404": ROW_RESPONDER_404,
    "responder_500": ROW_RESPONDER_500,
}


def _skips(row: str) -> list[Any]:
    if row == ROW_OWNER_ERROR:
        return [pytest.mark.skipif(sys.platform == "win32", reason=NO_PORTABLE_STOP)]
    return []


def _lanes(experiment: str | None) -> list[Any]:
    """Each lane as a parameter counted as its own row's cell, or, with no
    experiment, as a comparison counted as no cell."""
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
        for name, row in _LANES.items()
    ]


@pytest.mark.parametrize("row", _lanes("K1"))
async def test_the_frozen_direct_server_makes_the_same_requests_with_no_hop(
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


@pytest.mark.parametrize("row", _lanes("K3"))
async def test_the_candidate_frontend_recovers_from_an_owner_out_of_service(
    row, isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    await _candidate(
        row, "K3", isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
    )


@pytest.mark.parametrize("row", _lanes("K0"))
async def test_an_owner_out_of_service_repeats(
    row, isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    await _candidate(
        row, "K0", isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
    )


@pytest.mark.parametrize("row", _lanes(None))
def test_an_owner_out_of_service_is_no_worse_than_the_frozen_server(row):
    _no_worse(row)


@pytest.mark.parametrize("row", _lanes(None))
def test_an_owner_out_of_service_repeat_reads_as_the_candidate_did(row):
    _repeats_as_candidate(row)


# --- H-R9 ---------------------------------------------------------------------------


@pytest.mark.differential_row(row=ROW_H_R9, experiment="K1", column="integrated")
async def test_the_frozen_direct_server_is_killed_while_a_message_is_held(
    baseline_runtime,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    monkeypatch,
):
    await _frozen_direct(
        ROW_H_R9,
        baseline_runtime,
        isolate_profile_dir,
        synthetic_egress,
        differential_run,
        monkeypatch,
    )


@pytest.mark.differential_row(row=ROW_H_R9, experiment="K3", column="integrated")
async def test_the_candidate_owner_is_killed_while_a_message_is_held(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    await _candidate(
        ROW_H_R9,
        "K3",
        isolate_profile_dir,
        synthetic_egress,
        differential_run,
        monkeypatch,
    )


@pytest.mark.differential_row(row=ROW_H_R9, experiment="K0", column="integrated")
async def test_a_message_lost_after_dispatch_repeats(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    await _candidate(
        ROW_H_R9,
        "K0",
        isolate_profile_dir,
        synthetic_egress,
        differential_run,
        monkeypatch,
    )


def test_a_message_lost_after_dispatch_is_no_worse_than_the_frozen_server():
    _no_worse(ROW_H_R9)


def test_a_message_lost_after_dispatch_repeat_reads_as_the_candidate_did():
    _repeats_as_candidate(ROW_H_R9)
