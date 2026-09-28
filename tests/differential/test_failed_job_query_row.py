"""Row H-R11, a failed Job-membership query at a routine close: K1, K2 and K3.

Windows only. Each experiment's actors start from a venv of their own that
adds one declared shim to the runtime's code (``job_query``): it fails
``win32job.IsProcessInJob`` when ``_in_another_owned_job`` asks it, and
records each failure it plants with the lifetime that made the call. The same
shim text, and so the same SHA-256, goes into all three venvs. After the read
the row holds one browser dependency back in its row-private cache, so the
next call starts the product's installer, which waits on a download host that
never answers; then the host calls ``close_session`` with that installer
running.

Claim map. Each cell here is a native continuation
(``harness.NativeContinuation``) and says nothing about which caller ended an
installer: its termination cause is ``unobserved``. Which branch the routine
drain selects on an unanswered held-Job query is the source model's
(``job_query_model``), calibrated in this invocation against the exact sources
these runtimes import. The installer family's end after an unconfirmed close
is a shared path: the owner exits without a signal and its kill-on-close Jobs
run down, the primitive that ends K1's family at host quit. The final test
composes these, never adds them up.

* **K1 frozen** (baseline, Direct): the reference. No adopted Job, so no fault
  is required and none may be reached; the installer ends with the server at
  host quit, which is its family's settlement, and no recovery probe follows.
* **K2** (baseline, daemon): the owner that closed reached the planted fault
  about an installer lifetime inside its close; the family settled before
  the harness restored anything. The branch it then selects is the model's.
* **K3** (candidate, daemon): the same entry; then core.close logs consuming
  the drain's False, the owner stands down and exits, the family settles, and
  after the harness's restoration a successor serves the probe.

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
from differential.harness import (
    R11Ledger,
    RowResult,
    candidate_runtime,
    continuation_problems,
    default_browsers_path,
    measure_host_quit_row,
    r11_composition,
    row_identity,
)
from differential.job_query import ShimVenv, make_shim_venv
from differential.job_query_model import BASELINE, CANDIDATE, calibrate
from differential.synthetic_origin import (
    OPT_IN_ENV,
    EgressProxy,
    SyntheticOrigin,
    fence_breaches,
)
from linkedin_mcp_server import process_tree
from linkedin_mcp_server.config import reset_config

ROW_H_R11 = "H-R11"
WINDOWS = sys.platform == "win32"
_PROCESS_TREE = "linkedin_mcp_server/process_tree.py"

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
        not WINDOWS,
        reason=(
            "H-R11 is the Windows routine drain of the owner's adopted Job; no "
            "other platform has that Job or that query"
        ),
    ),
]


@pytest.fixture(scope="module")
def ledger(request) -> R11Ledger:
    """This invocation's continuations, and only this invocation's."""
    return R11Ledger(current(request.config).run)


@pytest.fixture(scope="module")
def baseline_runtime(tmp_path_factory) -> Iterator[Runtime]:
    configured = os.environ.get(BASELINE_DIR_ENV)
    directory = Path(configured) if configured else tmp_path_factory.mktemp("baseline")
    yield prepare_baseline(directory)
    if not configured:
        remove_baseline(directory)


@pytest.fixture(scope="module")
def baseline_shim(baseline_runtime, tmp_path_factory) -> ShimVenv:
    return make_shim_venv(
        baseline_runtime.python, tmp_path_factory.mktemp("shim-baseline") / "venv"
    )


@pytest.fixture(scope="module")
def candidate_shim(tmp_path_factory) -> ShimVenv:
    return make_shim_venv(
        candidate_runtime().python, tmp_path_factory.mktemp("shim-candidate") / "venv"
    )


def _candidate_revision() -> str | None:
    return row_identity().get("head")


async def _run(
    key: str,
    *,
    daemon: bool,
    shim: ShimVenv,
    profile: Path,
    egress: tuple[SyntheticOrigin, EgressProxy],
    log: EventLog,
    ledger: R11Ledger,
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
        work_dir=log.directory / "rows" / f"{ROW_H_R11}-{key}",
        runtime=runtime,
        row=ROW_H_R11,
        reference=reference,
        job_query_shim=shim,
    )
    # Recorded before any assertion, so the composition sees a failed cell as
    # failed rather than as missing.
    ledger.record(result.continuation)
    continuation = result.continuation
    print(
        f"{ROW_H_R11} {result.label}: {result.vector} "
        f"witnesses={len(continuation.witnesses) if continuation else None} "
        f"termination cause="
        f"{continuation.termination_cause if continuation else None} "
        f"shim={shim.shim_sha256}"
    )
    return result


@pytest.mark.differential_row(row=ROW_H_R11, experiment="K1", column="integrated")
async def test_the_frozen_direct_reference_continues_without_an_adopted_job(
    baseline_runtime,
    baseline_shim,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    ledger,
    monkeypatch,
):
    result = await _run(
        "K1-frozen",
        daemon=False,
        shim=baseline_shim,
        runtime=baseline_runtime,
        reference=f"frozen Direct, {baseline_runtime.short}, failed Job query",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        ledger=ledger,
        monkeypatch=monkeypatch,
    )
    problems = result.failures + continuation_problems(
        result.continuation, experiment="K1", revision=BASELINE_SHA, run=ledger.run
    )
    assert not problems, f"{problems}\n{result.report()}"


@pytest.mark.differential_row(row=ROW_H_R11, experiment="K2", column="integrated")
async def test_the_baseline_owner_reaches_the_failed_query_in_its_close(
    baseline_runtime,
    baseline_shim,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    ledger,
    monkeypatch,
):
    result = await _run(
        "K2",
        daemon=True,
        shim=baseline_shim,
        runtime=baseline_runtime,
        reference=f"baseline daemon, {baseline_runtime.short}, failed Job query",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        ledger=ledger,
        monkeypatch=monkeypatch,
    )
    # The rest of K2's outcome is the baseline's, recorded in the packet; its
    # validity is not.
    problems = continuation_problems(
        result.continuation, experiment="K2", revision=BASELINE_SHA, run=ledger.run
    )
    assert not problems, f"K2: {problems}\n{result.report()}"


@pytest.mark.differential_row(row=ROW_H_R11, experiment="K3", column="integrated")
async def test_the_candidate_owner_stands_down_and_a_successor_serves(
    candidate_shim,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    ledger,
    monkeypatch,
):
    result = await _run(
        "K3",
        daemon=True,
        shim=candidate_shim,
        reference="candidate daemon, failed Job query",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        ledger=ledger,
        monkeypatch=monkeypatch,
    )
    problems = result.failures + continuation_problems(
        result.continuation,
        experiment="K3",
        revision=_candidate_revision(),
        run=ledger.run,
    )
    assert not problems, f"{problems}\n{result.report()}"


def test_the_failed_job_query_composes_from_this_invocation_only(ledger):
    # The source model runs here, in this process, on the pinned baseline's
    # and this checkout's process_tree; the composition then binds it to what
    # the native cells' runtimes imported.
    model = calibrate(
        {
            BASELINE: baseline_file(_PROCESS_TREE),
            CANDIDATE: Path(process_tree.__file__).read_text(encoding="utf-8"),
        }
    )
    problems = r11_composition(
        model,
        ledger,
        revisions={
            "K1": BASELINE_SHA,
            "K2": BASELINE_SHA,
            "K3": _candidate_revision(),
        },
    )
    assert not problems, problems
