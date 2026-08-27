"""Contain subprocess descendants behind one platform-sized boundary."""

from __future__ import annotations

import contextlib
import ctypes
import importlib
import logging
import os
import secrets
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

logger = logging.getLogger(__name__)

_adopted_windows_job: int | None = None
_retained_windows_jobs: list[WindowsJob] = []
_registered_posix_groups: dict[int, str] = {}
_JOB_API_FAILURE_LIMIT = 3
_JOB_POLL_SECONDS = 0.01
_JOB_NAME_ATTEMPTS = 8
_RELEASE_NONCE_BYTES = 32
_POSIX_PROCESS_SNAPSHOT_SECONDS = 1.0


class ProcessTreeError(RuntimeError):
    """The operating system could not establish descendant containment."""


def _linux_process_rows() -> dict[int, tuple[int, int, str | None]]:
    """Read ancestry, groups and kernel start identities directly from procfs."""
    rows: dict[int, tuple[int, int, str | None]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text()
        except (OSError, UnicodeError):
            continue
        closing = raw.rfind(")")
        fields = raw[closing + 2 :].split() if closing >= 0 else []
        if len(fields) <= 19:
            continue
        try:
            process = int(entry.name)
            parent = int(fields[1])
            group = int(fields[2])
        except ValueError:
            continue
        rows[process] = (parent, group, f"proc:{fields[19]}")
    return rows


def _ps_process_rows() -> dict[int, tuple[int, int, str | None]]:
    """Read process ancestry on POSIX systems without procfs."""
    ps = next(
        (
            candidate
            for candidate in ("/bin/ps", "/usr/bin/ps")
            if Path(candidate).is_file()
        ),
        None,
    )
    if ps is None:
        return {}
    try:
        snapshot = subprocess.run(
            [ps, "-A", "-o", "pid=", "-o", "ppid=", "-o", "pgid="],
            check=True,
            capture_output=True,
            text=True,
            timeout=_POSIX_PROCESS_SNAPSHOT_SECONDS,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}

    rows: dict[int, tuple[int, int, str | None]] = {}
    for line in snapshot.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            process, parent, group = map(int, fields)
        except ValueError:
            continue
        rows[process] = (parent, group, None)
    return rows


def _posix_detached_descendants(
    pid: int, process_group: int
) -> tuple[tuple[int, int, str], ...]:
    """Snapshot escaped descendants, groups and kernel start identities."""
    try:
        rows = (
            _linux_process_rows()
            if sys.platform.startswith("linux")
            else _ps_process_rows()
        )
    except OSError:
        return ()
    children: dict[int, list[int]] = {}
    for process, (parent, _group, _identity) in rows.items():
        children.setdefault(parent, []).append(process)

    detached: list[tuple[int, int, int, str]] = []
    pending = [(pid, 0)]
    seen = {pid}
    while pending:
        parent, depth = pending.pop()
        for child in children.get(parent, ()):
            if child in seen:
                continue
            seen.add(child)
            pending.append((child, depth + 1))
            _parent, group, identity = rows[child]
            if group == process_group:
                continue
            identity = identity or _kernel_start_identity(child)
            if identity is not None:
                detached.append((depth + 1, child, group, identity))
    return tuple(
        (child, group, identity)
        for _depth, child, group, identity in sorted(detached, reverse=True)
    )


def remember_detached_process_groups() -> None:
    """Retain browser groups while their parentage is still observable."""
    if os.name == "nt":
        return
    pid = os.getpid()
    process_group = os.getpgrp()
    for _descendant, group, _identity in _posix_detached_descendants(
        pid, process_group
    ):
        identity = _kernel_start_identity(group)
        if identity is not None:
            _registered_posix_groups.setdefault(group, identity)


def forget_detached_process_groups() -> None:
    """Forget groups after browser shutdown has been confirmed."""
    _registered_posix_groups.clear()


def _kill_registered_process_groups() -> None:
    for group, identity in tuple(_registered_posix_groups.items()):
        current = _kernel_start_identity(group)
        if current is not None and current != identity:
            continue
        if current is None and not process_group_exists(group):
            continue
        with contextlib.suppress(OSError):
            os.killpg(group, signal.SIGKILL)


def hard_exit_process_tree(status: int) -> NoReturn:
    """Exit immediately and kill descendants across their POSIX process groups."""
    if os.name != "nt":
        pid = os.getpid()
        try:
            process_group = os.getpgrp()
            if process_group == pid:
                # Chromium is deliberately spawned detached by Patchright. Snapshot
                # it while the Node parent still makes ancestry observable, then kill
                # escaped descendants before the owner's own group.
                _kill_registered_process_groups()
                os.killpg(process_group, signal.SIGKILL)
        except OSError:
            pass
    # On Windows, process exit closes the only Job handle. On POSIX this is the
    # fallback when the caller was not launched as the expected group leader.
    os._exit(status)


def release_nonce() -> str:
    """Return an unpredictable process-gate release token."""
    return secrets.token_hex(_RELEASE_NONCE_BYTES)


def windows_gate_command(target: list[str], nonce: str) -> list[str]:
    """Wrap *target* in the stdlib-only isolated release gate."""
    gate = Path(__file__).with_name("process_gate.py").resolve()
    return [sys.executable, "-I", "-S", "-u", str(gate), nonce, "--", *target]


def release_windows_gate(stream: Any, nonce: str) -> None:
    """Release one assigned gate without consuming target stdin."""
    stream.write(f"release {nonce}\n".encode("ascii"))
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


class WindowsJob:
    """A parent-owned kill-on-close Windows Job Object."""

    def __init__(
        self,
        handle: Any,
        *,
        name: str | None,
        win32api: Any,
        win32job: Any,
    ) -> None:
        self._handle = handle
        self.name = name
        self._win32api = win32api
        self._win32job = win32job

    @classmethod
    def anonymous(cls) -> WindowsJob:
        """Create an anonymous Job for one installer run."""
        return cls._create(None)

    @classmethod
    def named(cls, purpose: str) -> WindowsJob:
        """Create a collision-checked named Job for owner handoff."""
        win32api, win32con, win32job, winerror = _windows_modules()
        for _ in range(_JOB_NAME_ATTEMPTS):
            name = f"Local\\linkedin-mcp-{purpose}-{secrets.token_hex(16)}"
            try:
                handle = win32job.CreateJobObject(None, name or "")
            except Exception as exc:
                raise ProcessTreeError(
                    "Windows could not create a named Job Object"
                ) from exc
            if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
                handle.Close()
                continue
            return cls._configure(
                handle,
                name=name,
                win32api=win32api,
                win32con=win32con,
                win32job=win32job,
            )
        raise ProcessTreeError("Windows could not create a unique named Job Object")

    @classmethod
    def _create(cls, name: str | None) -> WindowsJob:
        win32api, win32con, win32job, _winerror = _windows_modules()
        try:
            handle = win32job.CreateJobObject(None, name or "")
        except Exception as exc:
            raise ProcessTreeError("Windows could not create a Job Object") from exc
        return cls._configure(
            handle,
            name=name,
            win32api=win32api,
            win32con=win32con,
            win32job=win32job,
        )

    @classmethod
    def _configure(
        cls,
        handle: Any,
        *,
        name: str | None,
        win32api: Any,
        win32con: Any,
        win32job: Any,
    ) -> WindowsJob:
        try:
            win32api.SetHandleInformation(handle, win32con.HANDLE_FLAG_INHERIT, 0)
            limits = win32job.QueryInformationJobObject(
                handle, win32job.JobObjectExtendedLimitInformation
            )
            limits["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                handle, win32job.JobObjectExtendedLimitInformation, limits
            )
        except Exception as exc:
            handle.Close()
            raise ProcessTreeError("Windows could not configure a Job Object") from exc
        return cls(
            handle,
            name=name,
            win32api=win32api,
            win32job=win32job,
        )

    @property
    def closed(self) -> bool:
        return self._handle is None

    def assign_popen(self, child: subprocess.Popen[Any]) -> None:
        """Assign and verify a real ``Popen`` process handle."""
        process_handle = getattr(child, "_handle", None)
        if process_handle is None:
            raise ProcessTreeError("Windows Popen exposed no process handle")
        self._assign_handle(process_handle)

    def assign_asyncio_process(self, child: Any) -> subprocess.Popen[Any]:
        """Assign an asyncio child through its transport's real ``Popen``."""
        transport = getattr(child, "_transport", None)
        get_extra_info = getattr(transport, "get_extra_info", None)
        if not callable(get_extra_info):
            raise ProcessTreeError("Windows asyncio exposed no subprocess transport")
        popen = get_extra_info("subprocess")
        if popen is None or getattr(popen, "_handle", None) is None:
            raise ProcessTreeError("Windows asyncio exposed no underlying Popen")
        self.assign_popen(popen)
        return popen

    def _assign_handle(self, process_handle: Any) -> None:
        handle = self._require_handle()
        try:
            self._win32job.AssignProcessToJobObject(handle, process_handle)
            assigned = self._win32job.IsProcessInJob(process_handle, handle)
        except Exception as exc:
            raise ProcessTreeError(
                "Windows could not assign the managed process"
            ) from exc
        if not assigned:
            raise ProcessTreeError(
                "Windows did not retain the managed process in its Job"
            )

    def terminate(self) -> None:
        """Request unconditional termination of every process in the Job."""
        handle = self._require_handle()
        last_error: BaseException | None = None
        for attempt in range(_JOB_API_FAILURE_LIMIT):
            try:
                self._win32job.TerminateJobObject(handle, 1)
                return
            except BaseException as exc:  # noqa: BLE001 - wrapped after retries
                last_error = exc
            if attempt + 1 < _JOB_API_FAILURE_LIMIT:
                time.sleep(_JOB_POLL_SECONDS)
        raise ProcessTreeError("Windows could not terminate the managed Job") from (
            last_error
        )

    def release_popen_handle(self, child: subprocess.Popen[Any]) -> None:
        """Release an exited CPython ``Popen`` process-object reference."""
        if child.returncode is None:
            raise ProcessTreeError(
                "Windows cannot release a process handle before process exit"
            )
        process_handle = getattr(child, "_handle", None)
        close = getattr(process_handle, "Close", None)
        if not callable(close):
            raise ProcessTreeError("Windows Popen exposed no releasable process handle")
        try:
            close()
        except Exception as exc:
            raise ProcessTreeError(
                "Windows could not release the managed process handle"
            ) from exc

    def wait_until_empty(self, *, timeout: float) -> None:
        """Close the Job after bounded proof that no process remains associated."""
        handle = self._require_handle()
        deadline = time.monotonic() + max(timeout, 0.0)
        consecutive_failures = 0
        while True:
            try:
                accounting = self._win32job.QueryInformationJobObject(
                    handle, self._win32job.JobObjectBasicAccountingInformation
                )
                active = int(accounting["ActiveProcesses"])
            except BaseException as exc:  # noqa: BLE001 - never treated as empty
                consecutive_failures += 1
                if consecutive_failures >= _JOB_API_FAILURE_LIMIT:
                    self._retain()
                    raise ProcessTreeError(
                        "Windows could not verify that the managed Job drained"
                    ) from exc
            else:
                consecutive_failures = 0
                if active == 0:
                    self.close()
                    return

            if time.monotonic() >= deadline:
                self._retain()
                raise ProcessTreeError(
                    "The managed Windows Job did not drain before its deadline"
                )
            time.sleep(min(_JOB_POLL_SECONDS, max(deadline - time.monotonic(), 0.0)))

    def _retain(self) -> None:
        """Keep containment authority alive after drainage cannot be proved."""
        if self not in _retained_windows_jobs:
            _retained_windows_jobs.append(self)

    def close(self) -> None:
        """Close this owner handle without claiming the Job drained."""
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.Close()
        with contextlib.suppress(ValueError):
            _retained_windows_jobs.remove(self)

    def _require_handle(self) -> Any:
        if self._handle is None:
            raise ProcessTreeError("The Windows Job handle is already closed")
        return self._handle

    @staticmethod
    def verify_current_process(name: str) -> None:
        """Verify exact named-Job membership without retaining a handle."""
        win32api, _win32con, win32job, _winerror = _windows_modules()
        try:
            handle = win32job.OpenJobObject(win32job.JOB_OBJECT_QUERY, False, name)
            try:
                assigned = win32job.IsProcessInJob(win32api.GetCurrentProcess(), handle)
            finally:
                handle.Close()
        except Exception as exc:
            raise ProcessTreeError(
                "Windows could not verify owner Job membership"
            ) from exc
        if not assigned:
            raise ProcessTreeError("The owner is not a member of its named Windows Job")

    @staticmethod
    def adopt_current_process(name: str) -> None:
        """Retain a verified named Job handle until process teardown."""
        global _adopted_windows_job
        if _adopted_windows_job is not None:
            raise ProcessTreeError("The owner already adopted a Windows Job")
        win32api, _win32con, win32job, _winerror = _windows_modules()
        handle: Any | None = None
        try:
            handle = win32job.OpenJobObject(win32job.JOB_OBJECT_QUERY, False, name)
            if not win32job.IsProcessInJob(win32api.GetCurrentProcess(), handle):
                raise ProcessTreeError(
                    "The owner is not a member of its named Windows Job"
                )
            _adopted_windows_job = int(handle.Detach())
            handle = None
        except ProcessTreeError:
            raise
        except Exception as exc:
            raise ProcessTreeError("Windows could not adopt the owner Job") from exc
        finally:
            if handle is not None:
                handle.Close()


def _windows_modules() -> tuple[Any, Any, Any, Any]:
    if os.name != "nt":
        raise ProcessTreeError("Windows Job Objects do not exist on this platform")
    try:
        return (
            importlib.import_module("win32api"),
            importlib.import_module("win32con"),
            importlib.import_module("win32job"),
            importlib.import_module("winerror"),
        )
    except Exception as exc:
        raise ProcessTreeError("pywin32 Job Object APIs are unavailable") from exc


class _ProcBsdInfo(ctypes.Structure):
    """Darwin's ``struct proc_bsdinfo`` through the start timestamp fields."""

    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _kernel_start_identity(pid: int) -> str | None:
    """Return a process start identity precise enough to reject PID reuse."""
    if sys.platform == "darwin":
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            proc_pidinfo = libproc.proc_pidinfo
            proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            proc_pidinfo.restype = ctypes.c_int
            info = _ProcBsdInfo()
            read = proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        except (AttributeError, OSError):
            return None
        if read != ctypes.sizeof(info):
            return None
        return f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"

    try:
        raw = (Path(f"/proc/{pid}/stat")).read_text()
    except (OSError, UnicodeError):
        return None
    closing = raw.rfind(")")
    fields = raw[closing + 2 :].split() if closing >= 0 else []
    return f"proc:{fields[19]}" if len(fields) > 19 else None


def process_group_exists(pgid: int) -> bool:
    """Whether POSIX still has any process in *pgid*."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _darwin_child_exited_without_reaping(pid: int) -> bool:
    """Observe NOTE_EXIT without collecting the Darwin child."""
    kqueue = select.kqueue()
    try:
        event = select.kevent(
            pid,
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
            fflags=select.KQ_NOTE_EXIT,
        )
        try:
            kqueue.control([event], 0, 0)
        except ProcessLookupError:
            # A child that has exited but has not been waited for no longer accepts
            # a new process filter. Its zombie still pins the pid for its parent.
            return True
        return bool(kqueue.control(None, 1, 0))
    finally:
        kqueue.close()


def child_exited_without_reaping(child: subprocess.Popen[Any]) -> bool:
    """Observe a POSIX child exit while its zombie still pins the numeric pid."""
    if os.name == "nt":
        return child.poll() is not None
    waitid = getattr(os, "waitid", None)
    if waitid is None:
        if sys.platform == "darwin":
            return _darwin_child_exited_without_reaping(child.pid)
        raise ProcessTreeError("POSIX child identity cannot be observed safely")
    try:
        result = waitid(
            os.P_PID,
            child.pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
    except ChildProcessError:
        return child.returncode is not None
    return result is not None


def _reap_and_wait_for_group(
    pgid: int,
    child: subprocess.Popen[Any],
    deadline: float,
    leader_identity: str | None,
) -> bool:
    child.wait(timeout=max(deadline - time.monotonic(), 0.0))
    while process_group_exists(pgid):
        current_identity = _kernel_start_identity(pgid)
        if leader_identity is not None and current_identity not in (
            None,
            leader_identity,
        ):
            try:
                if os.getpgid(pgid) == pgid:
                    return True
            except ProcessLookupError:
                pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def terminate_process_group(
    pgid: int,
    *,
    timeout: float,
    child: subprocess.Popen[Any],
) -> bool:
    """Kill a POSIX child group while its unreaped leader pins the PGID."""
    if os.name == "nt":
        raise ProcessTreeError("POSIX process groups do not exist on Windows")

    leader_identity = _kernel_start_identity(child.pid)
    if child.returncode is not None:
        return False
    waitid = getattr(os, "waitid", None)
    if waitid is not None:
        try:
            waitid(
                os.P_PID,
                child.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError:
            return False
    elif sys.platform == "darwin":
        try:
            child_exited_without_reaping(child)
        except OSError as exc:
            raise ProcessTreeError(
                "Darwin child identity cannot be observed safely"
            ) from exc
    else:
        raise ProcessTreeError("POSIX child identity cannot be observed safely")

    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            child.poll()
            return True
        except PermissionError as exc:
            if child_exited_without_reaping(child):
                return _reap_and_wait_for_group(pgid, child, deadline, leader_identity)
            logger.warning("The process group %s could not be signalled: %s", pgid, exc)
            return False

        if child_exited_without_reaping(child):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            return _reap_and_wait_for_group(pgid, child, deadline, leader_identity)
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
