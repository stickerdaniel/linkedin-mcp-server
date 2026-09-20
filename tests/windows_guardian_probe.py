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


def file_identity(fd: int) -> tuple[int, int, int]:
    """Read the stable Windows file identity carried by an inherited handle."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTimeLow", wintypes.DWORD),
            ("ftCreationTimeHigh", wintypes.DWORD),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
    get_info.restype = wintypes.BOOL
    info = ByHandleFileInformation()
    handle = getattr(msvcrt, "get_osfhandle")(fd)
    if not get_info(handle, ctypes.byref(info)):
        raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
    return (
        int(info.dwVolumeSerialNumber),
        int(info.nFileIndexHigh),
        int(info.nFileIndexLow),
    )


def require_same_file_identity(
    expected: tuple[int, int, int] | list[int],
    observed: tuple[int, int, int] | list[int],
) -> None:
    if tuple(expected) != tuple(observed):
        raise RuntimeError("owner and guardian do not reference the same file identity")


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


def _inherited_fd(raw_handle: int) -> int:
    import msvcrt

    return int(getattr(msvcrt, "open_osfhandle")(raw_handle, os.O_RDWR))


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
    raw_fd_handle: int,
    ready_event: str,
    result_file: Path,
    options: dict[str, Any],
) -> int:
    win32api, win32con, win32event, win32job = _windows_modules()
    descriptor = _ActorFd(_inherited_fd(raw_fd_handle) if raw_fd_handle else -1)
    fd = descriptor.fd
    try_lock, unlock = _region_api()
    result: dict[str, Any] = {"mode": mode}
    job = None
    process_handle = None
    descendant_handles: list[Any] = []
    locked_offset: int | None = None
    first_error: BaseException | None = None
    try:
        if fd >= 0:
            result["file_identity"] = list(file_identity(fd))
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
            current_in_browser_job = bool(
                win32job.IsProcessInJob(win32api.GetCurrentProcess(), job)
            )
            watched_in_browser_job = bool(win32job.IsProcessInJob(process_handle, job))
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

        offset = int(options["offset"])
        if mode == "guardian-publish":
            acquired_b = False
            owner_alive_after_b: bool | None = None

            def acquire_b() -> bool:
                nonlocal acquired_b, locked_offset
                acquired_b = try_lock(fd, offset)
                if acquired_b:
                    locked_offset = offset
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

        if not try_lock(fd, offset):
            result["contention"] = True
            _atomic_json(result_file, result)
            _signal(ready_event)
            return 0
        locked_offset = offset

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
    try_admission: Callable[[int], bool],
    release_a: Callable[[int], None],
    close_fd: Callable[[int], None],
    deadline: float,
    wait_for_retry: Callable[[], None],
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[bool, int, float]:
    def attempt() -> bool | None:
        fd = open_fd()
        try:
            if not try_admission(fd):
                return None
            release_a(fd)
            return True
        finally:
            close_fd(fd)

    return retry_lock_rundown(
        attempt,
        deadline=deadline,
        wait_for_retry=wait_for_retry,
        monotonic=monotonic,
    )


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
    fd_handle: int,
    ready_name: str,
    result_file: Path,
    options: dict[str, Any],
    extra_handles: list[int] | None = None,
) -> subprocess.Popen[bytes]:
    win32api, win32con, _win32event, _win32job = _windows_modules()
    sources = [fd_handle] if fd_handle else []
    sources.extend(extra_handles or [])

    def duplicate(source: int) -> Any:
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
            str(mapping.get(fd_handle, 0)),
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


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    win32api, win32con, _win32event, _win32job = _windows_modules()
    handle = win32api.OpenProcess(
        win32con.PROCESS_TERMINATE | win32con.SYNCHRONIZE, False, process.pid
    )
    try:
        if _is_active(handle):
            win32api.TerminateProcess(handle, 203)
        _wait(handle, _DEADLINE_SECONDS, "process did not terminate")
    finally:
        handle.Close()
    process.wait(timeout=_DEADLINE_SECONDS)


def _probe_fd(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


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


def _run_conjunction_probe(scenario: str, root: Path) -> dict[str, Any]:
    """Run native conjunction scenarios under the caller's outer harness Job."""
    from linkedin_mcp_server import process_tree

    win32api, win32con, win32event, win32job = _windows_modules()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "auth" / "profile.lock"
    base_fd = _probe_fd(lock_path)
    import msvcrt

    base_handle = int(getattr(msvcrt, "get_osfhandle")(base_fd))
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
            mode, base_handle, ready_name, result_file, options, extra_handles
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
            successor, successor_fd = _attempt_here(lock_path)
            if not successor:
                os.close(successor_fd)
                raise RuntimeError("successor could not pass A+B")
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
                "late_successor_admitted": successor,
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
        outer_in_browser_job = bool(
            win32job.IsProcessInJob(
                win32api.GetCurrentProcess(), browser_job.job_handle
            )
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
            "conjunction-lock-regions",
            "conjunction-publication",
            "conjunction-owner-loss",
            "conjunction-guardian-loss-clean-close",
            "conjunction-owner-loss-terminate-error",
            "conjunction-owner-loss-query-error",
            "conjunction-owner-loss-drain-timeout",
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

    actor = subparsers.add_parser("actor")
    actor.add_argument(
        "mode",
        choices=(
            "hold",
            "attempt",
            "region-probe",
            "guardian-publish",
            "guardian-drain",
            "owner-watch",
        ),
    )
    actor.add_argument("raw_fd_handle", type=int)
    actor.add_argument("ready_event")
    actor.add_argument("result_file", type=Path)
    actor.add_argument("options", type=json.loads)

    gate = subparsers.add_parser("gate")
    gate.add_argument("wait_event")
    gate.add_argument("started_event")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if os.name != "nt":
        raise SystemExit("this probe requires native Windows Job Objects")
    if args.role == "run":
        runner = (
            _run_conjunction_probe
            if args.scenario.startswith("conjunction-")
            else _run_probe
        )
        print(json.dumps(runner(args.scenario, args.root)), flush=True)
        return 0
    if args.role == "actor":
        return _actor(
            args.mode,
            args.raw_fd_handle,
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
