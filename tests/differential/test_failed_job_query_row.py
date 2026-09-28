"""Row H-R11, a failed Job-membership query at a routine close: K1, K2 and K3.

Windows only. Each experiment's actors start from a venv of their own that
adds one declared shim to the runtime's code (``job_query``): it fails
``win32job.IsProcessInJob`` when ``_in_another_owned_job`` asks it, and
records every time it does. The same shim text, and so the same SHA-256, goes
into all three venvs. After the read the row holds one browser dependency
back in its row-private cache, so the next call starts the product's
installer, which waits on a download host that never answers; then the host
calls ``close_session`` with that installer running, and once more after.

* **K1 frozen** (baseline, Direct): no adopted Job, so the routine drain has
  no member to ask about and the shim is not reached. The installer ends with
  the server at host quit. The reference column.
* **K2** (baseline, daemon): the owner's drain asks whether the installer,
  a member of its adopted Job, is also in the installer's own Job; the failed
  answer is swallowed as "no" and the installer is terminated with exit code
  1 while the owner lives. K2 must read ``!``.
* **K3** (candidate, daemon): the failed answer counts the member as not
  ended; the drain runs out, the close stays unconfirmed, the owner stands
  down and a successor serves. K3 must read ``=`` and elect that successor.

The escalation is recorded in the packet: the drain's verdict in the owner's
log, the installer's end and exit code, the owner's exit, the lease's cleanup
and the successor. Native like the other rows: only where CI opted in after
trusting the CA, never under xdist, in file order.
"""

from __future__ import annotations

import os
import sys
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
    RowResult,
    RowVector,
    candidate_runtime,
    compare_to_direct,
    default_browsers_path,
    measure_host_quit_row,
    r11_reading,
    r11_verdict,
)
from differential.job_query import ShimVenv, make_shim_venv
from differential.synthetic_origin import (
    OPT_IN_ENV,
    EgressProxy,
    SyntheticOrigin,
    fence_breaches,
)
from linkedin_mcp_server.config import reset_config

ROW_H_R11 = "H-R11"
WINDOWS = sys.platform == "win32"

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

_VECTORS: dict[str, RowVector] = {}


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


async def _run(
    key: str,
    *,
    daemon: bool,
    shim: ShimVenv,
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
    print(
        f"{ROW_H_R11} {result.label}: {result.vector} r11={r11_reading(result)} "
        f"shim={shim.shim_sha256}"
    )
    return result


@pytest.mark.differential_row(row=ROW_H_R11, experiment="K1", column="integrated")
async def test_frozen_direct_server_is_never_asked(
    baseline_runtime,
    baseline_shim,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
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
        monkeypatch=monkeypatch,
    )
    problems = result.failures + r11_verdict(
        result, experiment="K1", non_windows=not WINDOWS
    )
    assert not problems, f"{problems}\n{result.report()}"
    assert result.vector is not None
    _VECTORS["K1"] = result.vector


@pytest.mark.differential_row(row=ROW_H_R11, experiment="K2", column="integrated")
async def test_the_baseline_owner_terminates_the_installer(
    baseline_runtime,
    baseline_shim,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
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
        monkeypatch=monkeypatch,
    )
    # The rest of K2's outcome is the baseline's, recorded in the packet.
    problems = r11_verdict(result, experiment="K2", non_windows=not WINDOWS)
    assert not problems, f"K2: {problems}\n{result.report()}"


@pytest.mark.differential_row(row=ROW_H_R11, experiment="K3", column="integrated")
async def test_the_candidate_owner_leaves_the_installer_and_is_replaced(
    candidate_shim, isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    result = await _run(
        "K3",
        daemon=True,
        shim=candidate_shim,
        reference="candidate daemon, failed Job query",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    problems = result.failures + r11_verdict(
        result, experiment="K3", non_windows=not WINDOWS
    )
    assert not problems, f"{problems}\n{result.report()}"
    assert result.vector is not None
    _VECTORS["K3"] = result.vector


def test_the_candidate_is_no_worse_than_the_frozen_direct_failed_job_query():
    reference, candidate = _VECTORS.get("K1"), _VECTORS.get("K3")
    if reference is None or candidate is None:
        pytest.fail(
            f"K1 frozen and K3 must both have passed in this process first; "
            f"have {sorted(_VECTORS)}"
        )
    differences = compare_to_direct(reference, candidate)
    assert not differences, f"K3 differs from K1 frozen on H-R11: {differences}"
