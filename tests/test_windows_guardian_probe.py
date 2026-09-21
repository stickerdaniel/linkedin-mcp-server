"""Native Windows measurements for the owner-crash profile fence."""

from __future__ import annotations

import contextlib
import ctypes
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
    acquire_actor_region,
    active_guardian_loss_wait_handles,
    conjunction_admission,
    conjunction_guardian_shutdown,
    guardian_loss_measurement,
    guardian_publication_sequence,
    guardian_shutdown_sequence,
    observe_browser_publication_order,
    observe_guardian_identity,
    observe_named_job_objects,
    open_descendant_handles_before_terminate,
    production_byte_zero_admission,
    query_control_job_membership,
    read_published_json,
    remaining_wait_milliseconds,
    require_same_file_identity,
    retry_lock_rundown,
    run_guardian_fail_closed,
    sample_guardian_loss_progress,
    sample_lease_acquisition,
    sample_pre_crash_contention,
    spawn_with_duplicated_handles,
    starter_termination_measurement,
    terminate_wait_close_handles,
    wait_on_unsignaled_throttle,
    zero_proven_release_sequence,
)

_WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
_PROBE = Path(__file__).with_name("windows_guardian_probe.py")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONJUNCTION_FAIL_CLOSED_SCENARIOS = {
    "conjunction-owner-loss-terminate-error",
    "conjunction-owner-loss-query-error",
    "conjunction-owner-loss-drain-timeout",
}
_FAIL_CLOSED_SCENARIOS = {
    "candidate-terminate-error",
    "candidate-query-error",
    "candidate-drain-timeout",
    *_CONJUNCTION_FAIL_CLOSED_SCENARIOS,
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


def await_fail_closed_conjunction(
    process: Any,
    harness: Any,
    root: Path,
    result_event: Any,
    *,
    timeout: float,
) -> dict[str, Any]:
    probe._wait(result_event, timeout, "the conjunction failure was not published")
    if process.poll() is not None:
        stdout, stderr = process.communicate()
        raise AssertionError(
            "the failed conjunction probe exited before harness cleanup: "
            f"stdout={stdout!r} stderr={stderr!r}"
        )
    measurement = read_published_json(
        root / "conjunction-result.json", deadline=time.monotonic() + 5
    )
    harness.terminate()
    try:
        process.wait(timeout=30)
    finally:
        harness.wait_until_empty(timeout=30)

    _win32api, _win32con, win32event, _win32job = probe._windows_modules()
    throttle = win32event.CreateEvent(None, False, False, None)
    rundown_error: BaseException | None = None
    try:
        acquired_after_drain, attempts, rundown_seconds = (
            probe.retry_admission_after_drain(
                open_fd=lambda: os.open(root / "auth" / "profile.lock", os.O_RDWR),
                try_admission=lambda fd, close: conjunction_admission(fd, close=close),
                release_a=lambda fd: probe._region_api()[1](fd, 0),
                close_fd=os.close,
                deadline=time.monotonic() + 10,
                wait_for_retry=lambda: probe.wait_on_unsignaled_throttle(
                    throttle, wait=win32event.WaitForSingleObject
                ),
            )
        )
    except BaseException as exc:
        rundown_error = exc
    finally:
        probe.close_preserving_error(throttle.Close, rundown_error)
    measurement["acquired_after_harness_drain"] = acquired_after_drain
    measurement["post_harness_rundown_attempts"] = attempts
    measurement["post_harness_rundown_seconds"] = rundown_seconds
    return measurement


def _run_probe(tmp_path: Path, scenario: str) -> dict[str, Any]:
    root = tmp_path / scenario
    harness = process_tree.WindowsJob.anonymous()
    nonce = process_tree.release_nonce()
    result_event = None
    environment = None
    if scenario in _CONJUNCTION_FAIL_CLOSED_SCENARIOS:
        _win32api, _win32con, win32event, _win32job = probe._windows_modules()
        result_event_name = probe._new_event_name("conjunction-result")
        result_event = win32event.CreateEvent(None, True, False, result_event_name)
        environment = {**os.environ, "CONJUNCTION_RESULT_EVENT": result_event_name}
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
        env=environment,
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
        if scenario in _CONJUNCTION_FAIL_CLOSED_SCENARIOS:
            if result_event is None:
                raise RuntimeError("the conjunction result event was not created")
            measurement = await_fail_closed_conjunction(
                process, harness, root, result_event, timeout=20
            )
            stdout, stderr = process.communicate()
        elif scenario in _FAIL_CLOSED_SCENARIOS:
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
        if result_event is not None:
            result_event.Close()
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
        lease_acquired_ns=12,
        lease_observed_ns=13,
        owner_active_before_job_query=True,
        owner_active_after_job_query=True,
        active_descendants=2,
        browser_job_active_processes=2,
    ) == {
        "guardian_termination_requested_ns": 10,
        "guardian_exit_observed_ns": 11,
        "lease_acquired_ns": 12,
        "lease_observed_ns": 13,
        "owner_active_at_lease_observation": True,
        "active_descendants_at_lease_observation": 2,
        "browser_job_active_processes_at_lease_observation": 2,
    }
    with pytest.raises(RuntimeError, match="before guardian exit"):
        guardian_loss_measurement(
            termination_requested_ns=10,
            guardian_exit_observed_ns=13,
            lease_acquired_ns=11,
            lease_observed_ns=12,
            owner_active_before_job_query=True,
            owner_active_after_job_query=True,
            active_descendants=2,
            browser_job_active_processes=2,
        )
    with pytest.raises(RuntimeError, match="timestamp is inconsistent"):
        guardian_loss_measurement(
            termination_requested_ns=10,
            guardian_exit_observed_ns=11,
            lease_acquired_ns=14,
            lease_observed_ns=13,
            owner_active_before_job_query=True,
            owner_active_after_job_query=True,
            active_descendants=2,
            browser_job_active_processes=2,
        )
    with pytest.raises(RuntimeError, match="owner exited"):
        guardian_loss_measurement(
            termination_requested_ns=10,
            guardian_exit_observed_ns=11,
            lease_acquired_ns=12,
            lease_observed_ns=13,
            owner_active_before_job_query=True,
            owner_active_after_job_query=False,
            active_descendants=2,
            browser_job_active_processes=2,
        )


def test_guardian_loss_samples_liveness_after_lease_signal() -> None:
    events: list[str] = []
    lease_was_observed = False
    ticks = iter([11, 13])

    def lease_signaled() -> bool:
        nonlocal lease_was_observed
        events.append("lease signal")
        lease_was_observed = True
        return True

    def after_lease(label: str, value: int | bool) -> int | bool:
        assert lease_was_observed
        events.append(label)
        return value

    measurement = sample_guardian_loss_progress(
        {},
        termination_requested_ns=10,
        lease_acquired_ns=lambda: int(after_lease("lease timestamp", 12)),
        guardian_active=lambda: events.append("guardian") or False,
        lease_signaled=lease_signaled,
        owner_active=lambda: bool(after_lease("owner", True)),
        active_descendants=lambda: int(after_lease("descendants", 2)),
        browser_job_active_processes=lambda: int(after_lease("browser job", 2)),
        clock_ns=lambda: next(ticks),
    )

    assert events == [
        "guardian",
        "lease signal",
        "owner",
        "descendants",
        "browser job",
        "owner",
        "lease timestamp",
    ]
    assert measurement is not None
    assert measurement["lease_acquired_ns"] == 12
    assert measurement["lease_observed_ns"] == 13


def test_guardian_loss_rechecks_owner_after_browser_job_query() -> None:
    owner_active = True

    def query_browser_job() -> int:
        nonlocal owner_active
        owner_active = False
        return 2

    with pytest.raises(RuntimeError, match="owner exited"):
        sample_guardian_loss_progress(
            {
                "guardian_exit_observed_ns": 11,
                "lease_observed_ns": 13,
            },
            termination_requested_ns=10,
            lease_acquired_ns=lambda: 12,
            guardian_active=lambda: False,
            lease_signaled=lambda: True,
            owner_active=lambda: owner_active,
            active_descendants=lambda: 2,
            browser_job_active_processes=query_browser_job,
        )


def test_guardian_loss_wait_deadline_is_checked_before_waiting() -> None:
    assert remaining_wait_milliseconds(10.0, monotonic=lambda: 9.5) == 500
    with pytest.raises(TimeoutError, match="deadline expired"):
        remaining_wait_milliseconds(10.0, monotonic=lambda: 10.0)


def test_guardian_loss_wait_excludes_exited_descendants() -> None:
    owner = object()
    exited = object()
    survivor = object()

    assert active_guardian_loss_wait_handles(
        owner,
        [exited, survivor],
        is_active=lambda handle: handle in {owner, survivor},
    ) == [owner, survivor]


def test_guardian_loss_wait_requires_owner_and_descendant_survivors() -> None:
    owner = object()
    descendant = object()

    with pytest.raises(RuntimeError, match="owner exited"):
        active_guardian_loss_wait_handles(
            owner,
            [descendant],
            is_active=lambda handle: handle is descendant,
        )
    with pytest.raises(RuntimeError, match="all descendants exited"):
        active_guardian_loss_wait_handles(
            owner,
            [descendant],
            is_active=lambda handle: handle is owner,
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


def test_conjunction_admission_acquires_a_then_b_and_retains_a() -> None:
    events: list[str] = []

    assert conjunction_admission(
        7,
        try_lock=lambda fd, offset: events.append(f"lock {fd} {offset}") or True,
        unlock=lambda fd, offset: events.append(f"unlock {fd} {offset}"),
    )

    assert events == ["lock 7 0", "lock 7 1", "unlock 7 1"]


def test_conjunction_admission_rolls_a_back_when_b_is_contended() -> None:
    events: list[str] = []

    assert not conjunction_admission(
        7,
        try_lock=lambda fd, offset: events.append(f"lock {fd} {offset}") or offset == 0,
        unlock=lambda fd, offset: events.append(f"unlock {fd} {offset}"),
    )

    assert events == ["lock 7 0", "lock 7 1", "unlock 7 0"]


def test_conjunction_admission_closes_fd_when_b_unlock_fails() -> None:
    events: list[str] = []

    def unlock(fd: int, offset: int) -> None:
        events.append(f"unlock {fd} {offset}")
        raise OSError("injected B unlock failure")

    with pytest.raises(OSError, match="injected B unlock failure"):
        conjunction_admission(
            7,
            try_lock=lambda fd, offset: events.append(f"lock {fd} {offset}") or True,
            unlock=unlock,
            close=lambda fd: events.append(f"close {fd}"),
        )

    assert events == ["lock 7 0", "lock 7 1", "unlock 7 1", "close 7"]


def test_b_lock_error_preserves_first_error_when_a_rollback_and_close_fail() -> None:
    events: list[str] = []
    lock_error = OSError("B lock failed")

    def try_lock(_fd: int, offset: int) -> bool:
        events.append(f"lock {offset}")
        if offset == 1:
            raise lock_error
        return True

    def unlock(_fd: int, offset: int) -> None:
        events.append(f"unlock {offset}")
        raise OSError("A unlock failed")

    def close(_fd: int) -> None:
        events.append("close")
        raise OSError("close failed")

    with pytest.raises(OSError) as raised:
        conjunction_admission(7, try_lock=try_lock, unlock=unlock, close=close)

    assert raised.value is lock_error
    assert events == ["lock 0", "lock 1", "unlock 0", "close"]


def test_b_contention_reports_a_unlock_error_and_attempts_close() -> None:
    events: list[str] = []
    unlock_error = OSError("A unlock failed")

    def unlock(_fd: int, offset: int) -> None:
        events.append(f"unlock {offset}")
        raise unlock_error

    with pytest.raises(OSError) as raised:
        conjunction_admission(
            7,
            try_lock=lambda _fd, offset: offset == 0,
            unlock=unlock,
            close=lambda _fd: (
                events.append("close") or (_ for _ in ()).throw(OSError("close failed"))
            ),
        )

    assert raised.value is unlock_error
    assert events == ["unlock 0", "close"]


def test_b_unlock_error_survives_a_close_error() -> None:
    events: list[str] = []
    unlock_error = OSError("B unlock failed")

    with pytest.raises(OSError) as raised:
        conjunction_admission(
            7,
            try_lock=lambda _fd, _offset: True,
            unlock=lambda _fd, offset: (
                events.append(f"unlock {offset}") or (_ for _ in ()).throw(unlock_error)
            ),
            close=lambda _fd: (
                events.append("close") or (_ for _ in ()).throw(OSError("close failed"))
            ),
        )

    assert raised.value is unlock_error
    assert events == ["unlock 1", "close"]


def _consume_actor_admission(
    *,
    try_lock: Any,
    unlock: Any,
    close: Any,
) -> None:
    descriptor = probe._ActorFd(7, close=close)
    first_error: BaseException | None = None
    try:
        probe._actor_admission(
            descriptor,
            try_lock=try_lock,
            unlock=unlock,
        )
    except BaseException as exc:
        first_error = exc
    finally:
        probe._finish_actor_fd(descriptor, first_error)


def test_actor_preserves_b_unlock_error_without_a_second_close() -> None:
    error = OSError("B unlock failed")
    closes: list[int] = []

    with pytest.raises(OSError) as raised:
        _consume_actor_admission(
            try_lock=lambda _fd, _offset: True,
            unlock=lambda _fd, offset: (
                (_ for _ in ()).throw(error) if offset == 1 else None
            ),
            close=closes.append,
        )

    assert raised.value is error
    assert str(raised.value) == "B unlock failed"
    assert closes == [7]


def test_actor_preserves_b_lock_error_when_a_unlock_also_fails() -> None:
    lock_error = OSError("B lock failed")
    closes: list[int] = []

    def try_lock(_fd: int, offset: int) -> bool:
        if offset == 1:
            raise lock_error
        return True

    with pytest.raises(OSError) as raised:
        _consume_actor_admission(
            try_lock=try_lock,
            unlock=lambda _fd, _offset: (_ for _ in ()).throw(
                OSError("A unlock failed")
            ),
            close=closes.append,
        )

    assert raised.value is lock_error
    assert str(raised.value) == "B lock failed"
    assert closes == [7]


def test_actor_preserves_a_unlock_error_after_b_contention() -> None:
    unlock_error = OSError("A unlock failed")
    closes: list[int] = []

    with pytest.raises(OSError) as raised:
        _consume_actor_admission(
            try_lock=lambda _fd, offset: offset == 0,
            unlock=lambda _fd, _offset: (_ for _ in ()).throw(unlock_error),
            close=closes.append,
        )

    assert raised.value is unlock_error
    assert str(raised.value) == "A unlock failed"
    assert closes == [7]


def test_actor_retries_failed_rescue_close_during_final_cleanup() -> None:
    unlock_error = OSError("B unlock failed")
    closes: list[int] = []

    def close(fd: int) -> None:
        closes.append(fd)
        if len(closes) == 1:
            raise OSError("rescue close failed")

    with pytest.raises(OSError) as raised:
        _consume_actor_admission(
            try_lock=lambda _fd, _offset: True,
            unlock=lambda _fd, _offset: (_ for _ in ()).throw(unlock_error),
            close=close,
        )

    assert raised.value is unlock_error
    assert str(raised.value) == "B unlock failed"
    assert closes == [7, 7]


def test_attempt_here_preserves_admission_error_when_fd_was_rescue_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_error = OSError("admission failed")
    closes: list[int] = []
    monkeypatch.setattr(probe, "_probe_fd", lambda _path: 7)
    monkeypatch.setattr(
        probe,
        "conjunction_admission",
        lambda _fd: (_ for _ in ()).throw(first_error),
    )

    def close(fd: int) -> None:
        closes.append(fd)
        raise OSError("already closed")

    monkeypatch.setattr(os, "close", close)

    with pytest.raises(OSError) as raised:
        probe._attempt_here(Path("profile.lock"))

    assert raised.value is first_error
    assert closes == [7]


def test_lock_rundown_retries_only_contention_and_measures_progress() -> None:
    attempts = iter([None, None, "acquired"])
    waits: list[str] = []
    clock = iter([10.0, 10.1, 10.2, 10.3])

    result, attempt_count, duration = retry_lock_rundown(
        lambda: next(attempts),
        deadline=20.0,
        wait_for_retry=lambda: waits.append("wait"),
        monotonic=lambda: next(clock),
    )

    assert result == "acquired"
    assert attempt_count == 3
    assert duration == pytest.approx(0.3)
    assert waits == ["wait", "wait"]

    failure = OSError("non-contention LockFileEx error")
    with pytest.raises(OSError) as raised:
        retry_lock_rundown(
            lambda: (_ for _ in ()).throw(failure),
            deadline=20.0,
            wait_for_retry=lambda: waits.append("unexpected wait"),
            monotonic=lambda: 10.0,
        )
    assert raised.value is failure
    assert "unexpected wait" not in waits


def test_post_harness_admission_retries_until_a_and_b_are_available() -> None:
    admissions = iter([False, False, True])
    opened = iter([10, 11, 12])
    events: list[str] = []
    clock = iter([1.0, 1.1, 1.2, 1.3])

    acquired, attempts, duration = probe.retry_admission_after_drain(
        open_fd=lambda: next(opened),
        try_admission=lambda fd, _close: (
            events.append(f"admit {fd}") or next(admissions)
        ),
        release_a=lambda fd: events.append(f"release A {fd}"),
        close_fd=lambda fd: events.append(f"close {fd}"),
        deadline=2.0,
        wait_for_retry=lambda: events.append("wait"),
        monotonic=lambda: next(clock),
    )

    assert acquired is True
    assert attempts == 3
    assert duration == pytest.approx(0.3)
    assert events == [
        "admit 10",
        "close 10",
        "wait",
        "admit 11",
        "close 11",
        "wait",
        "admit 12",
        "release A 12",
        "close 12",
    ]


def _retry_admission_failure(
    *,
    try_admission: Any,
    release_a: Any = lambda _fd: None,
    close_fd: Any,
) -> None:
    probe.retry_admission_after_drain(
        open_fd=lambda: 7,
        try_admission=try_admission,
        release_a=release_a,
        close_fd=close_fd,
        deadline=2.0,
        wait_for_retry=lambda: pytest.fail("non-contention error was retried"),
        monotonic=lambda: 1.0,
    )


def test_retry_preserves_b_unlock_error_without_double_close() -> None:
    error = OSError("B unlock failed")
    closes: list[int] = []

    with pytest.raises(OSError) as raised:
        _retry_admission_failure(
            try_admission=lambda fd, close: conjunction_admission(
                fd,
                try_lock=lambda _fd, _offset: True,
                unlock=lambda _fd, _offset: (_ for _ in ()).throw(error),
                close=close,
            ),
            close_fd=closes.append,
        )

    assert raised.value is error
    assert str(raised.value) == "B unlock failed"
    assert closes == [7]


def test_retry_preserves_b_lock_error_when_a_unlock_also_fails() -> None:
    lock_error = OSError("B lock failed")
    closes: list[int] = []

    def try_lock(_fd: int, offset: int) -> bool:
        if offset == 1:
            raise lock_error
        return True

    with pytest.raises(OSError) as raised:
        _retry_admission_failure(
            try_admission=lambda fd, close: conjunction_admission(
                fd,
                try_lock=try_lock,
                unlock=lambda _fd, _offset: (_ for _ in ()).throw(
                    OSError("A unlock failed")
                ),
                close=close,
            ),
            close_fd=closes.append,
        )

    assert raised.value is lock_error
    assert str(raised.value) == "B lock failed"
    assert closes == [7]


def test_retry_preserves_a_unlock_error_after_b_contention() -> None:
    unlock_error = OSError("A unlock failed")
    closes: list[int] = []

    with pytest.raises(OSError) as raised:
        _retry_admission_failure(
            try_admission=lambda fd, close: conjunction_admission(
                fd,
                try_lock=lambda _fd, offset: offset == 0,
                unlock=lambda _fd, _offset: (_ for _ in ()).throw(unlock_error),
                close=close,
            ),
            close_fd=closes.append,
        )

    assert raised.value is unlock_error
    assert str(raised.value) == "A unlock failed"
    assert closes == [7]


def test_retry_preserves_release_a_error() -> None:
    release_error = OSError("release A failed")
    closes: list[int] = []

    with pytest.raises(OSError) as raised:
        _retry_admission_failure(
            try_admission=lambda _fd, _close: True,
            release_a=lambda _fd: (_ for _ in ()).throw(release_error),
            close_fd=closes.append,
        )

    assert raised.value is release_error
    assert str(raised.value) == "release A failed"
    assert closes == [7]


def test_retry_retries_failed_rescue_close_without_masking_lock_error() -> None:
    unlock_error = OSError("B unlock failed")
    closes: list[int] = []

    def close(fd: int) -> None:
        closes.append(fd)
        if len(closes) == 1:
            raise OSError("rescue close failed")

    with pytest.raises(OSError) as raised:
        _retry_admission_failure(
            try_admission=lambda fd, tracked_close: conjunction_admission(
                fd,
                try_lock=lambda _fd, _offset: True,
                unlock=lambda _fd, _offset: (_ for _ in ()).throw(unlock_error),
                close=tracked_close,
            ),
            close_fd=close,
        )

    assert raised.value is unlock_error
    assert str(raised.value) == "B unlock failed"
    assert closes == [7, 7]


def test_actor_locks_before_checking_identity_and_current_path() -> None:
    events: list[str] = ["open path"]
    descriptor = probe._ActorFd(7, close=lambda _fd: events.append("close"))

    assert acquire_actor_region(
        descriptor,
        Path("profile.lock"),
        [1, 2, 3],
        1,
        try_lock=lambda _fd, _offset: events.append("lock B") or True,
        unlock=lambda _fd, _offset: events.append("unlock B"),
        identity=lambda _fd: events.append("identity") or (1, 2, 3),
        still_at=lambda _fd, _path: events.append("path current") or True,
    )
    assert events == ["open path", "lock B", "identity", "path current"]


def test_production_protocol_actor_targets_only_byte_zero() -> None:
    events: list[str] = []
    descriptor = probe._ActorFd(7, close=lambda _fd: events.append("close"))

    assert production_byte_zero_admission(
        descriptor,
        Path("profile.lock"),
        [1, 2, 3],
        try_lock=lambda fd, offset: events.append(f"lock {fd} {offset}") or True,
        unlock=lambda fd, offset: events.append(f"unlock {fd} {offset}"),
        identity=lambda _fd: events.append("identity") or (1, 2, 3),
        still_at=lambda _fd, _path: events.append("path current") or True,
    )

    assert events == ["lock 7 0", "identity", "path current"]


def test_actor_identity_mismatch_unlocks_and_closes_without_publication() -> None:
    events: list[str] = ["open path"]
    descriptor = probe._ActorFd(7, close=lambda _fd: events.append("close"))

    with pytest.raises(RuntimeError, match="same file identity"):
        acquire_actor_region(
            descriptor,
            Path("profile.lock"),
            [1, 2, 3],
            1,
            try_lock=lambda _fd, _offset: events.append("lock B") or True,
            unlock=lambda _fd, _offset: events.append("unlock B"),
            identity=lambda _fd: events.append("identity") or (1, 2, 4),
            still_at=lambda _fd, _path: events.append("path current") or True,
        )

    assert descriptor.fd == -1
    assert events == ["open path", "lock B", "identity", "unlock B", "close"]
    assert "publish ARMED" not in events


def test_real_self_handle_is_used_for_job_membership_and_closed() -> None:
    events: list[str] = []

    class Handle:
        def Close(self) -> None:
            events.append("close real self")

    real_self = Handle()

    class Win32Api:
        @staticmethod
        def GetCurrentProcess() -> str:
            events.append("get pseudo")
            return "pseudo"

        @staticmethod
        def DuplicateHandle(*args: Any) -> Handle:
            events.append(f"duplicate {args}")
            return real_self

    class Win32Con:
        DUPLICATE_SAME_ACCESS = 2

    class Win32Job:
        @staticmethod
        def IsProcessInJob(process: Any, job: str) -> bool:
            events.append(f"query {process is real_self} {process} {job}")
            assert process != "pseudo"
            return False

    assert query_control_job_membership(
        "browser-job",
        "owner-handle",
        win32api=Win32Api,
        win32con=Win32Con,
        win32job=Win32Job,
    ) == (False, False)
    assert events[-1] == "close real self"
    assert "query True" in events[-3]


def test_terminate_process_uses_popen_handle_without_pid_reopen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = object()
    events: list[str] = []
    active = iter([True, False])

    class Process:
        _handle = handle

        @property
        def pid(self) -> int:
            pytest.fail("PID was consulted")

        def wait(self, *, timeout: float) -> int:
            events.append(f"popen wait {timeout}")
            return 203

    class Win32Api:
        @staticmethod
        def OpenProcess(*_args: Any) -> None:
            pytest.fail("OpenProcess was called")

        @staticmethod
        def TerminateProcess(observed: Any, code: int) -> None:
            assert observed is handle
            events.append(f"terminate {code}")

    monkeypatch.setattr(
        probe,
        "_windows_modules",
        lambda: (Win32Api, object(), object(), object()),
    )
    monkeypatch.setattr(
        probe,
        "_is_active",
        lambda observed: events.append(f"active {observed is handle}") or next(active),
    )
    monkeypatch.setattr(
        probe,
        "_wait",
        lambda observed, timeout, message: events.append(
            f"wait {observed is handle} {timeout} {message}"
        ),
    )

    probe._terminate_process(Process())

    assert events == [
        "active True",
        "terminate 203",
        "wait True 30.0 process did not terminate",
        "active True",
        "popen wait 30.0",
    ]


def test_actor_duplicates_every_inherited_handle_and_closes_parent_copies() -> None:
    events: list[str] = []

    class Duplicate:
        def __init__(self, source: int) -> None:
            self.source = source

        def __int__(self) -> int:
            return self.source + 100

    def duplicate(source: int) -> Duplicate:
        events.append(f"duplicate {source}")
        return Duplicate(source)

    def build(mapping: dict[int, int]) -> list[str]:
        events.append(f"build {mapping}")
        return [str(mapping[7]), str(mapping[8])]

    def launch(arguments: list[str], handles: list[int]) -> str:
        events.append(f"launch {arguments} {handles}")
        assert handles == [107, 108]
        assert 7 not in handles and 8 not in handles
        return "process"

    assert (
        spawn_with_duplicated_handles(
            [7, 8],
            build_arguments=build,
            duplicate=duplicate,
            launch=launch,
            close_duplicate=lambda handle: events.append(
                f"close duplicate {int(handle)}"
            ),
        )
        == "process"
    )
    assert events == [
        "duplicate 7",
        "duplicate 8",
        "build {7: 107, 8: 108}",
        "launch ['107', '108'] [107, 108]",
        "close duplicate 107",
        "close duplicate 108",
    ]


def test_actor_never_inherits_internal_source_handles_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Duplicate:
        def __init__(self, value: int) -> None:
            self.value = value

        def __int__(self) -> int:
            return self.value

        def Close(self) -> None:
            events.append(f"close {self.value}")

    class Win32Api:
        @staticmethod
        def GetCurrentProcess() -> str:
            return "current"

        @staticmethod
        def DuplicateHandle(
            source_process: str,
            source: int,
            target_process: str,
            access: int,
            inheritable: bool,
            options: int,
        ) -> Duplicate:
            events.append(
                f"duplicate {source_process} {source} {target_process} "
                f"{access} {inheritable} {options}"
            )
            return Duplicate(source + 100)

    class Win32Con:
        DUPLICATE_SAME_ACCESS = 2

    def launch(arguments: list[str], handles: list[int]) -> str:
        events.append(f"launch {arguments[-5:]} {handles}")
        assert handles == [108]
        assert "profile.lock" in arguments
        assert "[1, 2, 3]" in arguments
        assert '"owner_handle": 108' in arguments[-1]
        return "process"

    monkeypatch.setattr(
        probe,
        "_windows_modules",
        lambda: (Win32Api, Win32Con, object(), object()),
    )
    monkeypatch.setattr(probe, "_spawn_inheriting", launch)

    assert (
        probe._spawn_actor(
            "guardian-publish",
            Path("profile.lock"),
            [1, 2, 3],
            "ready",
            Path("result.json"),
            {"owner_handle": 8},
            [8],
        )
        == "process"
    )
    assert all(" 7 " not in event for event in events)
    assert events[-1:] == ["close 108"]


def test_owner_watch_opens_descendant_handles_before_terminating_job() -> None:
    events: list[str] = []

    assert open_descendant_handles_before_terminate(
        open_handles=lambda: events.append("open handles") or ["one", "two"],
        terminate_job=lambda: events.append("terminate job"),
    ) == ["one", "two"]
    assert events == ["open handles", "terminate job"]


def test_browser_gate_opens_only_after_armed_observation() -> None:
    events: list[str] = []
    started = False

    def browser_started() -> bool:
        events.append("check started")
        return started

    def release_gate() -> None:
        nonlocal started
        events.append("release gate")
        started = True

    assert observe_browser_publication_order(
        browser_started=browser_started,
        observe_armed=lambda: events.append("observe ARMED"),
        release_gate=release_gate,
        observe_browser_start=lambda: events.append("observe browser start"),
    )
    assert events == [
        "check started",
        "observe ARMED",
        "check started",
        "release gate",
        "observe browser start",
    ]


def test_by_handle_file_information_matches_win32_layout() -> None:
    fields = probe._ByHandleFileInformation._fields_
    assert [name for name, _field_type in fields] == [
        "dwFileAttributes",
        "ftCreationTime",
        "ftLastAccessTime",
        "ftLastWriteTime",
        "dwVolumeSerialNumber",
        "nFileSizeHigh",
        "nFileSizeLow",
        "nNumberOfLinks",
        "nFileIndexHigh",
        "nFileIndexLow",
    ]

    cursor = 0
    maximum_alignment = 1
    expected_offsets: dict[str, int] = {}
    for name, field_type in fields:
        alignment = ctypes.alignment(field_type)
        maximum_alignment = max(maximum_alignment, alignment)
        cursor = (cursor + alignment - 1) // alignment * alignment
        expected_offsets[name] = cursor
        cursor += ctypes.sizeof(field_type)
    expected_size = (
        (cursor + maximum_alignment - 1) // maximum_alignment * maximum_alignment
    )

    assert expected_offsets == {
        "dwFileAttributes": 0,
        "ftCreationTime": 4,
        "ftLastAccessTime": 12,
        "ftLastWriteTime": 20,
        "dwVolumeSerialNumber": 28,
        "nFileSizeHigh": 32,
        "nFileSizeLow": 36,
        "nNumberOfLinks": 40,
        "nFileIndexHigh": 44,
        "nFileIndexLow": 48,
    }
    assert {
        name: getattr(probe._ByHandleFileInformation, name).offset
        for name, _field_type in fields
    } == expected_offsets
    assert expected_size == 52
    assert ctypes.sizeof(probe._ByHandleFileInformation) == expected_size


def test_file_identity_uses_complete_file_information_fields() -> None:
    info = probe._ByHandleFileInformation()
    info.ftCreationTime.dwLowDateTime = 11
    info.ftLastAccessTime.dwLowDateTime = 22
    info.ftLastWriteTime.dwLowDateTime = 33
    info.dwVolumeSerialNumber = 0xAABBCCDD
    info.nFileIndexHigh = 0x11223344
    info.nFileIndexLow = 0x55667788

    assert probe._identity_from_file_information(info) == (
        0xAABBCCDD,
        0x11223344,
        0x55667788,
    )


def test_conjunction_requires_self_open_post_lock_identity() -> None:
    require_same_file_identity((1, 2, 3), [1, 2, 3])
    with pytest.raises(RuntimeError, match="same file identity"):
        require_same_file_identity((1, 2, 3), [1, 2, 4])


def test_guardian_arms_only_after_b_and_a_live_owner() -> None:
    events: list[str] = []

    assert guardian_publication_sequence(
        acquire_b=lambda: events.append("B") or True,
        owner_alive=lambda: events.append("owner") or True,
        release_b=lambda: events.append("release B"),
        publish_armed=lambda: events.append("ARMED"),
    )
    assert events == ["B", "owner", "ARMED"]


def test_guardian_never_checks_owner_or_arms_when_b_is_contended() -> None:
    events: list[str] = []

    assert not guardian_publication_sequence(
        acquire_b=lambda: events.append("B contended") or False,
        owner_alive=lambda: events.append("owner") or True,
        release_b=lambda: events.append("release B"),
        publish_armed=lambda: events.append("ARMED"),
    )
    assert events == ["B contended"]


def test_late_guardian_never_arms_after_owner_exit() -> None:
    events: list[str] = []

    assert not guardian_publication_sequence(
        acquire_b=lambda: events.append("B") or True,
        owner_alive=lambda: events.append("owner dead") or False,
        release_b=lambda: events.append("release B"),
        publish_armed=lambda: events.append("ARMED"),
    )
    assert events == ["B", "owner dead", "release B"]


def test_real_guardian_exception_publishes_and_holds_without_releasing_b() -> None:
    events: list[str] = []
    failure = OSError("real termination failure")

    with pytest.raises(RuntimeError, match="resumed") as raised:
        run_guardian_fail_closed(
            shutdown=lambda: (_ for _ in ()).throw(failure),
            publish_failure=lambda exc: events.append(
                f"publish {type(exc).__name__}: {exc}"
            ),
            hold_failure=lambda: events.append("hold"),
            release_b=lambda: events.append("release B"),
        )

    assert raised.value.__cause__ is failure
    assert events == ["publish OSError: real termination failure", "hold"]


def test_conjunction_shutdown_uses_unsignaled_throttle_until_real_zero() -> None:
    active = iter([2, 0])
    descendants = iter([2, 0])
    throttle = object()
    events: list[str] = []
    result: dict[str, Any] = {}

    conjunction_guardian_shutdown(
        result,
        active_descendants=lambda: next(descendants),
        terminate_job=lambda: events.append("terminate"),
        query_job=lambda: next(active),
        wait_for_retry=lambda: wait_on_unsignaled_throttle(
            throttle,
            wait=lambda waitable, milliseconds: events.append(
                f"wait {waitable is throttle} {milliseconds}"
            ),
        ),
        monotonic=lambda: 0.0,
    )

    assert events == ["terminate", "wait True 1"]
    assert result["terminate_attempted"] is True
    assert result["terminate_completed"] is True
    assert result["query_samples"] == [
        {"active_processes": 2, "active_descendants": 2},
        {"active_processes": 0, "active_descendants": 0},
    ]
    assert result["zero_proven"] is True


def test_conjunction_shutdown_timeout_comes_from_query_deadline_loop() -> None:
    result: dict[str, Any] = {}
    events: list[str] = []
    clock = iter([0.0, 31.0])

    with pytest.raises(TimeoutError, match="did not drain"):
        conjunction_guardian_shutdown(
            result,
            active_descendants=lambda: 0,
            terminate_job=lambda: events.append("terminate"),
            query_job=lambda: events.append("query") or 1,
            wait_for_retry=lambda: events.append("wait"),
            monotonic=lambda: next(clock),
        )

    assert events == ["terminate", "query"]
    assert result["query_timeout"] is True
    assert result["query_samples"] == [{"active_processes": 1, "active_descendants": 0}]


def test_b_remains_held_between_zero_proven_and_release_permission() -> None:
    events: list[str] = []

    zero_proven_release_sequence(
        publish_zero=lambda: events.append("ZERO_PROVEN"),
        wait_allow_release=lambda: events.append("ALLOW_B_RELEASE"),
        close_job=lambda: events.append("close job"),
        release_b=lambda: events.append("release B"),
    )

    assert events == [
        "ZERO_PROVEN",
        "ALLOW_B_RELEASE",
        "close job",
        "release B",
    ]


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
    assert before["guardian_active"] is True
    assert before["owner_active"] is True
    assert before["active_descendants"] == measurement["descendant_count"]
    assert before["browser_job_active_processes"] >= measurement["descendant_count"]
    assert measurement["guardian_outside_owner_job"] is True
    assert measurement["guardian_returncode"] != 0
    assert (
        loss["guardian_termination_requested_ns"]
        < loss["guardian_exit_observed_ns"]
        <= loss["lease_observed_ns"]
    )
    assert (
        loss["guardian_termination_requested_ns"]
        < loss["lease_acquired_ns"]
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


@_WINDOWS_ONLY
def test_falsifies_original_assignment_after_owner_loss(tmp_path: Path) -> None:
    measurement = _run_probe(tmp_path, "falsification-original-owner-loss")
    _record_measurement(measurement)

    assert measurement["assignment"] == {"owner": 0, "guardian": 1, "entrant": 0}
    assert measurement["protocol"] == "current-source-production-byte-zero"
    assert measurement["owner_exit_observed"] is True
    assert measurement["guardian_active_before_entry"] is True
    assert measurement["guardian_active_after_entry"] is True
    assert measurement["browser_active_before_entry"] > 0
    assert measurement["browser_active_after_entry"] > 0
    assert measurement["live_children_before_entry"] > 0
    assert measurement["byte_zero_entrant_acquired"] is True
    assert measurement["unsafe_compatibility_result"] is True


@_WINDOWS_ONLY
def test_falsifies_inverted_assignment_after_guardian_loss(tmp_path: Path) -> None:
    measurement = _run_probe(tmp_path, "falsification-inverted-guardian-loss")
    _record_measurement(measurement)

    assert measurement["assignment"] == {"owner": 1, "guardian": 0, "entrant": 0}
    assert measurement["protocol"] == "current-source-production-byte-zero"
    assert measurement["guardian_exit_observed"] is True
    assert measurement["owner_cleanup_paused"] is True
    assert measurement["browser_active_before_entry"] > 0
    assert measurement["browser_active_after_entry"] > 0
    assert measurement["live_children_before_entry"] > 0
    assert measurement["byte_zero_entrant_acquired"] is True
    assert measurement["unsafe_compatibility_result"] is True


@_WINDOWS_ONLY
def test_falsifies_inverted_pre_arm_window(tmp_path: Path) -> None:
    measurement = _run_probe(tmp_path, "falsification-inverted-pre-arm")
    _record_measurement(measurement)

    assert measurement["assignment"] == {"owner": 1, "guardian": 0, "entrant": 0}
    assert measurement["protocol"] == "current-source-production-byte-zero"
    assert measurement["transient_admission_acquired"] is True
    assert measurement["transient_admission_released"] is True
    assert measurement["byte_zero_entrant_acquired_before_guardian_arm"] is True
    assert measurement["guardian_contention"] is True
    assert measurement["guardian_armed"] is False
    assert measurement["armed_event_unpublished"] is True
    assert measurement["guardian_job_authority"] is False
    assert measurement["guardian_browser_authority"] is False
    assert measurement["unsafe_compatibility_result"] is True


@_WINDOWS_ONLY
def test_falsifies_inverted_post_disarm_mutation_window(tmp_path: Path) -> None:
    measurement = _run_probe(tmp_path, "falsification-inverted-post-disarm-mutation")
    _record_measurement(measurement)

    assert measurement["assignment"] == {"owner": 1, "guardian": 0, "entrant": 0}
    assert measurement["protocol"] == "current-source-production-byte-zero"
    assert measurement["guardian_disarm_observed"] is True
    assert measurement["outer_mutation_active_after_disarm"] is True
    assert measurement["owner_active_during_mutation"] is True
    assert measurement["byte_zero_entrant_acquired"] is True
    assert measurement["unsafe_compatibility_result"] is True


@_WINDOWS_ONLY
def test_falsifies_inverted_exclusive_mutation_window(tmp_path: Path) -> None:
    measurement = _run_probe(tmp_path, "falsification-inverted-exclusive-mutation")
    _record_measurement(measurement)

    assert measurement["assignment"] == {"owner": 1, "guardian": None, "entrant": 0}
    assert measurement["protocol"] == "current-source-production-byte-zero"
    assert measurement["owner_active_during_mutation"] is True
    assert measurement["guardian_started"] is False
    assert measurement["byte_zero_entrant_acquired"] is True
    assert measurement["unsafe_compatibility_result"] is True


@_WINDOWS_ONLY
def test_conjunction_lock_regions(tmp_path: Path) -> None:
    measurement = _run_probe(tmp_path, "conjunction-lock-regions")
    _record_measurement(measurement)

    assert measurement["owner_identity"] == measurement["file_identity"]
    assert measurement["guardian_identity"] == measurement["file_identity"]
    assert measurement["blocked_by_a"] is True
    assert measurement["a_acquired_b_blocked_after_owner_exit"] is True
    assert measurement["a_released_after_b_contention"] is True
    assert measurement["owner_rundown_attempts"] >= 1
    assert measurement["owner_rundown_seconds"] >= 0
    assert measurement["guardian_rundown_attempts"] >= 1
    assert measurement["guardian_rundown_seconds"] >= 0
    assert measurement["c_acquired"] is True
    assert measurement["d_blocked_after_b_unlock"] is True
    assert measurement["d_acquired_after_c_close"] is True


@_WINDOWS_ONLY
def test_conjunction_publication(tmp_path: Path) -> None:
    measurement = _run_probe(tmp_path, "conjunction-publication")
    _record_measurement(measurement)

    assert measurement["owner_identity"] == measurement["file_identity"]
    assert measurement["guardian_identity"] == measurement["file_identity"]
    assert measurement["armed"] is True
    assert measurement["b_probe_blocked"] is True
    assert measurement["browser_started_after_armed"] is True
    assert measurement["late_successor_admitted"] is True
    assert measurement["late_guardian_armed"] is False
    assert measurement["conflict_guardian_contention"] is True
    assert measurement["conflict_guardian_armed"] is False
    assert measurement["conflict_guardian_job_authority"] is False
    assert measurement["conflict_guardian_browser_authority"] is False
    assert measurement["conflict_guardian_returncode"] == 0


@_WINDOWS_ONLY
def test_conjunction_owner_loss(tmp_path: Path) -> None:
    measurement = _run_probe(tmp_path, "conjunction-owner-loss")
    _record_measurement(measurement)

    assert measurement["owner_identity"] == measurement["file_identity"]
    assert measurement["guardian_identity"] == measurement["file_identity"]
    assert measurement["outer_in_browser_job"] is False
    assert measurement["owner_in_browser_job"] is False
    assert measurement["guardian_in_browser_job"] is False
    assert measurement["prearmed_rejected"] is True
    assert measurement["zero_proven"] is True
    assert measurement["job_active_at_zero"] == 0
    assert measurement["a_acquired_b_blocked_before_release"] is True
    assert measurement["acquired_after_b_release"] is True


@_WINDOWS_ONLY
def test_conjunction_guardian_loss_clean_close(tmp_path: Path) -> None:
    measurement = _run_probe(tmp_path, "conjunction-guardian-loss-clean-close")
    _record_measurement(measurement)

    assert measurement["outer_in_browser_job"] is False
    assert measurement["owner_in_browser_job"] is False
    assert measurement["guardian_in_browser_job"] is False
    assert measurement["post_exit_attempts_rejected"] is True
    assert measurement["active_processes_before_owner_drain"] > 0
    assert measurement["live_descendants_before_owner_drain"] > 0
    assert measurement["owner_observed_guardian_exit"] is True
    assert measurement["zero_proven"] is True
    assert measurement["blocked_while_a_held_at_zero"] is True
    assert measurement["acquired_after_owner_release"] is True
    assert measurement["respawn_claimed"] is False


@_WINDOWS_ONLY
@pytest.mark.parametrize(
    ("fault", "error_type", "error", "operation"),
    [
        (
            "terminate-error",
            "OSError",
            "OSError: injected browser Job termination failure",
            "terminate",
        ),
        (
            "query-error",
            "OSError",
            "OSError: injected browser Job query failure",
            "query",
        ),
        (
            "drain-timeout",
            "TimeoutError",
            "TimeoutError: browser Job did not drain before its deadline",
            "deadline",
        ),
    ],
)
def test_conjunction_owner_loss_failure_holds_b(
    tmp_path: Path,
    fault: str,
    error_type: str,
    error: str,
    operation: str,
) -> None:
    measurement = _run_probe(tmp_path, f"conjunction-owner-loss-{fault}")
    _record_measurement(measurement)

    assert measurement["outer_in_browser_job"] is False
    assert measurement["owner_in_browser_job"] is False
    assert measurement["guardian_in_browser_job"] is False
    assert measurement["fault"] == fault
    assert measurement["guardian_error_type"] == error_type
    assert measurement["guardian_error"] == error
    assert measurement["fault_operation"] == operation
    assert measurement["terminate_attempted"] is True
    assert measurement["terminate_completed"] is (fault != "terminate-error")
    assert measurement["external_probe_acquired_a"] is True
    assert measurement["external_probe_acquired_b"] is False
    assert measurement["guardian_alive"] is True
    assert measurement["identity_mutex_owned"] is True
    assert measurement["acquired_after_harness_drain"] is True
    assert measurement["post_harness_rundown_attempts"] >= 1
    assert measurement["post_harness_rundown_seconds"] >= 0
    if fault == "drain-timeout":
        assert measurement["query_timeout"] is True
        assert measurement["query_samples"]
    elif fault == "query-error":
        assert measurement["query_samples"] == []
    else:
        assert measurement["query_samples"] == []
