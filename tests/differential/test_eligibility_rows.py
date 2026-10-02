"""H-R12 remainder and H-R14, native: configurations the daemon refuses, and
a rival owner of the same build with another configuration.

Each refused configuration (``eligibility_rows.CASES``) runs K1 frozen
Direct, K3 with the daemon enabled, and K0, the configuration the same in
every column: ``disabled_env``, ``no_daemon``, ``container``, ``http``
(POSIX), ``synced``, ``state_synced``, ``unknown`` and ``cloud_storage``
(macOS). The provider metadata a storage cell needs is planted around the
whole cell, K1, K3 and K0 alike, by ``planted``: only on a disposable
GitHub-hosted runner, refused when any provider configuration is already
where the product reads one, written exclusively so nothing is ever
overwritten, and removed exactly. **H-R14** (``rival``) runs K1 frozen, K3
and K0.

K3 is held to K1 on O1 to O4 (``compare_to_direct``), from two valid
records (``eligibility_rows.comparison_refusals``); K0 to K3 on every
classification (``eligibility_rows.semantic_differences``). K2 is recorded
not applicable (``eligibility_rows.K2_NOT_APPLICABLE``). The lanes no cell
reaches are mapped to their models (``eligibility_rows.MODEL_COVERAGE``).

Native, like ``test_auth_repair_rows``: only where CI opted in after
trusting the CA, never under xdist, in file order, with only passing
results recorded for the comparisons. CI runs each group in a step of its
own, chosen by keyword, one cell at a time, so no two cells' provider
metadata ever meet.
"""

from __future__ import annotations

import contextlib
import os
import sys
import uuid
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
from differential.eligibility_rows import (
    CASES,
    CLOUD_STORAGE,
    K2_NOT_APPLICABLE,
    ROW_CLOUD,
    ROW_CONTAINER,
    ROW_DISABLED_ENV,
    ROW_HTTP,
    ROW_NO_DAEMON,
    ROW_RIVAL,
    ROW_STATE_SYNCED,
    ROW_SYNCED,
    ROW_UNKNOWN,
    Planted,
    cloud_storage_profile,
    cloud_storage_root,
    comparison_refusals,
    plant_for,
    plant_tree,
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

#: Read at import, before ``ignore_the_developers_environment`` deletes every
#: ``LINKEDIN*`` variable: provider metadata is planted only on a disposable
#: GitHub-hosted runner, the same guard the CI's trust step holds.
_DISPOSABLE_RUNNER = (
    os.environ.get(OPT_IN_ENV) == "1"
    and os.environ.get("GITHUB_ACTIONS") == "true"
    and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
    and not os.environ.get("ACT")
)

#: Valid results measured so far in this process, by row and column.
_VECTORS: dict[str, RowVector] = {}
_RECORDS: dict[str, dict[str, Any]] = {}

#: Each row by the id CI selects its step with.
_ROWS = {
    "disabled_env": ROW_DISABLED_ENV,
    "no_daemon": ROW_NO_DAEMON,
    "container": ROW_CONTAINER,
    "http": ROW_HTTP,
    "synced": ROW_SYNCED,
    "state_synced": ROW_STATE_SYNCED,
    "unknown": ROW_UNKNOWN,
    "cloud_storage": ROW_CLOUD,
    "rival": ROW_RIVAL,
}


@pytest.fixture(scope="module")
def baseline_runtime(tmp_path_factory) -> Iterator[Runtime]:
    configured = os.environ.get(BASELINE_DIR_ENV)
    directory = Path(configured) if configured else tmp_path_factory.mktemp("baseline")
    yield prepare_baseline(directory)
    if not configured:
        remove_baseline(directory)


def _point_at(profile: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point this process at *profile* the way ``isolate_profile_dir`` points
    it at its own, for the in-process staging and the product's lookups."""
    monkeypatch.setenv("USER_DATA_DIR", str(profile))
    for module in (
        "linkedin_mcp_server.drivers.browser",
        "linkedin_mcp_server.authentication",
        "linkedin_mcp_server.cli_main",
        "linkedin_mcp_server.setup",
        "linkedin_mcp_server.session_state",
    ):
        with contextlib.suppress(AttributeError):
            monkeypatch.setattr(f"{module}.DEFAULT_PROFILE_DIR", profile)
    for module in (
        "linkedin_mcp_server.drivers.browser",
        "linkedin_mcp_server.authentication",
        "linkedin_mcp_server.cli_main",
        "linkedin_mcp_server.setup",
    ):
        with contextlib.suppress(AttributeError):
            monkeypatch.setattr(f"{module}.get_profile_dir", lambda: profile)
    for module in (
        "linkedin_mcp_server.session_state",
        "linkedin_mcp_server.authentication",
        "linkedin_mcp_server.drivers.browser",
        "linkedin_mcp_server.debug_trace",
        "linkedin_mcp_server.error_diagnostics",
    ):
        with contextlib.suppress(AttributeError):
            monkeypatch.setattr(f"{module}.get_source_profile_dir", lambda: profile)


@contextlib.contextmanager
def planted(row: str, profile: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The cell's provider metadata in place for the whole cell, and the
    profile it runs on; removed exactly afterwards, whatever the cell did.

    Refused off a disposable runner and under xdist: the metadata is the
    account's, and two cells must never meet in it.
    """
    case = CASES.get(row)
    if case is None or case.plant is None:
        yield profile
        return
    if not _DISPOSABLE_RUNNER:
        pytest.fail(
            "provider metadata is planted only on a disposable GitHub-hosted "
            "runner; refusing"
        )
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.fail("provider metadata cells run one at a time, never under xdist")
    from linkedin_mcp_server import daemon_descriptor
    from linkedin_mcp_server.session_state import canonical

    if case.plant == CLOUD_STORAGE:
        root = cloud_storage_root(
            Path.home(), f"linkedin-mcp-differential-{uuid.uuid4().hex[:12]}"
        )
        made = Planted(CLOUD_STORAGE, (plant_tree(root),))
        profile = cloud_storage_profile(root)
        _point_at(profile, monkeypatch)
    else:
        made = plant_for(
            case.plant,
            auth_root=canonical(profile).parent,
            state_root=daemon_descriptor.daemon_state_root(),
        )
    try:
        yield profile
    finally:
        problems = made.remove()
        assert not problems, f"the planted metadata was not removed exactly: {problems}"


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
    key = "K1-frozen" if runtime is not None else experiment
    with planted(row, profile, monkeypatch) as cell_profile:
        reset_config()
        result = await measure_host_quit_row(
            profile=cell_profile,
            experiment=experiment,
            daemon=daemon,
            egress=egress,
            log=log,
            work_dir=log.directory / "rows" / f"{row}-{key}",
            runtime=runtime,
            row=row,
            reference=f"frozen Direct, {runtime.short}"
            if runtime is not None
            else None,
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
    case = CASES.get(row)
    reason = case.skip_reason(sys.platform) if case is not None else None
    return [pytest.mark.skip(reason=reason)] if reason is not None else []


def _cells(experiment: str | None) -> list[Any]:
    """Each row as a parameter counted as its own row's cell, or, with no
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
        for name, row in _ROWS.items()
    ]


@pytest.mark.parametrize("row", _cells("K1"))
async def test_the_frozen_direct_server_reads_under_the_setting(
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


@pytest.mark.parametrize("row", _cells("K3"))
async def test_the_candidate_decides_with_the_daemon_enabled(
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
async def test_the_candidate_s_decision_repeats(
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


@pytest.mark.parametrize("row", _cells(None))
def test_the_candidate_is_no_worse_than_the_frozen_server(row):
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
def test_the_repeat_reads_as_the_candidate_did(row):
    differences = semantic_differences(
        _RECORDS.get(f"{row} K3"), _RECORDS.get(f"{row} K0")
    )
    assert not differences, f"{row} K0 differs from K3: {differences}"
