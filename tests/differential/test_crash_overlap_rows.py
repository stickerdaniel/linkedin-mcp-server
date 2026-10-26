"""Row H-R6-contended, native and Windows only: #808's discriminator.

A contender armed on the profile's real lock before a hard kill
(``crash_overlap``): K1 frozen kills the frozen Direct server and a fresh
host reads at once; K3 kills the candidate owner and the same host reads at
once, through a successor; K0 runs K3 again. Each lane's reading
(``crash_reading``) is overlap, no overlap or inconclusive, with the
milliseconds from the kill to the lease seen free, to the last old process
seen gone and to the successor's root; it is printed and kept beside the row
as ``reading.json``. An inconclusive lane fails. The comparison fails only on
an overlap K3 shows and K1 does not (``o1_findings``); a second root the
watcher saw is that comparison's, not a lane's failure (``lane_failures``).
K0 must reach K3's verdict (``repeat_findings``) with K3's vector
(``compare_repeat``). K2 is not applicable.

Native, like ``test_owner_loss_rows``: only where CI opted in after trusting
the CA, never under xdist, in file order.
"""

from __future__ import annotations

import json
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
from differential.crash_overlap import (
    K2_NOT_APPLICABLE,
    NOT_WINDOWS,
    ROW,
    Reading,
    crash_reading,
    lane_failures,
    o1_findings,
    repeat_findings,
)
from differential.events import EventLog
from differential.harness import (
    RowResult,
    RowVector,
    compare_repeat,
    default_browsers_path,
    measure_host_quit_row,
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
    pytest.mark.skipif(sys.platform != "win32", reason=NOT_WINDOWS),
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

#: Each lane's reading in this process, recorded whether or not it passed.
_READINGS: dict[str, Reading] = {}
#: K3's vector, recorded only once its lane passed.
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
    work_dir = log.directory / "rows" / f"{ROW}-{key}"
    result = await measure_host_quit_row(
        profile=profile,
        experiment=key.split("-", 1)[0],
        daemon=daemon,
        egress=egress,
        log=log,
        work_dir=work_dir,
        runtime=runtime,
        row=ROW,
        reference=f"frozen Direct, {runtime.short}" if runtime is not None else None,
    )
    reading = crash_reading(result.record or {})
    _READINGS[key.split("-", 1)[0]] = reading
    (work_dir / "reading.json").write_text(
        json.dumps(reading.as_record(), indent=2) + "\n"
    )
    print(f"{ROW} {result.label}: {reading.verdict} {reading.ms} {reading.reasons}")
    return result


def _recorded(result: RowResult) -> None:
    assert result.record is not None
    assert result.record["k2"] == K2_NOT_APPLICABLE
    assert not lane_failures(result.failures), result.report()


@pytest.mark.differential_row(row=ROW, experiment="K1", column="integrated")
async def test_the_frozen_direct_server_is_killed_under_a_contender(
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
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    _recorded(result)


@pytest.mark.differential_row(row=ROW, experiment="K3", column="integrated")
async def test_the_candidate_owner_is_killed_under_a_contender(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    result = await _run(
        "K3",
        daemon=True,
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    _recorded(result)
    assert result.vector is not None
    _VECTORS["K3"] = result.vector


@pytest.mark.differential_row(row=ROW, experiment="K0", column="integrated")
async def test_the_candidate_owner_killed_under_a_contender_repeats(
    isolate_profile_dir, synthetic_egress, differential_run, monkeypatch
):
    reference = _VECTORS.get("K3")
    if reference is None:
        pytest.fail("K3 produced no valid result in this run, so nothing to repeat")
    result = await _run(
        "K0",
        daemon=True,
        profile=isolate_profile_dir,
        egress=synthetic_egress,
        log=differential_run,
        monkeypatch=monkeypatch,
    )
    _recorded(result)
    assert result.vector is not None
    problems = repeat_findings(_READINGS.get("K3"), _READINGS.get("K0"))
    problems += compare_repeat(reference, result.vector)
    assert not problems, f"K0: {problems}\n{result.report()}"


def test_no_successor_browser_overlaps_only_under_the_daemon():
    findings = o1_findings(_READINGS.get("K1"), _READINGS.get("K3"))
    assert not findings, findings
