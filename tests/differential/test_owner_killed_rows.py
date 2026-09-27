"""Row H-R6, owner killed: K1 frozen, K2 and K3.

After the row's one call the harness kills the process that drives the
browser, SIGKILL on POSIX and TerminateProcess on Windows, through the handle
it took when it tied that process to the watcher's record of it:

* **K1 frozen** (baseline, Direct): the server the host started, a non-leader
  in the host's group, so its guardian gets group 0 and drains only the marked
  browser groups.
* **K2** (baseline, daemon): the owner. Before Path A its guardian is given the
  owner's group, which it kills when the owner dies: a signal no Direct
  guardian sends, so K2 must read ``!`` (``harness.r6_verdict``).
* **K3** (candidate, daemon): the owner. Its guardian gets 0, sends no group
  kill, and the frontend recovers on a second call; O1, O2 and O4 must be no
  worse than K1 frozen's.

The ``!`` rests on the guardian's argv, which the watcher reads on every POSIX
platform before the kill; on Linux the signal oracle also records the group
kill itself and must agree. Windows starts no guardian, so there the reading
is not applicable and only the kill, the recovery and O1/O4 are compared.

Native like the other rows: only where CI opted in after trusting the CA,
never under xdist, in file order.
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
    compare_to_direct,
    default_browsers_path,
    measure_host_quit_row,
    r6_reading,
    r6_verdict,
)
from differential.synthetic_origin import (
    OPT_IN_ENV,
    EgressProxy,
    SyntheticOrigin,
    fence_breaches,
)
from linkedin_mcp_server.config import reset_config

ROW_H_R6 = "H-R6"
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
]

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
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(default_browsers_path()))
    monkeypatch.setenv("PROXY_SERVER", proxy.url)
    reset_config()
    result = await measure_host_quit_row(
        profile=profile,
        experiment=key.split("-", 1)[0],
        daemon=daemon,
        egress=egress,
        log=log,
        work_dir=log.directory / "rows" / f"{ROW_H_R6}-{key}",
        runtime=runtime,
        row=ROW_H_R6,
        reference=reference,
        kill_actor=True,
    )
    print(
        f"{ROW_H_R6} {result.label}: {result.vector} "
        f"r6={r6_reading(result)} killed={result.killed} "
        f"o2={result.o2.state if result.o2 else None}"
    )
    return result


@pytest.mark.differential_row(row=ROW_H_R6, experiment="K1", column="integrated")
async def test_frozen_direct_server_killed(
    baseline_runtime,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    monkeypatch,
):
    result = await _run(
        "K1-frozen",
        daemon=False,
        runtime=baseline_runtime,
        reference=f"frozen Direct, {baseline_runtime.short}, server killed",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    problems = result.failures + r6_verdict(result, experiment="K1", windows=WINDOWS)
    assert not problems, f"{problems}\n{result.report()}"
    assert result.vector is not None
    _VECTORS["K1"] = result.vector


@pytest.mark.differential_row(row=ROW_H_R6, experiment="K2", column="integrated")
async def test_the_baseline_guardian_kills_the_owners_group(
    baseline_runtime,
    isolate_profile_dir,
    synthetic_egress,
    differential_run,
    monkeypatch,
):
    result = await _run(
        "K2",
        daemon=True,
        runtime=baseline_runtime,
        reference=f"baseline daemon, {baseline_runtime.short}, owner killed",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    # The rest of K2's outcome is the baseline's, recorded in the packet.
    problems = r6_verdict(result, experiment="K2", windows=WINDOWS)
    assert not problems, f"K2: {problems}\n{result.report()}"


@pytest.mark.differential_row(row=ROW_H_R6, experiment="K3", column="integrated")
async def test_the_candidate_owner_killed_drains_and_recovers(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    result = await _run(
        "K3",
        daemon=True,
        reference="candidate daemon, owner killed",
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    problems = result.failures + r6_verdict(result, experiment="K3", windows=WINDOWS)
    assert not problems, f"{problems}\n{result.report()}"
    assert result.vector is not None
    _VECTORS["K3"] = result.vector


def test_the_candidate_is_no_worse_than_the_frozen_direct_server_killed():
    reference, candidate = _VECTORS.get("K1"), _VECTORS.get("K3")
    if reference is None or candidate is None:
        pytest.fail(
            f"K1 frozen and K3 must both have passed in this process first; "
            f"have {sorted(_VECTORS)}"
        )
    differences = compare_to_direct(reference, candidate)
    assert not differences, f"K3 differs from K1 frozen on H-R6: {differences}"
