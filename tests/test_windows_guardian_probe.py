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
    classify_breakaway_result,
    classify_post_exit_membership,
    create_topology_holder,
    create_topology_process,
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
    raise_cleanup_error_unless_unwinding,
    read_published_json,
    remaining_wait_milliseconds,
    require_accepted_breakaway_result,
    require_publication_witnesses,
    require_same_file_identity,
    require_successful_actor_completion,
    require_topology_actor_ready,
    retry_contended_publication,
    retry_lock_rundown,
    run_guardian_fail_closed,
    sample_guardian_loss_progress,
    sample_lease_acquisition,
    sample_pre_crash_contention,
    spawn_with_duplicated_handles,
    starter_termination_measurement,
    terminate_common_ancestor,
    terminate_wait_close_handles,
    topology_runner,
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


def _prepare_probe_harness(
    tmp_path: Path, scenario: str
) -> tuple[Path, Any, Any, subprocess.Popen[bytes], str]:
    root = tmp_path / scenario
    topology = scenario.startswith(("job-topology-", "browser-launch-"))
    harness = (
        process_tree.WindowsJob.named("topology-outer")
        if topology
        else process_tree.WindowsJob.anonymous()
    )
    result_event = None
    try:
        if topology:
            root.mkdir(parents=True)
            if harness.name is None:
                raise RuntimeError("the topology harness Job has no name")
            (root / "outer-job.json").write_text(
                json.dumps({"name": harness.name}), encoding="utf-8"
            )
        nonce = process_tree.release_nonce()
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
        return root, harness, result_event, process, nonce
    except BaseException:
        first_error = sys.exception()
        if result_event is not None:
            try:
                result_event.Close()
            except BaseException:
                pass
        try:
            harness.close()
        except BaseException:
            pass
        assert first_error is not None
        raise first_error


def _run_probe(tmp_path: Path, scenario: str) -> dict[str, Any]:
    root, harness, result_event, process, nonce = _prepare_probe_harness(
        tmp_path, scenario
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


def test_named_harness_setup_failure_closes_and_preserves_primary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    setup_error = OSError("metadata write failed")

    class Harness:
        name = "outer"

        def close(self) -> None:
            events.append("close harness")
            raise OSError("cleanup failed")

    monkeypatch.setattr(
        process_tree.WindowsJob,
        "named",
        classmethod(lambda _cls, _label: Harness()),
    )
    monkeypatch.setattr(
        Path,
        "write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(setup_error),
    )

    with pytest.raises(OSError) as raised:
        _prepare_probe_harness(tmp_path, "job-topology-breakaway")

    assert raised.value is setup_error
    assert events == ["close harness"]


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


def test_actor_completion_rejects_nonzero_exit_with_diagnostics() -> None:
    class Process:
        returncode = 7

        def wait(self, *, timeout: float) -> int:
            assert timeout == 3
            return self.returncode

        def communicate(self) -> tuple[bytes, bytes]:
            return b"actor stdout", b"actor stderr"

    with pytest.raises(RuntimeError) as raised:
        require_successful_actor_completion(Process(), "post-entry", timeout=3)

    assert str(raised.value) == (
        "actor failed: phase=post-entry returncode=7 "
        "stdout=b'actor stdout' stderr=b'actor stderr'"
    )


def test_started_topology_actor_pre_ready_failure_is_not_creation_denial() -> None:
    class Process:
        returncode = 9

        def poll(self) -> int:
            return self.returncode

        def communicate(self) -> tuple[bytes, bytes]:
            return b"started", b"failed before ready"

    with pytest.raises(RuntimeError) as raised:
        require_topology_actor_ready(
            Process(),
            "guardian-candidate",
            wait_ready=lambda: (_ for _ in ()).throw(TimeoutError("not ready")),
        )

    assert str(raised.value) == (
        "topology actor failed before ready: phase=guardian-candidate "
        "returncode=9 stdout=b'started' stderr=b'failed before ready'"
    )


def test_topology_creation_classifies_only_popen_failure() -> None:
    events: list[str] = []

    class CreationDenied(OSError):
        winerror = 5

    process, error = create_topology_process(
        lambda: events.append("Popen") or (_ for _ in ()).throw(CreationDenied()),
        register=lambda _process: events.append("register"),
        phase="guardian-candidate-creation",
    )

    assert process is None
    assert error == {
        "operation": "Popen",
        "phase": "guardian-candidate-creation",
        "win32_error": 5,
    }
    assert events == ["Popen"]


def test_topology_creation_registers_before_actor_readiness() -> None:
    process = object()
    events: list[str] = []

    created, error = create_topology_process(
        lambda: events.append("Popen") or process,
        register=lambda observed: events.append(f"register {observed is process}"),
        phase="guardian-candidate-creation",
    )
    events.append("readiness failed")

    assert created is process
    assert error is None
    assert events == ["Popen", "register True", "readiness failed"]


def test_holder_preparation_failure_is_not_a_creation_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PreparationFailed(OSError):
        winerror = 87

    failure = PreparationFailed("invalid holder path")
    events: list[str] = []
    processes: list[subprocess.Popen[bytes]] = []

    def fail_preparation(
        ready_event: str, release_event: str
    ) -> tuple[tuple[str, ...], int]:
        events.append(f"prepare {ready_event} {release_event}")
        raise failure

    def unexpected_popen(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        events.append("Popen")
        raise AssertionError("Popen reached after preparation failure")

    monkeypatch.setattr(probe.subprocess, "Popen", unexpected_popen)
    with pytest.raises(PreparationFailed) as raised:
        create_topology_holder(
            "ready-event",
            "release-event",
            creationflags=0x01000000,
            register=processes.append,
            phase="guardian-candidate-creation",
            prepare=fail_preparation,
        )

    assert raised.value is failure
    assert events == ["prepare ready-event release-event"]
    assert processes == []


def test_common_ancestor_requires_live_actors_and_exact_exit_codes() -> None:
    actors = {"owner": object(), "guardian": object(), "browser-descendant": object()}
    events: list[str] = []

    assert terminate_common_ancestor(
        actors,
        is_active=lambda actor: (
            events.append(
                f"active {next(label for label, value in actors.items() if value is actor)}"
            )
            or True
        ),
        terminate=lambda: events.append("terminate"),
        wait=lambda actor: events.append(
            f"wait {next(label for label, value in actors.items() if value is actor)}"
        ),
        exit_code=lambda _actor: 204,
    ) == {label: 204 for label in actors}
    assert events[:4] == [
        "active owner",
        "active guardian",
        "active browser-descendant",
        "terminate",
    ]

    with pytest.raises(RuntimeError, match="guardian exited before"):
        terminate_common_ancestor(
            actors,
            is_active=lambda actor: actor is not actors["guardian"],
            terminate=lambda: pytest.fail("premature actor exit reached termination"),
            wait=lambda _actor: None,
            exit_code=lambda _actor: 204,
        )

    with pytest.raises(RuntimeError, match="not common-ancestor code 204"):
        terminate_common_ancestor(
            actors,
            is_active=lambda _actor: True,
            terminate=lambda: None,
            wait=lambda _actor: None,
            exit_code=lambda actor: 7 if actor is actors["owner"] else 204,
        )


def test_post_exit_membership_failure_remains_diagnostic() -> None:
    assert classify_post_exit_membership(
        lambda: (_ for _ in ()).throw(OSError("process object no longer queryable"))
    ) == {
        "status": "error",
        "error": "OSError: process object no longer queryable",
    }


def test_contended_publication_rejects_owner_expiry_after_entry() -> None:
    owner = object()
    owner_alive = True

    def attempt() -> str:
        nonlocal owner_alive
        owner_alive = False
        return "published"

    with pytest.raises(RuntimeError, match="owner exited during entrant publication"):
        retry_contended_publication(
            attempt,
            require_window=lambda: require_publication_witnesses(
                {"owner": owner},
                is_active=lambda handle: handle is owner and owner_alive,
            ),
            deadline=2.0,
            wait_for_retry=lambda: pytest.fail("a successful attempt was retried"),
            monotonic=lambda: 1.0,
        )


def test_contended_publication_discards_result_after_witness_failure() -> None:
    witness_error = RuntimeError("guardian exited")
    cleanup_error = OSError("descriptor close failed")
    witness_checks = 0
    closes: list[int] = []

    def require_window() -> None:
        nonlocal witness_checks
        witness_checks += 1
        if witness_checks == 2:
            raise witness_error

    def discard_result(descriptor: int) -> None:
        closes.append(descriptor)
        raise cleanup_error

    with pytest.raises(RuntimeError) as raised:
        retry_contended_publication(
            lambda: 7,
            require_window=require_window,
            deadline=2.0,
            wait_for_retry=lambda: pytest.fail("an acquired result was retried"),
            discard_result=discard_result,
            monotonic=lambda: 1.0,
        )

    assert raised.value is witness_error
    assert closes == [7]


def test_post_entry_child_witness_must_match_pre_entry_handle() -> None:
    first = object()
    replacement = object()
    live = {first}
    children_before = require_publication_witnesses(
        {},
        is_active=lambda handle: handle in live,
        child_handles=[first, replacement],
    )
    live = {replacement}

    with pytest.raises(RuntimeError, match="no pre-entry child process survived"):
        require_publication_witnesses(
            {},
            is_active=lambda handle: handle in live,
            child_handles=[first, replacement],
            required_children=children_before,
        )


def test_contended_publication_rechecks_window_for_each_attempt() -> None:
    attempts = iter([None, "published"])
    events: list[str] = []
    clock = iter([1.0, 1.1, 1.2])

    result, attempt_count, duration = retry_contended_publication(
        lambda: events.append("attempt") or next(attempts),
        require_window=lambda: events.append("witness"),
        deadline=2.0,
        wait_for_retry=lambda: events.append("wait"),
        monotonic=lambda: next(clock),
    )

    assert result == "published"
    assert attempt_count == 2
    assert duration == pytest.approx(0.2)
    assert events == [
        "witness",
        "attempt",
        "witness",
        "wait",
        "witness",
        "attempt",
        "witness",
    ]


def test_contended_publication_rejects_window_loss_before_retry() -> None:
    guardian = object()
    guardian_alive = True
    attempts = 0

    def attempt() -> None:
        nonlocal attempts
        attempts += 1
        return None

    def require_window() -> None:
        require_publication_witnesses(
            {"guardian": guardian},
            is_active=lambda handle: handle is guardian and guardian_alive,
        )

    def wait_for_retry() -> None:
        nonlocal guardian_alive
        guardian_alive = False

    with pytest.raises(
        RuntimeError, match="guardian exited during entrant publication"
    ):
        retry_contended_publication(
            attempt,
            require_window=require_window,
            deadline=2.0,
            wait_for_retry=wait_for_retry,
            monotonic=lambda: 1.0,
        )

    assert attempts == 1


def test_cleanup_error_does_not_replace_active_body_error() -> None:
    body_error = RuntimeError("body failed")
    cleanup_error = OSError("cleanup failed")

    raise_cleanup_error_unless_unwinding(cleanup_error, body_error)
    with pytest.raises(OSError) as raised:
        raise_cleanup_error_unless_unwinding(cleanup_error, None)
    assert raised.value is cleanup_error


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


def test_breakaway_classification_distinguishes_inner_from_all_known_jobs() -> None:
    assert (
        classify_breakaway_result(
            process_created=True,
            memberships={"inner": False, "outer": True},
            error_code=None,
        )
        == "inner-only-breakaway"
    )
    assert (
        classify_breakaway_result(
            process_created=True,
            memberships={"inner": False, "outer": False},
            error_code=None,
        )
        == "all-known-jobs-breakaway"
    )
    assert (
        classify_breakaway_result(
            process_created=True,
            memberships={"inner": True, "outer": True},
            error_code=None,
        )
        == "retained-in-inner-and-outer"
    )
    assert (
        classify_breakaway_result(
            process_created=False,
            memberships=None,
            error_code=5,
        )
        == "could-not-start"
    )


def test_breakaway_acceptance_is_independent_of_complete_classification() -> None:
    assert (
        require_accepted_breakaway_result(
            "job-topology-breakaway",
            inner_breakaway_enabled=True,
            process_created=True,
            memberships={"inner": False, "outer": True},
            creation_error=None,
        )
        == "inner-only-breakaway"
    )
    assert (
        require_accepted_breakaway_result(
            "job-topology-breakaway-denied",
            inner_breakaway_enabled=False,
            process_created=False,
            memberships=None,
            creation_error={
                "operation": "Popen",
                "phase": "guardian-candidate-creation",
                "win32_error": 5,
            },
        )
        == "could-not-start"
    )
    assert (
        require_accepted_breakaway_result(
            "job-topology-breakaway-denied",
            inner_breakaway_enabled=False,
            process_created=True,
            memberships={"inner": True, "outer": True},
            creation_error=None,
        )
        == "retained-in-inner-and-outer"
    )


@pytest.mark.parametrize(
    ("scenario", "enabled", "memberships", "message"),
    [
        (
            "job-topology-breakaway",
            True,
            {"inner": False, "outer": False},
            "escaped every known harness Job",
        ),
        (
            "job-topology-breakaway-denied",
            False,
            {"inner": True, "outer": False},
            "retained the inner Job but escaped the outer Job",
        ),
        (
            "job-topology-breakaway-denied",
            False,
            {"inner": False, "outer": True},
            "not accepted for this scenario",
        ),
    ],
)
def test_breakaway_acceptance_rejects_invalid_harness_outcomes(
    scenario: str, enabled: bool, memberships: dict[str, bool], message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        require_accepted_breakaway_result(
            scenario,
            inner_breakaway_enabled=enabled,
            process_created=True,
            memberships=memberships,
            creation_error=None,
        )


def test_breakaway_acceptance_rejects_configuration_and_fake_refusal() -> None:
    with pytest.raises(RuntimeError, match="does not match"):
        require_accepted_breakaway_result(
            "job-topology-breakaway",
            inner_breakaway_enabled=False,
            process_created=True,
            memberships={"inner": False, "outer": True},
            creation_error=None,
        )
    with pytest.raises(RuntimeError, match="did not come from Popen"):
        require_accepted_breakaway_result(
            "job-topology-breakaway-denied",
            inner_breakaway_enabled=False,
            process_created=False,
            memberships=None,
            creation_error={
                "operation": "event-setup",
                "phase": "guardian-candidate-creation",
                "win32_error": 5,
            },
        )


def test_breakaway_classification_requires_complete_membership_and_error_evidence() -> (
    None
):
    with pytest.raises(RuntimeError, match="inner and outer"):
        classify_breakaway_result(
            process_created=True,
            memberships={"inner": False},
            error_code=None,
        )
    with pytest.raises(RuntimeError, match="no Win32 error"):
        classify_breakaway_result(
            process_created=False,
            memberships=None,
            error_code=None,
        )


def test_topology_scenarios_route_without_changing_existing_probe_families() -> None:
    assert topology_runner("job-topology-breakaway") == "job-topology"
    assert topology_runner("conjunction-owner-loss") == "conjunction"
    assert topology_runner("falsification-original-owner-loss") == (
        "region-falsification"
    )
    assert topology_runner("baseline") == "crash-fence"


def test_win32_error_extraction_preserves_the_primary_failure() -> None:
    class Win32Failure(OSError):
        winerror = 5

    failure = Win32Failure("assignment denied")
    assert probe._win32_error_code(failure) == 5
    with pytest.raises(RuntimeError) as raised:
        probe._win32_error_code(OSError("missing code"))
    assert raised.value.__cause__ is not None


@_WINDOWS_ONLY
@pytest.mark.parametrize(
    "scenario",
    [
        "job-topology-self-assignment",
        "job-topology-breakaway",
        "job-topology-breakaway-denied",
        "job-topology-common-ancestor-loss",
    ],
)
def test_job_topology_scenarios_are_native_only(tmp_path: Path, scenario: str) -> None:
    measurement = _run_probe(tmp_path, scenario)
    _record_measurement(measurement)

    assert measurement["scenario"] == scenario
    assert measurement["actor_in_outer_before_start_gate"] is True

    if scenario == "job-topology-self-assignment":
        before = measurement["actor_memberships_before_assignment"]
        after = measurement["actor_memberships_after_assignment"]
        child = measurement["post_assignment_child"]
        assert before == {"inner": False, "outer": True}
        assert child["created"] is True
        assert child["memberships"]["outer"] is True
        if measurement["self_assignment_succeeded"]:
            assert measurement["self_assignment_error"] is None
            assert after == {"inner": True, "outer": True}
            assert child["memberships"]["inner"] is True
        else:
            assert isinstance(measurement["self_assignment_error"], int)
            assert after == {"inner": False, "outer": True}
            assert child["memberships"]["inner"] is False
        return

    if scenario == "job-topology-common-ancestor-loss":
        before = measurement["memberships_before_termination"]
        after = measurement["post_exit_observations"]
        assert set(before) == {"owner", "guardian", "browser-descendant"}
        assert all(
            memberships == {"inner": True, "outer": True}
            for memberships in before.values()
        )
        assert all(not actor["active"] for actor in after.values())
        assert all(code == 204 for code in measurement["exit_codes"].values())
        assert all(
            actor["membership_diagnostic"]["status"] in {"success", "error"}
            for actor in after.values()
        )
        assert all(
            observed_ns > measurement["common_ancestor_termination_requested_ns"]
            for observed_ns in measurement["exit_observed_ns"].values()
        )
        assert measurement["browser_descendant_drained"] is True
        assert measurement["structural_counterexample"] is True
        return

    owner = measurement["owner_memberships_after_assignment"]
    ordinary = measurement["ordinary_child"]
    guardian = measurement["guardian_candidate"]
    assert measurement["owner_assignment_succeeded"] is True
    assert owner == {"inner": True, "outer": True}
    assert ordinary == {
        "created": True,
        "memberships": {"inner": True, "outer": True},
    }
    assert guardian["classification"] == require_accepted_breakaway_result(
        scenario,
        inner_breakaway_enabled=measurement["inner_breakaway_enabled"],
        process_created=guardian["created"],
        memberships=guardian["memberships"],
        creation_error=guardian["creation_error"],
    )
    if guardian["created"]:
        assert guardian["creation_error"] is None
    else:
        assert guardian["creation_error"]["operation"] == "Popen"
        assert isinstance(guardian["creation_error"]["win32_error"], int)


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
    assert measurement["live_children_after_entry"] > 0
    assert measurement["same_child_live_before_and_after_entry"] is True
    assert measurement["entrant_rundown_attempts"] >= 1
    assert measurement["entrant_rundown_seconds"] >= 0
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
    assert measurement["live_children_after_entry"] > 0
    assert measurement["same_child_live_before_and_after_entry"] is True
    assert measurement["entrant_rundown_attempts"] >= 1
    assert measurement["entrant_rundown_seconds"] >= 0
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
    assert measurement["late_successor_attempts"] >= 1
    assert measurement["late_successor_seconds"] >= 0
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


class TestBrowserLaunchEvidenceHelpers:
    def test_role_normalization_preserves_unknown_and_rejects_conflicts(self) -> None:
        assert probe.normalize_cdp_processes(
            [
                {"id": 11, "type": "browser"},
                {"id": 12, "type": "renderer"},
                {"id": 13, "type": "future-service"},
            ]
        ) == {11: "browser", 12: "renderer", 13: "unknown:future-service"}
        with pytest.raises(RuntimeError, match="conflicting roles"):
            probe.normalize_cdp_processes(
                [
                    {"id": 11, "type": "browser"},
                    {"id": 11, "type": "renderer"},
                ]
            )

    def test_browser_and_renderer_are_mandatory_and_cdp_is_a_subset(self) -> None:
        with pytest.raises(RuntimeError, match="required roles"):
            probe.require_browser_inventory({2: "browser"}, {1, 2}, 1)
        with pytest.raises(RuntimeError, match="absent from"):
            probe.require_browser_inventory({2: "browser", 3: "renderer"}, {1, 2}, 1)
        result = probe.require_browser_inventory(
            {2: "browser", 3: "renderer", 4: "unknown:new"}, {1, 2, 3, 4, 5}, 1
        )
        assert result["unclassified_job_pids"] == [1, 5]
        assert result["cdp_processes"][-1] == {"pid": 4, "role": "unknown:new"}

    def test_census_retries_churn_and_closes_provisional_handles(self) -> None:
        jobs = iter([{1, 2}, {1, 3}, {1, 2, 3}, {1, 2, 3}])
        cdp = iter(
            [
                [{"id": 2, "type": "browser"}, {"id": 3, "type": "renderer"}],
                [{"id": 2, "type": "browser"}, {"id": 3, "type": "renderer"}],
                [{"id": 2, "type": "browser"}, {"id": 3, "type": "renderer"}],
                [{"id": 2, "type": "browser"}, {"id": 3, "type": "renderer"}],
            ]
        )
        closed: list[int] = []
        waits: list[str] = []
        normalized, handles = probe.retain_stable_browser_inventory(
            sample_cdp=lambda: next(cdp),
            sample_job_pids=lambda: next(jobs),
            open_handle=lambda pid: pid,
            validate_handle=lambda _pid, _handle: None,
            close_handle=closed.append,
            driver_pid=1,
            deadline=10,
            wait_for_retry=lambda: waits.append("wait"),
            monotonic=lambda: 0,
        )
        assert normalized == {2: "browser", 3: "renderer"}
        assert handles == {1: 1, 2: 2, 3: 3}
        assert closed == [1, 2]
        assert waits == ["wait"]

    def test_open_failure_invalidates_the_whole_census(self) -> None:
        closed: list[int] = []
        error = OSError("OpenProcess failed")
        with pytest.raises(OSError) as raised:
            probe.retain_stable_browser_inventory(
                sample_cdp=lambda: [
                    {"id": 2, "type": "browser"},
                    {"id": 3, "type": "renderer"},
                ],
                sample_job_pids=lambda: {1, 2, 3},
                open_handle=lambda pid: (
                    (_ for _ in ()).throw(error) if pid == 2 else pid
                ),
                validate_handle=lambda _pid, _handle: None,
                close_handle=closed.append,
                driver_pid=1,
                deadline=10,
                wait_for_retry=lambda: pytest.fail("open errors are not churn"),
            )
        assert raised.value is error
        assert closed == [1]

    def test_handle_waits_share_one_deadline(self) -> None:
        times = iter([0.0, 0.4])
        timeouts: list[int] = []
        probe.wait_handles_to_deadline(
            {1: "one", 2: "two"},
            wait_one=lambda _handle, timeout: timeouts.append(timeout) or True,
            deadline=1.0,
            monotonic=lambda: next(times),
        )
        assert timeouts == [1000, 600]

    def test_job_zero_polls_while_termination_is_still_draining(self) -> None:
        samples = iter([3, 1, 0, 0])
        events: list[str] = []
        waits: list[str] = []

        def query() -> int:
            events.append("query")
            return next(samples)

        probe.browser_guardian_shutdown(
            retained={1: object()},
            active_pids=lambda _handles: set(),
            terminate_job=lambda: events.append("terminate"),
            query_active_processes=query,
            signal_job_zero=lambda: events.append("JOB_ZERO"),
            wait_check_stable_handles=lambda: None,
            wait_handles=lambda _handles: events.append("wait handles"),
            signal_both_zero=lambda: events.append("BOTH_ZERO"),
            wait_allow_fence_release=lambda: None,
            release_fence=lambda: events.append("release"),
            deadline=5,
            wait_for_retry=lambda: waits.append("wait"),
            monotonic=lambda: 0,
        )

        assert waits == ["wait", "wait"]
        assert events[:5] == ["terminate", "query", "query", "query", "JOB_ZERO"]
        assert "release" in events

    def test_job_zero_timeout_reports_the_remaining_count(self) -> None:
        with pytest.raises(RuntimeError, match="retained 2"):
            probe.browser_guardian_shutdown(
                retained={1: object()},
                active_pids=lambda _handles: {1},
                terminate_job=lambda: None,
                query_active_processes=lambda: 2,
                signal_job_zero=lambda: pytest.fail("job zero was signaled early"),
                wait_check_stable_handles=lambda: None,
                wait_handles=lambda _handles: None,
                signal_both_zero=lambda: None,
                wait_allow_fence_release=lambda: None,
                release_fence=lambda: pytest.fail("fence released"),
                deadline=1,
                wait_for_retry=lambda: pytest.fail("deadline already passed"),
                monotonic=lambda: 2,
            )

    def test_shutdown_holds_fence_through_job_and_handle_zero(self) -> None:
        events: list[str] = []
        active = iter([{1}, set()])
        probe.browser_guardian_shutdown(
            retained={1: object()},
            active_pids=lambda _handles: next(active),
            terminate_job=lambda: events.append("terminate"),
            query_active_processes=lambda: events.append("query") or 0,
            signal_job_zero=lambda: events.append("JOB_ZERO"),
            wait_check_stable_handles=lambda: events.append("CHECK_HANDLES"),
            wait_handles=lambda _handles: events.append("wait handles"),
            signal_both_zero=lambda: events.append("BOTH_ZERO"),
            wait_allow_fence_release=lambda: events.append("ALLOW_RELEASE"),
            release_fence=lambda: events.append("release"),
            deadline=5,
            wait_for_retry=lambda: None,
            monotonic=lambda: 0,
        )
        assert events == [
            "terminate",
            "query",
            "JOB_ZERO",
            "CHECK_HANDLES",
            "wait handles",
            "query",
            "BOTH_ZERO",
            "ALLOW_RELEASE",
            "release",
        ]

    def test_query_and_handle_failures_never_release_the_fence(self) -> None:
        for query, wait, message in [
            (lambda: (_ for _ in ()).throw(OSError("query")), lambda _h: None, "query"),
            (
                lambda: 0,
                lambda _h: (_ for _ in ()).throw(TimeoutError("handles")),
                "handles",
            ),
        ]:
            events: list[str] = []
            with pytest.raises((OSError, TimeoutError), match=message):
                probe.browser_guardian_shutdown(
                    retained={1: object()},
                    active_pids=lambda _handles: {1},
                    terminate_job=lambda: None,
                    query_active_processes=query,
                    signal_job_zero=lambda: events.append("JOB_ZERO"),
                    wait_check_stable_handles=lambda: None,
                    wait_handles=wait,
                    signal_both_zero=lambda: events.append("BOTH_ZERO"),
                    wait_allow_fence_release=lambda: None,
                    release_fence=lambda: events.append("release"),
                    deadline=5,
                    wait_for_retry=lambda: None,
                    monotonic=lambda: 0,
                )
            assert "release" not in events
            assert "BOTH_ZERO" not in events

    def test_browser_launch_routes_separately(self) -> None:
        assert probe.topology_runner("browser-launch-owner-loss") == "browser-launch"


class TestReviewedBrowserLaunchRepairs:
    def test_browser_launch_job_is_configured_before_use(self) -> None:
        events: list[str] = []
        job = probe.create_browser_launch_job(
            "inner",
            create=lambda name: events.append(f"create {name}") or object(),
            configure=lambda _job: events.append("configure kill-on-close"),
        )
        events.append(f"use {job is not None}")
        assert events == ["create inner", "configure kill-on-close", "use True"]

    def test_inner_job_configuration_enables_kill_on_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        limits = {"BasicLimitInformation": {"LimitFlags": 4}}
        observed: list[tuple[object, int, dict[str, Any]]] = []

        class Win32Job:
            JobObjectExtendedLimitInformation = 9
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

            @staticmethod
            def QueryInformationJobObject(job: object, kind: int) -> dict[str, Any]:
                assert job == "inner"
                assert kind == 9
                return limits

            @staticmethod
            def SetInformationJobObject(
                job: object, kind: int, value: dict[str, Any]
            ) -> None:
                observed.append((job, kind, value))

        monkeypatch.setattr(
            probe,
            "_windows_modules",
            lambda: (object(), object(), object(), Win32Job),
        )
        probe._configure_kill_on_close("inner")
        assert limits["BasicLimitInformation"]["LimitFlags"] == 0x2004
        assert observed == [("inner", 9, limits)]

    def test_run_parser_accepts_browser_launch_owner_loss(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            sys,
            "argv",
            ["probe", "run", "browser-launch-owner-loss", str(tmp_path)],
        )
        args = probe._parse_args()
        assert args.role == "run"
        assert args.scenario == "browser-launch-owner-loss"
        assert args.root == tmp_path

    def test_job_process_ids_accept_real_pywin32_sequence_shape(self) -> None:
        assert probe.job_process_ids_from_query((101, 202, 303)) == {101, 202, 303}
        with pytest.raises(RuntimeError, match="unexpected pywin32 shape"):
            probe.job_process_ids_from_query({"ProcessIdList": [101]})
        with pytest.raises(RuntimeError, match="non-positive"):
            probe.job_process_ids_from_query((0, 101))

    def test_process_creation_identity_normalizes_pytime_shaped_datetimes(self) -> None:
        import datetime

        assert (
            probe.process_creation_identity(
                datetime.datetime(2026, 9, 22, 12, 30, 1, 123456)
            )
            == "2026-09-22T12:30:01.123456+00:00"
        )
        assert (
            probe.process_creation_identity(
                datetime.datetime(
                    2026,
                    9,
                    22,
                    20,
                    30,
                    1,
                    123456,
                    tzinfo=datetime.timezone(datetime.timedelta(hours=8)),
                )
            )
            == "2026-09-22T12:30:01.123456+00:00"
        )
        with pytest.raises(RuntimeError, match="datetime-shaped"):
            probe.process_creation_identity(123)

    def test_fresh_cdp_b_change_retries_before_acceptance(self) -> None:
        samples = iter(
            [
                [{"id": 2, "type": "browser"}, {"id": 3, "type": "renderer"}],
                [{"id": 2, "type": "browser"}, {"id": 4, "type": "renderer"}],
                [{"id": 2, "type": "browser"}, {"id": 3, "type": "renderer"}],
                [{"id": 2, "type": "browser"}, {"id": 3, "type": "renderer"}],
            ]
        )
        sample_calls: list[str] = []
        closed: list[int] = []

        def sample() -> list[dict[str, Any]]:
            sample_calls.append("sample")
            return next(samples)

        cdp, retained = probe.retain_stable_browser_inventory(
            sample_cdp=sample,
            sample_job_pids=lambda: {1, 2, 3},
            open_handle=lambda pid: pid,
            validate_handle=lambda _pid, _handle: None,
            close_handle=closed.append,
            driver_pid=1,
            deadline=2,
            wait_for_retry=lambda: None,
            monotonic=lambda: 0,
        )
        assert sample_calls == ["sample"] * 4
        assert closed == [1, 2, 3]
        assert cdp == {2: "browser", 3: "renderer"}
        assert retained == {1: 1, 2: 2, 3: 3}

    def test_browser_role_must_match_resolved_chromium_image(
        self, tmp_path: Path
    ) -> None:
        expected = tmp_path / "chrome.exe"
        identity = {2: {"image_path": str(expected), "creation_time": "stable"}}
        assert probe.require_browser_image_identity(
            {2: "browser", 3: "renderer"}, identity, str(expected)
        ) == {"browser_pid": 2, "browser_image_path": str(expected)}
        with pytest.raises(RuntimeError, match="does not match"):
            probe.require_browser_image_identity(
                {2: "browser", 3: "renderer"},
                {2: {"image_path": str(tmp_path / "other.exe")}},
                str(expected),
            )
        with pytest.raises(RuntimeError, match="exactly one"):
            probe.require_browser_image_identity(
                {2: "browser", 3: "browser"}, identity, str(expected)
            )

    @pytest.mark.parametrize("role", ["owner", "guardian"])
    def test_actor_failure_preserves_published_error_and_logs(
        self, tmp_path: Path, role: str
    ) -> None:
        class Process:
            def poll(self) -> int:
                return 7

        (tmp_path / f"{role}-error.json").write_text(
            json.dumps({"error": f"RuntimeError: {role} failed"}), encoding="utf-8"
        )
        stdout = tmp_path / f"{role}.stdout"
        stderr = tmp_path / f"{role}.stderr"
        stdout.write_text(f"{role} out", encoding="utf-8")
        stderr.write_text(f"{role} err", encoding="utf-8")
        failure = probe.browser_actor_failure(
            tmp_path, {role: Process()}, {role: (stdout, stderr)}
        )
        assert f"RuntimeError: {role} failed" in str(failure)
        assert f"{role} out" in str(failure)
        assert f"{role} err" in str(failure)


class TestBrowserBarrierRace:
    @staticmethod
    def _failure_fixture(tmp_path: Path, role: str, returncode: int | None):
        class Process:
            def poll(self) -> int | None:
                return returncode

        stdout = tmp_path / f"{role}.stdout"
        stderr = tmp_path / f"{role}.stderr"
        stdout.write_text("actor stdout", encoding="utf-8")
        stderr.write_text("actor stderr", encoding="utf-8")
        (tmp_path / f"{role}-error.json").write_text(
            json.dumps({"error": "RuntimeError: primary actor failure"}),
            encoding="utf-8",
        )
        return Process(), {role: (stdout, stderr)}

    def test_waiter_rechecks_simultaneous_error_after_barrier_wins(
        self, tmp_path: Path
    ) -> None:
        process, logs = self._failure_fixture(tmp_path, "guardian", None)
        process._handle = object()

        class Win32Event:
            @staticmethod
            def WaitForMultipleObjects(
                _handles: list[Any], _all: bool, _timeout: int
            ) -> int:
                return probe._WAIT_OBJECT_0

            @staticmethod
            def WaitForSingleObject(_handle: object, _timeout: int) -> int:
                return probe._WAIT_OBJECT_0

        with pytest.raises(RuntimeError, match="primary actor failure"):
            probe._wait_browser_barrier(
                object(),
                actor_error=object(),
                actors={"guardian": process},
                logs=logs,
                root=tmp_path,
                deadline=time.monotonic() + 2,
                win32event=Win32Event,
            )

    def test_simultaneous_barrier_and_error_rechecks_error_after_lowest_index(
        self, tmp_path: Path
    ) -> None:
        process, logs = self._failure_fixture(tmp_path, "guardian", None)
        with pytest.raises(RuntimeError) as raised:
            probe.require_clean_browser_barrier(
                actor_error_signaled=True,
                actors={"guardian": process},
                expected_alive={"guardian"},
                logs=logs,
                root=tmp_path,
            )
        assert "primary actor failure" in str(raised.value)
        assert "actor stdout" in str(raised.value)
        assert "actor stderr" in str(raised.value)

    def test_waiter_rechecks_actor_exit_immediately_after_barrier(
        self, tmp_path: Path
    ) -> None:
        process, logs = self._failure_fixture(tmp_path, "guardian", 7)
        process._handle = object()

        class Win32Event:
            @staticmethod
            def WaitForMultipleObjects(
                _handles: list[Any], _all: bool, _timeout: int
            ) -> int:
                return probe._WAIT_OBJECT_0

            @staticmethod
            def WaitForSingleObject(_handle: object, _timeout: int) -> int:
                return probe._WAIT_TIMEOUT

        with pytest.raises(RuntimeError, match="primary actor failure"):
            probe._wait_browser_barrier(
                object(),
                actor_error=object(),
                actors={"guardian": process},
                logs=logs,
                root=tmp_path,
                deadline=time.monotonic() + 2,
                win32event=Win32Event,
            )

    def test_actor_exit_immediately_after_barrier_surfaces_published_primary(
        self, tmp_path: Path
    ) -> None:
        process, logs = self._failure_fixture(tmp_path, "guardian", 7)
        with pytest.raises(RuntimeError) as raised:
            probe.require_clean_browser_barrier(
                actor_error_signaled=False,
                actors={"guardian": process},
                expected_alive={"guardian"},
                logs=logs,
                root=tmp_path,
            )
        assert "primary actor failure" in str(raised.value)
        assert "returncode=7" in str(raised.value)

    def test_expected_actor_exit_is_not_rejected_without_an_error(
        self, tmp_path: Path
    ) -> None:
        class Process:
            def poll(self) -> int:
                return 0

        probe.require_clean_browser_barrier(
            actor_error_signaled=False,
            actors={"guardian": Process()},
            expected_alive=set(),
            logs={},
            root=tmp_path,
        )


class TestBrowserProbeRootOwnership:
    def test_coordinator_accepts_exact_harness_preparation(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "browser-launch-owner-loss"
        root.mkdir()
        (root / "outer-job.json").write_text(
            json.dumps({"name": "Local\\outer-job"}), encoding="utf-8"
        )

        assert probe.prepare_browser_probe_root(root) == "Local\\outer-job"
        assert {entry.name for entry in root.iterdir()} == {"outer-job.json", "auth"}
        assert (root / "auth").is_dir()

    @pytest.mark.parametrize(
        "prepare",
        [
            lambda root: None,
            lambda root: (root.mkdir(), (root / "unexpected").write_text("x")),
            lambda root: (
                root.mkdir(),
                (root / "outer-job.json").write_text(json.dumps({"name": "outer"})),
                (root / "stale.json").write_text("{}"),
            ),
        ],
    )
    def test_coordinator_rejects_missing_or_unexpected_preexisting_state(
        self, tmp_path: Path, prepare: Any
    ) -> None:
        root = tmp_path / "browser-launch-owner-loss"
        prepare(root)
        with pytest.raises(RuntimeError):
            probe.prepare_browser_probe_root(root)
        assert not (root / "auth").exists()

    @pytest.mark.parametrize(
        "metadata",
        [{}, {"name": ""}, {"name": 7}, {"name": "outer", "extra": True}],
    )
    def test_coordinator_rejects_invalid_outer_job_metadata(
        self, tmp_path: Path, metadata: dict[str, Any]
    ) -> None:
        root = tmp_path / "browser-launch-owner-loss"
        root.mkdir()
        (root / "outer-job.json").write_text(json.dumps(metadata), encoding="utf-8")
        with pytest.raises(RuntimeError, match="metadata"):
            probe.prepare_browser_probe_root(root)
        assert not (root / "auth").exists()

    def test_venv_actor_launches_the_base_interpreter(self, tmp_path: Path) -> None:
        base = tmp_path / "python.exe"
        launcher = tmp_path / "venv" / "python.exe"
        chosen, env = probe.probe_python_invocation(str(launcher), str(base), {})
        assert chosen == str(base)
        assert env["__PYVENV_LAUNCHER__"] == str(launcher)
        same, unchanged = probe.probe_python_invocation(str(base), str(base), {})
        assert same == str(base)
        assert "__PYVENV_LAUNCHER__" not in unchanged

    def test_worker_activity_waits_for_a_start_message(self) -> None:
        script = probe.browser_activity_script()
        assert script.index("worker.onmessage") < script.index(
            'worker.postMessage("start")'
        )
        assert "self.onmessage = () => postMessage(true)" in script
        assert "postMessage({ready:true})" not in script
        assert "error:" in script

    def test_close_guardian_signal_uses_the_stored_control_key(self) -> None:
        signaled: list[str] = []
        controls = {
            probe.browser_control_key(label): f"event-{label}"
            for label in probe._BROWSER_CONTROL_LABELS
        }

        probe.signal_browser_control(controls, "close-guardian", signaled.append)

        assert signaled == ["event-close-guardian"]
        assert "close-guardian" not in controls

    def test_owner_termination_kills_python_children_but_not_browser_descendants(
        self,
    ) -> None:
        selected = probe.python_pids_to_terminate(
            10,
            {10: 1, 11: 10, 12: 11, 13: 12},
            {
                10: r"C:\venv\Scripts\python.exe",
                11: r"C:\Python\python.exe",
                12: r"C:\node.exe",
                13: r"C:\chrome.exe",
            },
            {12, 13},
        )
        assert selected == [10, 11]
        escaped = probe.python_pids_to_terminate(
            10,
            {10: 1, 11: 10, 13: 11},
            {10: "python.exe", 11: "python.exe", 13: "chrome.exe"},
            set(),
        )
        assert escaped == [10, 11]

    def test_coordinator_allowance_includes_its_release_gate_ancestors(self) -> None:
        parents = {5: 4, 4: 2, 2: 1, 9: 5}
        assert probe.coordinator_process_ids({2, 4, 5, 9}, 5, parents) == {2, 4, 5}
        assert probe.coordinator_process_ids({5}, 5, parents) == {5}

    def test_outer_pids_must_match_the_coordinator(self) -> None:
        released: list[str] = []
        observed = iter([{2, 4, 5, 9}, {2, 4, 5}])
        parents = {5: 4, 4: 2, 2: 1, 9: 5}

        assert probe.prove_outer_pids_are_coordinator(
            ["owner"],
            release=released.append,
            query_pids=lambda: next(observed),
            allowed_pids=lambda pids: probe.coordinator_process_ids(pids, 5, parents),
            deadline=5,
            wait_for_retry=lambda: None,
            monotonic=lambda: 0,
        ) == [2, 4, 5]
        assert released == ["owner"]

    def test_outer_accounting_starts_after_exited_actor_handles_are_released(
        self,
    ) -> None:
        released: list[str] = []
        counts = iter([3, 1])

        active = probe.prove_coordinator_is_only_outer_process(
            ["owner", "guardian"],
            release=released.append,
            query_active=lambda: next(counts),
            deadline=5,
            wait_for_retry=lambda: None,
            monotonic=lambda: 0,
        )

        assert released == ["owner", "guardian"]
        assert active == 1

    def test_live_outer_process_is_not_treated_as_handle_accounting(
        self,
    ) -> None:
        with pytest.raises(RuntimeError, match="ActiveProcesses=2"):
            probe.prove_coordinator_is_only_outer_process(
                [],
                release=lambda _actor: None,
                query_active=lambda: 2,
                deadline=1,
                wait_for_retry=lambda: None,
                monotonic=lambda: 2,
            )

    def test_outer_remainder_error_includes_process_inventory(self) -> None:
        with pytest.raises(RuntimeError, match="pid=9 image=chrome.exe"):
            probe.prove_coordinator_is_only_outer_process(
                [],
                release=lambda _actor: None,
                query_active=lambda: 4,
                deadline=1,
                wait_for_retry=lambda: None,
                describe=lambda: "pid=9 image=chrome.exe",
                monotonic=lambda: 2,
            )

    def test_live_actor_handle_is_not_released(self) -> None:
        class Actor:
            def poll(self) -> None:
                return None

        with pytest.raises(RuntimeError, match="live browser actor"):
            probe.release_exited_actor(Actor())
