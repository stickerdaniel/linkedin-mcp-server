"""Native Windows measurements for the owner-crash profile fence."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from linkedin_mcp_server import process_tree
from windows_guardian_probe import (
    sample_pre_crash_contention,
    terminate_drain_and_release,
    terminate_wait_close_handles,
)

_WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
_PROBE = Path(__file__).with_name("windows_guardian_probe.py")
_REPO_ROOT = Path(__file__).resolve().parents[1]


def communicate_harness(
    process: Any, harness: Any, *, timeout: float
) -> tuple[bytes, bytes]:
    try:
        output = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        harness.terminate()
        try:
            process.wait(timeout=30)
        finally:
            harness.wait_until_empty(timeout=30)
        raise
    harness.wait_until_empty(timeout=30)
    return output


def _run_probe(tmp_path: Path, scenario: str) -> dict[str, Any]:
    harness = process_tree.WindowsJob.anonymous()
    nonce = process_tree.release_nonce()
    process = subprocess.Popen(
        process_tree.windows_gate_command(
            [
                sys.executable,
                str(_PROBE),
                "run",
                scenario,
                str(tmp_path / scenario),
            ],
            nonce,
        ),
        cwd=_REPO_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assigned = False
    try:
        harness.assign_popen(process)
        assigned = True
        if process.stdin is None:
            raise RuntimeError("the probe harness has no release stream")
        process_tree.release_windows_gate(process.stdin, nonce)
        stdout, stderr = communicate_harness(process, harness, timeout=180)
    finally:
        if not harness.closed:
            if assigned:
                with contextlib.suppress(Exception):
                    harness.terminate()
                with contextlib.suppress(Exception):
                    process.wait(timeout=30)
                with contextlib.suppress(Exception):
                    harness.wait_until_empty(timeout=30)
                if not harness.closed:
                    harness.close()
            else:
                harness.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
    assert process.returncode == 0, stderr.decode("utf-8", "replace")
    return json.loads(stdout)


def _record_measurement(measurement: dict[str, Any]) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary is None:
        return
    with Path(summary).open("a", encoding="utf-8") as stream:
        stream.write(f"### Windows crash fence: {measurement['scenario']}\n\n")
        stream.write(f"```json\n{json.dumps(measurement, sort_keys=True)}\n```\n\n")


def test_pre_crash_contention_is_measured_before_termination() -> None:
    events: list[str] = []

    sample = sample_pre_crash_contention(
        try_acquire=lambda: events.append("try") or False,
        release=lambda: events.append("release"),
        clock_ns=lambda: 123,
    )

    assert events == ["try"]
    assert sample == {"attempted_ns": 123, "acquired": False}


def test_harness_timeout_terminates_and_drains_the_outer_job() -> None:
    events: list[str] = []

    class Process:
        def communicate(self, *, timeout: float) -> tuple[bytes, bytes]:
            events.append(f"communicate {timeout}")
            raise subprocess.TimeoutExpired("probe", timeout)

        def wait(self, *, timeout: float) -> int:
            events.append(f"process wait {timeout}")
            return 1

    class Harness:
        def terminate(self) -> None:
            events.append("harness terminate")

        def wait_until_empty(self, *, timeout: float) -> None:
            events.append(f"harness drain {timeout}")

    with pytest.raises(subprocess.TimeoutExpired):
        communicate_harness(Process(), Harness(), timeout=180)

    assert events == [
        "communicate 180",
        "harness terminate",
        "process wait 30",
        "harness drain 30",
    ]


def test_retained_handles_are_terminated_and_waited_before_close() -> None:
    events: list[str] = []

    terminate_wait_close_handles(
        ["owner", "descendant"],
        is_active=lambda handle: events.append(f"active {handle}") or True,
        terminate=lambda handle: events.append(f"terminate {handle}"),
        wait=lambda handle: events.append(f"wait {handle}"),
        close=lambda handle: events.append(f"close {handle}"),
    )

    assert events == [
        "active owner",
        "terminate owner",
        "wait owner",
        "close owner",
        "active descendant",
        "terminate descendant",
        "wait descendant",
        "close descendant",
    ]


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

    assert measurement["pre_crash_contention"]["acquired"] is False
    assert (
        measurement["pre_crash_contention"]["attempted_ns"]
        < measurement["terminated_ns"]
    )
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

    assert measurement["pre_crash_contention"]["acquired"] is False
    assert (
        measurement["pre_crash_contention"]["attempted_ns"]
        < measurement["terminated_ns"]
    )
    assert measurement["guardian_outside_owner_job"] is True
    assert guardian["terminate_called"] is True
    assert len(samples) >= 2
    assert samples[0]["active_processes"] > 0
    assert samples[-1]["active_processes"] == 0
    assert "query_error" not in guardian
    assert "query_timeout" not in guardian
    assert measurement["lease_acquired_ns"] >= guardian["zero_observed_ns"]
