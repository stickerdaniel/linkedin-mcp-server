"""Native Windows evidence probe for the owner-crash profile fence."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEADLINE_SECONDS = 30.0
_DESCENDANT_COUNT = 24
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 128
_WAIT_TIMEOUT = 258
# Win32 MUTEX_MODIFY_STATE; pywin32 does not export it from win32con.
_MUTEX_MODIFY_STATE = 0x0001


def _atomic_json(path: Path, value: Any) -> None:
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_text(json.dumps(value), encoding="utf-8")
    partial.replace(path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_published_json(
    path: Path,
    *,
    deadline: float,
    read: Callable[[Path], Any] = _read_json,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    while True:
        try:
            return read(path)
        except PermissionError:
            if monotonic() >= deadline:
                raise
            sleep(0.01)


def _windows_modules() -> tuple[Any, Any, Any, Any]:
    return (
        importlib.import_module("win32api"),
        importlib.import_module("win32con"),
        importlib.import_module("win32event"),
        importlib.import_module("win32job"),
    )


def _event(name: str) -> Any:
    _win32api, win32con, win32event, _win32job = _windows_modules()
    return win32event.OpenEvent(win32con.EVENT_MODIFY_STATE, False, name)


def _signal(name: str) -> None:
    _win32api, _win32con, win32event, _win32job = _windows_modules()
    handle = _event(name)
    try:
        win32event.SetEvent(handle)
    finally:
        handle.Close()


def _wait(handle: Any, timeout_seconds: float, message: str) -> None:
    _win32api, _win32con, win32event, _win32job = _windows_modules()
    result = win32event.WaitForSingleObject(handle, int(timeout_seconds * 1000))
    if result != _WAIT_OBJECT_0:
        raise RuntimeError(message)


def _configure_kill_on_close(job_handle: Any) -> None:
    _win32api, _win32con, _win32event, win32job = _windows_modules()
    limits = win32job.QueryInformationJobObject(
        job_handle, win32job.JobObjectExtendedLimitInformation
    )
    limits["BasicLimitInformation"]["LimitFlags"] |= (
        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    win32job.SetInformationJobObject(
        job_handle, win32job.JobObjectExtendedLimitInformation, limits
    )


def observe_guardian_identity(identity_mutex_name: str) -> dict[str, bool]:
    _win32api, win32con, win32event, _win32job = _windows_modules()
    handle = win32event.OpenMutex(
        win32con.SYNCHRONIZE | _MUTEX_MODIFY_STATE,
        False,
        identity_mutex_name,
    )
    try:
        result = win32event.WaitForSingleObject(handle, 0)
        if result in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
            win32event.ReleaseMutex(handle)
        if result != _WAIT_TIMEOUT:
            raise RuntimeError("the guardian no longer owns its identity mutex")
        return {"identity_mutex_owned": True}
    finally:
        handle.Close()


def _query_named_job_active_processes(name: str) -> int:
    _win32api, _win32con, _win32event, win32job = _windows_modules()
    handle = win32job.OpenJobObject(win32job.JOB_OBJECT_QUERY, False, name)
    try:
        accounting = win32job.QueryInformationJobObject(
            handle, win32job.JobObjectBasicAccountingInformation
        )
        return int(accounting["ActiveProcesses"])
    finally:
        handle.Close()


def observe_named_job_objects(
    browser_job_name: str, project_job_name: str
) -> dict[str, int | bool]:
    observations: dict[str, int | bool] = {}
    for label, name in (
        ("browser", browser_job_name),
        ("project", project_job_name),
    ):
        observations[f"{label}_job_open"] = True
        observations[f"{label}_job_active_processes"] = (
            _query_named_job_active_processes(name)
        )
    return observations


def guardian_loss_measurement(
    *,
    termination_requested_ns: int,
    guardian_exit_observed_ns: int,
    lease_acquired_ns: int,
    lease_observed_ns: int,
    owner_active_before_job_query: bool,
    owner_active_after_job_query: bool,
    active_descendants: int,
    browser_job_active_processes: int,
) -> dict[str, int | bool]:
    if guardian_exit_observed_ns <= termination_requested_ns:
        raise RuntimeError("guardian exit did not follow its termination request")
    if lease_observed_ns < guardian_exit_observed_ns:
        raise RuntimeError("lease acquisition was observed before guardian exit")
    if not termination_requested_ns < lease_acquired_ns <= lease_observed_ns:
        raise RuntimeError("the lease acquisition timestamp is inconsistent")
    if not owner_active_before_job_query or not owner_active_after_job_query:
        raise RuntimeError("the owner exited before guardian-loss lease acquisition")
    if active_descendants <= 0:
        raise RuntimeError("all descendants exited before guardian-loss acquisition")
    if browser_job_active_processes <= 0:
        raise RuntimeError("the browser Job drained before guardian-loss acquisition")
    return {
        "guardian_termination_requested_ns": termination_requested_ns,
        "guardian_exit_observed_ns": guardian_exit_observed_ns,
        "lease_acquired_ns": lease_acquired_ns,
        "lease_observed_ns": lease_observed_ns,
        "owner_active_at_lease_observation": owner_active_after_job_query,
        "active_descendants_at_lease_observation": active_descendants,
        "browser_job_active_processes_at_lease_observation": (
            browser_job_active_processes
        ),
    }


def sample_guardian_loss_progress(
    observation: dict[str, int],
    *,
    termination_requested_ns: int,
    lease_acquired_ns: Callable[[], int],
    guardian_active: Callable[[], bool],
    lease_signaled: Callable[[], bool],
    owner_active: Callable[[], bool],
    active_descendants: Callable[[], int],
    browser_job_active_processes: Callable[[], int],
    clock_ns: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, int | bool] | None:
    if "guardian_exit_observed_ns" not in observation and not guardian_active():
        observation["guardian_exit_observed_ns"] = clock_ns()
    if "lease_observed_ns" not in observation and lease_signaled():
        observation["lease_observed_ns"] = clock_ns()
    if not {"guardian_exit_observed_ns", "lease_observed_ns"} <= observation.keys():
        return None

    owner_active_before_job_query = owner_active()
    living_descendants = active_descendants()
    browser_active = browser_job_active_processes()
    owner_active_after_job_query = owner_active()
    return guardian_loss_measurement(
        termination_requested_ns=termination_requested_ns,
        guardian_exit_observed_ns=observation["guardian_exit_observed_ns"],
        lease_acquired_ns=lease_acquired_ns(),
        lease_observed_ns=observation["lease_observed_ns"],
        owner_active_before_job_query=owner_active_before_job_query,
        owner_active_after_job_query=owner_active_after_job_query,
        active_descendants=living_descendants,
        browser_job_active_processes=browser_active,
    )


def remaining_wait_milliseconds(
    deadline: float,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("the guardian-loss observation deadline expired")
    return max(1, int(remaining * 1000))


def active_guardian_loss_wait_handles(
    owner_handle: Any,
    descendant_handles: list[Any],
    *,
    is_active: Callable[[Any], bool],
) -> list[Any]:
    if not is_active(owner_handle):
        raise RuntimeError("the owner exited before guardian-loss lease acquisition")
    active_descendants = [handle for handle in descendant_handles if is_active(handle)]
    if not active_descendants:
        raise RuntimeError("all descendants exited before guardian-loss acquisition")
    return [owner_handle, *active_descendants]


def _owner(
    scenario: str,
    auth_root: Path,
    project_job_name: str,
    browser_job_file: Path,
    metadata_file: Path,
    ready_event: str,
) -> int:
    from linkedin_mcp_server.profile_lease import ProfileLease

    _win32api, _win32con, _win32event, win32job = _windows_modules()
    project_job = win32job.OpenJobObject(
        win32job.JOB_OBJECT_QUERY, False, project_job_name
    )
    lease: ProfileLease | None = None
    browser_job = None
    descendants: list[subprocess.Popen[bytes]] = []
    try:
        if scenario == "baseline":
            lease = ProfileLease(auth_root)
            if not lease.try_acquire():
                raise RuntimeError(
                    "the baseline owner could not acquire the profile lease"
                )
        else:
            browser_job_name = _read_json(browser_job_file)["name"]
            browser_job = win32job.OpenJobObject(
                win32job.JOB_OBJECT_ALL_ACCESS, False, browser_job_name
            )

        for _ in range(_DESCENDANT_COUNT):
            descendant = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(600)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if browser_job is not None:
                process_handle = getattr(descendant, "_handle", None)
                if process_handle is None:
                    raise RuntimeError("Windows Popen exposed no process handle")
                win32job.AssignProcessToJobObject(browser_job, process_handle)
                if not win32job.IsProcessInJob(process_handle, browser_job):
                    raise RuntimeError("a descendant did not join the browser Job")
            descendants.append(descendant)

        _atomic_json(
            metadata_file,
            {
                "owner_pid": os.getpid(),
                "project_job_name": project_job_name,
                "descendant_pids": [process.pid for process in descendants],
            },
        )
        _signal(ready_event)
        time.sleep(600)
        return 0
    finally:
        if lease is not None:
            lease.release()
        if browser_job is not None:
            browser_job.Close()
        project_job.Close()


def starter_termination_measurement(
    terminated_ns: int, owner_exit_ns: int
) -> dict[str, int]:
    if owner_exit_ns <= terminated_ns:
        raise RuntimeError("the owner exit did not follow starter termination")
    return {"terminated_ns": terminated_ns, "owner_exit_ns": owner_exit_ns}


def sample_lease_acquisition(
    *,
    active_descendants: Callable[[], int],
    require_active: bool,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, int]:
    acquired_ns = clock_ns()
    active = active_descendants()
    if require_active and active <= 0:
        raise RuntimeError("all descendants exited before lease acquisition")
    return {
        "lease_acquired_ns": acquired_ns,
        "active_descendants_at_lease_acquire": active,
    }


def sample_pre_crash_contention(
    *,
    try_acquire: Callable[[], bool],
    release: Callable[[], None],
    clock_ns: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, int | bool]:
    attempted_ns = clock_ns()
    acquired = try_acquire()
    sample = {"attempted_ns": attempted_ns, "acquired": acquired}
    if acquired:
        release()
        raise RuntimeError("the profile fence was free before the owner crash")
    return sample


def terminate_wait_close_handles(
    handles: list[Any],
    *,
    is_active: Callable[[Any], bool],
    terminate: Callable[[Any], None],
    wait: Callable[[Any], None],
    close: Callable[[Any], None],
) -> None:
    first_error: BaseException | None = None
    for handle in handles:
        active = True
        try:
            active = is_active(handle)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if active:
            try:
                terminate(handle)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        try:
            wait(handle)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        try:
            close(handle)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


def guardian_shutdown_sequence(
    result: dict[str, Any],
    *,
    active_descendants: Callable[[], int],
    terminate_browser_job: Callable[[], None],
    query_browser_job: Callable[[], int],
    close_browser_job: Callable[[], None],
    release_fence: Callable[[], None],
    terminate_project_job: Callable[[], None],
    query_project_job: Callable[[], int],
    close_project_job: Callable[[], None],
    monotonic: Callable[[], float] = time.monotonic,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    result["owner_death_observed_ns"] = clock_ns()
    active_before = active_descendants()
    result["active_descendants_after_owner_death"] = active_before
    if active_before <= 0:
        raise RuntimeError("no browser descendant survived owner death")

    result["terminate_ns"] = clock_ns()
    terminate_browser_job()
    result["terminate_called"] = True
    deadline = monotonic() + _DEADLINE_SECONDS
    while True:
        living = active_descendants()
        if living < active_before and "first_descendant_exit_ns" not in result:
            result["first_descendant_exit_ns"] = clock_ns()
        try:
            active = query_browser_job()
        except BaseException as exc:
            result["query_error"] = f"{type(exc).__name__}: {exc}"
            raise RuntimeError("the guardian could not query the browser Job") from exc
        sampled_ns = clock_ns()
        result["query_samples"].append(
            {"sampled_ns": sampled_ns, "active_processes": active}
        )
        if active == 0:
            living = active_descendants()
            if living == 0:
                result.setdefault("first_descendant_exit_ns", sampled_ns)
                result["zero_observed_ns"] = sampled_ns
                close_browser_job()
                result["browser_job_closed_ns"] = clock_ns()
                release_fence()
                result["fence_released_ns"] = clock_ns()
                break
        if monotonic() >= deadline:
            result["query_timeout"] = True
            raise RuntimeError("the browser Job did not drain before its deadline")
        sleep(0.001)

    result["project_owner_terminate_ns"] = clock_ns()
    terminate_project_job()
    result["project_owner_terminate_called"] = True
    deadline = monotonic() + _DEADLINE_SECONDS
    result["project_owner_query_samples"] = []
    while True:
        try:
            active = query_project_job()
        except BaseException as exc:
            result["project_owner_query_error"] = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(
                "the guardian could not query the project owner Job"
            ) from exc
        sampled_ns = clock_ns()
        result["project_owner_query_samples"].append(
            {"sampled_ns": sampled_ns, "active_processes": active}
        )
        if active == 0:
            result["project_owner_zero_observed_ns"] = sampled_ns
            close_project_job()
            result["project_owner_closed_ns"] = clock_ns()
            return
        if monotonic() >= deadline:
            result["project_owner_query_timeout"] = True
            raise RuntimeError(
                "the project owner Job did not drain before its deadline"
            )
        sleep(0.001)


def _guardian(
    auth_root: Path,
    browser_job_file: Path,
    owner_metadata_file: Path,
    result_file: Path,
    ready_event: str,
    owner_ready_event: str,
    armed_event: str,
    fault: str,
) -> int:
    from linkedin_mcp_server.profile_lease import ProfileLease

    win32api, win32con, win32event, win32job = _windows_modules()
    lease = ProfileLease(auth_root)
    identity_mutex_name = f"Local\\linkedin-mcp-w1-guardian-{secrets.token_hex(16)}"
    identity_mutex = win32event.CreateMutex(None, True, identity_mutex_name)
    browser_job = None
    project_job = None
    descendant_handles: list[Any] = []
    fence_acquired = False
    result: dict[str, Any] = {
        "guardian_pid": os.getpid(),
        "guardian_identity_mutex": identity_mutex_name,
        "query_samples": [],
        "terminate_called": False,
    }
    try:
        if not lease.try_acquire():
            raise RuntimeError("the guardian could not acquire its profile fence")
        fence_acquired = True
        name = f"Local\\linkedin-mcp-w1-browser-{secrets.token_hex(16)}"
        browser_job = win32job.CreateJobObject(None, name)
        _configure_kill_on_close(browser_job)
        _atomic_json(browser_job_file, {"name": name})
        _signal(ready_event)

        owner_ready = win32event.OpenEvent(
            win32con.SYNCHRONIZE, False, owner_ready_event
        )
        try:
            _wait(owner_ready, _DEADLINE_SECONDS, "the owner did not become ready")
        finally:
            owner_ready.Close()

        metadata = _read_json(owner_metadata_file)
        owner_pid = int(metadata["owner_pid"])
        project_job = win32job.OpenJobObject(
            win32job.JOB_OBJECT_ALL_ACCESS, False, metadata["project_job_name"]
        )
        descendant_handles = [
            win32api.OpenProcess(
                win32con.SYNCHRONIZE | win32con.PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                int(pid),
            )
            for pid in metadata["descendant_pids"]
        ]
        accounting = win32job.QueryInformationJobObject(
            browser_job, win32job.JobObjectBasicAccountingInformation
        )
        result["query_samples"].append(
            {
                "sampled_ns": time.perf_counter_ns(),
                "active_processes": int(accounting["ActiveProcesses"]),
            }
        )
        owner = win32api.OpenProcess(win32con.SYNCHRONIZE, False, owner_pid)
        _signal(armed_event)
        try:
            _wait(owner, _DEADLINE_SECONDS, "the owner did not die")
        finally:
            owner.Close()

        def close_browser_job() -> None:
            nonlocal browser_job
            handle = browser_job
            if handle is None:
                raise RuntimeError("the browser Job handle is already closed")
            handle.Close()
            browser_job = None

        def close_project_job() -> None:
            nonlocal project_job
            handle = project_job
            if handle is None:
                raise RuntimeError("the project owner Job handle is already closed")
            handle.Close()
            project_job = None

        def mark_injected_fault() -> None:
            result["fault_injected"] = fault
            result["fault_injected_ns"] = time.perf_counter_ns()

        def terminate_browser_job() -> None:
            if fault == "terminate-error":
                mark_injected_fault()
                raise OSError("injected browser Job termination failure")
            win32job.TerminateJobObject(browser_job, 197)

        def query_browser_job() -> int:
            if fault == "query-error":
                mark_injected_fault()
                raise OSError("injected browser Job query failure")
            if fault == "drain-timeout":
                mark_injected_fault()
                return 1
            return int(
                win32job.QueryInformationJobObject(
                    browser_job, win32job.JobObjectBasicAccountingInformation
                )["ActiveProcesses"]
            )

        timeout_clock = iter([0.0, _DEADLINE_SECONDS + 1.0])
        result["fault"] = fault
        guardian_shutdown_sequence(
            result,
            active_descendants=lambda: sum(
                _is_active(handle) for handle in descendant_handles
            ),
            terminate_browser_job=terminate_browser_job,
            query_browser_job=query_browser_job,
            close_browser_job=close_browser_job,
            release_fence=lease.release,
            terminate_project_job=lambda: win32job.TerminateJobObject(project_job, 198),
            query_project_job=lambda: int(
                win32job.QueryInformationJobObject(
                    project_job, win32job.JobObjectBasicAccountingInformation
                )["ActiveProcesses"]
            ),
            close_project_job=close_project_job,
            monotonic=(
                lambda: (
                    next(timeout_clock)
                    if fault == "drain-timeout"
                    else time.monotonic()
                )
            ),
        )
        fence_acquired = False
        for handle in descendant_handles:
            handle.Close()
        win32event.ReleaseMutex(identity_mutex)
        identity_mutex.Close()
        _atomic_json(result_file, result)
        return 0
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        _atomic_json(result_file, result)
        if not fence_acquired:
            raise
        # The outer harness is the only authority allowed to break a failed
        # proof. Keep the lease and both Job handles until it terminates us.
        threading.Event().wait()
        raise RuntimeError("the fail-closed guardian resumed unexpectedly") from exc


def _process_handle(pid: int, access: int) -> Any:
    win32api, _win32con, _win32event, _win32job = _windows_modules()
    return win32api.OpenProcess(access, False, pid)


def _is_active(handle: Any) -> bool:
    _win32api, _win32con, win32event, _win32job = _windows_modules()
    result = win32event.WaitForSingleObject(handle, 0)
    if result == _WAIT_TIMEOUT:
        return True
    if result == _WAIT_OBJECT_0:
        return False
    raise RuntimeError(f"WaitForSingleObject returned {result}")


def _launch_owner(
    scenario: str,
    root: Path,
    project_job: Any,
    project_job_name: str,
    owner_ready_name: str,
) -> subprocess.Popen[bytes]:
    from linkedin_mcp_server import process_tree

    nonce = process_tree.release_nonce()
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "owner",
        scenario,
        str(root / "auth"),
        project_job_name,
        str(root / "browser-job.json"),
        str(root / "owner.json"),
        owner_ready_name,
    ]
    process = subprocess.Popen(
        process_tree.windows_gate_command(command, nonce),
        cwd=_REPO_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    project_job.assign_popen(process)
    if process.stdin is None:
        raise RuntimeError("the owner gate has no release stream")
    process_tree.release_windows_gate(process.stdin, nonce)
    return process


def _new_event_name(label: str) -> str:
    return f"Local\\linkedin-mcp-w1-{label}-{secrets.token_hex(16)}"


def _run_probe(scenario: str, root: Path) -> dict[str, Any]:
    from linkedin_mcp_server import process_tree
    from linkedin_mcp_server.profile_lease import ProfileLease

    win32api, win32con, win32event, win32job = _windows_modules()
    root.mkdir(parents=True, exist_ok=True)
    (root / "auth").mkdir()
    owner_ready_name = _new_event_name("owner-ready")
    owner_ready = win32event.CreateEvent(None, True, False, owner_ready_name)
    guardian_ready = None
    guardian_armed = None
    guardian: subprocess.Popen[bytes] | None = None
    guardian_handle = None
    project_job = None
    owner_gate: subprocess.Popen[bytes] | None = None
    owner_handle = None
    descendant_handles: list[Any] = []
    contender: ProfileLease | None = None
    contender_stop: threading.Event | None = None
    contender_thread: threading.Thread | None = None
    lease_acquired_event = None
    pre_crash_contention: dict[str, int | bool] | None = None
    candidate = scenario != "baseline"
    guardian_loss = scenario == "candidate-guardian-loss-before-owner"
    fault = (
        scenario.removeprefix("candidate-")
        if scenario
        in {
            "candidate-terminate-error",
            "candidate-query-error",
            "candidate-drain-timeout",
        }
        else "none"
    )
    try:
        guardian_ready_name = _new_event_name("guardian-ready")
        guardian_armed_name = _new_event_name("guardian-armed")
        if candidate:
            guardian_ready = win32event.CreateEvent(
                None, True, False, guardian_ready_name
            )
            guardian_armed = win32event.CreateEvent(
                None, True, False, guardian_armed_name
            )
            guardian = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "guardian",
                    str(root / "auth"),
                    str(root / "browser-job.json"),
                    str(root / "owner.json"),
                    str(root / "guardian-result.json"),
                    guardian_ready_name,
                    owner_ready_name,
                    guardian_armed_name,
                    fault,
                ],
                cwd=_REPO_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            guardian_handle = _process_handle(
                guardian.pid, win32con.PROCESS_TERMINATE | win32con.SYNCHRONIZE
            )
            _wait(
                guardian_ready,
                _DEADLINE_SECONDS,
                "the guardian did not acquire the fence",
            )

        project_job = process_tree.WindowsJob.named("w1-owner")
        if project_job.name is None:
            raise RuntimeError("the project owner Job has no name")
        owner_gate = _launch_owner(
            "candidate" if candidate else "baseline",
            root,
            project_job,
            project_job.name,
            owner_ready_name,
        )
        _wait(owner_ready, _DEADLINE_SECONDS, "the owner did not become ready")
        if guardian_armed is not None:
            _wait(
                guardian_armed,
                _DEADLINE_SECONDS,
                "the guardian did not retain the owner process handle",
            )
        metadata = _read_json(root / "owner.json")
        owner_pid = int(metadata["owner_pid"])
        descendant_pids = [int(pid) for pid in metadata["descendant_pids"]]
        owner_handle = _process_handle(
            owner_pid, win32con.PROCESS_TERMINATE | win32con.SYNCHRONIZE
        )
        descendant_handles = [
            _process_handle(
                pid,
                win32con.PROCESS_TERMINATE
                | win32con.PROCESS_QUERY_LIMITED_INFORMATION
                | win32con.SYNCHRONIZE,
            )
            for pid in descendant_pids
        ]
        if not all(
            win32job.IsProcessInJob(handle, project_job.job_handle)
            for handle in descendant_handles
        ):
            raise RuntimeError("a descendant is outside the project owner Job")

        guardian_outside_owner_job = None
        if guardian is not None:
            guardian_query_handle = _process_handle(
                guardian.pid, win32con.PROCESS_QUERY_LIMITED_INFORMATION
            )
            try:
                guardian_outside_owner_job = not win32job.IsProcessInJob(
                    guardian_query_handle, project_job.job_handle
                )
            finally:
                guardian_query_handle.Close()
            if not guardian_outside_owner_job:
                raise RuntimeError("the guardian joined the project owner Job")

        contender = ProfileLease(root / "auth")
        lease_acquired_event = win32event.CreateEvent(None, True, False, None)
        contender_stop = threading.Event()
        contender_ready = threading.Event()
        lease_observation: dict[str, Any] = {}

        def contend_for_lease() -> None:
            nonlocal pre_crash_contention
            assert contender is not None
            assert contender_stop is not None
            assert lease_acquired_event is not None
            try:
                pre_crash_contention = sample_pre_crash_contention(
                    try_acquire=contender.try_acquire,
                    release=contender.release,
                )
            except BaseException as exc:
                lease_observation["error"] = exc
                win32event.SetEvent(lease_acquired_event)
                contender_ready.set()
                return
            contender_ready.set()
            while not contender_stop.is_set():
                try:
                    acquired = contender.try_acquire()
                except BaseException as exc:
                    lease_observation["error"] = exc
                    win32event.SetEvent(lease_acquired_event)
                    return
                if acquired:
                    try:
                        lease_observation.update(
                            sample_lease_acquisition(
                                active_descendants=lambda: sum(
                                    _is_active(handle) for handle in descendant_handles
                                ),
                                require_active=not candidate or guardian_loss,
                            )
                        )
                    except BaseException as exc:
                        lease_observation["error"] = exc
                    win32event.SetEvent(lease_acquired_event)
                    return
                contender_stop.wait(0.001)

        contender_thread = threading.Thread(target=contend_for_lease, daemon=True)
        contender_thread.start()
        if not contender_ready.wait(_DEADLINE_SECONDS):
            raise RuntimeError("the lease contender did not become ready")
        if pre_crash_contention is None:
            raise RuntimeError("the lease contender did not publish contention")
        if "lease_acquired_ns" in lease_observation:
            raise RuntimeError("the profile fence was free before termination")
        if "error" in lease_observation:
            raise RuntimeError("the lease contender failed before termination") from (
                lease_observation["error"]
            )

        # The owner now holds the only project-Job handle. Keeping this observer
        # handle would suppress kill-on-close and invalidate the baseline.
        project_job.close()
        project_job = None

        if guardian_loss:
            if guardian is None or guardian_handle is None:
                raise RuntimeError("the guardian-loss scenario has no guardian")
            browser_job_name = _read_json(root / "browser-job.json")["name"]
            owner_active_before = _is_active(owner_handle)
            active_descendants_before = sum(
                _is_active(handle) for handle in descendant_handles
            )
            browser_active_before = _query_named_job_active_processes(browser_job_name)
            if not _is_active(guardian_handle):
                raise RuntimeError("the guardian exited before its termination")
            if not owner_active_before:
                raise RuntimeError("the owner exited before guardian termination")
            if active_descendants_before <= 0:
                raise RuntimeError("no descendant survived until guardian termination")
            if browser_active_before <= 0:
                raise RuntimeError(
                    "the browser Job was empty before guardian termination"
                )

            guardian_termination_requested_ns = time.perf_counter_ns()
            win32api.TerminateProcess(guardian_handle, 195)
            deadline = time.monotonic() + _DEADLINE_SECONDS
            guardian_loss_observation: dict[str, int] = {}
            loss_measurement = None

            def observed_lease_acquired_ns() -> int:
                if "error" in lease_observation:
                    raise RuntimeError(
                        "the lease acquisition observation failed"
                    ) from (lease_observation["error"])
                return int(lease_observation["lease_acquired_ns"])

            while loss_measurement is None:
                wait_handles: list[Any] = []
                if "guardian_exit_observed_ns" not in guardian_loss_observation:
                    wait_handles.append(guardian_handle)
                if "lease_observed_ns" not in guardian_loss_observation:
                    wait_handles.append(lease_acquired_event)
                wait_handles.extend(
                    active_guardian_loss_wait_handles(
                        owner_handle,
                        descendant_handles,
                        is_active=_is_active,
                    )
                )
                remaining_ms = remaining_wait_milliseconds(deadline)
                wait_result = win32event.WaitForMultipleObjects(
                    wait_handles, False, remaining_ms
                )
                if wait_result == _WAIT_TIMEOUT:
                    raise RuntimeError(
                        "the guardian-loss probe did not observe lease acquisition"
                    )
                if not (
                    _WAIT_OBJECT_0 <= wait_result < _WAIT_OBJECT_0 + len(wait_handles)
                ):
                    raise RuntimeError(f"WaitForMultipleObjects returned {wait_result}")

                loss_measurement = sample_guardian_loss_progress(
                    guardian_loss_observation,
                    termination_requested_ns=guardian_termination_requested_ns,
                    lease_acquired_ns=observed_lease_acquired_ns,
                    guardian_active=lambda: _is_active(guardian_handle),
                    lease_signaled=lambda: not _is_active(lease_acquired_event),
                    owner_active=lambda: _is_active(owner_handle),
                    active_descendants=lambda: sum(
                        _is_active(handle) for handle in descendant_handles
                    ),
                    browser_job_active_processes=lambda: (
                        _query_named_job_active_processes(browser_job_name)
                    ),
                )

            guardian_stdout, guardian_stderr = guardian.communicate(
                timeout=_DEADLINE_SECONDS
            )
            if guardian.returncode == 0:
                raise RuntimeError(
                    "the terminated guardian exited successfully: "
                    f"stdout={guardian_stdout!r} stderr={guardian_stderr!r}"
                )
            return {
                "scenario": scenario,
                "lease_acquired_ns": int(lease_observation["lease_acquired_ns"]),
                "lease_acquired_with_live_descendant_ns": int(
                    lease_observation["lease_acquired_ns"]
                ),
                "active_descendants_at_lease_acquire": int(
                    lease_observation["active_descendants_at_lease_acquire"]
                ),
                "descendant_count": len(descendant_handles),
                "guardian_outside_owner_job": guardian_outside_owner_job,
                "pre_crash_contention": pre_crash_contention,
                "before_guardian_termination": {
                    "guardian_active": True,
                    "owner_active": owner_active_before,
                    "active_descendants": active_descendants_before,
                    "browser_job_active_processes": browser_active_before,
                },
                "guardian_loss": loss_measurement,
                "guardian_loss_samples": [loss_measurement],
                "guardian_returncode": guardian.returncode,
            }

        if not _is_active(owner_handle):
            raise RuntimeError("the owner exited before starter termination")
        terminated_ns = time.perf_counter_ns()
        win32api.TerminateProcess(owner_handle, 196)

        deadline = time.monotonic() + _DEADLINE_SECONDS
        termination = None
        termination_published = False
        acquired_ns = None
        active_at_acquire = None
        descendants_exit_ns = None
        pending_descendants = set(range(len(descendant_handles)))
        while True:
            wait_handles: list[Any] = []
            if termination is None:
                wait_handles.append(owner_handle)
            if acquired_ns is None:
                wait_handles.append(lease_acquired_event)
            wait_handles.extend(
                descendant_handles[index] for index in pending_descendants
            )
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            wait_result = win32event.WaitForMultipleObjects(
                wait_handles, False, remaining_ms
            )
            if wait_result == _WAIT_TIMEOUT:
                detail: Any = {
                    "owner_exit_observed": termination is not None,
                    "lease_acquired": acquired_ns is not None,
                    "active_descendants": len(pending_descendants),
                }
                guardian_result = root / "guardian-result.json"
                if guardian_result.exists():
                    try:
                        detail = _read_json(guardian_result)
                    except OSError as exc:
                        detail["guardian_result_read_error"] = (
                            f"{type(exc).__name__}: {exc}"
                        )
                raise RuntimeError(f"the crash probe did not settle: {detail}")
            if not (_WAIT_OBJECT_0 <= wait_result < _WAIT_OBJECT_0 + len(wait_handles)):
                raise RuntimeError(f"WaitForMultipleObjects returned {wait_result}")

            if termination is None and not _is_active(owner_handle):
                termination = starter_termination_measurement(
                    terminated_ns, time.perf_counter_ns()
                )
            if acquired_ns is None and not _is_active(lease_acquired_event):
                if "error" in lease_observation:
                    raise RuntimeError(
                        "the lease acquisition observation failed"
                    ) from (lease_observation["error"])
                acquired_ns = int(lease_observation["lease_acquired_ns"])
                active_at_acquire = int(
                    lease_observation["active_descendants_at_lease_acquire"]
                )
            pending_descendants = {
                index
                for index in pending_descendants
                if _is_active(descendant_handles[index])
            }
            if not pending_descendants and descendants_exit_ns is None:
                descendants_exit_ns = time.perf_counter_ns()
            if termination is not None and not termination_published:
                _atomic_json(root / "starter-termination.json", termination)
                termination_published = True
            if (
                termination_published
                and acquired_ns is not None
                and descendants_exit_ns is not None
            ):
                break

        if termination is None:
            raise RuntimeError("the owner exit was not observed")
        result: dict[str, Any] = {
            "scenario": scenario,
            **termination,
            "lease_acquired_ns": acquired_ns,
            "lease_acquired_with_live_descendant_ns": (
                acquired_ns if active_at_acquire and active_at_acquire > 0 else 0
            ),
            "descendants_exit_ns": descendants_exit_ns,
            "active_descendants_at_lease_acquire": active_at_acquire,
            "descendant_count": len(descendant_handles),
            "guardian_outside_owner_job": guardian_outside_owner_job,
            "pre_crash_contention": pre_crash_contention,
        }
        if guardian is not None:
            guardian_stdout, guardian_stderr = guardian.communicate(
                timeout=_DEADLINE_SECONDS
            )
            if guardian.returncode != 0:
                raise RuntimeError(
                    "the guardian failed: "
                    f"stdout={guardian_stdout!r} stderr={guardian_stderr!r}"
                )
            result["guardian"] = _read_json(root / "guardian-result.json")
        return result
    finally:
        if contender_stop is not None:
            contender_stop.set()
        if contender_thread is not None:
            contender_thread.join(timeout=5)
        if contender is not None:
            contender.release()
        if lease_acquired_event is not None:
            lease_acquired_event.Close()
        if project_job is not None and not project_job.closed:
            with contextlib.suppress(Exception):
                project_job.terminate()
            if owner_gate is not None:
                with contextlib.suppress(Exception):
                    owner_gate.wait(timeout=5)
            with contextlib.suppress(Exception):
                project_job.wait_until_empty(timeout=5)
            if not project_job.closed:
                with contextlib.suppress(Exception):
                    project_job.close()
        if owner_gate is not None:
            with contextlib.suppress(Exception):
                if owner_gate.poll() is None:
                    owner_gate.kill()
                owner_gate.wait(timeout=5)
        if guardian is not None:
            with contextlib.suppress(Exception):
                if guardian.poll() is None:
                    guardian.kill()
                guardian.wait(timeout=5)
        if guardian_handle is not None:
            with contextlib.suppress(Exception):
                _wait(guardian_handle, 5, "the retained guardian did not terminate")
                guardian_handle.Close()
        process_handles = [*descendant_handles]
        if owner_handle is not None:
            process_handles.append(owner_handle)
        with contextlib.suppress(Exception):
            terminate_wait_close_handles(
                process_handles,
                is_active=_is_active,
                terminate=lambda handle: win32api.TerminateProcess(handle, 198),
                wait=lambda handle: _wait(
                    handle, 5, "a retained process did not terminate"
                ),
                close=lambda handle: handle.Close(),
            )
        owner_ready.Close()
        if guardian_ready is not None:
            guardian_ready.Close()
        if guardian_armed is not None:
            guardian_armed.Close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="role", required=True)

    run = subparsers.add_parser("run")
    run.add_argument(
        "scenario",
        choices=(
            "baseline",
            "candidate",
            "candidate-guardian-loss-before-owner",
            "candidate-terminate-error",
            "candidate-query-error",
            "candidate-drain-timeout",
        ),
    )
    run.add_argument("root", type=Path)

    owner = subparsers.add_parser("owner")
    owner.add_argument("scenario", choices=("baseline", "candidate"))
    owner.add_argument("auth_root", type=Path)
    owner.add_argument("project_job_name")
    owner.add_argument("browser_job_file", type=Path)
    owner.add_argument("metadata_file", type=Path)
    owner.add_argument("ready_event")

    guardian = subparsers.add_parser("guardian")
    guardian.add_argument("auth_root", type=Path)
    guardian.add_argument("browser_job_file", type=Path)
    guardian.add_argument("owner_metadata_file", type=Path)
    guardian.add_argument("result_file", type=Path)
    guardian.add_argument("ready_event")
    guardian.add_argument("owner_ready_event")
    guardian.add_argument("armed_event")
    guardian.add_argument(
        "fault", choices=("none", "terminate-error", "query-error", "drain-timeout")
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if os.name != "nt":
        raise SystemExit("this probe requires native Windows Job Objects")
    if args.role == "run":
        print(json.dumps(_run_probe(args.scenario, args.root)), flush=True)
        return 0
    if args.role == "owner":
        return _owner(
            args.scenario,
            args.auth_root,
            args.project_job_name,
            args.browser_job_file,
            args.metadata_file,
            args.ready_event,
        )
    return _guardian(
        args.auth_root,
        args.browser_job_file,
        args.owner_metadata_file,
        args.result_file,
        args.ready_event,
        args.owner_ready_event,
        args.armed_event,
        args.fault,
    )


if __name__ == "__main__":
    raise SystemExit(main())
