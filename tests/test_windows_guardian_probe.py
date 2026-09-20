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
import windows_guardian_probe as probe

from linkedin_mcp_server import process_tree
from linkedin_mcp_server.profile_lease import ProfileLease
from windows_guardian_probe import (
    guardian_loss_measurement,
    guardian_shutdown_sequence,
    observe_guardian_identity,
    observe_named_job_objects,
    read_published_json,
    sample_lease_acquisition,
    sample_pre_crash_contention,
    starter_termination_measurement,
    terminate_wait_close_handles,
)

_WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
_PROBE = Path(__file__).with_name("windows_guardian_probe.py")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_FAIL_CLOSED_SCENARIOS = {
    "candidate-terminate-error",
    "candidate-query-error",
    "candidate-drain-timeout",
}


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
            guardian = read_published_json(result_path, deadline=deadline)
            if "error" not in guardian:
                time.sleep(0.01)
                continue
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(
                    "the failed guardian exited before handle observation: "
                    f"stdout={stdout!r} stderr={stderr!r}"
                )
            termination_path = root / "starter-termination.json"
            while not termination_path.exists():
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    raise AssertionError(
                        "the probe exited before starter termination evidence: "
                        f"stdout={stdout!r} stderr={stderr!r}"
                    )
                if time.monotonic() >= deadline:
                    raise TimeoutError("the probe did not report starter termination")
                time.sleep(0.01)
            termination = read_published_json(termination_path, deadline=deadline)
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
            guardian_identity = observe_guardian_identity(
                guardian["guardian_identity_mutex"]
            )
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
                **termination,
                "guardian": guardian,
                "guardian_identity_before_harness_drain": guardian_identity,
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
    measurement = None
    stdout = b""
    stderr = b""
    try:
        harness.assign_popen(process)
        assigned = True
        if process.stdin is None:
            raise RuntimeError("the probe harness has no release stream")
        process_tree.release_windows_gate(process.stdin, nonce)
        if scenario in _FAIL_CLOSED_SCENARIOS:
            measurement = await_fail_closed_guardian(process, harness, root, timeout=20)
            stdout, stderr = process.communicate()
        else:
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
    expected_returncode = 1 if scenario in _FAIL_CLOSED_SCENARIOS else 0
    assert process.returncode == expected_returncode, stderr.decode("utf-8", "replace")
    if measurement is not None:
        return measurement
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


def test_lease_acquisition_requires_a_live_descendant() -> None:
    assert sample_lease_acquisition(
        active_descendants=lambda: 2,
        require_active=True,
        clock_ns=lambda: 123,
    ) == {
        "lease_acquired_ns": 123,
        "active_descendants_at_lease_acquire": 2,
    }
    with pytest.raises(RuntimeError, match="all descendants exited"):
        sample_lease_acquisition(
            active_descendants=lambda: 0,
            require_active=True,
            clock_ns=lambda: 456,
        )
    assert sample_lease_acquisition(
        active_descendants=lambda: 0,
        require_active=False,
        clock_ns=lambda: 789,
    ) == {
        "lease_acquired_ns": 789,
        "active_descendants_at_lease_acquire": 0,
    }


def test_starter_termination_requires_a_later_owner_exit() -> None:
    assert starter_termination_measurement(10, 11) == {
        "terminated_ns": 10,
        "owner_exit_ns": 11,
    }
    with pytest.raises(RuntimeError, match="did not follow"):
        starter_termination_measurement(10, 10)


def test_guardian_loss_requires_exit_before_live_acquisition() -> None:
    assert guardian_loss_measurement(
        termination_requested_ns=10,
        guardian_exit_observed_ns=11,
        lease_observed_ns=12,
        owner_active=True,
        active_descendants=2,
        browser_job_active_processes=2,
    ) == {
        "guardian_termination_requested_ns": 10,
        "guardian_exit_observed_ns": 11,
        "lease_observed_ns": 12,
        "owner_active_at_lease_observation": True,
        "active_descendants_at_lease_observation": 2,
        "browser_job_active_processes_at_lease_observation": 2,
    }
    with pytest.raises(RuntimeError, match="before guardian exit"):
        guardian_loss_measurement(
            termination_requested_ns=10,
            guardian_exit_observed_ns=12,
            lease_observed_ns=11,
            owner_active=True,
            active_descendants=2,
            browser_job_active_processes=2,
        )
    with pytest.raises(RuntimeError, match="owner exited"):
        guardian_loss_measurement(
            termination_requested_ns=10,
            guardian_exit_observed_ns=11,
            lease_observed_ns=12,
            owner_active=False,
            active_descendants=2,
            browser_job_active_processes=2,
        )
    with pytest.raises(RuntimeError, match="all descendants exited"):
        guardian_loss_measurement(
            termination_requested_ns=10,
            guardian_exit_observed_ns=11,
            lease_observed_ns=12,
            owner_active=True,
            active_descendants=0,
            browser_job_active_processes=2,
        )
    with pytest.raises(RuntimeError, match="browser Job drained"):
        guardian_loss_measurement(
            termination_requested_ns=10,
            guardian_exit_observed_ns=11,
            lease_observed_ns=12,
            owner_active=True,
            active_descendants=2,
            browser_job_active_processes=0,
        )


def test_guardian_identity_requires_an_owned_mutex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Handle:
        def Close(self) -> None:
            events.append("close")

    class Win32Con:
        SYNCHRONIZE = 0x00100000

    class Win32Event:
        @staticmethod
        def OpenMutex(access: int, inherit: bool, name: str) -> Handle:
            events.append(f"open {access} {inherit} {name}")
            return Handle()

        @staticmethod
        def WaitForSingleObject(_handle: Handle, timeout: int) -> int:
            events.append(f"wait {timeout}")
            return 0

        @staticmethod
        def ReleaseMutex(_handle: Handle) -> None:
            events.append("release")

    monkeypatch.setattr(
        probe,
        "_windows_modules",
        lambda: (object(), Win32Con, Win32Event, object()),
    )

    with pytest.raises(RuntimeError, match="no longer owns"):
        observe_guardian_identity("guardian-mutex")

    assert events == [
        "open 1048577 False guardian-mutex",
        "wait 0",
        "release",
        "close",
    ]


def test_published_json_retries_a_windows_share_violation() -> None:
    attempts = iter([PermissionError("sharing violation"), {"ready": True}])
    sleeps: list[float] = []

    def read(_path: Path) -> Any:
        value = next(attempts)
        if isinstance(value, BaseException):
            raise value
        return value

    assert read_published_json(
        Path("result.json"),
        deadline=1.0,
        read=read,
        monotonic=lambda: 0.0,
        sleep=sleeps.append,
    ) == {"ready": True}
    assert sleeps == [0.01]


@pytest.mark.parametrize("harness_returncode", [1, 0])
def test_failed_probe_path_requires_harness_termination_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness_returncode: int,
) -> None:
    events: list[str] = []

    class Process:
        stdin = object()
        returncode: int | None = None

        def communicate(self) -> tuple[bytes, bytes]:
            events.append("communicate")
            return b"", b""

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, *, timeout: float) -> int:
            events.append(f"process wait {timeout}")
            assert self.returncode is not None
            return self.returncode

        def kill(self) -> None:
            events.append("process kill")
            self.returncode = 1

    process = Process()

    class Harness:
        closed = False

        def assign_popen(self, assigned: Process) -> None:
            assert assigned is process
            events.append("assign")

        def terminate(self) -> None:
            events.append("harness terminate")
            process.returncode = harness_returncode

        def wait_until_empty(self, *, timeout: float) -> None:
            events.append(f"harness drain {timeout}")
            self.closed = True

        def close(self) -> None:
            events.append("harness close")
            self.closed = True

    harness = Harness()

    def await_failure(
        awaited_process: Process,
        awaited_harness: Harness,
        root: Path,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        assert awaited_process is process
        assert awaited_harness is harness
        events.append(f"await {root.name} {timeout}")
        harness.terminate()
        process.wait(timeout=30)
        harness.wait_until_empty(timeout=30)
        return {"scenario": root.name}

    monkeypatch.setattr(
        process_tree.WindowsJob,
        "anonymous",
        classmethod(lambda _cls: harness),
    )
    monkeypatch.setattr(process_tree, "release_nonce", lambda: "nonce")
    monkeypatch.setattr(
        process_tree,
        "windows_gate_command",
        lambda command, nonce: command if nonce == "nonce" else [],
    )
    monkeypatch.setattr(
        process_tree,
        "release_windows_gate",
        lambda stream, nonce: events.append(
            f"release {stream is process.stdin} {nonce}"
        ),
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        sys.modules[__name__], "await_fail_closed_guardian", await_failure
    )

    if harness_returncode == 1:
        assert _run_probe(tmp_path, "candidate-query-error") == {
            "scenario": "candidate-query-error"
        }
    else:
        with pytest.raises(AssertionError):
            _run_probe(tmp_path, "candidate-query-error")

    assert events == [
        "assign",
        "release True nonce",
        "await candidate-query-error 20",
        "harness terminate",
        "process wait 30",
        "harness drain 30",
        "communicate",
    ]


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


def test_guardian_waits_for_process_handles_after_job_reports_zero() -> None:
    descendants = iter([1, 1, 1, 0, 0])
    browser = iter([0, 0])
    events: list[str] = []
    result: dict[str, Any] = {"query_samples": []}

    guardian_shutdown_sequence(
        result,
        active_descendants=lambda: next(descendants),
        terminate_browser_job=lambda: events.append("browser terminate"),
        query_browser_job=lambda: next(browser),
        close_browser_job=lambda: events.append("browser close"),
        release_fence=lambda: events.append("release"),
        terminate_project_job=lambda: events.append("project terminate"),
        query_project_job=lambda: 0,
        close_project_job=lambda: events.append("project close"),
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: events.append("sleep"),
    )

    assert events == [
        "browser terminate",
        "sleep",
        "browser close",
        "release",
        "project terminate",
        "project close",
    ]
    assert result["query_samples"][-1]["active_processes"] == 0
    assert result["active_descendants_after_owner_death"] == 1


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
    assert measurement["owner_exit_ns"] > measurement["terminated_ns"]
    assert (
        measurement["lease_acquired_with_live_descendant_ns"]
        == measurement["lease_acquired_ns"]
        > measurement["terminated_ns"]
    )
    assert measurement["lease_acquired_ns"] < measurement["descendants_exit_ns"]
    assert measurement["active_descendants_at_lease_acquire"] > 0
    assert measurement["guardian_outside_owner_job"] is None


@_WINDOWS_ONLY
def test_guardian_loss_releases_lease_before_owner_or_browser_exit(
    tmp_path: Path,
) -> None:
    measurement = _run_probe(tmp_path, "candidate-guardian-loss-before-owner")
    _record_measurement(measurement)
    before = measurement["before_guardian_termination"]
    loss = measurement["guardian_loss"]

    assert measurement["pre_crash_contention"]["acquired"] is False
    assert (
        measurement["pre_crash_contention"]["attempted_ns"]
        < loss["guardian_termination_requested_ns"]
    )
    assert before == {
        "guardian_active": True,
        "owner_active": True,
        "active_descendants": measurement["descendant_count"],
        "browser_job_active_processes": measurement["descendant_count"],
    }
    assert measurement["guardian_outside_owner_job"] is True
    assert measurement["guardian_returncode"] != 0
    assert (
        loss["guardian_termination_requested_ns"]
        < loss["guardian_exit_observed_ns"]
        <= loss["lease_observed_ns"]
    )
    assert loss["owner_active_at_lease_observation"] is True
    assert loss["active_descendants_at_lease_observation"] > 0
    assert loss["browser_job_active_processes_at_lease_observation"] > 0
    assert measurement["active_descendants_at_lease_acquire"] > 0
    assert (
        measurement["lease_acquired_with_live_descendant_ns"]
        == measurement["lease_acquired_ns"]
    )


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
    guardian_identity = measurement["guardian_identity_before_harness_drain"]

    assert measurement["owner_exit_ns"] > measurement["terminated_ns"]
    assert guardian_identity["identity_mutex_owned"] is True
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
    assert measurement["owner_exit_ns"] > measurement["terminated_ns"]
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
