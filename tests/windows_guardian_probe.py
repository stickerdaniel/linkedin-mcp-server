"""Native Windows evidence probe for the owner-crash profile fence."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import datetime
import faulthandler
import importlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
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


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", _FileTime),
        ("ftLastAccessTime", _FileTime),
        ("ftLastWriteTime", _FileTime),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


_get_file_information_by_handle: Any | None = None
_get_file_information_last_error: Callable[[], int] | None = None
_get_file_information_error: Callable[[int], BaseException] | None = None


def _phase(name: str) -> None:
    print(f"windows-guardian-probe phase={name}", file=sys.stderr, flush=True)


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


def query_control_job_membership(
    job: Any,
    watched_process: Any | None,
    *,
    win32api: Any,
    win32con: Any,
    win32job: Any,
) -> tuple[bool, bool | None]:
    _phase("duplicate-real-self-process-handle")
    pseudo = win32api.GetCurrentProcess()
    real_self = win32api.DuplicateHandle(
        pseudo,
        pseudo,
        pseudo,
        0,
        False,
        win32con.DUPLICATE_SAME_ACCESS,
    )
    try:
        _phase("query-self-browser-job-membership")
        current = bool(win32job.IsProcessInJob(real_self, job))
        watched = None
        if watched_process is not None:
            _phase("query-watched-browser-job-membership")
            watched = bool(win32job.IsProcessInJob(watched_process, job))
        return current, watched
    finally:
        _phase("close-real-self-process-handle")
        real_self.Close()


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


def _region_api() -> tuple[Callable[[int, int], bool], Callable[[int, int], None]]:
    """Return probe-local one-byte LockFileEx helpers."""
    import ctypes
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    lock_file_ex = kernel32.LockFileEx
    lock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    lock_file_ex.restype = wintypes.BOOL
    unlock_file_ex = kernel32.UnlockFileEx
    unlock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    unlock_file_ex.restype = wintypes.BOOL

    def handle(fd: int) -> int:
        import msvcrt

        return int(getattr(msvcrt, "get_osfhandle")(fd))

    def try_lock(fd: int, offset: int) -> bool:
        overlapped = Overlapped()
        overlapped.Offset = offset
        _phase(f"lock-file-region-{offset}")
        if lock_file_ex(
            handle(fd), 0x00000001 | 0x00000002, 0, 1, 0, ctypes.byref(overlapped)
        ):
            return True
        error = getattr(ctypes, "get_last_error")()
        if error == 33:  # ERROR_LOCK_VIOLATION
            return False
        raise getattr(ctypes, "WinError")(error)

    def unlock(fd: int, offset: int) -> None:
        overlapped = Overlapped()
        overlapped.Offset = offset
        _phase(f"unlock-file-region-{offset}")
        if not unlock_file_ex(handle(fd), 0, 1, 0, ctypes.byref(overlapped)):
            raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())

    return try_lock, unlock


def conjunction_admission(
    fd: int,
    *,
    try_lock: Callable[[int, int], bool] | None = None,
    unlock: Callable[[int, int], None] | None = None,
    close: Callable[[int], None] = os.close,
) -> bool:
    """Acquire A then transient B, retaining A only on full admission."""
    if try_lock is None or unlock is None:
        try_lock, unlock = _region_api()

    def rescue_close() -> None:
        try:
            close(fd)
        except BaseException:
            pass

    def rollback_a(first_error: BaseException | None) -> None:
        try:
            unlock(fd, 0)
        except BaseException as unlock_error:
            rescue_close()
            raise first_error or unlock_error
        if first_error is not None:
            raise first_error

    if not try_lock(fd, 0):
        return False
    try:
        acquired_b = try_lock(fd, 1)
    except BaseException as lock_error:
        rollback_a(lock_error)
        raise AssertionError("A rollback unexpectedly returned")
    if not acquired_b:
        rollback_a(None)
        return False
    try:
        unlock(fd, 1)
    except BaseException as unlock_error:
        # Closing is the only safe rescue when the offset-specific unlock failed:
        # it releases both regions and prevents a caller from treating A as held.
        rescue_close()
        raise unlock_error
    return True


def probe_conjunction_regions(fd: int) -> tuple[bool, bool]:
    """Try A then B and release every acquired region before returning."""
    try_lock, unlock = _region_api()
    acquired_a = try_lock(fd, 0)
    if not acquired_a:
        return False, False
    try:
        acquired_b = try_lock(fd, 1)
        if acquired_b:
            unlock(fd, 1)
        return True, acquired_b
    finally:
        unlock(fd, 0)


def _file_information_api() -> tuple[
    Any, Callable[[], int], Callable[[int], BaseException]
]:
    global _get_file_information_by_handle
    global _get_file_information_error
    global _get_file_information_last_error

    if _get_file_information_by_handle is None:
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        get_info = kernel32.GetFileInformationByHandle
        get_info.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        get_info.restype = ctypes.c_int32
        _get_file_information_by_handle = get_info
        _get_file_information_last_error = getattr(ctypes, "get_last_error")
        _get_file_information_error = getattr(ctypes, "WinError")
    assert _get_file_information_last_error is not None
    assert _get_file_information_error is not None
    return (
        _get_file_information_by_handle,
        _get_file_information_last_error,
        _get_file_information_error,
    )


def _identity_from_file_information(
    info: _ByHandleFileInformation,
) -> tuple[int, int, int]:
    return (
        int(info.dwVolumeSerialNumber),
        int(info.nFileIndexHigh),
        int(info.nFileIndexLow),
    )


def file_identity(fd: int) -> tuple[int, int, int]:
    """Read the stable Windows identity of an open file descriptor."""
    import msvcrt

    get_info, get_last_error, win_error = _file_information_api()
    info = _ByHandleFileInformation()
    handle = getattr(msvcrt, "get_osfhandle")(fd)
    _phase("get-file-information-by-handle")
    if not get_info(handle, ctypes.byref(info)):
        raise win_error(get_last_error())
    return _identity_from_file_information(info)


def require_same_file_identity(
    expected: tuple[int, int, int] | list[int],
    observed: tuple[int, int, int] | list[int],
) -> None:
    if tuple(expected) != tuple(observed):
        raise RuntimeError("owner and guardian do not reference the same file identity")


def require_locked_actor_identity(
    fd: int,
    path: Path,
    expected: tuple[int, int, int] | list[int],
    *,
    identity: Callable[[int], tuple[int, int, int]] = file_identity,
    still_at: Callable[[int, Path], bool] | None = None,
) -> None:
    if still_at is None:
        from linkedin_mcp_server.common_utils import is_still_at

        still_at = is_still_at
    require_same_file_identity(expected, identity(fd))
    # The probe root is private to its harness. Under that protected-parent
    # premise, the post-lock path check closes the separate-open split election.
    if not still_at(fd, path):
        raise RuntimeError("locked profile file is no longer at its expected path")


def guardian_publication_sequence(
    *,
    acquire_b: Callable[[], bool],
    owner_alive: Callable[[], bool],
    release_b: Callable[[], None],
    publish_armed: Callable[[], None],
) -> bool:
    """Publish only after this process owns B and rechecks the owner handle."""
    if not acquire_b():
        return False
    if not owner_alive():
        release_b()
        return False
    publish_armed()
    return True


def zero_proven_release_sequence(
    *,
    publish_zero: Callable[[], None],
    wait_allow_release: Callable[[], None],
    close_job: Callable[[], None],
    release_b: Callable[[], None],
) -> None:
    publish_zero()
    wait_allow_release()
    close_job()
    release_b()


def conjunction_guardian_shutdown(
    result: dict[str, Any],
    *,
    active_descendants: Callable[[], int],
    terminate_job: Callable[[], None],
    query_job: Callable[[], int],
    wait_for_retry: Callable[[], None],
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    result["terminate_attempted"] = True
    terminate_job()
    result["terminate_completed"] = True
    deadline = monotonic() + _DEADLINE_SECONDS
    result["query_samples"] = []
    while True:
        active = query_job()
        living = active_descendants()
        result["query_samples"].append(
            {"active_processes": active, "active_descendants": living}
        )
        if active == 0 and living == 0:
            result["zero_proven"] = True
            return
        if monotonic() >= deadline:
            result["query_timeout"] = True
            raise TimeoutError("browser Job did not drain before its deadline")
        wait_for_retry()


def run_guardian_fail_closed(
    *,
    shutdown: Callable[[], None],
    publish_failure: Callable[[BaseException], None],
    hold_failure: Callable[[], None],
    release_b: Callable[[], None],
) -> bool:
    """Run shutdown while reserving B release for the proven-success caller."""
    _ = release_b
    try:
        shutdown()
    except BaseException as exc:
        try:
            publish_failure(exc)
        finally:
            hold_failure()
        raise RuntimeError("the failed conjunction proof resumed unexpectedly") from exc
    return True


def wait_on_unsignaled_throttle(
    throttle: Any,
    *,
    wait: Callable[[Any, int], Any],
) -> None:
    wait(throttle, 1)


def _actor_event(name: str, access: int) -> Any:
    _win32api, _win32con, win32event, _win32job = _windows_modules()
    return win32event.OpenEvent(access, False, name)


def _actor_wait(name: str) -> None:
    _win32api, win32con, _win32event, _win32job = _windows_modules()
    handle = _actor_event(name, win32con.SYNCHRONIZE)
    try:
        _wait(handle, _DEADLINE_SECONDS, f"event {name} was not signaled")
    finally:
        handle.Close()


class _ActorFd:
    def __init__(self, fd: int, *, close: Callable[[int], None] = os.close) -> None:
        self.fd = fd
        self._close = close

    def close(self, fd: int | None = None) -> None:
        if self.fd < 0:
            return
        if fd is not None and fd != self.fd:
            raise RuntimeError("actor close targeted a different descriptor")
        current = self.fd
        self._close(current)
        self.fd = -1


def acquire_actor_region(
    descriptor: _ActorFd,
    path: Path,
    expected_identity: list[int],
    offset: int,
    *,
    try_lock: Callable[[int, int], bool],
    unlock: Callable[[int, int], None],
    identity: Callable[[int], tuple[int, int, int]] = file_identity,
    still_at: Callable[[int, Path], bool] | None = None,
) -> bool:
    if not try_lock(descriptor.fd, offset):
        return False
    try:
        require_locked_actor_identity(
            descriptor.fd,
            path,
            expected_identity,
            identity=identity,
            still_at=still_at,
        )
    except BaseException as first_error:
        try:
            unlock(descriptor.fd, offset)
        except BaseException:
            pass
        try:
            descriptor.close()
        except BaseException:
            pass
        raise first_error
    return True


def _actor_admission(
    descriptor: _ActorFd,
    *,
    try_lock: Callable[[int, int], bool],
    unlock: Callable[[int, int], None],
) -> bool:
    return conjunction_admission(
        descriptor.fd,
        try_lock=try_lock,
        unlock=unlock,
        close=descriptor.close,
    )


def production_byte_zero_admission(
    descriptor: _ActorFd,
    path: Path,
    expected_identity: list[int],
    *,
    try_lock: Callable[[int, int], bool],
    unlock: Callable[[int, int], None],
    identity: Callable[[int], tuple[int, int, int]] = file_identity,
    still_at: Callable[[int, Path], bool] | None = None,
) -> bool:
    """Model the current production protocol without conjunction helpers."""
    return acquire_actor_region(
        descriptor,
        path,
        expected_identity,
        0,
        try_lock=try_lock,
        unlock=unlock,
        identity=identity,
        still_at=still_at,
    )


def _finish_actor_fd(descriptor: _ActorFd, first_error: BaseException | None) -> None:
    try:
        descriptor.close()
    except BaseException as close_error:
        if first_error is None:
            raise close_error
    if first_error is not None:
        raise first_error


def _actor(
    mode: str,
    lock_path: Path,
    expected_identity: list[int],
    ready_event: str,
    result_file: Path,
    options: dict[str, Any],
) -> int:
    win32api, win32con, win32event, win32job = _windows_modules()
    descriptor = _ActorFd(_probe_fd(lock_path))
    fd = descriptor.fd
    try_lock, unlock = _region_api()
    result: dict[str, Any] = {"mode": mode}
    job = None
    process_handle = None
    descendant_handles: list[Any] = []
    locked_offset: int | None = None
    first_error: BaseException | None = None
    try:
        job_name = options.get("job_name")
        if job_name:
            job = win32job.OpenJobObject(
                win32job.JOB_OBJECT_ALL_ACCESS, False, job_name
            )
        owner_handle = int(options.get("owner_handle", 0))
        guardian_handle = int(options.get("guardian_handle", 0))
        if owner_handle:
            process_handle = owner_handle
        elif guardian_handle:
            process_handle = guardian_handle
        if job is not None and process_handle:
            current_in_browser_job, watched_in_browser_job = (
                query_control_job_membership(
                    job,
                    process_handle,
                    win32api=win32api,
                    win32con=win32con,
                    win32job=win32job,
                )
            )
            result["current_process_in_browser_job"] = current_in_browser_job
            result["watched_process_in_browser_job"] = watched_in_browser_job
            if current_in_browser_job or watched_in_browser_job:
                raise RuntimeError("control process entered the browser Job")

        pause_event = options.get("pause_event")
        if pause_event:
            _signal(ready_event)
            _actor_wait(pause_event)

        if mode == "region-probe":
            acquired_a = try_lock(fd, 0)
            acquired_b = False
            if acquired_a:
                try:
                    require_locked_actor_identity(fd, lock_path, expected_identity)
                    result["file_identity"] = list(file_identity(fd))
                    acquired_b = try_lock(fd, 1)
                    if acquired_b:
                        unlock(fd, 1)
                finally:
                    unlock(fd, 0)
            result.update({"acquired_a": acquired_a, "acquired_b": acquired_b})
            _atomic_json(result_file, result)
            _signal(ready_event)
            return 0

        if mode == "attempt":
            acquired = _actor_admission(descriptor, try_lock=try_lock, unlock=unlock)
            result["acquired"] = acquired
            if acquired:
                locked_offset = 0
                require_locked_actor_identity(fd, lock_path, expected_identity)
                result["file_identity"] = list(file_identity(fd))
                hold_event = options.get("hold_event")
                _atomic_json(result_file, result)
                _signal(ready_event)
                if hold_event:
                    _actor_wait(hold_event)
                unlock(fd, 0)
                locked_offset = None
            else:
                _atomic_json(result_file, result)
                _signal(ready_event)
            return 0

        if mode == "production-byte-zero":
            acquired = production_byte_zero_admission(
                descriptor,
                lock_path,
                expected_identity,
                try_lock=try_lock,
                unlock=unlock,
            )
            result.update(
                {
                    "acquired": acquired,
                    "protocol": "current-source-production-byte-zero",
                    "offset": 0,
                }
            )
            if acquired:
                locked_offset = 0
                result["file_identity"] = list(file_identity(fd))
            _atomic_json(result_file, result)
            _signal(ready_event)
            hold_event = options.get("hold_event")
            if acquired and hold_event:
                _actor_wait(hold_event)
            if acquired:
                unlock(fd, 0)
                locked_offset = None
            return 0

        offset = int(options["offset"])
        if mode == "guardian-publish":
            acquired_b = False
            owner_alive_after_b: bool | None = None

            def acquire_b() -> bool:
                nonlocal acquired_b, locked_offset
                acquired_b = acquire_actor_region(
                    descriptor,
                    lock_path,
                    expected_identity,
                    offset,
                    try_lock=try_lock,
                    unlock=unlock,
                )
                if acquired_b:
                    locked_offset = offset
                    result["file_identity"] = list(file_identity(fd))
                return acquired_b

            def owner_alive() -> bool:
                nonlocal owner_alive_after_b
                owner_alive_after_b = _is_active(process_handle)
                return owner_alive_after_b

            def release_b() -> None:
                nonlocal locked_offset
                unlock(fd, offset)
                locked_offset = None

            def publish_armed() -> None:
                result["armed"] = True
                armed_event = options.get("armed_event")
                if armed_event:
                    _signal(armed_event)

            armed = guardian_publication_sequence(
                acquire_b=acquire_b,
                owner_alive=owner_alive,
                release_b=release_b,
                publish_armed=publish_armed,
            )
            result["contention"] = not acquired_b
            result["owner_alive_after_b"] = owner_alive_after_b
            result["armed"] = armed
            result["job_authority"] = job is not None
            result["browser_authority"] = False
            _atomic_json(result_file, result)
            _signal(ready_event)
            if armed and options.get("hold_event"):
                _actor_wait(options["hold_event"])
            return 0

        if not acquire_actor_region(
            descriptor,
            lock_path,
            expected_identity,
            offset,
            try_lock=try_lock,
            unlock=unlock,
        ):
            result["contention"] = True
            _atomic_json(result_file, result)
            _signal(ready_event)
            return 0
        locked_offset = offset
        result["file_identity"] = list(file_identity(fd))

        if mode == "guardian-drain":
            identity_name = options.get("identity_mutex")
            identity = win32event.CreateMutex(None, True, identity_name)
            result["identity_mutex"] = identity_name
            result["fault"] = options.get("fault", "none")
            _atomic_json(result_file, result)
            _signal(ready_event)
            _wait(process_handle, _DEADLINE_SECONDS, "owner did not exit")
            descendant_handles = [
                win32api.OpenProcess(win32con.SYNCHRONIZE, False, int(pid))
                for pid in options["descendant_pids"]
            ]
            throttle = win32event.CreateEvent(None, False, False, None)
            timeout_clock = iter([0.0, _DEADLINE_SECONDS + 1.0])

            def mark_fault(operation: str) -> None:
                result["fault_operation"] = operation
                result["fault_injected"] = result["fault"]

            def terminate_job() -> None:
                if result["fault"] == "terminate-error":
                    mark_fault("terminate")
                    raise OSError("injected browser Job termination failure")
                win32job.TerminateJobObject(job, 201)

            def query_job() -> int:
                if result["fault"] == "query-error":
                    mark_fault("query")
                    raise OSError("injected browser Job query failure")
                active = int(
                    win32job.QueryInformationJobObject(
                        job, win32job.JobObjectBasicAccountingInformation
                    )["ActiveProcesses"]
                )
                if result["fault"] == "drain-timeout":
                    mark_fault("deadline")
                    return max(1, active)
                return active

            def release_b() -> None:
                nonlocal locked_offset
                unlock(fd, offset)
                locked_offset = None

            def publish_failure(exc: BaseException) -> None:
                result["error_type"] = type(exc).__name__
                result["error"] = f"{type(exc).__name__}: {exc}"
                _atomic_json(result_file, result)
                _signal(options["fault_event"])

            def hold_failure() -> None:
                threading.Event().wait()

            run_guardian_fail_closed(
                shutdown=lambda: conjunction_guardian_shutdown(
                    result,
                    active_descendants=lambda: sum(
                        _is_active(handle) for handle in descendant_handles
                    ),
                    terminate_job=terminate_job,
                    query_job=query_job,
                    wait_for_retry=lambda: wait_on_unsignaled_throttle(
                        throttle, wait=win32event.WaitForSingleObject
                    ),
                    monotonic=(
                        lambda: (
                            next(timeout_clock)
                            if result["fault"] == "drain-timeout"
                            else time.monotonic()
                        )
                    ),
                ),
                publish_failure=publish_failure,
                hold_failure=hold_failure,
                release_b=release_b,
            )
            throttle.Close()

            def publish_zero() -> None:
                _atomic_json(result_file, result)
                _signal(options["zero_event"])

            def close_retained_job() -> None:
                nonlocal job
                retained_job = job
                if retained_job is None:
                    raise RuntimeError("guardian lost its browser Job handle")
                retained_job.Close()
                job = None

            zero_proven_release_sequence(
                publish_zero=publish_zero,
                wait_allow_release=lambda: _actor_wait(options["allow_release_event"]),
                close_job=close_retained_job,
                release_b=release_b,
            )
            win32event.ReleaseMutex(identity)
            identity.Close()
            return 0

        if mode == "owner-watch":
            _atomic_json(result_file, result)
            _signal(ready_event)
            _wait(process_handle, _DEADLINE_SECONDS, "guardian did not exit")
            result["guardian_exit_observed"] = True
            _atomic_json(result_file, result)
            _signal(options["guardian_exit_event"])
            _actor_wait(options["begin_drain_event"])
            descendant_handles = open_descendant_handles_before_terminate(
                open_handles=lambda: [
                    win32api.OpenProcess(win32con.SYNCHRONIZE, False, int(pid))
                    for pid in options["descendant_pids"]
                ],
                terminate_job=lambda: win32job.TerminateJobObject(job, 202),
            )
            deadline = time.monotonic() + _DEADLINE_SECONDS
            throttle = win32event.CreateEvent(None, False, False, None)
            try:
                while True:
                    active = int(
                        win32job.QueryInformationJobObject(
                            job, win32job.JobObjectBasicAccountingInformation
                        )["ActiveProcesses"]
                    )
                    if active == 0 and not any(
                        _is_active(handle) for handle in descendant_handles
                    ):
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("owner could not drain browser Job")
                    wait_on_unsignaled_throttle(
                        throttle, wait=win32event.WaitForSingleObject
                    )
            finally:
                throttle.Close()
            result["zero_proven"] = True
            _atomic_json(result_file, result)
            _signal(options["zero_event"])
            _actor_wait(options["allow_a_release_event"])
            unlock(fd, offset)
            locked_offset = None
            return 0

        _atomic_json(result_file, result)
        _signal(ready_event)
        hold_event = options.get("hold_event")
        if hold_event:
            _actor_wait(hold_event)
        else:
            threading.Event().wait()
        return 0
    except BaseException as exc:
        first_error = exc
    finally:
        for handle in descendant_handles:
            try:
                handle.Close()
            except BaseException as exc:
                first_error = first_error or exc
        if locked_offset is not None:
            try:
                unlock(fd, locked_offset)
            except BaseException as exc:
                first_error = first_error or exc
        if job is not None:
            try:
                job.Close()
            except BaseException as exc:
                first_error = first_error or exc
        if process_handle:
            try:
                win32api.CloseHandle(process_handle)
            except BaseException as exc:
                first_error = first_error or exc
        _finish_actor_fd(descriptor, first_error)
    raise AssertionError("actor exception cleanup returned without raising")


def close_preserving_error(
    close: Callable[[], None], first_error: BaseException | None
) -> None:
    try:
        close()
    except BaseException as close_error:
        if first_error is None:
            raise close_error
    if first_error is not None:
        raise first_error


def retry_lock_rundown[T](
    attempt: Callable[[], T | None],
    *,
    deadline: float,
    wait_for_retry: Callable[[], None],
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[T, int, float]:
    started = monotonic()
    attempts = 0
    while True:
        attempts += 1
        result = attempt()
        if result is not None:
            return result, attempts, monotonic() - started
        if monotonic() >= deadline:
            raise TimeoutError(
                "Windows lock rundown did not complete before its deadline"
            )
        wait_for_retry()


def retry_admission_after_drain(
    *,
    open_fd: Callable[[], int],
    try_admission: Callable[[int, Callable[[int], None]], bool],
    release_a: Callable[[int], None],
    close_fd: Callable[[int], None],
    deadline: float,
    wait_for_retry: Callable[[], None],
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[bool, int, float]:
    def attempt() -> bool | None:
        descriptor = _ActorFd(open_fd(), close=close_fd)
        first_error: BaseException | None = None
        try:
            if not try_admission(descriptor.fd, descriptor.close):
                return None
            release_a(descriptor.fd)
            return True
        except BaseException as exc:
            first_error = exc
        finally:
            _finish_actor_fd(descriptor, first_error)
        raise AssertionError("retry admission cleanup returned without raising")

    return retry_lock_rundown(
        attempt,
        deadline=deadline,
        wait_for_retry=wait_for_retry,
        monotonic=monotonic,
    )


def retry_contended_publication[T](
    attempt: Callable[[], T | None],
    *,
    require_window: Callable[[], object],
    deadline: float,
    wait_for_retry: Callable[[], None],
    discard_result: Callable[[T], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[T, int, float]:
    """Retry contention while proving the protected observation window remains."""
    accepted_result: T | None = None

    def witnessed_attempt() -> T | None:
        nonlocal accepted_result
        require_window()
        result = attempt()
        try:
            require_window()
        except BaseException as exc:
            if result is not None and discard_result is not None:
                close_preserving_error(lambda: discard_result(result), exc)
            raise
        accepted_result = result
        return result

    try:
        outcome = retry_lock_rundown(
            witnessed_attempt,
            deadline=deadline,
            wait_for_retry=wait_for_retry,
            monotonic=monotonic,
        )
    except BaseException as exc:
        owned_result = accepted_result
        if owned_result is not None and discard_result is not None:
            close_preserving_error(lambda: discard_result(owned_result), exc)
        raise
    accepted_result = None
    return outcome


def require_publication_witnesses[T](
    survivors: dict[str, T],
    *,
    is_active: Callable[[T], bool],
    child_handles: list[T] | None = None,
    required_children: list[T] | None = None,
) -> list[T]:
    for label, handle in survivors.items():
        if not is_active(handle):
            raise RuntimeError(f"{label} exited during entrant publication")
    if child_handles is None:
        return []
    live_children = [handle for handle in child_handles if is_active(handle)]
    if not live_children:
        raise RuntimeError("no child process survived entrant publication")
    if required_children is not None and not any(
        required is observed
        for required in required_children
        for observed in live_children
    ):
        raise RuntimeError("no pre-entry child process survived entrant publication")
    return live_children


def raise_cleanup_error_unless_unwinding(
    cleanup_error: BaseException | None,
    active_exception: BaseException | None,
) -> None:
    if cleanup_error is not None and active_exception is None:
        raise cleanup_error


def open_descendant_handles_before_terminate[T](
    *,
    open_handles: Callable[[], list[T]],
    terminate_job: Callable[[], None],
) -> list[T]:
    handles = open_handles()
    terminate_job()
    return handles


def observe_browser_publication_order(
    *,
    browser_started: Callable[[], bool],
    observe_armed: Callable[[], None],
    release_gate: Callable[[], None],
    observe_browser_start: Callable[[], None],
) -> bool:
    if browser_started():
        raise RuntimeError("browser started before guardian publication")
    observe_armed()
    if browser_started():
        raise RuntimeError("browser started before its gate was released")
    release_gate()
    observe_browser_start()
    return True


def spawn_with_duplicated_handles[T](
    sources: list[int],
    *,
    build_arguments: Callable[[dict[int, int]], list[str]],
    duplicate: Callable[[int], Any],
    launch: Callable[[list[str], list[int]], T],
    close_duplicate: Callable[[Any], None],
) -> T:
    duplicates: list[Any] = []
    first_error: BaseException | None = None
    try:
        mapping: dict[int, int] = {}
        for source in dict.fromkeys(sources):
            duplicated = duplicate(source)
            duplicates.append(duplicated)
            mapping[source] = int(duplicated)
        return launch(build_arguments(mapping), list(mapping.values()))
    except BaseException as exc:
        first_error = exc
    finally:
        for duplicated in duplicates:
            try:
                close_duplicate(duplicated)
            except BaseException as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error
    raise AssertionError("duplicated-handle launch returned no process")


def _spawn_inheriting(
    arguments: list[str], handles: list[int]
) -> subprocess.Popen[bytes]:
    startup = None
    if handles:
        startup = getattr(subprocess, "STARTUPINFO")()
        startup.lpAttributeList = {"handle_list": handles}
    return subprocess.Popen(
        arguments,
        cwd=_REPO_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        startupinfo=startup,
    )


def _new_event(win32event: Any, label: str) -> tuple[str, Any]:
    name = _new_event_name(label)
    return name, win32event.CreateEvent(None, True, False, name)


def _spawn_actor(
    mode: str,
    lock_path: Path,
    expected_identity: list[int],
    ready_name: str,
    result_file: Path,
    options: dict[str, Any],
    extra_handles: list[int] | None = None,
) -> subprocess.Popen[bytes]:
    win32api, win32con, _win32event, _win32job = _windows_modules()
    sources = list(extra_handles or [])

    def duplicate(source: int) -> Any:
        _phase("duplicate-inherited-process-handle")
        current = win32api.GetCurrentProcess()
        return win32api.DuplicateHandle(
            current,
            source,
            current,
            0,
            True,
            win32con.DUPLICATE_SAME_ACCESS,
        )

    def build_arguments(mapping: dict[int, int]) -> list[str]:
        inherited_options = dict(options)
        for key in ("owner_handle", "guardian_handle"):
            source = int(inherited_options.get(key, 0))
            if source:
                inherited_options[key] = mapping[source]
        return [
            sys.executable,
            str(Path(__file__).resolve()),
            "actor",
            mode,
            str(lock_path),
            json.dumps(expected_identity),
            ready_name,
            str(result_file),
            json.dumps(inherited_options),
        ]

    return spawn_with_duplicated_handles(
        sources,
        build_arguments=build_arguments,
        duplicate=duplicate,
        launch=_spawn_inheriting,
        close_duplicate=lambda handle: handle.Close(),
    )


def _terminate_process(process: Any) -> None:
    win32api, _win32con, _win32event, _win32job = _windows_modules()
    handle = getattr(process, "_handle", None)
    if handle is None:
        raise RuntimeError("Windows Popen exposed no stable process handle")
    _phase("check-process-before-terminate")
    if _is_active(handle):
        _phase("terminate-process-stable-handle")
        win32api.TerminateProcess(handle, 203)
    _phase("wait-process-stable-handle")
    _wait(handle, _DEADLINE_SECONDS, "process did not terminate")
    if _is_active(handle):
        raise RuntimeError("process remained active after stable-handle wait")
    process.wait(timeout=_DEADLINE_SECONDS)


def _probe_fd(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    return os.open(path, os.O_RDWR | os.O_CREAT | cloexec, 0o600)


def _attempt_here(path: Path) -> tuple[bool, int]:
    fd = _probe_fd(path)
    try:
        acquired = conjunction_admission(fd)
    except BaseException:
        # Admission may already have closed the descriptor as its lock rescue.
        # A second close is harmless here, but its EBADF must not hide that cause.
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
    return acquired, fd


def require_topology_actor_ready(
    process: Any,
    phase: str,
    *,
    wait_ready: Callable[[], None],
) -> None:
    try:
        wait_ready()
    except BaseException as exc:
        stdout, stderr = (
            process.communicate() if process.poll() is not None else (b"", b"")
        )
        raise RuntimeError(
            f"topology actor failed before ready: phase={phase} "
            f"returncode={process.poll()!r} stdout={stdout!r} stderr={stderr!r}"
        ) from exc


def require_successful_actor_completion(
    process: Any,
    phase: str,
    *,
    timeout: float = _DEADLINE_SECONDS,
) -> None:
    try:
        process.wait(timeout=timeout)
    except BaseException as exc:
        raise RuntimeError(f"actor did not complete: phase={phase}") from exc
    if process.returncode != 0:
        stdout, stderr = process.communicate()
        raise RuntimeError(
            f"actor failed: phase={phase} returncode={process.returncode!r} "
            f"stdout={stdout!r} stderr={stderr!r}"
        )


def classify_breakaway_result(
    *,
    process_created: bool,
    memberships: dict[str, bool] | None,
    error_code: int | None,
) -> str:
    if not process_created:
        if error_code is None:
            raise RuntimeError("a failed breakaway creation has no Win32 error")
        return "could-not-start"
    if memberships is None:
        raise RuntimeError("a created breakaway process has no membership witness")
    if set(memberships) != {"inner", "outer"}:
        raise RuntimeError("breakaway membership must identify inner and outer Jobs")
    if not memberships["inner"] and memberships["outer"]:
        return "inner-only-breakaway"
    if not memberships["inner"] and not memberships["outer"]:
        return "all-known-jobs-breakaway"
    if memberships["inner"] and memberships["outer"]:
        return "retained-in-inner-and-outer"
    return "retained-in-inner-only"


def require_accepted_breakaway_result(
    scenario: str,
    *,
    inner_breakaway_enabled: bool,
    process_created: bool,
    memberships: dict[str, bool] | None,
    creation_error: dict[str, int | str] | None,
) -> str:
    expected_breakaway = scenario == "job-topology-breakaway"
    if scenario not in {
        "job-topology-breakaway",
        "job-topology-breakaway-denied",
    }:
        raise RuntimeError(f"unsupported breakaway scenario: {scenario}")
    if inner_breakaway_enabled is not expected_breakaway:
        raise RuntimeError("inner breakaway configuration does not match the scenario")

    error_code = None
    if creation_error is not None:
        if creation_error.get("operation") != "Popen":
            raise RuntimeError("creation refusal did not come from Popen")
        if creation_error.get("phase") != "guardian-candidate-creation":
            raise RuntimeError("creation refusal has the wrong phase")
        observed_code = creation_error.get("win32_error")
        if not isinstance(observed_code, int):
            raise RuntimeError("creation refusal has no numeric Win32 error")
        error_code = observed_code

    classification = classify_breakaway_result(
        process_created=process_created,
        memberships=memberships,
        error_code=error_code,
    )
    if not process_created:
        if expected_breakaway:
            raise RuntimeError("breakaway-enabled guardian creation was refused")
        return classification
    if creation_error is not None:
        raise RuntimeError("a created guardian also reported a creation refusal")
    assert memberships is not None
    if not memberships["outer"]:
        if memberships["inner"]:
            raise RuntimeError(
                "guardian retained the inner Job but escaped the outer Job"
            )
        raise RuntimeError("guardian escaped every known harness Job")
    expected = (
        {"inner": False, "outer": True}
        if expected_breakaway
        else {"inner": True, "outer": True}
    )
    if memberships != expected:
        raise RuntimeError("guardian membership is not accepted for this scenario")
    return classification


def classify_post_exit_membership(
    query: Callable[[], dict[str, bool]],
) -> dict[str, Any]:
    try:
        return {"status": "success", "memberships": query()}
    except BaseException as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def terminate_common_ancestor[T](
    actors: dict[str, T],
    *,
    is_active: Callable[[T], bool],
    terminate: Callable[[], None],
    wait: Callable[[T], None],
    exit_code: Callable[[T], int],
) -> dict[str, int]:
    for label, actor in actors.items():
        if not is_active(actor):
            raise RuntimeError(f"{label} exited before common-ancestor termination")
    terminate()
    exit_codes: dict[str, int] = {}
    for label, actor in actors.items():
        wait(actor)
        code = exit_code(actor)
        if code != 204:
            raise RuntimeError(
                f"{label} exited with {code}, not common-ancestor code 204"
            )
        exit_codes[label] = code
    return exit_codes


def membership_matrix(
    *,
    inner: bool,
    outer: bool,
) -> dict[str, bool]:
    return {"inner": inner, "outer": outer}


_BROWSER_DEADLINE_SECONDS = 60.0
_BROWSER_REQUIRED_ROLES = frozenset({"browser", "renderer"})
_BROWSER_ROLE_ALIASES = {
    "browser": "browser",
    "renderer": "renderer",
    "gpu-process": "gpu",
    "gpu": "gpu",
    "utility": "utility",
    "crashpad-handler": "crashpad",
    "crashpad": "crashpad",
}


def normalize_cdp_processes(entries: list[dict[str, Any]]) -> dict[int, str]:
    """Normalize CDP process rows without discarding unrecognized roles."""
    normalized: dict[int, str] = {}
    for entry in entries:
        pid = entry.get("id", entry.get("pid"))
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise RuntimeError("CDP process inventory contains a non-positive PID")
        raw_role = entry.get("type", entry.get("role", "unknown"))
        if not isinstance(raw_role, str) or not raw_role.strip():
            raw_role = "unknown"
        role_key = raw_role.strip().lower().replace("_", "-")
        role = _BROWSER_ROLE_ALIASES.get(role_key, f"unknown:{raw_role.strip()}")
        previous = normalized.get(pid)
        if previous is not None and previous != role:
            raise RuntimeError(f"CDP PID {pid} has conflicting roles")
        normalized[pid] = role
    return normalized


def require_browser_inventory(
    cdp_processes: dict[int, str], retained_pids: set[int], driver_pid: int
) -> dict[str, Any]:
    roles = set(cdp_processes.values())
    missing = _BROWSER_REQUIRED_ROLES - roles
    if missing:
        raise RuntimeError(f"browser inventory lacks required roles: {sorted(missing)}")
    if driver_pid not in retained_pids:
        raise RuntimeError(
            "the Patchright driver is absent from the retained Job census"
        )
    missing_handles = set(cdp_processes) - retained_pids
    if missing_handles:
        raise RuntimeError(
            f"CDP processes are absent from the retained Job census: {sorted(missing_handles)}"
        )
    return {
        "cdp_processes": [
            {"pid": pid, "role": role} for pid, role in sorted(cdp_processes.items())
        ],
        "unclassified_job_pids": sorted(retained_pids - set(cdp_processes)),
        "required_roles": sorted(_BROWSER_REQUIRED_ROLES),
    }


def retain_stable_browser_inventory[T](
    *,
    sample_cdp: Callable[[], list[dict[str, Any]]],
    sample_job_pids: Callable[[], set[int]],
    open_handle: Callable[[int], T],
    validate_handle: Callable[[int, T], None],
    close_handle: Callable[[T], None],
    driver_pid: int,
    deadline: float,
    wait_for_retry: Callable[[], None],
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[dict[int, str], dict[int, T]]:
    """Retain one quiescent two-sided census under a single deadline."""
    while True:
        provisional: dict[int, T] = {}
        first_error: BaseException | None = None
        try:
            cdp_a = normalize_cdp_processes(sample_cdp())
            job_a = sample_job_pids()
            for pid in sorted(job_a):
                handle = open_handle(pid)
                provisional[pid] = handle
                validate_handle(pid, handle)
            cdp_b = normalize_cdp_processes(sample_cdp())
            job_b = sample_job_pids()
            if cdp_a == cdp_b and job_a == job_b == set(provisional):
                require_browser_inventory(cdp_b, set(provisional), driver_pid)
                for pid, handle in provisional.items():
                    validate_handle(pid, handle)
                return cdp_b, provisional
        except BaseException as exc:
            first_error = exc
        if first_error is not None:
            for handle in provisional.values():
                with contextlib.suppress(BaseException):
                    close_handle(handle)
            raise first_error
        for handle in provisional.values():
            close_handle(handle)
        if monotonic() >= deadline:
            raise TimeoutError("browser process census did not become quiescent")
        wait_for_retry()


def wait_handles_to_deadline[T](
    handles: dict[int, T],
    *,
    wait_one: Callable[[T, int], bool],
    deadline: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    for pid, handle in handles.items():
        remaining = deadline - monotonic()
        if remaining <= 0 or not wait_one(handle, max(1, int(remaining * 1000))):
            raise TimeoutError(f"process {pid} remained active at the shared deadline")


def browser_guardian_shutdown[T](
    *,
    retained: dict[int, T],
    active_pids: Callable[[dict[int, T]], set[int]],
    terminate_job: Callable[[], None],
    query_active_processes: Callable[[], int],
    signal_job_zero: Callable[[], None],
    wait_check_stable_handles: Callable[[], None],
    wait_handles: Callable[[dict[int, T]], None],
    signal_both_zero: Callable[[], None],
    wait_allow_fence_release: Callable[[], None],
    release_fence: Callable[[], None],
) -> dict[str, Any]:
    """Drain the retained launch while keeping the fence through both proofs."""
    before = sorted(active_pids(retained))
    terminate_job()
    active = query_active_processes()
    if active != 0:
        raise RuntimeError(f"browser Job retained {active} active processes")
    signal_job_zero()
    wait_check_stable_handles()
    wait_handles(retained)
    after = sorted(active_pids(retained))
    if after:
        raise RuntimeError(f"retained browser handles remain active: {after}")
    if query_active_processes() != 0:
        raise RuntimeError("browser Job became non-empty after retained-handle waits")
    signal_both_zero()
    wait_allow_fence_release()
    release_fence()
    return {"active_before_termination": before, "active_after_wait": after}


def topology_runner(scenario: str) -> str:
    if scenario.startswith("browser-launch-"):
        return "browser-launch"
    if scenario.startswith("job-topology-"):
        return "job-topology"
    if scenario.startswith("conjunction-"):
        return "conjunction"
    if scenario.startswith("falsification-"):
        return "region-falsification"
    return "crash-fence"


def _win32_error_code(exc: BaseException) -> int:
    code = getattr(exc, "winerror", None)
    if isinstance(code, int):
        return code
    if exc.args and isinstance(exc.args[0], int):
        return int(exc.args[0])
    raise RuntimeError("the Win32 failure exposed no numeric error code") from exc


def _configure_topology_job(job: Any, *, breakaway: bool, win32job: Any) -> None:
    if not breakaway:
        return
    limits = win32job.QueryInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation
    )
    limits["BasicLimitInformation"]["LimitFlags"] |= (
        win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK
    )
    win32job.SetInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation, limits
    )


def _duplicate_real_self(win32api: Any, win32con: Any) -> Any:
    pseudo = win32api.GetCurrentProcess()
    return win32api.DuplicateHandle(
        pseudo,
        pseudo,
        pseudo,
        0,
        False,
        win32con.DUPLICATE_SAME_ACCESS,
    )


def _known_memberships(
    process_handle: Any,
    *,
    inner_job: Any,
    outer_job: Any,
    win32job: Any,
) -> dict[str, bool]:
    return membership_matrix(
        inner=bool(win32job.IsProcessInJob(process_handle, inner_job)),
        outer=bool(win32job.IsProcessInJob(process_handle, outer_job)),
    )


def _popen_handle(process: subprocess.Popen[bytes]) -> Any:
    handle = getattr(process, "_handle", None)
    if handle is None:
        raise RuntimeError("Windows Popen exposed no stable process handle")
    return handle


def create_topology_process[T](
    spawn: Callable[[], T],
    *,
    register: Callable[[T], None],
    phase: str,
) -> tuple[T | None, dict[str, int | str] | None]:
    try:
        process = spawn()
    except BaseException as exc:
        return None, {
            "operation": "Popen",
            "phase": phase,
            "win32_error": _win32_error_code(exc),
        }
    register(process)
    return process, None


def _prepare_topology_holder(
    ready_event: str,
    release_event: str,
    *,
    creationflags: int = 0,
) -> tuple[tuple[str, ...], int]:
    return (
        (
            sys.executable,
            str(Path(__file__).resolve()),
            "topology-holder",
            ready_event,
            release_event,
        ),
        creationflags,
    )


def create_topology_holder(
    ready_event: str,
    release_event: str,
    *,
    creationflags: int,
    register: Callable[[subprocess.Popen[bytes]], None],
    phase: str,
    prepare: Callable[[str, str], tuple[tuple[str, ...], int]] | None = None,
) -> tuple[subprocess.Popen[bytes] | None, dict[str, int | str] | None]:
    if prepare is None:
        args, prepared_creationflags = _prepare_topology_holder(
            ready_event, release_event, creationflags=creationflags
        )
    else:
        args, prepared_creationflags = prepare(ready_event, release_event)
    return create_topology_process(
        lambda: subprocess.Popen(
            args,
            cwd=_REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=prepared_creationflags,
        ),
        register=register,
        phase=phase,
    )


def _topology_actor(
    scenario: str,
    outer_job_name: str,
    start_event: str,
    result_file: Path,
) -> int:
    win32api, win32con, win32event, win32job = _windows_modules()
    _actor_wait(start_event)
    outer_job = win32job.OpenJobObject(
        win32job.JOB_OBJECT_ALL_ACCESS, False, outer_job_name
    )
    inner_job = None
    self_handle = _duplicate_real_self(win32api, win32con)
    events: list[Any] = []
    processes: list[subprocess.Popen[bytes]] = []
    first_error: BaseException | None = None

    def event(label: str) -> tuple[str, Any]:
        name, handle = _new_event(win32event, label)
        events.append(handle)
        return name, handle

    def holder_controls(label: str) -> tuple[str, str, Any, Any]:
        ready_name, ready = event(f"{label}-ready")
        release_name, release = event(f"{label}-release")
        return ready_name, release_name, ready, release

    def spawn_holder(
        controls: tuple[str, str, Any, Any], *, creationflags: int = 0
    ) -> subprocess.Popen[bytes]:
        ready_name, release_name, _ready, _release = controls
        args, prepared_creationflags = _prepare_topology_holder(
            ready_name, release_name, creationflags=creationflags
        )
        process = subprocess.Popen(
            args,
            cwd=_REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=prepared_creationflags,
        )
        # Ownership begins when Popen returns, before readiness or Job assignment.
        processes.append(process)
        return process

    def wait_ready(process: subprocess.Popen[bytes], ready: Any, phase: str) -> None:
        require_topology_actor_ready(
            process,
            phase,
            wait_ready=lambda: _wait(
                ready, _DEADLINE_SECONDS, f"{phase} did not become ready"
            ),
        )

    try:
        actor_before = membership_matrix(
            inner=False,
            outer=bool(win32job.IsProcessInJob(self_handle, outer_job)),
        )
        if not actor_before["outer"]:
            raise RuntimeError("topology actor was outside the outer harness Job")

        if scenario == "job-topology-common-ancestor-loss":
            common_name = _new_event_name("common-ancestor-job")
            inner_job = win32job.CreateJobObject(None, common_name)
            _configure_topology_job(inner_job, breakaway=False, win32job=win32job)
            candidates: dict[str, subprocess.Popen[bytes]] = {}
            controls: dict[str, tuple[Any, Any]] = {}
            for label in ("owner", "guardian", "browser-descendant"):
                holder = holder_controls(label)
                process = spawn_holder(holder)
                candidates[label] = process
                controls[label] = (holder[2], holder[3])
                win32job.AssignProcessToJobObject(inner_job, _popen_handle(process))
                wait_ready(process, holder[2], label)
            before = {
                label: _known_memberships(
                    _popen_handle(process),
                    inner_job=inner_job,
                    outer_job=outer_job,
                    win32job=win32job,
                )
                for label, process in candidates.items()
            }
            terminated_ns = time.perf_counter_ns()
            exits: dict[str, int] = {}

            def wait_for_common_ancestor(process: subprocess.Popen[bytes]) -> None:
                label = next(
                    name
                    for name, candidate in candidates.items()
                    if candidate is process
                )
                _wait(
                    _popen_handle(process),
                    _DEADLINE_SECONDS,
                    f"{label} survived common ancestor",
                )
                exits[label] = time.perf_counter_ns()

            exit_codes = terminate_common_ancestor(
                candidates,
                is_active=lambda process: _is_active(_popen_handle(process)),
                terminate=lambda: win32job.TerminateJobObject(inner_job, 204),
                wait=wait_for_common_ancestor,
                exit_code=lambda process: process.wait(timeout=_DEADLINE_SECONDS),
            )
            after = {
                label: {
                    "active": _is_active(_popen_handle(process)),
                    "membership_diagnostic": classify_post_exit_membership(
                        lambda process=process: _known_memberships(
                            _popen_handle(process),
                            inner_job=inner_job,
                            outer_job=outer_job,
                            win32job=win32job,
                        )
                    ),
                }
                for label, process in candidates.items()
            }
            result = {
                "scenario": scenario,
                "topology": "disposable-common-ancestor",
                "actor_memberships_before": actor_before,
                "memberships_before_termination": before,
                "common_ancestor_termination_requested_ns": terminated_ns,
                "exit_observed_ns": exits,
                "exit_codes": exit_codes,
                "post_exit_observations": after,
                "browser_descendant_drained": not after["browser-descendant"]["active"],
                "structural_counterexample": not after["owner"]["active"]
                and not after["guardian"]["active"],
            }
            _atomic_json(result_file, result)
            return 0

        inner_name = _new_event_name("topology-inner-job")
        inner_job = win32job.CreateJobObject(None, inner_name)
        breakaway_enabled = scenario == "job-topology-breakaway"
        _configure_topology_job(
            inner_job, breakaway=breakaway_enabled, win32job=win32job
        )
        assignment_error = None
        try:
            win32job.AssignProcessToJobObject(inner_job, self_handle)
            assignment_succeeded = True
        except BaseException as exc:
            assignment_succeeded = False
            assignment_error = _win32_error_code(exc)
        actor_after = _known_memberships(
            self_handle,
            inner_job=inner_job,
            outer_job=outer_job,
            win32job=win32job,
        )

        if scenario == "job-topology-self-assignment":
            child_controls = holder_controls("post-assignment-child")
            child = spawn_holder(child_controls)
            child_ready, child_release = child_controls[2:]
            wait_ready(child, child_ready, "post-assignment-child")
            child_memberships = _known_memberships(
                _popen_handle(child),
                inner_job=inner_job,
                outer_job=outer_job,
                win32job=win32job,
            )
            result = {
                "scenario": scenario,
                "topology": "outer-harness-then-inner-self-assignment",
                "actor_memberships_before_assignment": actor_before,
                "self_assignment_succeeded": assignment_succeeded,
                "self_assignment_error": assignment_error,
                "actor_memberships_after_assignment": actor_after,
                "post_assignment_child": {
                    "created": True,
                    "memberships": child_memberships,
                },
            }
            _atomic_json(result_file, result)
            win32event.SetEvent(child_release)
            require_successful_actor_completion(child, "post-assignment-child")
            return 0

        ordinary_controls = holder_controls("ordinary-child")
        ordinary = spawn_holder(ordinary_controls)
        ordinary_ready, ordinary_release = ordinary_controls[2:]
        wait_ready(ordinary, ordinary_ready, "ordinary-child")
        ordinary_memberships = _known_memberships(
            _popen_handle(ordinary),
            inner_job=inner_job,
            outer_job=outer_job,
            win32job=win32job,
        )
        guardian_controls = holder_controls("guardian-candidate")
        guardian, creation_error = create_topology_holder(
            guardian_controls[0],
            guardian_controls[1],
            creationflags=getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB"),
            register=processes.append,
            phase="guardian-candidate-creation",
        )
        if guardian is not None:
            wait_ready(guardian, guardian_controls[2], "guardian-candidate")
        guardian_memberships = (
            _known_memberships(
                _popen_handle(guardian),
                inner_job=inner_job,
                outer_job=outer_job,
                win32job=win32job,
            )
            if guardian is not None
            else None
        )
        classification = require_accepted_breakaway_result(
            scenario,
            inner_breakaway_enabled=breakaway_enabled,
            process_created=guardian is not None,
            memberships=guardian_memberships,
            creation_error=creation_error,
        )
        result = {
            "scenario": scenario,
            "topology": "outer-harness-with-experimental-inner-breakaway",
            "inner_breakaway_enabled": breakaway_enabled,
            "actor_memberships_before_assignment": actor_before,
            "owner_assignment_succeeded": assignment_succeeded,
            "owner_assignment_error": assignment_error,
            "owner_memberships_after_assignment": actor_after,
            "ordinary_child": {
                "created": True,
                "memberships": ordinary_memberships,
            },
            "guardian_candidate": {
                "created": guardian is not None,
                "creation_error": creation_error,
                "memberships": guardian_memberships,
                "classification": classification,
            },
        }
        _atomic_json(result_file, result)
        win32event.SetEvent(ordinary_release)
        require_successful_actor_completion(ordinary, "ordinary-child")
        if guardian is not None:
            win32event.SetEvent(guardian_controls[3])
            require_successful_actor_completion(guardian, "guardian-candidate")
        return 0
    except BaseException as exc:
        first_error = exc
    finally:
        for process in reversed(processes):
            try:
                if process.poll() is None:
                    _terminate_process(process)
            except BaseException as exc:
                first_error = first_error or exc
        for handle in events:
            try:
                handle.Close()
            except BaseException as exc:
                first_error = first_error or exc
        for handle in (self_handle, inner_job, outer_job):
            if handle is None:
                continue
            try:
                handle.Close()
            except BaseException as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error
    raise AssertionError("topology actor cleanup returned without raising")


def _topology_holder(ready_event: str, release_event: str) -> int:
    _signal(ready_event)
    _actor_wait(release_event)
    return 0


def _run_job_topology_probe(scenario: str, root: Path) -> dict[str, Any]:
    _win32api, _win32con, win32event, win32job = _windows_modules()
    root.mkdir(parents=True, exist_ok=True)
    outer_job_name = _read_json(root / "outer-job.json")["name"]
    outer_job = win32job.OpenJobObject(win32job.JOB_OBJECT_QUERY, False, outer_job_name)
    start_name, start_event = _new_event(win32event, "topology-start")
    result_file = root / "topology-result.json"
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "topology-actor",
            scenario,
            outer_job_name,
            start_name,
            str(result_file),
        ],
        cwd=_REPO_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        actor_in_outer_before_start = bool(
            win32job.IsProcessInJob(_popen_handle(process), outer_job)
        )
        if not actor_in_outer_before_start:
            raise RuntimeError("topology actor was not in the outer Job before start")
        win32event.SetEvent(start_event)
        require_successful_actor_completion(process, scenario)
        result = read_published_json(result_file, deadline=time.monotonic() + 5)
        result["actor_in_outer_before_start_gate"] = actor_in_outer_before_start
        return result
    finally:
        active_exception = sys.exception()
        first_error: BaseException | None = None
        if process.poll() is None:
            try:
                _terminate_process(process)
            except BaseException as exc:
                first_error = exc
        for handle in (start_event, outer_job):
            try:
                handle.Close()
            except BaseException as exc:
                first_error = first_error or exc
        raise_cleanup_error_unless_unwinding(first_error, active_exception)


def _run_region_falsification_probe(scenario: str, root: Path) -> dict[str, Any]:
    """Measure static region assignments against a byte-zero-only entrant."""
    from linkedin_mcp_server import process_tree

    _win32api, _win32con, win32event, _win32job = _windows_modules()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "auth" / "profile.lock"
    base_fd = _probe_fd(lock_path)
    events: list[Any] = []
    processes: list[subprocess.Popen[bytes]] = []
    jobs: list[Any] = []
    actors_by_result: dict[Path, subprocess.Popen[bytes]] = {}

    def event(label: str) -> tuple[str, Any]:
        name, handle = _new_event(win32event, label)
        events.append(handle)
        return name, handle

    def spawn(
        mode: str,
        label: str,
        options: dict[str, Any],
        extra_handles: list[int] | None = None,
    ) -> tuple[subprocess.Popen[bytes], Path, Any]:
        ready_name, ready = event(f"{label}-ready")
        result_file = root / f"{label}.json"
        process = _spawn_actor(
            mode,
            lock_path,
            identity,
            ready_name,
            result_file,
            options,
            extra_handles,
        )
        processes.append(process)
        actors_by_result[result_file] = process
        return process, result_file, ready

    def wait_result(path: Path, ready: Any) -> dict[str, Any]:
        try:
            _wait(ready, _DEADLINE_SECONDS, f"{path.name} was not ready")
        except BaseException as exc:
            process = actors_by_result[path]
            returncode = process.poll()
            stdout = b""
            stderr = b""
            if returncode is not None:
                stdout, stderr = process.communicate()
            raise RuntimeError(
                f"{path.name} was not ready: phase=before-ready "
                f"returncode={returncode!r} stdout={stdout!r} stderr={stderr!r}"
            ) from exc
        return read_published_json(path, deadline=time.monotonic() + 5)

    def signal(handle: Any) -> None:
        win32event.SetEvent(handle)

    def active(process: subprocess.Popen[bytes]) -> bool:
        handle = getattr(process, "_handle", None)
        if handle is None:
            raise RuntimeError("Windows Popen exposed no stable process handle")
        return _is_active(handle)

    def finish(process: subprocess.Popen[bytes], release: Any, phase: str) -> None:
        if process.poll() is not None:
            require_successful_actor_completion(process, f"{phase}-before-release")
            raise RuntimeError(
                f"actor exited before observation acknowledgement: {phase}"
            )
        signal(release)
        require_successful_actor_completion(process, phase)

    def production_entrant(
        label: str,
    ) -> tuple[subprocess.Popen[bytes], dict[str, Any], Any]:
        release_name, release = event(f"{label}-release")
        actor, result_file, ready = spawn(
            "production-byte-zero",
            label,
            {"hold_event": release_name},
        )
        result = wait_result(result_file, ready)
        if result["acquired"]:
            require_same_file_identity(identity, result["file_identity"])
        else:
            require_successful_actor_completion(actor, f"{label}-contention")
        return actor, result, release

    def browser_fixture(label: str) -> tuple[Any, list[subprocess.Popen[bytes]]]:
        job = process_tree.WindowsJob.named(label)
        jobs.append(job)
        if job.name is None:
            raise RuntimeError("browser Job has no name")
        children = []
        for _ in range(4):
            child = subprocess.Popen(
                [sys.executable, "-c", "import threading; threading.Event().wait()"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append(child)
            job.assign_popen(child)
            children.append(child)
        return job, children

    def publish_entrant(
        label: str,
        survivors: dict[str, subprocess.Popen[bytes]],
        child_handles: list[subprocess.Popen[bytes]] | None = None,
    ) -> tuple[
        subprocess.Popen[bytes],
        dict[str, Any],
        Any,
        list[subprocess.Popen[bytes]],
        list[subprocess.Popen[bytes]],
    ]:
        children_before = require_publication_witnesses(
            survivors,
            is_active=active,
            child_handles=child_handles,
        )
        actor, result, release = production_entrant(label)
        children_after = require_publication_witnesses(
            survivors,
            is_active=active,
            child_handles=child_handles,
            required_children=children_before if child_handles is not None else None,
        )
        return actor, result, release, children_before, children_after

    def retry_holder_death_entrant(
        label: str,
        survivors: dict[str, subprocess.Popen[bytes]],
        child_handles: list[subprocess.Popen[bytes]],
        browser_active_processes: Callable[[], int],
    ) -> tuple[
        subprocess.Popen[bytes],
        dict[str, Any],
        Any,
        list[subprocess.Popen[bytes]],
        list[subprocess.Popen[bytes]],
        int,
        float,
    ]:
        children_before = require_publication_witnesses(
            survivors,
            is_active=active,
            child_handles=child_handles,
        )
        attempt_number = 0
        throttle = win32event.CreateEvent(None, False, False, None)
        events.append(throttle)

        def require_window() -> None:
            require_publication_witnesses(
                survivors,
                is_active=active,
                child_handles=child_handles,
                required_children=children_before,
            )
            if browser_active_processes() <= 0:
                raise RuntimeError("browser Job drained during entrant publication")

        def attempt() -> tuple[subprocess.Popen[bytes], dict[str, Any], Any] | None:
            nonlocal attempt_number
            attempt_number += 1
            actor, result, release = production_entrant(
                f"{label}-attempt-{attempt_number}"
            )
            if not result["acquired"]:
                return None
            return actor, result, release

        (actor, result, release), attempts, seconds = retry_contended_publication(
            attempt,
            require_window=require_window,
            deadline=time.monotonic() + _DEADLINE_SECONDS,
            wait_for_retry=lambda: wait_on_unsignaled_throttle(
                throttle, wait=win32event.WaitForSingleObject
            ),
        )
        children_after = require_publication_witnesses(
            survivors,
            is_active=active,
            child_handles=child_handles,
            required_children=children_before,
        )
        return (
            actor,
            result,
            release,
            children_before,
            children_after,
            attempts,
            seconds,
        )

    try:
        identity = list(file_identity(base_fd))

        if scenario == "falsification-original-owner-loss":
            browser_job, children = browser_fixture("original-owner-loss-browser")
            owner, owner_file, owner_ready = spawn("hold", "owner", {"offset": 0})
            guardian_release_name, guardian_release = event("guardian-drain-complete")
            guardian, guardian_file, guardian_ready = spawn(
                "hold",
                "guardian",
                {
                    "offset": 1,
                    "job_name": browser_job.name,
                    "hold_event": guardian_release_name,
                },
            )
            owner_result = wait_result(owner_file, owner_ready)
            guardian_result = wait_result(guardian_file, guardian_ready)
            require_same_file_identity(identity, owner_result["file_identity"])
            require_same_file_identity(identity, guardian_result["file_identity"])
            browser_job.close()
            _terminate_process(owner)
            guardian_active_before = active(guardian)
            browser_active_before = _query_named_job_active_processes(browser_job.name)
            (
                entrant,
                entrant_result,
                entrant_release,
                children_before,
                children_after,
                entrant_attempts,
                entrant_rundown_seconds,
            ) = retry_holder_death_entrant(
                "byte-zero-entrant",
                {"guardian": guardian},
                children,
                lambda: _query_named_job_active_processes(browser_job.name),
            )
            guardian_active_after = active(guardian)
            browser_active_after = _query_named_job_active_processes(browser_job.name)
            finish(entrant, entrant_release, "owner-loss-entrant")
            finish(guardian, guardian_release, "owner-loss-guardian")
            return {
                "scenario": scenario,
                "assignment": {"owner": 0, "guardian": 1, "entrant": 0},
                "protocol": entrant_result["protocol"],
                "owner_exit_observed": not active(owner),
                "guardian_active_before_entry": guardian_active_before,
                "guardian_active_after_entry": guardian_active_after,
                "browser_active_before_entry": browser_active_before,
                "browser_active_after_entry": browser_active_after,
                "live_children_before_entry": len(children_before),
                "live_children_after_entry": len(children_after),
                "same_child_live_before_and_after_entry": any(
                    child in children_after for child in children_before
                ),
                "entrant_rundown_attempts": entrant_attempts,
                "entrant_rundown_seconds": entrant_rundown_seconds,
                "byte_zero_entrant_acquired": entrant_result["acquired"],
                "unsafe_compatibility_result": entrant_result["acquired"],
            }

        if scenario == "falsification-inverted-guardian-loss":
            browser_job, children = browser_fixture("inverted-guardian-loss-browser")
            owner_release_name, owner_release = event("allow-owner-drain")
            owner, owner_file, owner_ready = spawn(
                "hold",
                "owner",
                {
                    "offset": 1,
                    "job_name": browser_job.name,
                    "hold_event": owner_release_name,
                },
            )
            guardian, guardian_file, guardian_ready = spawn(
                "hold", "guardian", {"offset": 0}
            )
            owner_result = wait_result(owner_file, owner_ready)
            guardian_result = wait_result(guardian_file, guardian_ready)
            require_same_file_identity(identity, owner_result["file_identity"])
            require_same_file_identity(identity, guardian_result["file_identity"])
            browser_job.close()
            _terminate_process(guardian)
            owner_active_before = active(owner)
            browser_active_before = _query_named_job_active_processes(browser_job.name)
            (
                entrant,
                entrant_result,
                entrant_release,
                children_before,
                children_after,
                entrant_attempts,
                entrant_rundown_seconds,
            ) = retry_holder_death_entrant(
                "byte-zero-entrant",
                {"owner": owner},
                children,
                lambda: _query_named_job_active_processes(browser_job.name),
            )
            owner_active_after = active(owner)
            browser_active_after = _query_named_job_active_processes(browser_job.name)
            finish(entrant, entrant_release, "guardian-loss-entrant")
            finish(owner, owner_release, "guardian-loss-owner")
            return {
                "scenario": scenario,
                "assignment": {"owner": 1, "guardian": 0, "entrant": 0},
                "protocol": entrant_result["protocol"],
                "guardian_exit_observed": not active(guardian),
                "owner_cleanup_paused": owner_active_before and owner_active_after,
                "browser_active_before_entry": browser_active_before,
                "browser_active_after_entry": browser_active_after,
                "live_children_before_entry": len(children_before),
                "live_children_after_entry": len(children_after),
                "same_child_live_before_and_after_entry": any(
                    child in children_after for child in children_before
                ),
                "entrant_rundown_attempts": entrant_attempts,
                "entrant_rundown_seconds": entrant_rundown_seconds,
                "byte_zero_entrant_acquired": entrant_result["acquired"],
                "unsafe_compatibility_result": entrant_result["acquired"],
            }

        if scenario == "falsification-inverted-pre-arm":
            owner_release_name, owner_release = event("owner-release")
            owner, owner_file, owner_ready = spawn(
                "hold",
                "owner",
                {"offset": 1, "hold_event": owner_release_name},
            )
            owner_result = wait_result(owner_file, owner_ready)
            require_same_file_identity(identity, owner_result["file_identity"])

            admission, admission_file, admission_ready = spawn(
                "production-byte-zero", "transient-admission", {}
            )
            admission_result = wait_result(admission_file, admission_ready)
            require_successful_actor_completion(admission, "transient-admission")

            guardian_pause_name, guardian_pause = event("allow-guardian-arm")
            armed_name, armed = event("guardian-armed")
            owner_handle = int(getattr(owner, "_handle"))
            guardian, guardian_file, guardian_ready = spawn(
                "guardian-publish",
                "guardian",
                {
                    "offset": 0,
                    "owner_handle": owner_handle,
                    "pause_event": guardian_pause_name,
                    "armed_event": armed_name,
                },
                [owner_handle],
            )
            _wait(guardian_ready, _DEADLINE_SECONDS, "guardian did not reach pre-ARM")
            entrant, entrant_result, entrant_release, _before, _after = publish_entrant(
                "byte-zero-entrant", {"owner": owner}
            )
            signal(guardian_pause)
            require_successful_actor_completion(guardian, "pre-arm-guardian")
            guardian_result = read_published_json(
                guardian_file, deadline=time.monotonic() + 5
            )
            armed_state = win32event.WaitForSingleObject(armed, 0)
            finish(entrant, entrant_release, "pre-arm-entrant")
            finish(owner, owner_release, "pre-arm-owner")
            return {
                "scenario": scenario,
                "assignment": {"owner": 1, "guardian": 0, "entrant": 0},
                "protocol": entrant_result["protocol"],
                "transient_admission_acquired": admission_result["acquired"],
                "transient_admission_released": admission.returncode == 0,
                "byte_zero_entrant_acquired_before_guardian_arm": entrant_result[
                    "acquired"
                ],
                "guardian_contention": guardian_result["contention"],
                "guardian_armed": guardian_result["armed"],
                "armed_event_unpublished": armed_state == _WAIT_TIMEOUT,
                "guardian_job_authority": guardian_result["job_authority"],
                "guardian_browser_authority": guardian_result["browser_authority"],
                "unsafe_compatibility_result": (
                    entrant_result["acquired"] and not guardian_result["armed"]
                ),
            }

        if scenario == "falsification-inverted-post-disarm-mutation":
            owner_release_name, owner_release = event("outer-mutation-complete")
            owner, owner_file, owner_ready = spawn(
                "hold",
                "owner",
                {"offset": 1, "hold_event": owner_release_name},
            )
            guardian_release_name, guardian_release = event("guardian-disarm")
            guardian, guardian_file, guardian_ready = spawn(
                "hold",
                "guardian",
                {"offset": 0, "hold_event": guardian_release_name},
            )
            wait_result(owner_file, owner_ready)
            wait_result(guardian_file, guardian_ready)
            mutation_name, mutation_active = event("outer-mutation-active")
            _ = mutation_name
            signal(mutation_active)
            finish(guardian, guardian_release, "modeled-guardian-disarm")
            mutation_still_active = (
                win32event.WaitForSingleObject(mutation_active, 0) == _WAIT_OBJECT_0
            )
            entrant, entrant_result, entrant_release, _before, _after = publish_entrant(
                "byte-zero-entrant", {"owner": owner}
            )
            owner_active = active(owner)
            finish(entrant, entrant_release, "post-disarm-entrant")
            finish(owner, owner_release, "post-disarm-owner")
            return {
                "scenario": scenario,
                "assignment": {"owner": 1, "guardian": 0, "entrant": 0},
                "protocol": entrant_result["protocol"],
                "guardian_disarm_observed": guardian.returncode == 0,
                "outer_mutation_active_after_disarm": mutation_still_active,
                "owner_active_during_mutation": owner_active,
                "byte_zero_entrant_acquired": entrant_result["acquired"],
                "unsafe_compatibility_result": entrant_result["acquired"],
            }

        if scenario == "falsification-inverted-exclusive-mutation":
            owner_release_name, owner_release = event("exclusive-mutation-complete")
            owner, owner_file, owner_ready = spawn(
                "hold",
                "owner",
                {"offset": 1, "hold_event": owner_release_name},
            )
            wait_result(owner_file, owner_ready)
            entrant, entrant_result, entrant_release, _before, _after = publish_entrant(
                "byte-zero-entrant", {"owner": owner}
            )
            owner_active = active(owner)
            finish(entrant, entrant_release, "exclusive-mutation-entrant")
            finish(owner, owner_release, "exclusive-mutation-owner")
            return {
                "scenario": scenario,
                "assignment": {"owner": 1, "guardian": None, "entrant": 0},
                "protocol": entrant_result["protocol"],
                "owner_active_during_mutation": owner_active,
                "guardian_started": False,
                "byte_zero_entrant_acquired": entrant_result["acquired"],
                "unsafe_compatibility_result": entrant_result["acquired"],
            }

        raise RuntimeError(f"unknown region falsification scenario: {scenario}")
    finally:
        active_exception = sys.exception()
        first_error: BaseException | None = None
        for process in reversed(processes):
            try:
                if process.poll() is None:
                    _terminate_process(process)
            except BaseException as exc:
                first_error = first_error or exc
        for job in jobs:
            try:
                if not job.closed:
                    job.terminate()
                    job.wait_until_empty(timeout=_DEADLINE_SECONDS)
                    if not job.closed:
                        job.close()
            except BaseException as exc:
                first_error = first_error or exc
        for handle in events:
            try:
                handle.Close()
            except BaseException as exc:
                first_error = first_error or exc
        try:
            os.close(base_fd)
        except BaseException as exc:
            first_error = first_error or exc
        raise_cleanup_error_unless_unwinding(first_error, active_exception)


def _run_conjunction_probe(scenario: str, root: Path) -> dict[str, Any]:
    """Run native conjunction scenarios under the caller's outer harness Job."""
    from linkedin_mcp_server import process_tree

    win32api, win32con, win32event, win32job = _windows_modules()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "auth" / "profile.lock"
    base_fd = _probe_fd(lock_path)
    events: list[Any] = []
    processes: list[subprocess.Popen[bytes]] = []
    jobs: list[Any] = []
    retained_fds: list[int] = []
    actors_by_result: dict[Path, subprocess.Popen[bytes]] = {}

    def event(label: str) -> tuple[str, Any]:
        name, handle = _new_event(win32event, label)
        events.append(handle)
        return name, handle

    def spawn(
        mode: str,
        label: str,
        options: dict[str, Any],
        extra_handles: list[int] | None = None,
    ) -> tuple[subprocess.Popen[bytes], Path, Any]:
        ready_name, ready = event(f"{label}-ready")
        result_file = root / f"{label}.json"
        process = _spawn_actor(
            mode,
            lock_path,
            identity,
            ready_name,
            result_file,
            options,
            extra_handles,
        )
        processes.append(process)
        actors_by_result[result_file] = process
        return process, result_file, ready

    def wait_result(path: Path, ready: Any) -> dict[str, Any]:
        try:
            _wait(ready, _DEADLINE_SECONDS, f"{path.name} was not ready")
        except BaseException as exc:
            process = actors_by_result[path]
            returncode = process.poll()
            stdout = b""
            stderr = b""
            if returncode is not None:
                stdout, stderr = process.communicate()
            raise RuntimeError(
                f"{path.name} was not ready: phase=before-ready "
                f"returncode={returncode!r} stdout={stdout!r} stderr={stderr!r}"
            ) from exc
        return read_published_json(path, deadline=time.monotonic() + 5)

    def signal(handle: Any) -> None:
        win32event.SetEvent(handle)

    def active(process: subprocess.Popen[bytes]) -> bool:
        handle = getattr(process, "_handle", None)
        if handle is None:
            raise RuntimeError("Windows Popen exposed no stable process handle")
        return _is_active(handle)

    def descendants(
        job: Any, count: int = 4
    ) -> tuple[list[subprocess.Popen[bytes]], list[int]]:
        children: list[subprocess.Popen[bytes]] = []
        pids: list[int] = []
        for _ in range(count):
            child = subprocess.Popen(
                [sys.executable, "-c", "import threading; threading.Event().wait()"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            job.assign_popen(child)
            children.append(child)
            processes.append(child)
            pids.append(child.pid)
        return children, pids

    try:
        identity = list(file_identity(base_fd))
        if scenario == "conjunction-lock-regions":
            owner, owner_file, owner_ready = spawn("hold", "owner", {"offset": 0})
            guardian, guardian_file, guardian_ready = spawn(
                "hold", "guardian", {"offset": 1}
            )
            owner_result = wait_result(owner_file, owner_ready)
            guardian_result = wait_result(guardian_file, guardian_ready)
            require_same_file_identity(identity, owner_result["file_identity"])
            require_same_file_identity(identity, guardian_result["file_identity"])
            first, first_fd = _attempt_here(lock_path)
            os.close(first_fd)
            _terminate_process(owner)
            throttle = win32event.CreateEvent(None, False, False, None)
            rundown_error: BaseException | None = None
            try:

                def observe_b_contention() -> tuple[bool, bool] | None:
                    second_fd = _probe_fd(lock_path)
                    try:
                        second_a, second_b = probe_conjunction_regions(second_fd)
                    finally:
                        os.close(second_fd)
                    if not second_a:
                        return None
                    if second_b:
                        raise RuntimeError("B was not held after owner exit")
                    return second_a, second_b

                (second_a, second_b), owner_rundown_attempts, owner_rundown_seconds = (
                    retry_lock_rundown(
                        observe_b_contention,
                        deadline=time.monotonic() + _DEADLINE_SECONDS,
                        wait_for_retry=lambda: wait_on_unsignaled_throttle(
                            throttle, wait=win32event.WaitForSingleObject
                        ),
                    )
                )
                _terminate_process(guardian)

                def acquire_after_guardian_exit() -> int | None:
                    acquired, acquired_fd = _attempt_here(lock_path)
                    if acquired:
                        return acquired_fd
                    os.close(acquired_fd)
                    return None

                third_fd, guardian_rundown_attempts, guardian_rundown_seconds = (
                    retry_lock_rundown(
                        acquire_after_guardian_exit,
                        deadline=time.monotonic() + _DEADLINE_SECONDS,
                        wait_for_retry=lambda: wait_on_unsignaled_throttle(
                            throttle, wait=win32event.WaitForSingleObject
                        ),
                    )
                )
            except BaseException as exc:
                rundown_error = exc
            finally:
                close_preserving_error(throttle.Close, rundown_error)
            third = True
            retained_fds.append(third_fd)
            d1_process, d1_file, d1_ready = spawn("attempt", "d-held-a", {})
            d1_result = wait_result(d1_file, d1_ready)
            d1_process.wait(timeout=_DEADLINE_SECONDS)
            _region_api()[1](third_fd, 0)
            os.close(third_fd)
            retained_fds.remove(third_fd)
            d2_process, d2_file, d2_ready = spawn("attempt", "d-after-close", {})
            d2_result = wait_result(d2_file, d2_ready)
            d2_process.wait(timeout=_DEADLINE_SECONDS)
            return {
                "scenario": scenario,
                "file_identity": identity,
                "owner_identity": owner_result["file_identity"],
                "guardian_identity": guardian_result["file_identity"],
                "blocked_by_a": not first,
                "a_acquired_b_blocked_after_owner_exit": second_a and not second_b,
                "a_released_after_b_contention": True,
                "owner_rundown_attempts": owner_rundown_attempts,
                "owner_rundown_seconds": owner_rundown_seconds,
                "guardian_rundown_attempts": guardian_rundown_attempts,
                "guardian_rundown_seconds": guardian_rundown_seconds,
                "c_acquired": third,
                "d_blocked_after_b_unlock": not d1_result["acquired"],
                "d_acquired_after_c_close": d2_result["acquired"],
            }

        if scenario == "conjunction-publication":
            owner, owner_file, owner_ready = spawn("hold", "owner", {"offset": 0})
            owner_result = wait_result(owner_file, owner_ready)
            owner_process_handle = int(getattr(owner, "_handle"))
            browser_gate_name, browser_gate = event("browser-gate")
            browser_started_name, browser_started = event("browser-started")
            browser = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "gate",
                    browser_gate_name,
                    browser_started_name,
                ],
                cwd=_REPO_ROOT,
            )
            processes.append(browser)
            stop_name, stop_event = event("guardian-stop")
            armed_name, armed_event = event("guardian-armed")
            guardian, guardian_file, guardian_ready = spawn(
                "guardian-publish",
                "guardian",
                {
                    "offset": 1,
                    "owner_handle": owner_process_handle,
                    "armed_event": armed_name,
                    "hold_event": stop_name,
                },
                [owner_process_handle],
            )
            guardian_result = wait_result(guardian_file, guardian_ready)
            require_same_file_identity(identity, owner_result["file_identity"])
            require_same_file_identity(identity, guardian_result["file_identity"])

            def browser_has_started() -> bool:
                state = win32event.WaitForSingleObject(browser_started, 0)
                if state == _WAIT_OBJECT_0:
                    return True
                if state == _WAIT_TIMEOUT:
                    return False
                raise RuntimeError(f"browser start event returned {state}")

            browser_started_after_armed = observe_browser_publication_order(
                browser_started=browser_has_started,
                observe_armed=lambda: _wait(
                    armed_event, _DEADLINE_SECONDS, "guardian did not arm"
                ),
                release_gate=lambda: signal(browser_gate),
                observe_browser_start=lambda: _wait(
                    browser_started, _DEADLINE_SECONDS, "browser did not start"
                ),
            )
            b_fd = _probe_fd(lock_path)
            b_blocked = not _region_api()[0](b_fd, 1)
            os.close(b_fd)
            signal(stop_event)
            guardian.wait(timeout=_DEADLINE_SECONDS)
            _terminate_process(owner)

            pause_name, pause_event = event("late-pause")
            late_owner, late_owner_file, late_owner_ready = spawn(
                "hold", "late-owner", {"offset": 0}
            )
            wait_result(late_owner_file, late_owner_ready)
            late_owner_handle = int(getattr(late_owner, "_handle"))
            late, late_file, late_ready = spawn(
                "guardian-publish",
                "late-guardian",
                {
                    "offset": 1,
                    "owner_handle": late_owner_handle,
                    "pause_event": pause_name,
                },
                [late_owner_handle],
            )
            _wait(late_ready, _DEADLINE_SECONDS, "late guardian did not pause")
            _terminate_process(late_owner)
            throttle = win32event.CreateEvent(None, False, False, None)
            events.append(throttle)

            def require_late_publication_window() -> None:
                require_publication_witnesses(
                    {"late guardian": late},
                    is_active=active,
                )
                if active(late_owner):
                    raise RuntimeError("late owner survived successor admission")

            def admit_successor() -> int | None:
                acquired, descriptor = _attempt_here(lock_path)
                if acquired:
                    return descriptor
                os.close(descriptor)
                return None

            successor_fd, successor_attempts, successor_seconds = (
                retry_contended_publication(
                    admit_successor,
                    require_window=require_late_publication_window,
                    deadline=time.monotonic() + _DEADLINE_SECONDS,
                    wait_for_retry=lambda: wait_on_unsignaled_throttle(
                        throttle, wait=win32event.WaitForSingleObject
                    ),
                    discard_result=os.close,
                )
            )
            retained_fds.append(successor_fd)
            signal(pause_event)
            late.wait(timeout=_DEADLINE_SECONDS)
            late_result = read_published_json(late_file, deadline=time.monotonic() + 5)
            _region_api()[1](successor_fd, 0)
            os.close(successor_fd)
            retained_fds.remove(successor_fd)

            conflict_owner, conflict_owner_file, conflict_owner_ready = spawn(
                "hold", "conflict-owner", {"offset": 0}
            )
            wait_result(conflict_owner_file, conflict_owner_ready)
            b_holder, b_holder_file, b_holder_ready = spawn(
                "hold", "conflict-b-holder", {"offset": 1}
            )
            b_holder_result = wait_result(b_holder_file, b_holder_ready)
            conflict_owner_handle = int(getattr(conflict_owner, "_handle"))
            conflict, conflict_file, conflict_ready = spawn(
                "guardian-publish",
                "conflict-guardian",
                {"offset": 1, "owner_handle": conflict_owner_handle},
                [conflict_owner_handle],
            )
            conflict_result = wait_result(conflict_file, conflict_ready)
            conflict.wait(timeout=_DEADLINE_SECONDS)
            _terminate_process(b_holder)
            _terminate_process(conflict_owner)
            require_same_file_identity(identity, b_holder_result["file_identity"])
            return {
                "scenario": scenario,
                "file_identity": identity,
                "owner_identity": owner_result["file_identity"],
                "guardian_identity": guardian_result["file_identity"],
                "armed": guardian_result["armed"],
                "b_probe_blocked": b_blocked,
                "browser_started_after_armed": browser_started_after_armed,
                "late_successor_admitted": True,
                "late_successor_attempts": successor_attempts,
                "late_successor_seconds": successor_seconds,
                "late_guardian_armed": late_result.get("armed", False),
                "late_guardian_contention": late_result.get("contention", False),
                "conflict_guardian_contention": conflict_result["contention"],
                "conflict_guardian_armed": conflict_result["armed"],
                "conflict_guardian_job_authority": conflict_result["job_authority"],
                "conflict_guardian_browser_authority": conflict_result[
                    "browser_authority"
                ],
                "conflict_guardian_returncode": conflict.returncode,
            }

        browser_job = process_tree.WindowsJob.named("conjunction-browser")
        jobs.append(browser_job)
        if browser_job.name is None:
            raise RuntimeError("browser Job has no name")
        outer_in_browser_job, _watched = query_control_job_membership(
            browser_job.job_handle,
            None,
            win32api=win32api,
            win32con=win32con,
            win32job=win32job,
        )
        if outer_in_browser_job:
            raise RuntimeError("outer probe entered the browser Job")
        _children, descendant_pids = descendants(browser_job)

        if scenario == "conjunction-guardian-loss-clean-close":
            guardian, guardian_file, guardian_ready = spawn(
                "hold",
                "guardian",
                {"offset": 1, "job_name": browser_job.name},
            )
            guardian_result = wait_result(guardian_file, guardian_ready)
            guardian_handle = int(getattr(guardian, "_handle"))
            guardian_exit_name, guardian_exit_event = event("guardian-exit-observed")
            begin_drain_name, begin_drain_event = event("begin-owner-drain")
            zero_name, zero_event = event("owner-zero-proven")
            allow_a_name, allow_a_event = event("allow-a-release")
            owner, owner_file, owner_ready = spawn(
                "owner-watch",
                "owner",
                {
                    "offset": 0,
                    "job_name": browser_job.name,
                    "guardian_handle": guardian_handle,
                    "descendant_pids": descendant_pids,
                    "guardian_exit_event": guardian_exit_name,
                    "begin_drain_event": begin_drain_name,
                    "zero_event": zero_name,
                    "allow_a_release_event": allow_a_name,
                },
                [guardian_handle],
            )
            owner_result = wait_result(owner_file, owner_ready)
            require_same_file_identity(identity, guardian_result["file_identity"])
            require_same_file_identity(identity, owner_result["file_identity"])
            browser_job.close()
            _terminate_process(guardian)
            _wait(
                guardian_exit_event,
                _DEADLINE_SECONDS,
                "owner did not observe guardian exit",
            )
            attempts_after_exit = []
            for _ in range(3):
                acquired, fd = _attempt_here(lock_path)
                attempts_after_exit.append(acquired)
                if acquired:
                    _region_api()[1](fd, 0)
                os.close(fd)
            active_before_drain = _query_named_job_active_processes(browser_job.name)
            descendants_before_drain = sum(child.poll() is None for child in _children)
            signal(begin_drain_event)
            _wait(zero_event, _DEADLINE_SECONDS, "owner did not prove browser zero")
            blocked_at_zero, zero_fd = _attempt_here(lock_path)
            if blocked_at_zero:
                _region_api()[1](zero_fd, 0)
            os.close(zero_fd)
            signal(allow_a_event)
            owner.wait(timeout=_DEADLINE_SECONDS)
            final, final_fd = _attempt_here(lock_path)
            if final:
                _region_api()[1](final_fd, 0)
            os.close(final_fd)
            owner_result = read_published_json(
                owner_file, deadline=time.monotonic() + 5
            )
            return {
                "scenario": scenario,
                "file_identity": identity,
                "guardian_identity": guardian_result["file_identity"],
                "outer_in_browser_job": outer_in_browser_job,
                "owner_in_browser_job": owner_result["current_process_in_browser_job"],
                "guardian_in_browser_job": owner_result[
                    "watched_process_in_browser_job"
                ],
                "post_exit_attempts_rejected": not any(attempts_after_exit),
                "active_processes_before_owner_drain": active_before_drain,
                "live_descendants_before_owner_drain": descendants_before_drain,
                "owner_observed_guardian_exit": owner_result["guardian_exit_observed"],
                "zero_proven": owner_result["zero_proven"],
                "blocked_while_a_held_at_zero": not blocked_at_zero,
                "acquired_after_owner_release": final,
                "respawn_claimed": False,
            }

        owner, owner_file, owner_ready = spawn(
            "hold",
            "owner",
            {"offset": 0, "job_name": browser_job.name},
        )
        owner_result = wait_result(owner_file, owner_ready)
        owner_handle = int(getattr(owner, "_handle"))
        zero_name, zero_event = event("zero-proven")
        allow_name, allow_event = event("allow-b-release")
        fault_name, fault_event = event("guardian-fault")
        fault = scenario.removeprefix("conjunction-owner-loss-")
        if scenario == "conjunction-owner-loss":
            fault = "none"
        identity_mutex = f"Local\\linkedin-mcp-conjunction-{secrets.token_hex(16)}"
        guardian, guardian_file, guardian_ready = spawn(
            "guardian-drain",
            "guardian",
            {
                "offset": 1,
                "job_name": browser_job.name,
                "owner_handle": owner_handle,
                "descendant_pids": descendant_pids,
                "zero_event": zero_name,
                "allow_release_event": allow_name,
                "fault_event": fault_name,
                "fault": fault,
                "identity_mutex": identity_mutex,
            },
            [owner_handle],
        )
        guardian_result = wait_result(guardian_file, guardian_ready)
        require_same_file_identity(identity, owner_result["file_identity"])
        require_same_file_identity(identity, guardian_result["file_identity"])
        browser_job.close()
        pre, pre_fd = _attempt_here(lock_path)
        os.close(pre_fd)
        _terminate_process(owner)
        if fault == "none":
            _wait(zero_event, _DEADLINE_SECONDS, "guardian did not prove zero")
            before_fd = _probe_fd(lock_path)
            before_a, before_b = probe_conjunction_regions(before_fd)
            os.close(before_fd)
            active_at_zero = _query_named_job_active_processes(browser_job.name)
            signal(allow_event)
            guardian.wait(timeout=_DEADLINE_SECONDS)
            after, after_fd = _attempt_here(lock_path)
            if after:
                _region_api()[1](after_fd, 0)
            os.close(after_fd)
            return {
                "scenario": scenario,
                "file_identity": identity,
                "owner_identity": owner_result["file_identity"],
                "guardian_identity": guardian_result["file_identity"],
                "outer_in_browser_job": outer_in_browser_job,
                "owner_in_browser_job": guardian_result[
                    "watched_process_in_browser_job"
                ],
                "guardian_in_browser_job": guardian_result[
                    "current_process_in_browser_job"
                ],
                "prearmed_rejected": not pre,
                "zero_proven": True,
                "job_active_at_zero": active_at_zero,
                "a_acquired_b_blocked_before_release": before_a and not before_b,
                "acquired_after_b_release": after,
            }

        _wait(fault_event, _DEADLINE_SECONDS, "guardian did not publish its fault")
        guardian_result = read_published_json(
            guardian_file, deadline=time.monotonic() + 5
        )
        probe_process, probe_file, probe_ready = spawn(
            "region-probe", f"external-{fault}", {}
        )
        probe_result = wait_result(probe_file, probe_ready)
        probe_process.wait(timeout=_DEADLINE_SECONDS)
        identity_owned = observe_guardian_identity(identity_mutex)
        measurement = {
            "scenario": scenario,
            "file_identity": identity,
            "outer_in_browser_job": outer_in_browser_job,
            "owner_in_browser_job": guardian_result["watched_process_in_browser_job"],
            "guardian_in_browser_job": guardian_result[
                "current_process_in_browser_job"
            ],
            "fault": fault,
            "guardian_error_type": guardian_result["error_type"],
            "guardian_error": guardian_result["error"],
            "fault_operation": guardian_result["fault_operation"],
            "terminate_attempted": guardian_result["terminate_attempted"],
            "terminate_completed": guardian_result.get("terminate_completed", False),
            "query_samples": guardian_result.get("query_samples", []),
            "query_timeout": guardian_result.get("query_timeout", False),
            "external_probe_acquired_a": probe_result["acquired_a"],
            "external_probe_acquired_b": probe_result["acquired_b"],
            "guardian_alive": guardian.poll() is None,
            "identity_mutex_owned": identity_owned["identity_mutex_owned"],
        }
        _atomic_json(root / "conjunction-result.json", measurement)
        result_event = os.environ.get("CONJUNCTION_RESULT_EVENT")
        if result_event is None:
            raise RuntimeError("the fail-closed scenario has no result event")
        _signal(result_event)
        # Only the already-assigned outer harness Job may break this failed proof.
        threading.Event().wait()
        raise RuntimeError("the failed conjunction proof resumed unexpectedly")
    finally:
        first_error: BaseException | None = None
        for fd in retained_fds:
            try:
                os.close(fd)
            except BaseException as exc:
                first_error = first_error or exc
        for process in reversed(processes):
            try:
                if process.poll() is None:
                    _terminate_process(process)
            except BaseException as exc:
                first_error = first_error or exc
        for job in jobs:
            try:
                if not job.closed:
                    job.terminate()
                    job.wait_until_empty(timeout=_DEADLINE_SECONDS)
                    if not job.closed:
                        job.close()
            except BaseException as exc:
                first_error = first_error or exc
        for handle in events:
            try:
                handle.Close()
            except BaseException as exc:
                first_error = first_error or exc
        try:
            os.close(base_fd)
        except BaseException as exc:
            first_error = first_error or exc
        if first_error is not None:
            raise first_error


def _gate(wait_event: str, started_event: str) -> int:
    _actor_wait(wait_event)
    _signal(started_event)
    return 0


def _remaining_browser_seconds(
    deadline: float, *, monotonic: Callable[[], float] = time.monotonic
) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("browser launch evidence deadline expired")
    return remaining


def _reset_event(name: str) -> None:
    _win32api, win32con, win32event, _win32job = _windows_modules()
    handle = _actor_event(name, win32con.EVENT_MODIFY_STATE)
    try:
        win32event.ResetEvent(handle)
    finally:
        handle.Close()


def _browser_event_wait(name: str, deadline: float) -> None:
    _win32api, win32con, _win32event, _win32job = _windows_modules()
    handle = _actor_event(name, win32con.SYNCHRONIZE)
    try:
        _wait(
            handle,
            _remaining_browser_seconds(deadline),
            f"event {name} was not signaled before the shared deadline",
        )
    finally:
        handle.Close()


def _full_process_image_path(handle: Any) -> str:
    from ctypes import wintypes

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    query = kernel32.QueryFullProcessImageNameW
    query.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        wintypes.LPDWORD,
    ]
    query.restype = wintypes.BOOL
    capacity = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(capacity.value)
    if not query(int(handle), 0, buffer, ctypes.byref(capacity)):
        raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
    return buffer.value


def job_process_ids_from_query(information: Any) -> set[int]:
    if not isinstance(information, (tuple, list)):
        raise RuntimeError("Job process inventory has an unexpected pywin32 shape")
    process_ids = {int(pid) for pid in information}
    if any(pid <= 0 for pid in process_ids):
        raise RuntimeError("Job process inventory contains a non-positive PID")
    return process_ids


def _job_process_ids(job: Any, win32job: Any) -> set[int]:
    return job_process_ids_from_query(
        win32job.QueryInformationJobObject(job, win32job.JobObjectBasicProcessIdList)
    )


def process_creation_identity(value: Any) -> str:
    if not isinstance(value, datetime.datetime):
        raise RuntimeError("process creation time is not datetime-shaped")
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC).isoformat(timespec="microseconds")


def require_browser_image_identity(
    cdp_processes: dict[int, str],
    process_identities: dict[int, dict[str, Any]],
    expected_path: str,
) -> dict[str, Any]:
    browser_pids = [pid for pid, role in cdp_processes.items() if role == "browser"]
    if len(browser_pids) != 1:
        raise RuntimeError("CDP inventory must identify exactly one browser process")
    browser_pid = browser_pids[0]
    identity = process_identities.get(browser_pid)
    if identity is None:
        raise RuntimeError("browser process has no retained stable-handle identity")
    observed = os.path.normcase(os.path.abspath(str(identity["image_path"])))
    expected = os.path.normcase(os.path.abspath(expected_path))
    if observed != expected:
        raise RuntimeError("browser process image does not match resolved Chromium")
    return {"browser_pid": browser_pid, "browser_image_path": identity["image_path"]}


def create_browser_launch_job[T](
    name: str, *, create: Callable[[str], T], configure: Callable[[T], None]
) -> T:
    job = create(name)
    configure(job)
    return job


def _asyncio_process_handle(process: Any) -> Any:
    transport = getattr(process, "_transport", None)
    popen = transport.get_extra_info("subprocess") if transport is not None else None
    handle = getattr(popen, "_handle", None)
    if handle is None:
        raise RuntimeError("Patchright Node exposed no stable asyncio/Popen handle")
    return handle


async def _browser_launch_owner(root: Path, controls: dict[str, str]) -> int:
    import patchright
    from patchright.async_api import async_playwright

    ignored_node_override = os.environ.pop("PLAYWRIGHT_NODEJS_PATH", None)
    expected_node = Path(patchright.__file__).parent / "driver" / "node.exe"
    _win32api, _win32con, _win32event, win32job = _windows_modules()
    deadline = time.monotonic() + _BROWSER_DEADLINE_SECONDS
    inner_name = f"Local\\linkedin-mcp-browser-launch-{secrets.token_hex(16)}"
    inner = create_browser_launch_job(
        inner_name,
        create=lambda name: win32job.CreateJobObject(None, name),
        configure=_configure_kill_on_close,
    )
    driver = await asyncio.wait_for(
        async_playwright().start(), _remaining_browser_seconds(deadline)
    )
    process = driver._impl_obj._connection._transport._proc
    node_handle = _asyncio_process_handle(process)
    win32job.AssignProcessToJobObject(inner, node_handle)
    node_pid = int(process.pid)
    prelaunch = _job_process_ids(inner, win32job)
    accounting = win32job.QueryInformationJobObject(
        inner, win32job.JobObjectBasicAccountingInformation
    )
    if prelaunch != {node_pid} or int(accounting["ActiveProcesses"]) != 1:
        raise RuntimeError("prelaunch inner Job membership is not exactly the driver")
    _atomic_json(
        root / "owner.json",
        {
            "inner_job_name": inner_name,
            "inner_job_kill_on_close": True,
            "owner_pid": os.getpid(),
            "driver_pid": node_pid,
            "expected_driver_path": str(expected_node),
            "ignored_playwright_nodejs_path": ignored_node_override is not None,
            "profile_path": str(root / "auth" / "profile"),
            "browser_path": driver.chromium.executable_path,
            "prelaunch_inner_pids": sorted(prelaunch),
            "prelaunch_active_processes": int(accounting["ActiveProcesses"]),
        },
    )
    _signal(controls["owner_ready"])
    _browser_event_wait(controls["guardian_armed"], deadline)
    context = await asyncio.wait_for(
        driver.chromium.launch_persistent_context(
            root / "auth" / "profile",
            channel="chromium",
            headless=True,
            viewport={"width": 1280, "height": 720},
            locale="en-US",
        ),
        _remaining_browser_seconds(deadline),
    )
    page = context.pages[0]
    await page.set_content(
        "<script>window.worker = new Worker(URL.createObjectURL(new Blob(["
        "'postMessage({ready:true})'], {type:'text/javascript'})));"
        "window.workerReady = new Promise(r => worker.onmessage = e => r(e.data.ready));"
        "</script>"
    )
    renderer_ready = await page.evaluate("() => 6 * 7 === 42")
    worker_ready = await page.evaluate("() => window.workerReady")
    browser = context.browser
    if browser is None:
        raise RuntimeError("persistent context exposed no browser-level CDP endpoint")
    session = await browser.new_browser_cdp_session()
    version = await session.send("Browser.getVersion")
    _atomic_json(
        root / "browser.json",
        {
            "renderer_ready": renderer_ready,
            "worker_ready": worker_ready,
            "patchright_browser_version": browser.version,
            "browser_get_version": version,
        },
    )
    _signal(controls["inventory_ready"])
    while True:
        _browser_event_wait(controls["cdp_request"], deadline)
        _reset_event(controls["cdp_request"])
        request = _read_json(root / "cdp-request.json")
        sequence = int(request["sequence"])
        process_info = await asyncio.wait_for(
            session.send("SystemInfo.getProcessInfo"),
            _remaining_browser_seconds(deadline),
        )
        _atomic_json(root / f"cdp-{sequence}.json", process_info["processInfo"])
        _signal(controls["cdp_ready"])
    return 0


def _browser_launch_guardian(root: Path, controls: dict[str, str]) -> int:
    from linkedin_mcp_server.profile_lease import ProfileLease

    win32api, win32con, _win32event, win32job = _windows_modules()
    win32process = importlib.import_module("win32process")

    deadline = time.monotonic() + _BROWSER_DEADLINE_SECONDS
    lease = ProfileLease(root / "auth")
    retained: dict[int, Any] = {}
    inner = None
    outer = None
    owner = None
    try:
        if not lease.try_acquire():
            raise RuntimeError("browser guardian could not acquire the profile lease")
        _browser_event_wait(controls["owner_ready"], deadline)
        metadata = _read_json(root / "owner.json")
        inner = win32job.OpenJobObject(
            win32job.JOB_OBJECT_ALL_ACCESS, False, metadata["inner_job_name"]
        )
        outer = win32job.OpenJobObject(
            win32job.JOB_OBJECT_QUERY, False, controls["outer_job_name"]
        )
        owner = win32api.OpenProcess(
            win32con.SYNCHRONIZE | win32con.PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            int(metadata["owner_pid"]),
        )
        driver = win32api.OpenProcess(
            win32con.SYNCHRONIZE | win32con.PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            int(metadata["driver_pid"]),
        )
        guardian_self = _duplicate_real_self(win32api, win32con)
        try:
            if win32job.IsProcessInJob(owner, inner):
                raise RuntimeError("owner entered the inner launch Job")
            if win32job.IsProcessInJob(guardian_self, inner):
                raise RuntimeError("guardian entered the inner launch Job")
            if not win32job.IsProcessInJob(owner, outer) or not win32job.IsProcessInJob(
                guardian_self, outer
            ):
                raise RuntimeError("a control process is outside the outer harness Job")
            if not win32job.IsProcessInJob(
                driver, inner
            ) or not win32job.IsProcessInJob(driver, outer):
                raise RuntimeError("Node is not a member of both launch Jobs")
        finally:
            guardian_self.Close()
            driver.Close()
        _signal(controls["guardian_armed"])
        _browser_event_wait(controls["inventory_ready"], deadline)
        process_identities: dict[int, dict[str, Any]] = {}
        cdp_sequence = 0

        def sample_cdp() -> list[dict[str, Any]]:
            nonlocal cdp_sequence
            cdp_sequence += 1
            _reset_event(controls["cdp_ready"])
            _atomic_json(root / "cdp-request.json", {"sequence": cdp_sequence})
            _signal(controls["cdp_request"])
            _browser_event_wait(controls["cdp_ready"], deadline)
            return _read_json(root / f"cdp-{cdp_sequence}.json")

        def open_handle(pid: int) -> Any:
            return win32api.OpenProcess(
                win32con.SYNCHRONIZE | win32con.PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                pid,
            )

        def validate(pid: int, handle: Any) -> None:
            if int(win32process.GetProcessId(handle)) != pid:
                raise RuntimeError("retained handle PID changed")
            if not _is_active(handle):
                raise RuntimeError("retained Job member exited during census")
            if not win32job.IsProcessInJob(
                handle, inner
            ) or not win32job.IsProcessInJob(handle, outer):
                raise RuntimeError("retained process left a required Job")
            path = _full_process_image_path(handle)
            created = win32process.GetProcessTimes(handle)["CreationTime"]
            if not path or created is None:
                raise RuntimeError("retained process identity is incomplete")
            identity = {
                "creation_time": process_creation_identity(created),
                "image_path": path,
            }
            previous = process_identities.get(pid)
            if previous is not None and previous != identity:
                raise RuntimeError("retained process identity changed during census")
            process_identities[pid] = identity

        cdp, retained = retain_stable_browser_inventory(
            sample_cdp=sample_cdp,
            sample_job_pids=lambda: _job_process_ids(inner, win32job),
            open_handle=open_handle,
            validate_handle=validate,
            close_handle=lambda handle: handle.Close(),
            driver_pid=int(metadata["driver_pid"]),
            deadline=deadline,
            wait_for_retry=lambda: win32api.Sleep(1),
        )
        inventory = require_browser_inventory(
            cdp, set(retained), int(metadata["driver_pid"])
        )
        browser_identity = require_browser_image_identity(
            cdp, process_identities, str(metadata["browser_path"])
        )
        inventory.update(
            {
                **browser_identity,
                "cdp_sample_count": cdp_sequence,
                "retained_pids": sorted(retained),
                "process_identities": {
                    str(pid): process_identities[pid] for pid in sorted(retained)
                },
                "owner_outside_inner": True,
                "guardian_outside_inner": True,
                "driver_in_inner_and_outer": True,
            }
        )
        _atomic_json(root / "inventory.json", inventory)
        _signal(controls["inventory_retained"])
        _wait(owner, _remaining_browser_seconds(deadline), "owner did not die")

        def wait_all(handles: dict[int, Any]) -> None:
            wait_handles_to_deadline(
                handles,
                wait_one=lambda handle, milliseconds: (
                    _windows_modules()[2].WaitForSingleObject(handle, milliseconds)
                    == _WAIT_OBJECT_0
                ),
                deadline=deadline,
            )

        shutdown = browser_guardian_shutdown(
            retained=retained,
            active_pids=lambda handles: {
                pid for pid, handle in handles.items() if _is_active(handle)
            },
            terminate_job=lambda: win32job.TerminateJobObject(inner, 208),
            query_active_processes=lambda: int(
                win32job.QueryInformationJobObject(
                    inner, win32job.JobObjectBasicAccountingInformation
                )["ActiveProcesses"]
            ),
            signal_job_zero=lambda: _signal(controls["job_zero"]),
            wait_check_stable_handles=lambda: _browser_event_wait(
                controls["check_stable_handles"], deadline
            ),
            wait_handles=wait_all,
            signal_both_zero=lambda: _signal(controls["both_zero"]),
            wait_allow_fence_release=lambda: _browser_event_wait(
                controls["allow_fence_release"], deadline
            ),
            release_fence=lease.release,
        )
        _atomic_json(root / "guardian.json", shutdown)
        _signal(controls["fence_released"])
        _browser_event_wait(controls["close_guardian"], deadline)
        return 0
    except BaseException as exc:
        _atomic_json(
            root / "guardian-error.json", {"error": f"{type(exc).__name__}: {exc}"}
        )
        _signal(controls["actor_error"])
        threading.Event().wait()
        raise
    finally:
        for handle in retained.values():
            with contextlib.suppress(BaseException):
                handle.Close()
        if owner is not None:
            with contextlib.suppress(BaseException):
                owner.Close()
        if inner is not None:
            with contextlib.suppress(BaseException):
                inner.Close()
        if outer is not None:
            with contextlib.suppress(BaseException):
                outer.Close()


def browser_actor_failure(
    root: Path, actors: dict[str, Any], logs: dict[str, tuple[Path, Path]]
) -> RuntimeError:
    for role, process in actors.items():
        error_path = root / f"{role}-error.json"
        if error_path.exists() or process.poll() is not None:
            error = (
                _read_json(error_path).get("error", "no published actor error")
                if error_path.exists()
                else "no published actor error"
            )
            stdout_path, stderr_path = logs[role]
            stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
            return RuntimeError(
                f"browser {role} failed: returncode={process.poll()!r} "
                f"error={error} stdout={stdout!r} stderr={stderr!r}"
            )
    return RuntimeError("browser actor error was signaled without a published failure")


def require_clean_browser_barrier(
    *,
    actor_error_signaled: bool,
    actors: dict[str, Any],
    expected_alive: set[str],
    logs: dict[str, tuple[Path, Path]],
    root: Path,
) -> None:
    if actor_error_signaled:
        raise browser_actor_failure(root, actors, logs)
    exited = {
        role: process for role, process in actors.items() if process.poll() is not None
    }
    unexpected = expected_alive & exited.keys()
    if unexpected:
        raise browser_actor_failure(
            root, {role: exited[role] for role in unexpected}, logs
        )


def _wait_browser_barrier(
    barrier: Any,
    *,
    actor_error: Any,
    actors: dict[str, Any],
    logs: dict[str, tuple[Path, Path]],
    root: Path,
    deadline: float,
    win32event: Any,
    expected_alive: set[str] | None = None,
) -> None:
    handles = [
        barrier,
        actor_error,
        *(_popen_handle(actor) for actor in actors.values()),
    ]
    result = win32event.WaitForMultipleObjects(
        handles, False, max(1, int(_remaining_browser_seconds(deadline) * 1000))
    )
    if result == _WAIT_OBJECT_0:
        require_clean_browser_barrier(
            actor_error_signaled=(
                win32event.WaitForSingleObject(actor_error, 0) == _WAIT_OBJECT_0
            ),
            actors=actors,
            expected_alive=set(actors) if expected_alive is None else expected_alive,
            logs=logs,
            root=root,
        )
        return
    if result == _WAIT_TIMEOUT:
        raise TimeoutError("browser launch barrier expired")
    if _WAIT_OBJECT_0 < result < _WAIT_OBJECT_0 + len(handles):
        raise browser_actor_failure(root, actors, logs)
    raise RuntimeError(f"WaitForMultipleObjects returned {result}")


def _wait_browser_actor_completion(
    role: str,
    process: Any,
    *,
    actor_error: Any,
    logs: dict[str, tuple[Path, Path]],
    root: Path,
    deadline: float,
    win32event: Any,
) -> None:
    actors = {role: process}
    result = win32event.WaitForMultipleObjects(
        [actor_error, _popen_handle(process)],
        False,
        max(1, int(_remaining_browser_seconds(deadline) * 1000)),
    )
    if result == _WAIT_TIMEOUT:
        raise TimeoutError(f"browser {role} did not complete before the deadline")
    if result not in {_WAIT_OBJECT_0, _WAIT_OBJECT_0 + 1}:
        raise RuntimeError(f"WaitForMultipleObjects returned {result}")
    require_clean_browser_barrier(
        actor_error_signaled=(
            win32event.WaitForSingleObject(actor_error, 0) == _WAIT_OBJECT_0
        ),
        actors=actors,
        expected_alive=set(),
        logs=logs,
        root=root,
    )
    process.wait(timeout=_remaining_browser_seconds(deadline))
    if process.returncode != 0:
        raise browser_actor_failure(root, actors, logs)


_BROWSER_CONTROL_LABELS = (
    "owner-ready",
    "guardian-armed",
    "inventory-ready",
    "cdp-request",
    "cdp-ready",
    "actor-error",
    "inventory-retained",
    "hold-owner",
    "job-zero",
    "check-stable-handles",
    "both-zero",
    "allow-fence-release",
    "fence-released",
    "close-guardian",
)


def browser_control_key(label: str) -> str:
    return label.replace("-", "_")


def signal_browser_control(
    controls: Mapping[str, str], label: str, signal: Callable[[str], None]
) -> None:
    signal(controls[browser_control_key(label)])


def release_exited_actor(process: Any) -> None:
    """Drop CPython's handle so Job accounting can forget an exited actor.

    ``ActiveProcesses`` still counts an exited member while this process holds
    its ``Popen`` handle. The same release precedes every production drain.
    """
    if process.poll() is None:
        raise RuntimeError("cannot release a live browser actor handle")
    handle = getattr(process, "_handle", None)
    close = getattr(handle, "Close", None)
    if not callable(close):
        raise RuntimeError("Windows Popen exposed no releasable process handle")
    close()


def prove_coordinator_is_only_outer_process[T](
    actors: list[T],
    *,
    release: Callable[[T], None],
    query_active: Callable[[], int],
    deadline: float,
    wait_for_retry: Callable[[], None],
    describe: Callable[[], str] = lambda: "",
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Require the coordinator alone after exited actor handles are released."""
    for actor in actors:
        release(actor)
    while True:
        active = query_active()
        if active == 1:
            return active
        if monotonic() >= deadline:
            detail = describe()
            suffix = f" {detail}" if detail else ""
            raise RuntimeError(
                f"outer harness ActiveProcesses={active}; expected the coordinator"
                f" alone{suffix}"
            )
        wait_for_retry()


def prepare_browser_probe_root(root: Path) -> str:
    if not root.is_dir():
        raise RuntimeError("browser probe root was not prepared by its outer harness")
    entries = {entry.name for entry in root.iterdir()}
    if entries != {"outer-job.json"}:
        raise RuntimeError(
            f"browser probe root has unexpected preexisting state: {sorted(entries)}"
        )
    outer_metadata = _read_json(root / "outer-job.json")
    if not isinstance(outer_metadata, dict) or set(outer_metadata) != {"name"}:
        raise RuntimeError("outer Job metadata has an unexpected shape")
    outer_name = outer_metadata["name"]
    if not isinstance(outer_name, str) or not outer_name:
        raise RuntimeError("outer Job metadata has no valid name")
    (root / "auth").mkdir()
    return outer_name


def _run_browser_launch_probe(scenario: str, root: Path) -> dict[str, Any]:
    from linkedin_mcp_server.profile_lease import ProfileLease

    if scenario != "browser-launch-owner-loss":
        raise RuntimeError(f"unsupported browser launch scenario: {scenario}")
    _win32api, _win32con, win32event, win32job = _windows_modules()
    outer_name = prepare_browser_probe_root(root)
    outer = win32job.OpenJobObject(win32job.JOB_OBJECT_QUERY, False, outer_name)
    events: dict[str, Any] = {}
    controls: dict[str, str] = {"outer_job_name": outer_name}
    for label in _BROWSER_CONTROL_LABELS:
        name, handle = _new_event(win32event, f"browser-{label}")
        controls[browser_control_key(label)] = name
        events[label] = handle
    processes: list[subprocess.Popen[bytes]] = []
    actor_logs: dict[str, tuple[Path, Path]] = {}
    owner = guardian = None
    deadline = time.monotonic() + _BROWSER_DEADLINE_SECONDS
    try:
        for role in ("guardian", "owner"):
            stdout_path = root / f"{role}.stdout"
            stderr_path = root / f"{role}.stderr"
            actor_logs[role] = (stdout_path, stderr_path)
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        f"browser-{role}",
                        str(root),
                        json.dumps(controls),
                    ],
                    cwd=_REPO_ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                )
            processes.append(process)
            if role == "owner":
                owner = process
            else:
                guardian = process
        assert owner is not None and guardian is not None
        actors = {"owner": owner, "guardian": guardian}
        _wait_browser_barrier(
            events["inventory-retained"],
            actor_error=events["actor-error"],
            actors=actors,
            logs=actor_logs,
            root=root,
            deadline=deadline,
            win32event=win32event,
        )
        contender = ProfileLease(root / "auth")
        rejections = []

        def reject(label: str) -> None:
            acquired = contender.try_acquire()
            if acquired:
                contender.release()
                raise RuntimeError(f"contender acquired at {label}")
            rejections.append(label)

        reject("inventory-retained")
        owner_handle = _popen_handle(owner)
        _win32api.TerminateProcess(owner_handle, 208)
        _wait(
            owner_handle,
            _remaining_browser_seconds(deadline),
            "owner did not terminate",
        )
        owner.wait(timeout=_remaining_browser_seconds(deadline))
        _wait_browser_barrier(
            events["job-zero"],
            actor_error=events["actor-error"],
            actors={"guardian": guardian},
            logs=actor_logs,
            root=root,
            deadline=deadline,
            win32event=win32event,
        )
        reject("job-zero")
        _signal(controls["check_stable_handles"])
        _wait_browser_barrier(
            events["both-zero"],
            actor_error=events["actor-error"],
            actors={"guardian": guardian},
            logs=actor_logs,
            root=root,
            deadline=deadline,
            win32event=win32event,
        )
        reject("both-zero")
        _signal(controls["allow_fence_release"])
        _wait_browser_barrier(
            events["fence-released"],
            actor_error=events["actor-error"],
            actors={"guardian": guardian},
            logs=actor_logs,
            root=root,
            deadline=deadline,
            win32event=win32event,
        )
        acquired, attempts, seconds = retry_lock_rundown(
            lambda: True if contender.try_acquire() else None,
            deadline=deadline,
            wait_for_retry=lambda: win32event.WaitForSingleObject(
                events["job-zero"], 1
            ),
        )
        assert acquired
        contender.release()
        signal_browser_control(controls, "close-guardian", _signal)
        _wait_browser_actor_completion(
            "guardian",
            guardian,
            actor_error=events["actor-error"],
            logs=actor_logs,
            root=root,
            deadline=deadline,
            win32event=win32event,
        )

        def describe_outer_remainder() -> str:
            try:
                pids = sorted(_job_process_ids(outer, win32job))
            except Exception as exc:
                return f"inventory={type(exc).__name__}: {exc}"
            inner_job = None
            inner_state = "closed"
            try:
                inner_job = win32job.OpenJobObject(
                    win32job.JOB_OBJECT_QUERY,
                    False,
                    _read_json(root / "owner.json")["inner_job_name"],
                )
                inner_state = "open"
            except Exception as exc:
                inner_state = f"unavailable:{type(exc).__name__}"
            rows: list[str] = []
            try:
                for pid in pids:
                    image = "unavailable"
                    in_inner = "unknown"
                    try:
                        handle = _win32api.OpenProcess(
                            _win32con.PROCESS_QUERY_LIMITED_INFORMATION
                            | _win32con.SYNCHRONIZE,
                            False,
                            pid,
                        )
                        try:
                            image = _full_process_image_path(handle)
                            if inner_job is not None:
                                in_inner = str(
                                    bool(win32job.IsProcessInJob(handle, inner_job))
                                )
                        finally:
                            handle.Close()
                    except Exception as exc:
                        image = f"{type(exc).__name__}: {exc}"
                    rows.append(f"{pid} inner={in_inner} image={image}")
            finally:
                if inner_job is not None:
                    inner_job.Close()
            return f"inner_job={inner_state} members=[" + "; ".join(rows) + "]"

        outer_active = prove_coordinator_is_only_outer_process(
            [owner, guardian],
            release=release_exited_actor,
            query_active=lambda: int(
                win32job.QueryInformationJobObject(
                    outer, win32job.JobObjectBasicAccountingInformation
                )["ActiveProcesses"]
            ),
            deadline=deadline,
            wait_for_retry=lambda: time.sleep(0.001),
            describe=describe_outer_remainder,
        )
        metadata = _read_json(root / "owner.json")
        browser = _read_json(root / "browser.json")
        return {
            "scenario": scenario,
            "metadata": metadata,
            "browser": browser,
            "inventory": _read_json(root / "inventory.json"),
            "guardian": _read_json(root / "guardian.json"),
            "lease_rejections": rejections,
            "post_release_acquired": acquired,
            "post_release_attempts": attempts,
            "post_release_seconds": seconds,
            "outer_active_processes_before_coordinator_exit": outer_active,
        }
    finally:
        for handle in events.values():
            with contextlib.suppress(BaseException):
                handle.Close()
        with contextlib.suppress(BaseException):
            outer.Close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="role", required=True)

    run = subparsers.add_parser("run")
    run.add_argument(
        "scenario",
        choices=(
            "baseline",
            "browser-launch-owner-loss",
            "candidate",
            "candidate-guardian-loss-before-owner",
            "candidate-terminate-error",
            "candidate-query-error",
            "candidate-drain-timeout",
            "conjunction-lock-regions",
            "conjunction-publication",
            "conjunction-owner-loss",
            "conjunction-guardian-loss-clean-close",
            "conjunction-owner-loss-terminate-error",
            "conjunction-owner-loss-query-error",
            "conjunction-owner-loss-drain-timeout",
            "falsification-original-owner-loss",
            "falsification-inverted-guardian-loss",
            "falsification-inverted-pre-arm",
            "falsification-inverted-post-disarm-mutation",
            "falsification-inverted-exclusive-mutation",
            "job-topology-self-assignment",
            "job-topology-breakaway",
            "job-topology-breakaway-denied",
            "job-topology-common-ancestor-loss",
        ),
    )
    run.add_argument("root", type=Path)

    for browser_role in ("browser-owner", "browser-guardian"):
        browser_actor = subparsers.add_parser(browser_role)
        browser_actor.add_argument("root", type=Path)
        browser_actor.add_argument("controls", type=json.loads)

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

    actor = subparsers.add_parser("actor")
    actor.add_argument(
        "mode",
        choices=(
            "hold",
            "attempt",
            "production-byte-zero",
            "region-probe",
            "guardian-publish",
            "guardian-drain",
            "owner-watch",
        ),
    )
    actor.add_argument("lock_path", type=Path)
    actor.add_argument("expected_identity", type=json.loads)
    actor.add_argument("ready_event")
    actor.add_argument("result_file", type=Path)
    actor.add_argument("options", type=json.loads)

    topology_actor = subparsers.add_parser("topology-actor")
    topology_actor.add_argument("scenario")
    topology_actor.add_argument("outer_job_name")
    topology_actor.add_argument("start_event")
    topology_actor.add_argument("result_file", type=Path)

    topology_holder = subparsers.add_parser("topology-holder")
    topology_holder.add_argument("ready_event")
    topology_holder.add_argument("release_event")

    gate = subparsers.add_parser("gate")
    gate.add_argument("wait_event")
    gate.add_argument("started_event")
    return parser.parse_args()


def main() -> int:
    faulthandler.enable()
    args = _parse_args()
    if os.name != "nt":
        raise SystemExit("this probe requires native Windows Job Objects")
    if args.role == "run":
        runner_name = topology_runner(args.scenario)
        runner = {
            "browser-launch": _run_browser_launch_probe,
            "job-topology": _run_job_topology_probe,
            "conjunction": _run_conjunction_probe,
            "region-falsification": _run_region_falsification_probe,
            "crash-fence": _run_probe,
        }[runner_name]
        print(json.dumps(runner(args.scenario, args.root)), flush=True)
        return 0
    if args.role == "browser-owner":
        try:
            return asyncio.run(_browser_launch_owner(args.root, args.controls))
        except BaseException as exc:
            _atomic_json(
                args.root / "owner-error.json",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            _signal(args.controls["actor_error"])
            raise
    if args.role == "browser-guardian":
        return _browser_launch_guardian(args.root, args.controls)
    if args.role == "topology-actor":
        return _topology_actor(
            args.scenario,
            args.outer_job_name,
            args.start_event,
            args.result_file,
        )
    if args.role == "topology-holder":
        return _topology_holder(args.ready_event, args.release_event)
    if args.role == "actor":
        return _actor(
            args.mode,
            args.lock_path,
            args.expected_identity,
            args.ready_event,
            args.result_file,
            args.options,
        )
    if args.role == "gate":
        return _gate(args.wait_event, args.started_event)
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
