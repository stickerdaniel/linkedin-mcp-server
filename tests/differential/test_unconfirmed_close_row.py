"""Row H-R7, a close the product cannot confirm: K0 controls, K1, K2 and K3.

Linux only. Every actor starts from a fault overlay of its runtime
(``fault_overlay``) with the idle close off from startup. After the first
read the row identifies the original actor, its guardian, the launch marker
and the profile lock, attaches strace to the actor and its guardian, and only
then activates the declared fault (``r7_fault``) for that one lifetime and
sends ``close_session``. The fault hands that close's real True back as
False, so the product proceeds as if the browser had not gone
(``unconfirmed_close``).

Claim map. Each cell is a native continuation (``R7Continuation``); the
fault's mechanism against each runtime's exact ``process_tree`` is the
source model's (``alias_model``); the phase after the real drain returned is
read from the actor's own trace; the whole row's O2 is a shared prefix read
apart. The final test composes these, never adds them up.

* **K0** (candidate, daemon): a control from the plain runtime and one from
  the overlay with its fault armed and never activated. Both closes confirm,
  the lease and guardian are released, the owner keeps serving; the two
  must read alike.
* **K1 frozen** (baseline, Direct), three times: the reference. The server
  consumes the False, keeps the lease to its host's quit, and then it and
  its guardian go and the lock is free.
* **K2** (baseline, daemon), three times: the owner consumes the False and
  kills its own group after the drain returned, in the shape a product-free
  probe calibrated in this invocation.
* **K3** (candidate, daemon), three times: the owner consumes the False and
  sends no signal after the drain returned; once it and its guardian are
  gone and the lock free, a successor serves the recovery before the host
  quits.

macOS runs no tracer and Windows no marked POSIX drain (H-R11 is its row), so
both are counted as skipped, not covered by Linux.

Native like the other rows: only where CI opted in after trusting the CA,
never under xdist, in file order.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from differential.accounting import current
from differential.baseline import (
    BASELINE_DIR_ENV,
    BASELINE_SHA,
    Runtime,
    baseline_file,
    prepare_baseline,
    remove_baseline,
)
from differential.events import EventLog
from differential.fault_overlay import Overlay, make_overlay
from differential.harness import (
    RowResult,
    candidate_runtime,
    compare_to_direct,
    default_browsers_path,
    measure_host_quit_row,
    row_identity,
)
from differential.synthetic_origin import (
    OPT_IN_ENV,
    EgressProxy,
    SyntheticOrigin,
    fence_breaches,
)
from differential.unconfirmed_close import (
    BASELINE,
    CANDIDATE,
    CLOSE_PATH,
    INERT,
    REPETITIONS,
    ROW_H_R7,
    UNSHIMMED,
    FatalCalibration,
    R7Ledger,
    R7Setup,
    alias_model,
    calibrate_fatal_group,
    r7_composition,
    r7_problems,
)
from linkedin_mcp_server import process_tree
from linkedin_mcp_server.config import reset_config

LINUX = sys.platform.startswith("linux")
_PROCESS_TREE = "linkedin_mcp_server/process_tree.py"
_REPO = Path(__file__).resolve().parents[2]

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
    pytest.mark.skipif(
        not LINUX,
        reason=(
            "H-R7 is the marked POSIX drain read by strace: macOS has no tracer "
            "for the phase after the drain, and Windows drains a Job instead "
            "(its row is H-R11). Counted as skipped here, not covered by Linux."
        ),
    ),
]


@pytest.fixture(scope="module")
def ledger(request) -> R7Ledger:
    """This invocation's continuations, and only this invocation's."""
    return R7Ledger(current(request.config).run)


@pytest.fixture(scope="module")
def baseline_runtime(tmp_path_factory) -> Iterator[Runtime]:
    configured = os.environ.get(BASELINE_DIR_ENV)
    directory = Path(configured) if configured else tmp_path_factory.mktemp("baseline")
    yield prepare_baseline(directory)
    if not configured:
        remove_baseline(directory)


@pytest.fixture(scope="module")
def baseline_overlay(baseline_runtime, tmp_path_factory) -> Overlay:
    return make_overlay(
        baseline_runtime.python, tmp_path_factory.mktemp("r7-baseline") / "venv"
    )


@pytest.fixture(scope="module")
def candidate_overlay(tmp_path_factory) -> Overlay:
    return make_overlay(
        candidate_runtime().python, tmp_path_factory.mktemp("r7-candidate") / "venv"
    )


@pytest.fixture(scope="module")
def calibration(request) -> FatalCalibration:
    """A product-free child killing its own group under this runner's strace.

    Once per invocation, before K2's first cell, so K2's witness is read
    against what this tracer writes for a fatal call, never a remembered
    example.
    """
    accounting = current(request.config)
    log = EventLog(accounting.directory, accounting.run)
    found = calibrate_fatal_group(accounting.directory / "rows" / f"{ROW_H_R7}-fatal")
    log.emit(
        experiment="K2",
        row=ROW_H_R7,
        actor="harness",
        kind="r7.calibration",
        shape=dict(found.shape) if found.shape is not None else None,
        problems=list(found.problems),
        returncode=found.returncode,
        transcript=list(found.transcript),
        evidence=found.evidence,
    )
    print(f"{ROW_H_R7} fatal own-group calibration: {found}")
    return found


def _candidate_revision() -> str | None:
    return row_identity().get("head")


async def _run(
    key: str,
    *,
    daemon: bool,
    setup: R7Setup,
    profile: Path,
    egress: tuple[SyntheticOrigin, EgressProxy],
    log: EventLog,
    ledger: R7Ledger,
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
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(default_browsers_path()))
    monkeypatch.setenv("PROXY_SERVER", proxy.url)
    reset_config()
    result = await measure_host_quit_row(
        profile=profile,
        experiment=key.split("-", 1)[0],
        daemon=daemon,
        egress=egress,
        log=log,
        work_dir=log.directory / "rows" / f"{ROW_H_R7}-{key}",
        runtime=runtime,
        row=ROW_H_R7,
        reference=reference,
        unconfirmed_close=setup,
    )
    # Recorded before any assertion, so the composition sees a failed cell as
    # failed rather than as missing.
    ledger.record(result.unconfirmed)
    c = result.unconfirmed
    print(
        f"{ROW_H_R7} {result.label}: {result.vector} "
        f"selected={not c.selection if c else None} "
        f"recovery={c.recovery if c else None} "
        f"phase={c.phase.collection if c and c.phase else None}"
    )
    return result


@pytest.mark.differential_row(row=ROW_H_R7, experiment="K0", column="integrated")
@pytest.mark.parametrize("control", [UNSHIMMED, INERT])
async def test_a_control_confirms_its_close(
    control,
    candidate_overlay,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    ledger,
    monkeypatch,
):
    result = await _run(
        f"K0-{control}",
        daemon=True,
        setup=R7Setup(
            overlay=candidate_overlay if control == INERT else None,
            activate=False,
            repetition=0,
            control=control,
        ),
        reference=f"candidate daemon, {control} control",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        ledger=ledger,
        monkeypatch=monkeypatch,
    )
    problems = result.failures + r7_problems(
        result.unconfirmed,
        experiment="K0",
        repetition=0,
        revision=_candidate_revision(),
        run=ledger.run,
        control=control,
    )
    assert not problems, f"{problems}\n{result.report()}"


@pytest.mark.differential_row(row=ROW_H_R7, experiment="K1", column="integrated")
@pytest.mark.parametrize("repetition", REPETITIONS)
async def test_the_frozen_direct_server_keeps_the_lease_to_its_quit(
    repetition,
    baseline_runtime,
    baseline_overlay,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    ledger,
    monkeypatch,
):
    result = await _run(
        f"K1-frozen-{repetition}",
        daemon=False,
        setup=R7Setup(overlay=baseline_overlay, activate=True, repetition=repetition),
        runtime=baseline_runtime,
        reference=f"frozen Direct, {baseline_runtime.short}, unconfirmed close",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        ledger=ledger,
        monkeypatch=monkeypatch,
    )
    problems = result.failures + r7_problems(
        result.unconfirmed,
        experiment="K1",
        repetition=repetition,
        revision=BASELINE_SHA,
        run=ledger.run,
    )
    assert not problems, f"{problems}\n{result.report()}"


@pytest.mark.differential_row(row=ROW_H_R7, experiment="K2", column="integrated")
@pytest.mark.parametrize("repetition", REPETITIONS)
async def test_the_baseline_owner_kills_its_own_group_after_the_drain(
    repetition,
    calibration,
    baseline_runtime,
    baseline_overlay,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    ledger,
    monkeypatch,
):
    result = await _run(
        f"K2-{repetition}",
        daemon=True,
        setup=R7Setup(overlay=baseline_overlay, activate=True, repetition=repetition),
        runtime=baseline_runtime,
        reference=f"baseline daemon, {baseline_runtime.short}, unconfirmed close",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        ledger=ledger,
        monkeypatch=monkeypatch,
    )
    # The rest of K2's outcome is the baseline's, recorded in the packet; its
    # validity is not, and neither is the witness.
    problems = r7_problems(
        result.unconfirmed,
        experiment="K2",
        repetition=repetition,
        revision=BASELINE_SHA,
        run=ledger.run,
        calibration=calibration,
    )
    assert not problems, f"K2: {problems}\n{result.report()}"


@pytest.mark.differential_row(row=ROW_H_R7, experiment="K3", column="integrated")
@pytest.mark.parametrize("repetition", REPETITIONS)
async def test_the_candidate_owner_signals_nothing_and_a_successor_serves(
    repetition,
    candidate_overlay,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    ledger,
    monkeypatch,
):
    result = await _run(
        f"K3-{repetition}",
        daemon=True,
        setup=R7Setup(overlay=candidate_overlay, activate=True, repetition=repetition),
        reference="candidate daemon, unconfirmed close",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        ledger=ledger,
        monkeypatch=monkeypatch,
    )
    problems = result.failures + r7_problems(
        result.unconfirmed,
        experiment="K3",
        repetition=repetition,
        revision=_candidate_revision(),
        run=ledger.run,
    )
    assert not problems, f"{problems}\n{result.report()}"


def test_the_unconfirmed_close_composes_from_this_invocation_only(ledger, calibration):
    # The source model runs here, in this process, on the pinned baseline's
    # and this checkout's process_tree; the composition then binds it to what
    # the native cells' actors imported.
    model = alias_model(
        {
            BASELINE: baseline_file(_PROCESS_TREE),
            CANDIDATE: Path(process_tree.__file__).read_text(encoding="utf-8"),
        },
        close_path={
            BASELINE: {path: baseline_file(path) for path in CLOSE_PATH},
            CANDIDATE: {
                path: (_REPO / path).read_text(encoding="utf-8") for path in CLOSE_PATH
            },
        },
    )
    candidate = _candidate_revision()
    problems = r7_composition(
        model,
        ledger,
        revisions={
            "K0": candidate,
            "K1": BASELINE_SHA,
            "K2": BASELINE_SHA,
            "K3": candidate,
        },
        calibration=calibration,
        compare_to_direct=compare_to_direct,
    )
    assert not problems, problems
