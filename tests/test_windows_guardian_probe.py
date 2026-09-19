"""Native Windows measurements for the owner-crash profile fence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from windows_guardian_probe import terminate_drain_and_release

_WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
_PROBE = Path(__file__).with_name("windows_guardian_probe.py")
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_probe(tmp_path: Path, scenario: str) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(_PROBE), "run", scenario, str(tmp_path / scenario)],
        cwd=_REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _record_measurement(measurement: dict[str, Any]) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary is None:
        return
    with Path(summary).open("a", encoding="utf-8") as stream:
        stream.write(f"### Windows crash fence: {measurement['scenario']}\n\n")
        stream.write(f"```json\n{json.dumps(measurement, sort_keys=True)}\n```\n\n")


def test_candidate_releases_only_after_observing_an_empty_job() -> None:
    active = iter([2, 1, 0])
    events: list[str] = []
    result: dict[str, Any] = {"query_samples": []}

    def query() -> int:
        events.append("query")
        return next(active)

    terminate_drain_and_release(
        result,
        terminate=lambda: events.append("terminate"),
        query_active=query,
        release=lambda: events.append("release"),
        sleep=lambda _seconds: None,
    )

    assert events == ["terminate", "query", "query", "query", "release"]
    assert [sample["active_processes"] for sample in result["query_samples"]] == [
        2,
        1,
        0,
    ]


def test_candidate_does_not_read_a_query_error_as_empty() -> None:
    events: list[str] = []
    result: dict[str, Any] = {"query_samples": []}

    def fail_query() -> int:
        events.append("query")
        raise OSError("unreadable")

    with pytest.raises(RuntimeError, match="could not query"):
        terminate_drain_and_release(
            result,
            terminate=lambda: events.append("terminate"),
            query_active=fail_query,
            release=lambda: events.append("release"),
        )

    assert events == ["terminate", "query"]
    assert result["query_error"] == "OSError: unreadable"


def test_candidate_does_not_read_a_drain_timeout_as_empty() -> None:
    now = iter([0.0, 31.0])
    events: list[str] = []
    result: dict[str, Any] = {"query_samples": []}

    with pytest.raises(RuntimeError, match="did not drain"):
        terminate_drain_and_release(
            result,
            terminate=lambda: events.append("terminate"),
            query_active=lambda: 1,
            release=lambda: events.append("release"),
            monotonic=lambda: next(now),
        )

    assert events == ["terminate"]
    assert result["query_timeout"] is True


@_WINDOWS_ONLY
def test_native_owner_crash_releases_lease_before_job_descendants_exit(
    tmp_path: Path,
) -> None:
    measurement = _run_probe(tmp_path, "baseline")
    _record_measurement(measurement)

    assert measurement["owner_exit_ns"] >= measurement["terminated_ns"]
    assert measurement["lease_acquired_ns"] < measurement["descendants_exit_ns"]
    assert measurement["active_descendants_at_lease_acquire"] > 0
    assert measurement["guardian_outside_owner_job"] is None


@_WINDOWS_ONLY
def test_external_guardian_holds_fence_until_browser_job_is_empty(
    tmp_path: Path,
) -> None:
    measurement = _run_probe(tmp_path, "candidate")
    _record_measurement(measurement)
    guardian = measurement["guardian"]
    samples = guardian["query_samples"]

    assert measurement["guardian_outside_owner_job"] is True
    assert guardian["terminate_called"] is True
    assert len(samples) >= 2
    assert samples[0]["active_processes"] > 0
    assert samples[-1]["active_processes"] == 0
    assert "query_error" not in guardian
    assert "query_timeout" not in guardian
    assert measurement["lease_acquired_ns"] >= guardian["zero_observed_ns"]
