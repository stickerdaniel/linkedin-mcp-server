"""A contender for the profile lease, armed before a hard kill (#808).

On Windows the killed owner's profile lock and its browser's kill-on-close Job
close together in the owner's teardown, and the Job's members then end
asynchronously, so the lock may come free while the old browser still runs.
``lease_probe`` asks once and only on POSIX. This one asks continuously, from
before the kill, with the primitive the product takes on each platform
(``profile_lease.try_lock``): ``LockFileEx``, exclusive and fail-immediately,
on byte 0 on Windows, and ``flock``, exclusive and non-blocking, on POSIX. A
grant is released at once, and the longest hold is recorded.

The helper runs as its own process with no site processing, like
``lease_probe``. It opens the existing lock file once, never creating,
truncating or announcing anything, and a handle to each process it is told to
watch, checked against the creation time the harness read. Then, every
``interval`` until the stop file appears or its deadline passes, it asks for
the lock and asks each watched process whether it has ended, and stamps each
answer. Equal consecutive lock answers fold into one run, so the record stays
small however long the helper ran.

It holds no Job handle and must not. A handle to a kill-on-close Job keeps the
Job open past its owner's death and postpones the termination being measured.
A process handle does not: a process ends whatever handles to it remain, and
holding one keeps its id from being reused while it is watched. On Windows the
same handle yields the kernel's exit time.

The POSIX branch exists so the loop can be exercised anywhere. There a watched
process is asked by id with signal 0, which cannot see an id reused, so only
the Windows row relies on what it says about processes.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

HELD = "held"
FREE = "free"
UNKNOWN = "unknown"
#: The kind of the watched process the harness kills, apart from its tree.
ACTOR = "actor"

CONFIG_FILE = "config.json"
READY_FILE = "ready.json"
STOP_FILE = "stop"
RESULT_FILE = "result.json"
STDERR_FILE = "stderr.txt"

#: How far a watched process's creation time may sit from the one the harness
#: read with psutil, which converts the same kernel value.
START_TOLERANCE_SECONDS = 0.05

#: The errnos ``profile_lease.try_lock`` reads as contention on POSIX.
_CONTENTION = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES})

#: A watched process as opened: its handle, whether its creation time matched
#: (None where the platform cannot say), why it could not be opened, and
#: whether it was already gone.
_Opened = tuple[Any, bool | None, str | None, bool]


class _PosixBackend:
    primitive = "flock"

    def raise_priority(self) -> bool:
        return False

    def lock(self, fd: int) -> tuple[str, str | None, int | None]:
        import fcntl

        began = time.monotonic_ns()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _CONTENTION:
                return HELD, None, None
            return UNKNOWN, f"flock failed: {exc!r}", None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return FREE, None, time.monotonic_ns() - began

    def open_process(self, pid: int, start: float) -> _Opened:
        if self._alive(pid) is False:
            return None, None, None, True
        return pid, None, None, False

    def ended(self, handle: Any) -> bool | None:
        alive = self._alive(handle)
        return None if alive is None else not alive

    def exit_wall_ns(self, handle: Any) -> int | None:
        return None

    def close(self, handle: Any) -> None:
        return None

    @staticmethod
    def _alive(pid: int) -> bool | None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None
        return True


class _WindowsBackend:  # pragma: no cover - Windows
    primitive = "LockFileEx"

    _SYNCHRONIZE = 0x00100000
    _QUERY_LIMITED_INFORMATION = 0x1000
    _WAIT_OBJECT_0 = 0x0
    _WAIT_TIMEOUT = 0x102
    _ERROR_LOCK_VIOLATION = 33
    #: What OpenProcess answers for an id no process holds.
    _ERROR_INVALID_PARAMETER = 87
    _LOCKFILE_EXCLUSIVE_LOCK = 0x2
    _LOCKFILE_FAIL_IMMEDIATELY = 0x1
    _HIGH_PRIORITY_CLASS = 0x80
    #: 1970-01-01 in FILETIME's 100 ns units since 1601-01-01.
    _UNIX_EPOCH = 116444736000000000

    def __init__(self) -> None:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        # Windows-only names, resolved dynamically for a checker elsewhere.
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        self._ctypes = ctypes
        self._last_error = getattr(ctypes, "get_last_error")
        self._os_handle = getattr(msvcrt, "get_osfhandle")
        self._overlapped = Overlapped
        self._filetime = wintypes.FILETIME
        filetime = ctypes.POINTER(wintypes.FILETIME)

        def bind(name: str, argtypes: list[Any], restype: Any) -> Any:
            function = getattr(kernel32, name)
            function.argtypes = argtypes
            function.restype = restype
            return function

        handle, dword, boolean = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
        self._lock_file_ex = bind(
            "LockFileEx",
            [handle, dword, dword, dword, dword, ctypes.POINTER(Overlapped)],
            boolean,
        )
        self._unlock_file_ex = bind(
            "UnlockFileEx",
            [handle, dword, dword, dword, ctypes.POINTER(Overlapped)],
            boolean,
        )
        self._open_process = bind("OpenProcess", [dword, boolean, dword], handle)
        self._close_handle = bind("CloseHandle", [handle], boolean)
        self._wait = bind("WaitForSingleObject", [handle, dword], dword)
        self._times = bind(
            "GetProcessTimes", [handle, filetime, filetime, filetime, filetime], boolean
        )
        self._current = bind("GetCurrentProcess", [], handle)
        self._priority = bind("SetPriorityClass", [handle, dword], boolean)

    def raise_priority(self) -> bool:
        # Asked for by the interpreter itself: a venv's python.exe is a
        # launcher, and a class given to it is not passed on (``watcher``).
        return bool(self._priority(self._current(), self._HIGH_PRIORITY_CLASS))

    def lock(self, fd: int) -> tuple[str, str | None, int | None]:
        handle = self._os_handle(fd)
        flags = self._LOCKFILE_FAIL_IMMEDIATELY | self._LOCKFILE_EXCLUSIVE_LOCK
        began = time.monotonic_ns()
        if not self._lock_file_ex(
            handle, flags, 0, 1, 0, self._ctypes.byref(self._overlapped())
        ):
            error = self._last_error()
            if error == self._ERROR_LOCK_VIOLATION:
                return HELD, None, None
            return UNKNOWN, f"LockFileEx failed with error {error}", None
        if not self._unlock_file_ex(
            handle, 0, 1, 0, self._ctypes.byref(self._overlapped())
        ):
            # A grant this helper cannot give back would hold the profile
            # against the very successor being measured: stop asking.
            raise OSError(f"UnlockFileEx failed with error {self._last_error()}")
        return FREE, None, time.monotonic_ns() - began

    def _filetimes(self, handle: Any) -> tuple[int, int] | None:
        created, exited = self._filetime(), self._filetime()
        kernel, user = self._filetime(), self._filetime()
        byref = self._ctypes.byref
        if not self._times(
            handle, byref(created), byref(exited), byref(kernel), byref(user)
        ):
            return None
        return self._ns(created), self._ns(exited)

    def _ns(self, value: Any) -> int:
        ticks = (value.dwHighDateTime << 32) | value.dwLowDateTime
        return (ticks - self._UNIX_EPOCH) * 100

    def open_process(self, pid: int, start: float) -> _Opened:
        access = self._SYNCHRONIZE | self._QUERY_LIMITED_INFORMATION
        handle = self._open_process(access, False, pid)
        if not handle:
            error = self._last_error()
            if error == self._ERROR_INVALID_PARAMETER:
                return None, None, None, True
            return None, None, f"OpenProcess failed with error {error}", False
        times = self._filetimes(handle)
        if times is None:
            error = self._last_error()
            self._close_handle(handle)
            return None, None, f"GetProcessTimes failed with error {error}", False
        created = times[0] / 1e9
        if abs(created - start) > START_TOLERANCE_SECONDS:
            # Windows gives an id out again only once the process that held
            # it is gone, so the one the harness read has ended.
            self._close_handle(handle)
            return None, False, f"the id now names a process created at {created}", True
        return handle, True, None, False

    def ended(self, handle: Any) -> bool | None:
        answer = self._wait(handle, 0)
        if answer == self._WAIT_OBJECT_0:
            return True
        if answer == self._WAIT_TIMEOUT:
            return False
        return None

    def exit_wall_ns(self, handle: Any) -> int | None:
        times = self._filetimes(handle)
        return times[1] if times is not None else None

    def close(self, handle: Any) -> None:
        if handle:
            self._close_handle(handle)


def _backend() -> Any:
    try:
        import fcntl  # noqa: F401
    except ImportError:  # pragma: no cover - Windows
        return _WindowsBackend()
    return _PosixBackend()


class _Watched:
    """One watched process: when it was last seen alive and first seen gone,
    on the wall clock, and the kernel's exit time where the platform says."""

    def __init__(self, spec: Mapping[str, Any], backend: Any) -> None:
        self.pid, self.start = int(spec["pid"]), float(spec["start"])
        self.kind = str(spec["kind"])
        self.handle, self.verified, self.error, gone = backend.open_process(
            self.pid, self.start
        )
        self.last_alive: int | None = None
        # Gone before the first round, and so before any kill it races.
        self.gone_before_watched = gone
        self.gone: int | None = time.time_ns() if gone else None
        self.exit: int | None = None
        self.unanswered = 0

    def observe(self, backend: Any) -> None:
        if self.handle is None or self.gone is not None:
            return
        # Alive at the call is alive at the stamp before it; gone at the call
        # is gone by the stamp after it.
        before = time.time_ns()
        ended = backend.ended(self.handle)
        if ended is None:
            self.unanswered += 1
        elif ended:
            self.gone = time.time_ns()
            self.exit = backend.exit_wall_ns(self.handle)
        else:
            self.last_alive = before

    def record(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "start": self.start,
            "kind": self.kind,
            "opened": self.handle is not None,
            "verified": self.verified,
            "gone_before_watched": self.gone_before_watched,
            "error": self.error,
            "last_alive_wall_ns": self.last_alive,
            "gone_wall_ns": self.gone,
            "exit_wall_ns": self.exit,
            "unanswered": self.unanswered,
        }


def _fold(runs: list[dict[str, Any]], state: str, reason: str | None) -> None:
    """Append one stamped answer to *runs*, folding it into the last run when
    it says the same."""
    wall, mono = time.time_ns(), time.monotonic_ns()
    last = runs[-1] if runs else None
    if last is not None and last["state"] == state and last["reason"] == reason:
        last.update(last_wall_ns=wall, last_ns=mono, count=last["count"] + 1)
        return
    runs.append(
        {
            "state": state,
            "reason": reason,
            "first_wall_ns": wall,
            "first_ns": mono,
            "last_wall_ns": wall,
            "last_ns": mono,
            "count": 1,
        }
    )


def _write(path: Path, value: Mapping[str, Any]) -> None:
    """Write *value* whole or not at all, so a reader never sees half."""
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(value))
    os.replace(partial, path)


def contend(config: Mapping[str, Any]) -> dict[str, Any]:
    """The helper's loop; see the module docstring. Never raises."""
    backend = _backend()
    directory = Path(config["directory"])
    found: dict[str, Any] = {
        "platform": sys.platform,
        "primitive": backend.primitive,
        "interval": float(config["interval"]),
        "priority_raised": False,
        "ready": None,
        "lease": [],
        "watched": [],
        "probes": 0,
        "max_hold_ns": 0,
        "max_gap_ns": 0,
        "ended": None,
        "error": None,
    }
    try:
        found["priority_raised"] = backend.raise_priority()
    except Exception as exc:  # noqa: BLE001 - recorded; the loop goes on
        found["priority_error"] = repr(exc)
    flags = os.O_RDONLY | getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(config["lock"], flags)
    except OSError as exc:
        found["error"] = f"the lock file cannot be opened: {exc!r}"
        found["stopped_wall_ns"] = time.time_ns()
        return found
    watched: list[_Watched] = []
    try:
        for spec in config["watch"]:
            watched.append(_Watched(spec, backend))
        deadline = time.monotonic() + float(config["deadline"])
        previous: int | None = None
        while True:
            state, reason, hold = backend.lock(fd)
            _fold(found["lease"], state, reason)
            if hold is not None:
                found["max_hold_ns"] = max(found["max_hold_ns"], hold)
            for each in watched:
                each.observe(backend)
            found["probes"] += 1
            now = time.monotonic_ns()
            if previous is not None:
                found["max_gap_ns"] = max(found["max_gap_ns"], now - previous)
            previous = now
            if found["ready"] is None:
                found["ready"] = {
                    "state": state,
                    "wall_ns": found["lease"][-1]["first_wall_ns"],
                    "ns": found["lease"][-1]["first_ns"],
                    "unwatched": [each.pid for each in watched if each.handle is None],
                }
                _write(directory / READY_FILE, found["ready"])
            if (directory / STOP_FILE).exists():
                found["ended"] = "stopped"
                break
            if time.monotonic() >= deadline:
                found["ended"] = "deadline"
                break
            time.sleep(found["interval"])
    except Exception as exc:  # noqa: BLE001 - the helper's own evidence
        found["error"] = repr(exc)
    finally:
        os.close(fd)
        for each in watched:
            backend.close(each.handle)
    found["stopped_wall_ns"] = time.time_ns()
    found["watched"] = [each.record() for each in watched]
    return found


class Contender:
    """The helper as the harness owns it, from its start until it is reaped.

    Files only, all in *directory*: the configuration it reads, the ready
    answer it writes after its first round, the stop file that ends it, and
    its result. ``end`` kills and reaps it, for a row that has to retain one
    that would not stop.
    """

    def __init__(
        self,
        directory: Path,
        lock: Path,
        watch: Sequence[Mapping[str, Any]],
        *,
        interval: float,
        deadline: float,
        python: str = sys.executable,
    ) -> None:
        self.directory = directory
        self.config = {
            "directory": str(directory),
            "lock": str(lock),
            "watch": [dict(spec) for spec in watch],
            "interval": interval,
            "deadline": deadline,
        }
        self.python = python
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        config = self.directory / CONFIG_FILE
        config.write_text(json.dumps(self.config))
        with open(self.directory / STDERR_FILE, "wb") as stderr:
            self.process = subprocess.Popen(
                [self.python, "-I", "-S", os.path.abspath(__file__), str(config)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                close_fds=True,
            )

    def wait_ready(self, seconds: float) -> dict[str, Any] | None:
        """The first round's answer, once written, or None if it never is."""
        deadline = time.monotonic() + seconds
        path = self.directory / READY_FILE
        while True:
            if path.exists():
                return json.loads(path.read_text())
            if self.process is None or self.process.poll() is not None:
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)

    @property
    def reaped(self) -> bool:
        return self.process is None or self.process.returncode is not None

    def end(self, grace: float) -> bool:
        """Kill the helper if it still runs and reap it; whether it is reaped."""
        if self.process is None:
            return True
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            return False
        return True

    def stop(self, seconds: float) -> dict[str, Any]:
        """Ask the helper to stop, wait for it, and return its result. One
        that does not stop within *seconds* is killed; its record says so."""
        if self.process is None:
            return {"error": "the contender was never started"}
        (self.directory / STOP_FILE).touch()
        try:
            self.process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            reaped = self.end(5.0)
            return {
                "error": f"the contender did not stop within {seconds}s and was "
                f"killed (reaped: {reaped})"
            }
        result = self.directory / RESULT_FILE
        if not result.exists():
            stderr = (self.directory / STDERR_FILE).read_text(errors="replace")
            return {
                "error": f"the contender exited {self.process.returncode} with no "
                f"result: {stderr[-500:]}"
            }
        return json.loads(result.read_text())


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: crash_contender.py CONFIG", file=sys.stderr)
        return 2
    config = json.loads(Path(argv[1]).read_text())
    _write(Path(config["directory"]) / RESULT_FILE, contend(config))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
