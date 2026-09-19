"""Native Windows measurements for the owner-crash profile fence."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from linkedin_mcp_server import process_tree
from linkedin_mcp_server.profile_lease import ProfileLease
from windows_guardian_probe import (
    guardian_shutdown_sequence,
    observe_named_job_objects,
    sample_pre_crash_contention,
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


def await_fail_closed_guardian(
    process: Any, harness: Any, root: Path, *, timeout: float
) -> dict[str, Any]:
    result_path = root / "guardian-result.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result_path.exists():
            guardian = json.loads(result_path.read_text(encoding="utf-8"))
            if "error" not in guardian:
                time.sleep(0.01)
                continue
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(
                    "the failed guardian exited before handle observation: "
                    f"stdout={stdout!r} stderr={stderr!r}"
                )
            browser_job_name = json.loads(
                (root / "browser-job.json").read_text(encoding="utf-8")
            )["name"]
            project_job_name = json.loads(
                (root / "owner.json").read_text(encoding="utf-8")
            )["project_job_name"]
            retained_jobs = observe_named_job_objects(
                browser_job_name, project_job_name
            )
            contender = ProfileLease(root / "auth")
            acquired_before_drain = contender.try_acquire()
            if acquired_before_drain:
                contender.release()
                raise AssertionError("the failed guardian released its profile fence")
            harness.terminate()
            try:
                process.wait(timeout=30)
            finally:
                harness.wait_until_empty(timeout=30)
            acquired_after_drain = False
            release_deadline = time.monotonic() + 10
            while time.monotonic() < release_deadline:
                if contender.try_acquire():
                    acquired_after_drain = True
                    contender.release()
                    break
                time.sleep(0.01)
            return {
                "scenario": root.name,
                "guardian": guardian,
                "guardian_alive_before_harness_drain": True,
                "retained_jobs_before_harness_drain": retained_jobs,
                "contended_before_harness_drain": not acquired_before_drain,
                "acquired_after_harness_drain": acquired_after_drain,
            }
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                "the failed guardian exited before harness cleanup: "
                f"stdout={stdout!r} stderr={stderr!r}"
            )
        time.sleep(0.01)
    raise TimeoutError("the guardian did not report its injected failure")


def _run_probe(tmp_path: Path, scenario: str) -> dict[str, Any]:
    root = tmp_path / scenario
    harness = process_tree.WindowsJob.anonymous()
    nonce = process_tree.release_nonce()
    process = subprocess.Popen(
        process_tree.windows_gate_command(
            [
                sys.executable,
                str(_PROBE),
                "run",
                scenario,
                str(root),
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
        if scenario.startswith("candidate-"):
            return await_fail_closed_guardian(process, harness, root, timeout=20)
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


def test_guardian_drains_browser_before_terminating_project_owner_job() -> None:
    descendants = iter([2, 2, 1, 0, 0])
    browser = iter([2, 1, 0])
    project = iter([1, 0])
    events: list[str] = []
    ticks = iter(range(1, 100))
    result: dict[str, Any] = {"query_samples": []}

    def active_descendants() -> int:
        events.append("descendants")
        return next(descendants)

    def query_browser() -> int:
        events.append("browser query")
        return next(browser)

    def query_project() -> int:
        events.append("project query")
        return next(project)

    guardian_shutdown_sequence(
        result,
        active_descendants=active_descendants,
        terminate_browser_job=lambda: events.append("browser terminate"),
        query_browser_job=query_browser,
        close_browser_job=lambda: events.append("browser close"),
        release_fence=lambda: events.append("release"),
        terminate_project_job=lambda: events.append("project terminate"),
        query_project_job=query_project,
        close_project_job=lambda: events.append("project close"),
        monotonic=lambda: 0.0,
        clock_ns=lambda: next(ticks),
        sleep=lambda _seconds: events.append("sleep"),
    )

    assert events == [
        "descendants",
        "browser terminate",
        "descendants",
        "browser query",
        "sleep",
        "descendants",
        "browser query",
        "sleep",
        "descendants",
        "browser query",
        "descendants",
        "browser close",
        "release",
        "project terminate",
        "project query",
        "sleep",
        "project query",
        "project close",
    ]
    assert result["active_descendants_after_owner_death"] == 2
    assert result["terminate_ns"] < result["first_descendant_exit_ns"]
    assert result["first_descendant_exit_ns"] <= result["zero_observed_ns"]
    assert result["zero_observed_ns"] < result["browser_job_closed_ns"]
    assert result["browser_job_closed_ns"] < result["project_owner_terminate_ns"]
    assert (
        result["project_owner_terminate_ns"]
        <= result["project_owner_zero_observed_ns"]
        < result["project_owner_closed_ns"]
    )


@pytest.mark.parametrize(
    ("fault", "message", "failure_key"),
    [
        ("terminate", "termination failed", None),
        ("query", "could not query", "query_error"),
        ("timeout", "did not drain", "query_timeout"),
    ],
)
def test_guardian_failure_never_releases_an_unproven_fence(
    fault: str, message: str, failure_key: str | None
) -> None:
    events: list[str] = []
    result: dict[str, Any] = {"query_samples": []}
    now = iter([0.0, 31.0])

    def terminate_browser() -> None:
        events.append("browser terminate")
        if fault == "terminate":
            raise OSError("termination failed")

    def query_browser() -> int:
        events.append("browser query")
        if fault == "query":
            raise OSError("unreadable")
        return 1

    with pytest.raises((OSError, RuntimeError), match=message):
        guardian_shutdown_sequence(
            result,
            active_descendants=lambda: 1,
            terminate_browser_job=terminate_browser,
            query_browser_job=query_browser,
            close_browser_job=lambda: events.append("browser close"),
            release_fence=lambda: events.append("release"),
            terminate_project_job=lambda: events.append("project terminate"),
            query_project_job=lambda: 0,
            close_project_job=lambda: events.append("project close"),
            monotonic=(lambda: next(now)) if fault == "timeout" else (lambda: 0),
            sleep=lambda _seconds: None,
        )

    assert "zero_observed_ns" not in result
    assert "fence_released_ns" not in result
    assert "release" not in events
    assert "browser close" not in events
    assert "project terminate" not in events
    assert "project close" not in events
    if failure_key is not None:
        assert failure_key in result


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
@pytest.mark.parametrize(
    ("scenario", "fault", "error"),
    [
        (
            "candidate-terminate-error",
            "terminate-error",
            "OSError: injected browser Job termination failure",
        ),
        (
            "candidate-query-error",
            "query-error",
            "RuntimeError: the guardian could not query the browser Job",
        ),
        (
            "candidate-drain-timeout",
            "drain-timeout",
            "RuntimeError: the browser Job did not drain before its deadline",
        ),
    ],
)
def test_failed_guardian_holds_fence_until_outer_harness_drain(
    tmp_path: Path, scenario: str, fault: str, error: str
) -> None:
    measurement = _run_probe(tmp_path, scenario)
    _record_measurement(measurement)
    guardian = measurement["guardian"]
    retained_jobs = measurement["retained_jobs_before_harness_drain"]

    assert measurement["guardian_alive_before_harness_drain"] is True
    assert measurement["contended_before_harness_drain"] is True
    assert retained_jobs["browser_job_open"] is True
    assert retained_jobs["project_job_open"] is True
    assert measurement["acquired_after_harness_drain"] is True
    assert guardian["fault"] == fault
    assert guardian["fault_injected"] == fault
    assert guardian["error"] == error
    assert guardian["active_descendants_after_owner_death"] > 0
    assert (
        guardian["owner_death_observed_ns"]
        <= guardian["terminate_ns"]
        <= guardian["fault_injected_ns"]
    )
    assert "zero_observed_ns" not in guardian
    assert "fence_released_ns" not in guardian
    assert "browser_job_closed_ns" not in guardian
    assert "project_owner_terminate_ns" not in guardian
    assert "project_owner_closed_ns" not in guardian

    if fault == "terminate-error":
        assert guardian["terminate_called"] is False
        assert "query_error" not in guardian
        assert "query_timeout" not in guardian
    elif fault == "query-error":
        assert guardian["terminate_called"] is True
        assert guardian["query_error"] == (
            "OSError: injected browser Job query failure"
        )
        assert "query_timeout" not in guardian
    else:
        assert guardian["terminate_called"] is True
        assert guardian["query_timeout"] is True
        assert guardian["query_samples"][-1]["active_processes"] == 1
        assert (
            guardian["fault_injected_ns"] <= guardian["query_samples"][-1]["sampled_ns"]
        )
        assert "query_error" not in guardian


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
    assert guardian["active_descendants_after_owner_death"] > 0
    assert guardian["owner_death_observed_ns"] < guardian["terminate_ns"]
    assert guardian["terminate_called"] is True
    assert guardian["terminate_ns"] < guardian["first_descendant_exit_ns"]
    assert guardian["first_descendant_exit_ns"] <= guardian["zero_observed_ns"]
    assert len(samples) >= 2
    assert samples[0]["active_processes"] > 0
    assert samples[-1]["active_processes"] == 0
    assert "query_error" not in guardian
    assert "query_timeout" not in guardian
    assert guardian["zero_observed_ns"] < guardian["browser_job_closed_ns"]
    assert guardian["browser_job_closed_ns"] < guardian["project_owner_terminate_ns"]
    assert guardian["project_owner_terminate_called"] is True
    assert (
        guardian["project_owner_terminate_ns"]
        <= guardian["project_owner_zero_observed_ns"]
        < guardian["project_owner_closed_ns"]
    )
    assert guardian["project_owner_query_samples"][-1]["active_processes"] == 0
    assert "project_owner_query_error" not in guardian
    assert "project_owner_query_timeout" not in guardian
    assert measurement["lease_acquired_ns"] >= guardian["zero_observed_ns"]
