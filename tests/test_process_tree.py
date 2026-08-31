"""Process containment against real descendants and deterministic race doubles."""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from collections.abc import Iterator
from itertools import chain, repeat
from types import SimpleNamespace
from typing import Any, cast

import pytest

import linkedin_mcp_server.process_gate as process_gate
import linkedin_mcp_server.process_guardian as process_guardian
from linkedin_mcp_server.process_protocol import new_nonce
import linkedin_mcp_server.process_tree as process_tree
from linkedin_mcp_server.profile_lease import ProfileLease

_REPO_ROOT = Path(__file__).resolve().parents[1]
_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
_WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="PR_SET_CHILD_SUBREAPER is a Linux facility",
)
_ZOMBIE_STATE = "Z"

#: ``PR_SET_CHILD_SUBREAPER``, from ``linux/prctl.h``. Never changed since it
#: was added in 3.4, and reading it out of a header at runtime is not something
#: a test can do portably.
_PR_SET_CHILD_SUBREAPER = 36

#: Reproduce a container's non-reaping PID 1 in one process, then time a real
#: group settlement against it. The grandchild shares the leader's group, so
#: killing the group orphans it; the subreaper below inherits it and never
#: waits, which is what keeps its unreaped entry in the group indefinitely.
_SUBREAPER_SETTLEMENT = f"""
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

from linkedin_mcp_server.process_tree import terminate_process_group

if ctypes.CDLL("libc.so.6", use_errno=True).prctl(
    {_PR_SET_CHILD_SUBREAPER}, 1, 0, 0, 0
) != 0:
    raise SystemExit("prctl(PR_SET_CHILD_SUBREAPER) was refused")

marker = Path(sys.argv[1])
leader = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import os,subprocess,sys,time;"
        "c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(600)']);"
        "Path=__import__('pathlib').Path;"
        "p=Path(sys.argv[1]);q=p.with_name(p.name+'.partial');"
        "q.write_text(f'{{os.getpid()}} {{c.pid}}');q.replace(p);"
        "time.sleep(600)",
        str(marker),
    ],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
for _ in range(1000):
    if marker.exists() and len(marker.read_text().split()) == 2:
        break
    time.sleep(0.01)
else:
    raise SystemExit("the group leader never spawned its grandchild")

grandchild = int(marker.read_text().split()[1])
pgid = os.getpgid(leader.pid)
if pgid != leader.pid or os.getpgid(grandchild) != pgid:
    raise SystemExit("the grandchild did not land in the leader's group")

started = time.monotonic()
settled = terminate_process_group(pgid, timeout=10.0, child=leader)
elapsed = time.monotonic() - started

try:
    raw = Path(f"/proc/{{grandchild}}/stat").read_text()
    state = raw[raw.rfind(")") + 2:].split()[0]
except OSError:
    state = "reaped"
print(settled, f"{{elapsed:.3f}}", state, flush=True)
"""


def _windows_alive(pid: int) -> bool:
    """Query a Windows process without signaling or terminating it."""
    win32api = importlib.import_module("win32api")
    win32con = importlib.import_module("win32con")
    win32process = importlib.import_module("win32process")
    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
    except Exception:
        return False
    try:
        return win32process.GetExitCodeProcess(handle) == win32con.STILL_ACTIVE
    finally:
        handle.Close()


def _alive(pid: int) -> bool:
    if os.name == "nt":
        return _windows_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _wait_gone(*pids: int) -> bool:
    for _ in range(500):
        if not any(_alive(pid) for pid in pids):
            return True
        time.sleep(0.01)
    return False


@pytest.mark.parametrize(("exit_code", "expected"), [(259, True), (7, False)])
def test_windows_liveness_query_does_not_signal(
    exit_code: int, expected: bool, monkeypatch: pytest.MonkeyPatch
):
    class _Handle:
        closed = False

        def Close(self) -> None:
            self.closed = True

    handle = _Handle()

    class _Api:
        @staticmethod
        def OpenProcess(access: int, inherit: bool, pid: int) -> _Handle:
            assert (access, inherit, pid) == (0x1000, False, 4242)
            return handle

    class _Constants:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259

    class _Process:
        @staticmethod
        def GetExitCodeProcess(opened: _Handle) -> int:
            assert opened is handle
            return exit_code

    modules = {
        "win32api": _Api,
        "win32con": _Constants,
        "win32process": _Process,
    }
    monkeypatch.setattr(importlib, "import_module", modules.__getitem__)

    assert _windows_alive(4242) is expected
    assert handle.closed


def test_gate_preserves_payload_streams_and_target_site_startup(tmp_path: Path):
    base_executable = getattr(sys, "_base_executable", sys.executable)
    environment = os.environ.copy()
    environment.pop("PYTHONNOUSERSITE", None)
    environment["PYTHONUSERBASE"] = str(tmp_path / "user-base")
    probe = subprocess.run(
        [base_executable, "-c", "import site; print(site.getusersitepackages())"],
        capture_output=True,
        text=True,
        env=environment,
        check=True,
    )
    user_site = Path(probe.stdout.strip())
    user_site.mkdir(parents=True)
    sentinel = tmp_path / "site-started.txt"
    (user_site / "sitecustomize.py").write_text(
        f"import os\nopen({str(sentinel)!r}, 'a').write(str(os.getpid()) + '\\n')\n"
    )
    target = [
        base_executable,
        "-c",
        "import os,sys; data=os.read(0, 128); "
        "print('stdout:' + data.decode()); "
        "print('stderr-only', file=sys.stderr)",
    ]
    nonce = process_tree.release_nonce()
    gate = subprocess.Popen(
        process_tree.windows_gate_command(target, nonce),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    payload = b"target payload"
    stdout, stderr = gate.communicate(
        f"release {nonce}\n".encode("ascii") + payload,
        timeout=30,
    )

    assert gate.returncode == 0
    # Normalized, because print() through the Windows text layer ends its line
    # with CRLF and what this asserts is that the payload reached the target.
    assert stdout.replace(b"\r\n", b"\n") == b"stdout:target payload\n"
    assert stderr.replace(b"\r\n", b"\n") == b"stderr-only\n"
    started = sentinel.read_text().splitlines()
    assert len(started) == 1
    assert int(started[0]) != gate.pid


def test_gate_uses_explicit_standard_handles(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[list[str], dict[str, object]]] = []

    class _Target:
        def wait(self) -> int:
            return 7

    monkeypatch.setattr(process_gate, "_await_release", lambda _nonce: True)
    monkeypatch.setattr(
        process_gate.subprocess,
        "Popen",
        lambda command, **kwargs: calls.append((command, kwargs)) or _Target(),
    )

    assert process_gate.main(["gate", "00" * 32, "--", "target", "arg"]) == 7
    assert calls == [
        (
            ["target", "arg"],
            {"stdin": 0, "stdout": 1, "stderr": 2, "close_fds": True},
        )
    ]


def test_gate_command_disables_site_before_the_absolute_script():
    command = process_tree.windows_gate_command(["target", "arg"], "00" * 32)

    assert command[1:4] == ["-I", "-S", "-u"]
    assert Path(command[4]).is_absolute()
    assert command[-3:] == ["--", "target", "arg"]


def _supervisor(target_script: str) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-m",
            "linkedin_mcp_server.installer_supervisor",
            "--",
            sys.executable,
            "-c",
            target_script,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name != "nt",
        cwd=_REPO_ROOT,
    )
    assert process.stdin is not None and process.stderr is not None
    nonce = new_nonce()
    process.stdin.write(f"{nonce}\n")
    process.stdin.flush()
    assert process.stderr.readline().strip() == f"armed {nonce}"
    process.stdin.write(f"start {nonce}\n")
    process.stdin.flush()
    cast(Any, process)._supervisor_nonce = nonce
    return process


def _started_pid(process: subprocess.Popen[str]) -> int:
    assert process.stderr is not None
    parts = process.stderr.readline().split()
    assert parts[:2] == ["started", cast(Any, process)._supervisor_nonce]
    assert len(parts) == 3
    return int(parts[2])


class _JobHandle:
    def __init__(self, value: int = 12345) -> None:
        self.value = value
        self.closed = False
        self.detached = False

    def Close(self) -> None:
        self.closed = True

    def Detach(self) -> int:
        self.detached = True
        return self.value


class TestWindowsJobSetup:
    def _modules(
        self,
        events: list[tuple[str, Any]],
        handle: _JobHandle,
        *,
        active: Iterator[object] | None = None,
    ) -> dict[str, object]:
        accounting = active or iter([0])

        class _Win32Api:
            @staticmethod
            def SetHandleInformation(*args: object) -> None:
                events.append(("non-inheritable", args))

            @staticmethod
            def GetCurrentProcess() -> int:
                return 67890

            @staticmethod
            def GetLastError() -> int:
                return 0

        class _Win32Con:
            HANDLE_FLAG_INHERIT = 1

        class _WinError:
            ERROR_ALREADY_EXISTS = 183

        class _Win32Job:
            JobObjectExtendedLimitInformation = 1
            JobObjectBasicAccountingInformation = 2
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 4
            JOB_OBJECT_QUERY = 8

            @staticmethod
            def CreateJobObject(*args: object) -> _JobHandle:
                events.append(("create", args))
                return handle

            @staticmethod
            def OpenJobObject(*args: object) -> _JobHandle:
                events.append(("open", args))
                return handle

            @staticmethod
            def QueryInformationJobObject(
                _handle: object, info_class: int
            ) -> dict[str, Any]:
                if info_class == _Win32Job.JobObjectExtendedLimitInformation:
                    return {"BasicLimitInformation": {"LimitFlags": 16}}
                events.append(("accounting", (_handle, info_class)))
                value = next(accounting)
                if isinstance(value, BaseException):
                    raise value
                return {"ActiveProcesses": value}

            @staticmethod
            def SetInformationJobObject(*args: object) -> None:
                events.append(("limits", args))

            @staticmethod
            def AssignProcessToJobObject(*args: object) -> None:
                events.append(("assign", args))

            @staticmethod
            def IsProcessInJob(*args: object) -> bool:
                events.append(("member", args))
                return True

            @staticmethod
            def TerminateJobObject(*args: object) -> None:
                events.append(("terminate", args))

        return {
            "win32api": _Win32Api,
            "win32con": _Win32Con,
            "win32job": _Win32Job,
            "winerror": _WinError,
        }

    def _patch_modules(
        self,
        monkeypatch: pytest.MonkeyPatch,
        modules: dict[str, object],
    ) -> None:
        # getppid alongside name: adoption records the gate it was launched
        # by, and a namespace without it reports the whole adoption as failed.
        monkeypatch.setattr(
            process_tree,
            "os",
            SimpleNamespace(name="nt", getpid=lambda: 4242, getppid=lambda: 909),
        )
        monkeypatch.setattr(
            process_tree.importlib, "import_module", modules.__getitem__
        )

    def test_job_is_non_inheritable_and_kills_on_last_close(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        handle = _JobHandle()
        modules = self._modules(events, handle)
        self._patch_modules(monkeypatch, modules)

        job = process_tree.WindowsJob.anonymous()

        non_inherit = next(event for event in events if event[0] == "non-inheritable")
        assert non_inherit[1][1:] == (1, 0)
        limits = next(event for event in events if event[0] == "limits")
        configured = cast(dict[str, Any], limits[1][2])
        assert configured["BasicLimitInformation"]["LimitFlags"] == 20
        assert not job.closed

    def test_assignment_uses_the_popen_handle_and_verifies_membership(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        handle = _JobHandle()
        modules = self._modules(events, handle)
        self._patch_modules(monkeypatch, modules)
        job = process_tree.WindowsJob.anonymous()
        child = type("Child", (), {"pid": 999, "_handle": 777})()

        job.assign_popen(cast(Any, child))

        assigned = next(event for event in events if event[0] == "assign")
        member = next(event for event in events if event[0] == "member")
        assert assigned[1] == (handle, 777)
        assert member[1] == (777, handle)
        assert 999 not in assigned[1]

    def test_asyncio_assignment_extracts_the_transport_popen(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        handle = _JobHandle()
        modules = self._modules(events, handle)
        self._patch_modules(monkeypatch, modules)
        job = process_tree.WindowsJob.anonymous()
        popen = type("Popen", (), {"_handle": 777})()

        class _Transport:
            def get_extra_info(self, name: str) -> object:
                events.append(("extra", name))
                return popen

        child = type("Child", (), {"_transport": _Transport()})()

        assert job.assign_asyncio_process(child) is popen
        assert ("extra", "subprocess") in events
        assert any(event[0] == "assign" and event[1][1] == 777 for event in events)

    def test_asyncio_assignment_fails_closed_without_the_popen(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        modules = self._modules(events, _JobHandle())
        self._patch_modules(monkeypatch, modules)
        job = process_tree.WindowsJob.anonymous()
        child = type(
            "Child",
            (),
            {
                "_transport": type(
                    "Transport", (), {"get_extra_info": lambda *_: None}
                )()
            },
        )()

        with pytest.raises(process_tree.ProcessTreeError, match="underlying Popen"):
            job.assign_asyncio_process(child)

    def test_owner_verification_closes_and_adoption_detaches_the_named_handle(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        verified = _JobHandle(1)
        adopted = _JobHandle(2)
        handles = iter([verified, adopted])
        modules = self._modules(events, verified)
        job_api = cast(Any, modules["win32job"])
        monkeypatch.setattr(job_api, "OpenJobObject", lambda *_args: next(handles))
        monkeypatch.setattr(process_tree, "_adopted_windows_job", None)
        monkeypatch.setattr(process_tree, "_adopted_windows_gate", None)
        self._patch_modules(monkeypatch, modules)

        process_tree.WindowsJob.verify_current_process("named-owner")
        process_tree.WindowsJob.adopt_current_process("named-owner")

        assert verified.closed
        assert not verified.detached
        assert adopted.detached
        assert not adopted.closed
        assert process_tree._adopted_windows_job == 2
        assert process_tree._adopted_windows_gate == 909, (
            "the gate that waits on this owner was recorded while it was alive"
        )

    def test_failed_owner_adoption_closes_the_named_handle(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        handle = _JobHandle()
        modules = self._modules(events, handle)
        job_api = cast(Any, modules["win32job"])
        monkeypatch.setattr(job_api, "IsProcessInJob", lambda *_args: False)
        monkeypatch.setattr(process_tree, "_adopted_windows_job", None)
        self._patch_modules(monkeypatch, modules)

        with pytest.raises(process_tree.ProcessTreeError, match="not a member"):
            process_tree.WindowsJob.adopt_current_process("named-owner")

        assert handle.closed
        assert not handle.detached
        assert process_tree._adopted_windows_job is None

    def test_named_job_collision_regenerates(self, monkeypatch: pytest.MonkeyPatch):
        events: list[tuple[str, Any]] = []
        handles = iter([_JobHandle(1), _JobHandle(2)])
        last_errors = iter([183, 0])
        modules = self._modules(events, _JobHandle())
        api = cast(Any, modules["win32api"])
        job_api = cast(Any, modules["win32job"])
        monkeypatch.setattr(job_api, "CreateJobObject", lambda *_args: next(handles))
        monkeypatch.setattr(api, "GetLastError", lambda: next(last_errors))
        tokens = iter(["a" * 32, "b" * 32])
        monkeypatch.setattr(
            process_tree.secrets, "token_hex", lambda _size: next(tokens)
        )
        self._patch_modules(monkeypatch, modules)

        job = process_tree.WindowsJob.named("owner")

        assert job.name == f"Local\\linkedin-mcp-owner-{'b' * 32}"

    def test_termination_and_drain_are_separate_and_bounded(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        handle = _JobHandle()
        modules = self._modules(events, handle, active=iter([3, 1, 0]))
        self._patch_modules(monkeypatch, modules)
        monkeypatch.setattr(process_tree.time, "sleep", lambda _seconds: None)
        job = process_tree.WindowsJob.anonymous()

        job.terminate()
        assert len([event for event in events if event[0] == "terminate"]) == 1
        assert not handle.closed

        job.wait_until_empty(timeout=1)

        assert handle.closed

    def test_exited_popen_handle_is_released_before_job_drain(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        modules = self._modules(events, _JobHandle())
        self._patch_modules(monkeypatch, modules)
        job = process_tree.WindowsJob.anonymous()

        class _ProcessHandle:
            closed = False

            def Close(self) -> None:
                self.closed = True

        process_handle = _ProcessHandle()
        child = cast(
            subprocess.Popen[Any],
            SimpleNamespace(returncode=7, _handle=process_handle),
        )

        job.release_popen_handle(child)

        assert process_handle.closed

    def test_live_popen_handle_cannot_be_released(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        modules = self._modules(events, _JobHandle())
        self._patch_modules(monkeypatch, modules)
        job = process_tree.WindowsJob.anonymous()
        child = cast(
            subprocess.Popen[Any],
            SimpleNamespace(returncode=None, _handle=_JobHandle()),
        )

        with pytest.raises(process_tree.ProcessTreeError, match="before process exit"):
            job.release_popen_handle(child)

    def test_query_failures_never_mean_empty_and_retain_containment(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        handle = _JobHandle()
        failures = iter([OSError("query")] * 3)
        modules = self._modules(events, handle, active=failures)
        self._patch_modules(monkeypatch, modules)
        monkeypatch.setattr(process_tree.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(process_tree, "_retained_windows_jobs", [])
        job = process_tree.WindowsJob.anonymous()

        with pytest.raises(process_tree.ProcessTreeError, match="verify"):
            job.wait_until_empty(timeout=1)

        assert not handle.closed
        assert process_tree._retained_windows_jobs == [job]

    def test_positive_active_count_hits_deadline_and_retains_containment(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        handle = _JobHandle()
        modules = self._modules(events, handle, active=repeat(1))
        self._patch_modules(monkeypatch, modules)
        monkeypatch.setattr(process_tree, "_retained_windows_jobs", [])
        job = process_tree.WindowsJob.anonymous()

        with pytest.raises(process_tree.ProcessTreeError, match="deadline"):
            job.wait_until_empty(timeout=0)

        assert not handle.closed
        assert process_tree._retained_windows_jobs == [job]

    def test_failed_termination_retries_without_polling_the_job(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        handle = _JobHandle()
        modules = self._modules(events, handle, active=iter([0]))
        job_api = cast(Any, modules["win32job"])
        attempts = 0

        def terminate(*args: object) -> None:
            nonlocal attempts
            attempts += 1
            events.append(("terminate", args))
            if attempts == 1:
                raise OSError("busy")

        monkeypatch.setattr(job_api, "TerminateJobObject", terminate)
        self._patch_modules(monkeypatch, modules)
        monkeypatch.setattr(process_tree.time, "sleep", lambda _seconds: None)
        job = process_tree.WindowsJob.anonymous()

        job.terminate()

        assert attempts == 2
        assert not [event for event in events if event[0] == "accounting"]
        assert not handle.closed

    def test_persistent_termination_failure_is_fatal_and_retains_the_handle(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        handle = _JobHandle()
        modules = self._modules(events, handle)
        job_api = cast(Any, modules["win32job"])

        def fail_termination(*_args: object) -> None:
            raise OSError("terminate")

        monkeypatch.setattr(job_api, "TerminateJobObject", fail_termination)
        self._patch_modules(monkeypatch, modules)
        monkeypatch.setattr(process_tree.time, "sleep", lambda _seconds: None)
        job = process_tree.WindowsJob.anonymous()

        with pytest.raises(process_tree.ProcessTreeError, match="terminate"):
            job.terminate()

        assert not handle.closed


class TestPosixProcessGroups:
    def test_darwin_kqueue_observes_exit_without_reaping(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        class _Kqueue:
            closed = False

            def control(
                self,
                changes: object,
                max_events: int,
                timeout: float,
            ) -> list[object]:
                return [] if changes is not None else [object()]

            def close(self) -> None:
                self.closed = True

        queue = _Kqueue()

        class _Select:
            KQ_FILTER_PROC = 1
            KQ_EV_ADD = 2
            KQ_EV_ENABLE = 4
            KQ_NOTE_EXIT = 8

            @staticmethod
            def kqueue() -> _Kqueue:
                return queue

            @staticmethod
            def kevent(*args: object, **kwargs: object) -> object:
                return object()

        monkeypatch.setattr(process_tree, "select", _Select)

        assert process_tree._darwin_child_exited_without_reaping(4242)
        assert queue.closed

    @_POSIX_ONLY
    def test_darwin_without_waitid_uses_kqueue(self, monkeypatch: pytest.MonkeyPatch):
        child = type("Child", (), {"pid": 4242, "returncode": None})()
        observed: list[int] = []
        monkeypatch.delattr(process_tree.os, "waitid", raising=False)
        monkeypatch.setattr(process_tree.sys, "platform", "darwin")
        monkeypatch.setattr(
            process_tree,
            "_darwin_child_exited_without_reaping",
            lambda pid: observed.append(pid) or True,
        )

        assert process_tree.child_exited_without_reaping(cast(Any, child))
        assert observed == [4242]

    @_POSIX_ONLY
    def test_cleanup_repeats_until_the_group_is_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        observations = iter([None, object()])
        signals: list[int] = []
        waits: list[str] = []
        wait_flags: list[int] = []

        def observe_child(*args: int) -> object | None:
            wait_flags.append(args[2])
            return next(observations)

        monkeypatch.setattr(process_tree.os, "waitid", observe_child, raising=False)
        monkeypatch.setattr(
            process_tree.os, "killpg", lambda pgid, sent: signals.append(sent)
        )
        monkeypatch.setattr(process_tree, "process_group_exists", lambda pgid: False)

        class _Child:
            pid = 12345
            returncode: int | None = None

            def poll(self) -> None:
                return None

            def wait(self, timeout: float | None = None) -> int:
                waits.append("wait")
                self.returncode = -signal.SIGKILL
                return self.returncode

        assert process_tree.terminate_process_group(
            12345, timeout=1.0, child=cast(Any, _Child())
        )
        assert signals == [signal.SIGKILL, signal.SIGKILL]
        assert waits == ["wait"]
        assert len(wait_flags) == 2
        assert all(options & os.WNOWAIT for options in wait_flags)

    @_POSIX_ONLY
    def test_zombie_descendants_are_waited_out_without_another_group_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        observations = iter([None, object()])
        remaining = iter([True, False])
        signals: list[int] = []
        monkeypatch.setattr(
            process_tree.os,
            "waitid",
            lambda *a, **k: next(observations),
            raising=False,
        )
        monkeypatch.setattr(
            process_tree.os, "killpg", lambda pgid, sent: signals.append(sent)
        )
        monkeypatch.setattr(
            process_tree, "process_group_exists", lambda pgid: next(remaining)
        )
        monkeypatch.setattr(process_tree.time, "sleep", lambda seconds: None)

        class _Child:
            pid = 12345
            returncode: int | None = None

            def wait(self, timeout: float | None = None) -> int:
                self.returncode = -signal.SIGKILL
                return self.returncode

        assert process_tree.terminate_process_group(
            12345, timeout=1.0, child=cast(Any, _Child())
        )
        assert signals == [signal.SIGKILL, signal.SIGKILL]

    @_POSIX_ONLY
    def test_reused_pgid_does_not_replace_a_valid_installer_result(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        observations = iter([None, object()])
        identities = iter(["original leader", "replacement leader"])
        monkeypatch.setattr(
            process_tree.os,
            "waitid",
            lambda *a, **k: next(observations),
            raising=False,
        )
        monkeypatch.setattr(process_tree.os, "killpg", lambda *a, **k: None)
        monkeypatch.setattr(process_tree.os, "getpgid", lambda pid: pid)
        monkeypatch.setattr(process_tree, "process_group_exists", lambda pgid: True)
        # The group holds a live process, so only the identity comparison can
        # end this wait. Pinned rather than left to the machine's real process
        # table, which decides nothing here and could answer either way.
        monkeypatch.setattr(
            process_tree, "process_group_has_live_member", lambda pgid: True
        )
        monkeypatch.setattr(
            process_tree, "_kernel_start_identity", lambda pid: next(identities)
        )

        class _Child:
            pid = 12345
            returncode: int | None = None

            def wait(self, timeout: float | None = None) -> int:
                self.returncode = 0
                return self.returncode

        assert process_tree.terminate_process_group(
            12345, timeout=1.0, child=cast(Any, _Child())
        )

    @staticmethod
    def _settle(
        monkeypatch: pytest.MonkeyPatch,
        rows: dict[int, tuple[int, int, str | None, str | None]],
        *,
        timeout: float,
    ) -> tuple[bool, float]:
        """Run one group settlement whose only exit is the kernel run state.

        ``process_group_exists`` never goes false and no identity ever changes,
        so the group's own members decide the result and nothing else can.
        """
        observations = iter([None, object()])
        monkeypatch.setattr(
            process_tree.os,
            "waitid",
            lambda *a, **k: next(observations),
            raising=False,
        )
        monkeypatch.setattr(process_tree.os, "killpg", lambda *a, **k: None)
        monkeypatch.setattr(process_tree, "process_group_exists", lambda pgid: True)
        monkeypatch.setattr(process_tree, "_kernel_start_identity", lambda pid: None)
        monkeypatch.setattr(process_tree, "_posix_process_rows", lambda: rows)

        class _Child:
            pid = 12345
            returncode: int | None = None

            def wait(self, timeout: float | None = None) -> int:
                self.returncode = -signal.SIGKILL
                return self.returncode

        started = time.monotonic()
        settled = process_tree.terminate_process_group(
            12345, timeout=timeout, child=cast(Any, _Child())
        )
        return settled, time.monotonic() - started

    @_POSIX_ONLY
    def test_a_zombie_only_group_settles_without_waiting_out_the_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The container shape: a reparented target nobody reaps.

        Measured in a plain ``python:3.13-slim`` container: SIGKILL through the
        group leaves the target as ``state=Z`` under a PID 1 that never waits,
        and ``killpg(pgid, 0)`` keeps answering that the group is alive. The
        installer supervisor turned that ten-second wait into exit status 70
        for an install that had already finished.
        """
        settled, elapsed = self._settle(
            monkeypatch,
            {
                12345: (1, 12345, "proc:100", "Z"),
                12346: (1, 12345, "proc:101", "Z"),
            },
            timeout=5.0,
        )

        assert settled
        assert elapsed < 1.0

    @_POSIX_ONLY
    @pytest.mark.parametrize("state", ["R", "S", "D", "T", "t"])
    def test_any_live_member_keeps_the_group_blocking(
        self, state: str, monkeypatch: pytest.MonkeyPatch
    ):
        """Only the zombie is discounted.

        Stopped, traced and uninterruptible members are processes that can be
        resumed and still hold everything they opened, so a group carrying one
        has not settled however many zombies stand next to it.
        """
        settled, _elapsed = self._settle(
            monkeypatch,
            {
                12345: (1, 12345, "proc:100", "Z"),
                12346: (1, 12345, "proc:101", state),
            },
            timeout=0.05,
        )

        assert not settled

    @_POSIX_ONLY
    @pytest.mark.parametrize("rows", [{}, {999: (1, 998, "proc:1", "Z")}])
    def test_a_snapshot_that_names_no_member_keeps_the_group_blocking(
        self,
        rows: dict[int, tuple[int, int, str | None, str | None]],
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Not knowing is not evidence that a group is empty.

        An unreadable process table and a snapshot taken while the group was
        mid-exit look identical from here, and neither may release a caller
        that ``killpg`` still reports a group for.
        """
        settled, _elapsed = self._settle(monkeypatch, rows, timeout=0.05)

        assert not settled

    @_POSIX_ONLY
    def test_an_unreadable_process_table_keeps_the_group_blocking(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        def refuse() -> dict[int, tuple[int, int, str | None, str | None]]:
            raise OSError("the process table could not be read")

        monkeypatch.setattr(process_tree, "_posix_process_rows", refuse)

        assert process_tree.process_group_has_live_member(12345)

    @_LINUX_ONLY
    def test_a_zombie_only_group_settles_under_a_reaper_that_never_waits(
        self, tmp_path: Path
    ):
        """The same measurement against real processes rather than a fixture.

        ``PR_SET_CHILD_SUBREAPER`` makes the helper the parent every orphan
        below it reparents to, and it never waits for one: that is what a
        container's PID 1 is, without needing a container. The helper reports
        the grandchild's kernel state after the call, so a run that never
        produced a zombie fails instead of passing for the wrong reason.
        """
        marker = tmp_path / "pids"
        helper = subprocess.run(
            [sys.executable, "-c", _SUBREAPER_SETTLEMENT, str(marker)],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert helper.returncode == 0, helper.stderr
        settled, elapsed, state = helper.stdout.split()

        assert state == _ZOMBIE_STATE
        assert settled == "True"
        assert float(elapsed) < 2.0

    @_POSIX_ONLY
    def test_darwin_zombie_only_group_is_reaped_after_eperm(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        observations = iter([None, None, object()])
        signals: list[int] = []
        monkeypatch.setattr(
            process_tree.os,
            "waitid",
            lambda *a, **k: next(observations),
            raising=False,
        )

        def signal_group(pgid: int, sent: int) -> None:
            signals.append(sent)
            if len(signals) == 2:
                raise PermissionError("zombie-only process group")

        monkeypatch.setattr(process_tree.os, "killpg", signal_group)
        monkeypatch.setattr(process_tree, "process_group_exists", lambda pgid: False)
        monkeypatch.setattr(process_tree.time, "sleep", lambda seconds: None)

        class _Child:
            pid = 12345
            returncode: int | None = None

            def wait(self, timeout: float | None = None) -> int:
                self.returncode = -signal.SIGKILL
                return self.returncode

        assert process_tree.terminate_process_group(
            12345, timeout=1.0, child=cast(Any, _Child())
        )
        assert signals == [signal.SIGKILL, signal.SIGKILL]

    @_POSIX_ONLY
    def test_a_reaped_leader_never_authorizes_a_numeric_group_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        signals: list[int] = []
        monkeypatch.setattr(
            process_tree.os,
            "waitid",
            lambda *a, **k: (_ for _ in ()).throw(ChildProcessError()),
            raising=False,
        )
        monkeypatch.setattr(
            process_tree.os, "killpg", lambda pgid, sent: signals.append(sent)
        )

        class _Child:
            pid = 12345
            returncode = 0

        assert not process_tree.terminate_process_group(
            12345, timeout=1.0, child=cast(Any, _Child())
        )
        assert signals == []

    @_POSIX_ONLY
    def test_parent_lease_eof_removes_target_and_grandchild(self):
        target = (
            "import os,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(600)']); "
            "print(os.getpid(), child.pid, flush=True); time.sleep(600)"
        )
        supervisor = _supervisor(target)
        try:
            assert supervisor.stderr is not None
            assert supervisor.stdout is not None
            assert supervisor.stdin is not None
            worker_pid = _started_pid(supervisor)
            target_pid, grandchild_pid = map(int, supervisor.stdout.readline().split())
            assert os.getpgid(target_pid) == worker_pid

            supervisor.stdin.close()
            # Status 70 means the bounded group probe still saw a dead zombie.
            # It is a failed install, and live-process absence is the property
            # the shared cache depends on.
            assert supervisor.wait(timeout=30) in {1, 70}
            assert _wait_gone(worker_pid, target_pid, grandchild_pid)
            with pytest.raises(ProcessLookupError):
                os.killpg(worker_pid, 0)
        finally:
            if supervisor.poll() is None:
                os.killpg(supervisor.pid, signal.SIGKILL)
                supervisor.wait(timeout=30)

    @_POSIX_ONLY
    def test_worker_cleans_immediately_after_supervisor_death(self):
        target = (
            "import os,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(600)']); "
            "print(os.getpid(), child.pid, flush=True); time.sleep(600)"
        )
        supervisor = _supervisor(target)
        try:
            assert supervisor.stderr is not None
            assert supervisor.stdout is not None
            assert supervisor.stdin is not None
            worker_pid = _started_pid(supervisor)
            target_pid, grandchild_pid = map(int, supervisor.stdout.readline().split())

            os.kill(supervisor.pid, signal.SIGKILL)
            assert supervisor.wait(timeout=30) == -signal.SIGKILL
            assert _wait_gone(worker_pid, target_pid, grandchild_pid)
        finally:
            for pid in (
                locals().get("worker_pid"),
                locals().get("target_pid"),
                locals().get("grandchild_pid"),
            ):
                if isinstance(pid, int) and _alive(pid):
                    os.kill(pid, signal.SIGKILL)

    @_POSIX_ONLY
    def test_worker_death_leaves_a_pinned_group_for_supervisor_cleanup(self):
        target = (
            "import os,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(600)']); "
            "print(os.getpid(), child.pid, flush=True); time.sleep(600)"
        )
        supervisor = _supervisor(target)
        try:
            assert supervisor.stderr is not None
            assert supervisor.stdout is not None
            worker_pid = _started_pid(supervisor)
            target_pid, grandchild_pid = map(int, supervisor.stdout.readline().split())

            os.kill(worker_pid, signal.SIGKILL)
            assert supervisor.wait(timeout=30) in {-signal.SIGKILL, 70}
            assert _wait_gone(worker_pid, target_pid, grandchild_pid)
        finally:
            if supervisor.poll() is None:
                os.killpg(supervisor.pid, signal.SIGKILL)
                supervisor.wait(timeout=30)
            for pid in (
                locals().get("worker_pid"),
                locals().get("target_pid"),
                locals().get("grandchild_pid"),
            ):
                if isinstance(pid, int) and _alive(pid):
                    os.kill(pid, signal.SIGKILL)

    @_POSIX_ONLY
    def test_normal_target_exit_removes_a_lingering_descendant(self):
        target = (
            "import os,subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(600)']); "
            "print(os.getpid(), child.pid, flush=True); raise SystemExit(7)"
        )
        supervisor = _supervisor(target)
        try:
            assert supervisor.stderr is not None
            assert supervisor.stdout is not None
            worker_pid = _started_pid(supervisor)
            target_pid, grandchild_pid = map(int, supervisor.stdout.readline().split())

            # Cleanup waits for the orphaned descendant's zombie to be collected,
            # then preserves the target's original status.
            assert supervisor.wait(timeout=30) == 7
            assert _wait_gone(worker_pid, target_pid, grandchild_pid)
        finally:
            if supervisor.poll() is None:
                os.killpg(supervisor.pid, signal.SIGKILL)
                supervisor.wait(timeout=30)

    @_POSIX_ONLY
    def test_hard_parent_exit_triggers_the_supervisor_lease(self):
        parent_script = r"""
import json, os, secrets, subprocess, sys
repo, target = sys.argv[1], sys.argv[2]
proc = subprocess.Popen(
    [sys.executable, "-I", "-m", "linkedin_mcp_server.installer_supervisor",
     "--", sys.executable, "-c", target],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, start_new_session=True, cwd=repo,
)
nonce = secrets.token_hex(32)
proc.stdin.write(nonce + "\n")
proc.stdin.flush()
assert proc.stderr.readline().strip() == "armed " + nonce
proc.stdin.write("start " + nonce + "\n")
proc.stdin.flush()
started, reported_nonce, worker_pid = proc.stderr.readline().split()
assert started == "started" and reported_nonce == nonce
worker_pid = int(worker_pid)
target_pid, grandchild_pid = map(int, proc.stdout.readline().split())
print(json.dumps([proc.pid, worker_pid, target_pid, grandchild_pid]), flush=True)
os._exit(0)
"""
        target = (
            "import os,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(600)']); "
            "print(os.getpid(), child.pid, flush=True); time.sleep(600)"
        )
        parent = subprocess.run(
            [sys.executable, "-c", parent_script, str(_REPO_ROOT), target],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        supervisor_pid, worker_pid, target_pid, grandchild_pid = json.loads(
            parent.stdout
        )
        try:
            assert _wait_gone(supervisor_pid, worker_pid, target_pid, grandchild_pid)
        finally:
            for pid in (supervisor_pid, worker_pid, target_pid, grandchild_pid):
                if _alive(pid):
                    os.kill(pid, signal.SIGKILL)


@_POSIX_ONLY
def test_registered_group_kill_revalidates_kernel_identity(
    monkeypatch: pytest.MonkeyPatch,
):
    killed: list[tuple[int, signal.Signals]] = []
    original = dict(process_tree._registered_posix_groups)
    process_tree._registered_posix_groups.clear()
    process_tree._registered_posix_groups.update(
        {
            123: process_tree._PosixGroupRegistration("old", {124: "old-member"}),
            456: process_tree._PosixGroupRegistration("gone", {457: "same-member"}),
            789: process_tree._PosixGroupRegistration("gone", {790: "unknown-member"}),
        }
    )
    monkeypatch.setattr(
        process_tree,
        "_kernel_start_identity",
        lambda process: (
            "new" if process == 123 else "same-member" if process == 457 else None
        ),
    )
    monkeypatch.setattr(
        process_tree,
        "_posix_process_rows",
        lambda: {457: (1, 456, "same-member", "S"), 790: (1, 789, None, "S")},
    )
    monkeypatch.setattr(process_tree, "process_group_exists", lambda group: True)
    monkeypatch.setattr(os, "killpg", lambda group, sig: killed.append((group, sig)))

    try:
        process_tree._kill_registered_process_groups()
    finally:
        process_tree._registered_posix_groups.clear()
        process_tree._registered_posix_groups.update(original)

    assert killed == [(456, signal.SIGKILL)]


@_POSIX_ONLY
def test_registered_group_uses_kernel_fallback_when_snapshot_is_empty(
    monkeypatch: pytest.MonkeyPatch,
):
    registration = process_tree._PosixGroupRegistration(
        leader_identity=None,
        members={457: "member-start"},
    )
    monkeypatch.setattr(process_tree, "process_group_exists", lambda _group: True)
    monkeypatch.setattr(os, "getpgid", lambda process: 456 if process == 457 else 0)
    monkeypatch.setattr(
        process_tree,
        "_kernel_start_identity",
        lambda process: "member-start" if process == 457 else None,
    )

    assert process_tree._registered_group_still_matches(456, registration, {})


def test_unknown_leader_identity_does_not_authenticate_a_reused_group(
    monkeypatch: pytest.MonkeyPatch,
):
    registration = process_tree._PosixGroupRegistration(
        leader_identity=None,
        members={},
    )
    monkeypatch.setattr(process_tree, "process_group_exists", lambda _group: True)
    monkeypatch.setattr(process_tree, "_kernel_start_identity", lambda _process: None)

    assert not process_tree._registered_group_still_matches(
        456,
        registration,
        {456: (1, 456, None, "S")},
    )


@_POSIX_ONLY
def test_hard_exit_rescans_markers_for_later_browser_groups(
    monkeypatch: pytest.MonkeyPatch,
):
    killed: list[tuple[int, signal.Signals]] = []
    original_groups = dict(process_tree._registered_posix_groups)
    original_markers = set(process_tree._registered_browser_markers)
    process_tree._registered_posix_groups.clear()
    process_tree._registered_browser_markers.clear()
    process_tree._registered_browser_markers.add("launch-marker")
    monkeypatch.setattr(
        process_tree,
        "_scan_marked_posix_processes",
        lambda marker: process_tree._MarkerScan((900,), True),
    )
    monkeypatch.setattr(
        process_tree,
        "_posix_process_rows",
        lambda: {900: (1, 789, "late-member", "S")},
    )
    monkeypatch.setattr(process_tree, "_kernel_start_identity", lambda process: None)
    monkeypatch.setattr(process_tree, "process_group_exists", lambda group: True)
    monkeypatch.setattr(os, "getpgrp", lambda: 100)
    monkeypatch.setattr(os, "killpg", lambda group, sig: killed.append((group, sig)))

    try:
        targeted = process_tree._kill_registered_process_groups()
        registration = process_tree._registered_posix_groups[789]
    finally:
        process_tree._registered_posix_groups.clear()
        process_tree._registered_posix_groups.update(original_groups)
        process_tree._registered_browser_markers.clear()
        process_tree._registered_browser_markers.update(original_markers)

    assert targeted == (789,)
    assert registration.members == {900: "late-member"}
    assert killed == [(789, signal.SIGKILL)]


@_POSIX_ONLY
def test_hard_exit_waits_for_targeted_groups_to_disappear(
    monkeypatch: pytest.MonkeyPatch,
):
    checks = iter([True, True, False])
    slept: list[float] = []
    monkeypatch.setattr(process_tree, "_registered_browser_markers", set())
    monkeypatch.setattr(
        process_tree,
        "_registered_posix_groups",
        {
            456: process_tree._PosixGroupRegistration(
                leader_identity=None,
                members={457: "member-start"},
            )
        },
    )
    monkeypatch.setattr(
        process_tree,
        "_posix_process_rows",
        lambda: {457: (1, 456, "member-start", "S")},
    )
    monkeypatch.setattr(
        process_tree, "process_group_exists", lambda group: next(checks)
    )
    monkeypatch.setattr(process_tree.time, "sleep", slept.append)

    process_tree._wait_for_process_groups((456,))

    assert slept == [process_tree._JOB_POLL_SECONDS, process_tree._JOB_POLL_SECONDS]


@_POSIX_ONLY
def test_hard_exit_stops_waiting_when_a_group_identity_is_reused(
    monkeypatch: pytest.MonkeyPatch,
):
    snapshots = iter(
        [
            {457: (1, 456, "member-start", "S")},
            {456: (1, 456, "reused-leader", "S")},
        ]
    )
    slept: list[float] = []
    monkeypatch.setattr(process_tree, "_registered_browser_markers", set())
    monkeypatch.setattr(
        process_tree,
        "_registered_posix_groups",
        {
            456: process_tree._PosixGroupRegistration(
                leader_identity="leader-start",
                members={457: "member-start"},
            )
        },
    )
    monkeypatch.setattr(process_tree, "_posix_process_rows", lambda: next(snapshots))
    monkeypatch.setattr(process_tree, "process_group_exists", lambda _group: True)
    monkeypatch.setattr(process_tree.time, "sleep", slept.append)

    process_tree._wait_for_process_groups((456,))

    assert slept == [process_tree._JOB_POLL_SECONDS]


@_POSIX_ONLY
def test_posix_hard_exit_drains_browser_groups_before_owner_exit(
    monkeypatch: pytest.MonkeyPatch,
):
    events: list[object] = []
    monkeypatch.setattr(os, "getpid", lambda: 100)
    monkeypatch.setattr(os, "getpgrp", lambda: 100)
    monkeypatch.setattr(
        process_tree,
        "_kill_registered_process_groups",
        lambda: events.append("kill-browser") or (456,),
    )
    monkeypatch.setattr(
        process_tree,
        "_wait_for_process_groups",
        lambda groups: events.append(("drain-browser", groups)),
    )
    monkeypatch.setattr(
        os,
        "killpg",
        lambda group, sig: events.append(("kill-owner", group, sig)),
    )
    monkeypatch.setattr(os, "_exit", lambda status: events.append(("exit", status)))

    process_tree.hard_exit_process_tree(7)

    assert events == [
        "kill-browser",
        ("drain-browser", (456,)),
        ("kill-owner", 100, signal.SIGKILL),
        ("exit", 7),
    ]


@_POSIX_ONLY
def test_reused_browser_group_replaces_the_old_registration(
    monkeypatch: pytest.MonkeyPatch,
):
    original = dict(process_tree._registered_posix_groups)
    process_tree._registered_posix_groups.clear()
    process_tree._registered_posix_groups[456] = process_tree._PosixGroupRegistration(
        "old", {457: "old-member"}
    )
    monkeypatch.setattr(process_tree, "_posix_process_rows", lambda: {})
    monkeypatch.setattr(
        process_tree,
        "_posix_detached_descendants",
        lambda *_args: ((458, 456, "new-member"),),
    )
    monkeypatch.setattr(
        process_tree, "_kernel_start_identity", lambda process: "new-leader"
    )

    try:
        process_tree.remember_detached_process_groups()
        registration = process_tree._registered_posix_groups[456]
        assert registration.leader_identity == "new-leader"
        assert registration.members == {458: "new-member"}
    finally:
        process_tree._registered_posix_groups.clear()
        process_tree._registered_posix_groups.update(original)


def test_confirmed_browser_close_forgets_its_marker_registration(
    monkeypatch: pytest.MonkeyPatch,
):
    marker = "launch-marker"
    shared = "shared-marker"
    monkeypatch.setattr(process_tree, "_registered_browser_markers", {marker, shared})
    monkeypatch.setattr(
        process_tree,
        "_registered_posix_groups",
        {
            456: process_tree._PosixGroupRegistration(
                leader_identity="leader",
                members={457: "member"},
                markers={marker},
            ),
            789: process_tree._PosixGroupRegistration(
                leader_identity="leader",
                members={790: "member"},
                markers={marker, shared},
            ),
        },
    )

    process_tree.forget_browser_process_marker(marker)

    assert process_tree._registered_browser_markers == {shared}
    assert 456 not in process_tree._registered_posix_groups
    assert process_tree._registered_posix_groups[789].markers == {shared}


@_POSIX_ONLY
class TestOneLaunchesResidualBrowser:
    """What a single browser close may kill, and what it must prove.

    Patchright's graceful close waits for the leader it spawned and for its
    temporary directories, and signals the detached group only when that attempt
    fails (1.61.2, ``packages/utils/processLauncher.ts``). So a close that
    returns cleanly is not evidence, and the drain that supplies it runs while
    the owner keeps living -- which is what makes its aim, rather than its
    reach, the thing worth testing.
    """

    @staticmethod
    def _registry(monkeypatch: pytest.MonkeyPatch, groups: dict) -> None:
        monkeypatch.setattr(process_tree, "_registered_browser_markers", {"browser"})
        monkeypatch.setattr(process_tree, "_registered_posix_groups", groups)
        monkeypatch.setattr(
            process_tree,
            "_scan_marked_posix_processes",
            lambda _m: process_tree._MarkerScan((), True),
        )
        monkeypatch.setattr(process_tree, "_kernel_start_identity", lambda _p: None)
        monkeypatch.setattr(os, "getpgrp", lambda: 100)

    def test_it_spares_a_group_only_ancestry_tied_to_this_launch(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The installer supervisor is detached too, and nobody asked it to stop."""
        killed: list[tuple[int, signal.Signals]] = []
        alive = iter([True, False])
        self._registry(
            monkeypatch,
            {
                456: process_tree._PosixGroupRegistration(
                    leader_identity=None,
                    members={457: "browser-member"},
                    markers={"browser"},
                    proved_markers={"browser"},
                ),
                789: process_tree._PosixGroupRegistration(
                    leader_identity=None,
                    members={790: "installer-member"},
                    markers={"browser"},
                ),
            },
        )
        monkeypatch.setattr(
            process_tree,
            "_posix_process_rows",
            lambda: {
                457: (1, 456, "browser-member", "S"),
                790: (1, 789, "installer-member", "S"),
            },
        )
        monkeypatch.setattr(
            process_tree, "process_group_exists", lambda _group: next(alive)
        )
        monkeypatch.setattr(
            os, "killpg", lambda group, sig: killed.append((group, sig))
        )

        assert process_tree.drain_browser_process_marker("browser") is True
        assert killed == [(456, signal.SIGKILL)]
        assert 789 in process_tree._registered_posix_groups

    def test_it_never_signals_the_owners_own_group(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A registration can only reach the owner's group by mistake, and once is enough."""
        killed: list[int] = []
        self._registry(
            monkeypatch,
            {
                100: process_tree._PosixGroupRegistration(
                    leader_identity=None,
                    members={101: "member"},
                    markers={"browser"},
                    proved_markers={"browser"},
                )
            },
        )
        monkeypatch.setattr(
            process_tree, "_posix_process_rows", lambda: {101: (1, 100, "member", "S")}
        )
        monkeypatch.setattr(process_tree, "process_group_exists", lambda _group: True)
        monkeypatch.setattr(os, "killpg", lambda group, _sig: killed.append(group))

        assert process_tree.drain_browser_process_marker("browser") is True
        assert killed == []

    def test_a_group_that_will_not_die_is_reported_rather_than_waited_out(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        killed: list[int] = []
        self._registry(
            monkeypatch,
            {
                456: process_tree._PosixGroupRegistration(
                    leader_identity=None,
                    members={457: "browser-member"},
                    markers={"browser"},
                    proved_markers={"browser"},
                )
            },
        )
        monkeypatch.setattr(
            process_tree,
            "_posix_process_rows",
            lambda: {457: (1, 456, "browser-member", "S")},
        )
        monkeypatch.setattr(process_tree, "process_group_exists", lambda _group: True)
        monkeypatch.setattr(os, "killpg", lambda group, _sig: killed.append(group))
        monkeypatch.setattr(process_tree.time, "sleep", lambda _seconds: None)

        assert (
            process_tree.drain_browser_process_marker("browser", timeout=0.0) is False
        )
        assert killed == [456]

    def test_a_marked_process_no_group_kill_can_reach_stays_unproven(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The kill works on groups; the proof is that nothing carries the marker."""
        self._registry(monkeypatch, {})
        monkeypatch.setattr(
            process_tree,
            "_scan_marked_posix_processes",
            lambda _m: process_tree._MarkerScan((900,), True),
        )
        monkeypatch.setattr(
            process_tree,
            "_posix_process_rows",
            lambda: {900: (1, 100, "owner-group", "S")},
        )
        monkeypatch.setattr(process_tree.time, "sleep", lambda _seconds: None)

        assert (
            process_tree.drain_browser_process_marker("browser", timeout=0.0) is False
        )

    def test_a_launch_that_registered_nothing_needs_no_scan(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(process_tree, "_registered_browser_markers", set())
        monkeypatch.setattr(
            process_tree,
            "_scan_marked_posix_processes",
            lambda _m: pytest.fail("scanned for a marker no browser was given"),
        )

        assert process_tree.drain_browser_process_marker("browser") is True

    def test_a_scan_that_could_not_look_never_ends_the_drain(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A broken ``ps`` reports an empty machine, and that is not a drain.

        The registry is empty here, so nothing is killed and nothing is waited
        for: the only thing standing between this and a confirmed shutdown is
        whether the scan that found nothing was able to look.
        """
        self._registry(monkeypatch, {})
        monkeypatch.setattr(
            process_tree,
            "_scan_marked_posix_processes",
            lambda _m: process_tree._MarkerScan((), False),
        )
        monkeypatch.setattr(process_tree.time, "sleep", lambda _seconds: None)

        assert (
            process_tree.drain_browser_process_marker("browser", timeout=0.0) is False
        )

    def test_a_scan_that_looked_and_found_nothing_ends_the_drain(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The same empty result, from a scan that actually ran."""
        self._registry(monkeypatch, {})
        monkeypatch.setattr(process_tree.time, "sleep", lambda _seconds: None)

        assert process_tree.drain_browser_process_marker("browser", timeout=0.0) is True


@_POSIX_ONLY
class TestTheMarkerScanSeparatesEmptyFromUnanswerable:
    """Outside Linux the scan shells out, and a shell-out has a third outcome.

    ``ps`` can be absent, hang or exit non-zero, and each of those produces the
    same no rows as a browser that has gone. Only one of the four is proof, so
    the scan reports whether it managed to look rather than leaving the caller
    to read it out of an empty tuple.
    """

    @staticmethod
    def _bsd(monkeypatch: pytest.MonkeyPatch) -> None:
        """Take the ``ps`` branch, which is the only one that can fail to answer."""
        monkeypatch.setattr(process_tree.sys, "platform", "darwin")

    def test_a_missing_ps_answers_nothing(self, monkeypatch: pytest.MonkeyPatch):
        self._bsd(monkeypatch)
        monkeypatch.setattr(process_tree.Path, "is_file", lambda _self: False)
        monkeypatch.setattr(
            process_tree.subprocess,
            "run",
            lambda *_args, **_kwargs: pytest.fail("ran a ps that is not installed"),
        )

        assert process_tree._scan_marked_posix_processes("marker") == (
            process_tree._MarkerScan((), False)
        )

    @pytest.mark.parametrize(
        "failure",
        [
            subprocess.TimeoutExpired(cmd="ps", timeout=1.0),
            subprocess.CalledProcessError(1, "ps"),
            OSError("could not execute ps"),
        ],
        ids=["timeout", "non-zero", "oserror"],
    )
    def test_a_ps_that_fails_answers_nothing(
        self, monkeypatch: pytest.MonkeyPatch, failure: Exception
    ):
        self._bsd(monkeypatch)
        monkeypatch.setattr(process_tree.Path, "is_file", lambda _self: True)

        def run(*_args: Any, **_kwargs: Any) -> Any:
            raise failure

        monkeypatch.setattr(process_tree.subprocess, "run", run)

        assert process_tree._scan_marked_posix_processes("marker") == (
            process_tree._MarkerScan((), False)
        )

    def test_a_ps_that_ran_and_saw_nothing_is_proof(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        self._bsd(monkeypatch)
        monkeypatch.setattr(process_tree.Path, "is_file", lambda _self: True)
        monkeypatch.setattr(
            process_tree.subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(stdout=b""),
        )

        assert process_tree._scan_marked_posix_processes("marker") == (
            process_tree._MarkerScan((), True)
        )

    def test_a_ps_that_ran_still_names_the_marked_processes(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        self._bsd(monkeypatch)
        monkeypatch.setattr(process_tree.Path, "is_file", lambda _self: True)
        rows = (
            b"  901 /chromium --headless\n"
            b"  902 /chromium LINKEDIN_MCP_BROWSER_PROCESS_MARKER=marker --type=gpu\n"
        )
        monkeypatch.setattr(
            process_tree.subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(stdout=rows),
        )

        assert process_tree._scan_marked_posix_processes("marker") == (
            process_tree._MarkerScan((902,), True)
        )

    @pytest.mark.parametrize("mode", ["missing", "failing"])
    def test_an_unusable_ps_is_reported_once_rather_than_once_per_poll(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, mode
    ):
        """The scan is the loop body, so its diagnosis may not be loud.

        A drain polls every ``_JOB_POLL_SECONDS`` against a ten second budget
        and scans twice a round, once to refresh the registry and once for the
        proof. Warning on each of those buries the single line that says what to
        do under about a thousand copies of the line that says it again. The
        drain says it once, at its deadline, and that is the line the operator
        needs.

        The clock is driven here rather than slept through, so the round count
        is the assertion rather than a timing accident.
        """
        self._bsd(monkeypatch)
        monkeypatch.setattr(
            process_tree.Path, "is_file", lambda _self: mode != "missing"
        )
        if mode == "failing":

            def run(*_args: Any, **_kwargs: Any) -> Any:
                raise subprocess.TimeoutExpired(cmd="ps", timeout=1.0)

            monkeypatch.setattr(process_tree.subprocess, "run", run)

        monkeypatch.setattr(process_tree, "_registered_browser_markers", {"browser"})
        monkeypatch.setattr(process_tree, "_registered_posix_groups", {})
        monkeypatch.setattr(process_tree, "_posix_process_rows", dict)
        monkeypatch.setattr(os, "getpgrp", lambda: 100)
        # A shim rather than a patch on the real module: ``process_tree.time``
        # *is* ``time``, so replacing ``monotonic`` there hands a scripted clock
        # to pytest and asyncio as well and the run never ends.
        #
        # One reading sets the deadline, then one per round decides whether to
        # poll again. Three rounds of polling, then a clock past the deadline.
        clock = chain([0.0] * 4, repeat(100.0))
        monkeypatch.setattr(
            process_tree,
            "time",
            SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _seconds: None),
        )

        # Counted around the real scan rather than around ``ps``, so a missing
        # executable and a broken one are measured the same way.
        scans = 0
        real_scan = process_tree._scan_marked_posix_processes

        def counting(marker: str) -> process_tree._MarkerScan:
            nonlocal scans
            scans += 1
            return real_scan(marker)

        monkeypatch.setattr(process_tree, "_scan_marked_posix_processes", counting)

        with caplog.at_level(logging.DEBUG, logger=process_tree.__name__):
            assert (
                process_tree.drain_browser_process_marker("browser", timeout=1.0)
                is False
            )

        assert scans > 2, "the loop did not poll, so quietness proves nothing"
        assert any(record.levelno == logging.DEBUG for record in caplog.records), (
            "the scan diagnosed nothing at all, so the demotion lost it"
        )
        loud = [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ]
        assert len(loud) == 1, loud
        assert "unproven" in loud[0]

    def test_the_real_scan_answers_for_a_marker_nobody_carries(self):
        """Against the platform's own process table rather than a double.

        Linux reads ``/proc`` and cannot be inconclusive; everywhere else this
        is the one assertion that ``ps`` is where the code looks for it and
        speaks the flags it is given.
        """
        assert process_tree._scan_marked_posix_processes(secrets.token_hex(32)) == (
            process_tree._MarkerScan((), True)
        )


@_POSIX_ONLY
def test_marker_drain_buries_a_group_whose_leader_already_exited():
    """The leader Patchright watched can go while its group does not.

    Two launches run at once here, because the drain has to aim: the second one
    stands in for every browser, installer and guardian that is not the one
    being closed, and it is still running afterwards.
    """
    surviving = secrets.token_hex(32)
    closing = secrets.token_hex(32)
    leader_code = (
        "import subprocess, sys\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(600)'],\n"
        "    stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "print(child.pid, flush=True)\n"
    )

    def launch(marker: str) -> tuple[int, int]:
        environment = dict(os.environ)
        environment[process_tree._BROWSER_PROCESS_MARKER] = marker
        leader = subprocess.Popen(
            [sys.executable, "-c", leader_code],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=environment,
        )
        assert leader.stdout is not None
        child_pid = int(leader.stdout.readline())
        # The leader exits at once, exactly as it does once Patchright has
        # closed the browser it spawned; its group outlives it.
        assert leader.wait(timeout=30) == 0
        return leader.pid, child_pid

    closing_group, closing_child = launch(closing)
    surviving_group, surviving_child = launch(surviving)
    try:
        process_tree.remember_detached_process_groups(closing)
        process_tree.remember_detached_process_groups(surviving)
        registration = process_tree._registered_posix_groups[closing_group]
        assert registration.proved_markers == {closing}

        assert process_tree.drain_browser_process_marker(closing) is True
        assert _wait_gone(closing_child)
        assert _alive(surviving_child), "the drain reached another launch"
        assert surviving in process_tree._registered_browser_markers
        assert surviving in (
            process_tree._registered_posix_groups[surviving_group].markers
        )
    finally:
        for marker in (closing, surviving):
            process_tree._registered_browser_markers.discard(marker)
        for group in (closing_group, surviving_group):
            process_tree._registered_posix_groups.pop(group, None)
        for pid in (closing_child, surviving_child):
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
        assert _wait_gone(closing_child, surviving_child)


def _a_buried_browser_job() -> Any:
    """A per-launch Job that already proved itself empty and let its handle go."""
    return cast(Any, SimpleNamespace(closed=True, drained=True))


def test_windows_marker_drain_spares_the_owner_and_its_other_jobs(
    monkeypatch: pytest.MonkeyPatch,
):
    """The owner-Job sweep that runs after this launch's own Job was buried.

    Windows has no marker to read, so a Job is the whole of the attribution.
    This launch's Job is drained and closed first, which is what leaves its
    escapees -- anything that Job never held -- in scope here. The exclusions
    are then the substance: this owner, and the members of a Job it still holds
    for something else, which is where the installer supervisor and its worker
    sit and where any concurrent browser launch now sits too.
    """
    current = os.getpid()
    queries = iter([(current, 700, 800), (current, 800)])
    terminated: list[int] = []

    class ProcessHandle:
        def __init__(self, process: int) -> None:
            self.process = process

        def Close(self) -> None:
            pass

    class Api:
        @staticmethod
        def OpenProcess(access: int, inherit: bool, process: int) -> ProcessHandle:
            return ProcessHandle(process)

        @staticmethod
        def TerminateProcess(handle: ProcessHandle, status: int) -> None:
            terminated.append(handle.process)

    class Con:
        PROCESS_TERMINATE = 1
        PROCESS_QUERY_LIMITED_INFORMATION = 2

    class Job:
        JobObjectBasicProcessIdList = 3

        @staticmethod
        def QueryInformationJobObject(handle: int, information: int) -> tuple[int, ...]:
            assert handle == 123
            return next(queries)

        @staticmethod
        def IsProcessInJob(handle: ProcessHandle, job: Any) -> bool:
            if job == "installer-job":
                return handle.process == 800
            return True

    monkeypatch.setattr(process_tree, "_IS_WINDOWS", True)
    monkeypatch.setattr(process_tree, "_adopted_windows_job", 123)
    monkeypatch.setattr(
        process_tree,
        "_live_windows_jobs",
        [SimpleNamespace(job_handle="installer-job")],
    )
    monkeypatch.setattr(
        process_tree, "_windows_modules", lambda: (Api(), Con(), Job(), object())
    )
    monkeypatch.setattr(process_tree.time, "sleep", lambda _seconds: None)

    assert (
        process_tree.drain_browser_process_marker(
            "browser", containment=_a_buried_browser_job()
        )
        is True
    )
    assert terminated == [700]


def test_windows_marker_drain_reports_a_member_that_stays(
    monkeypatch: pytest.MonkeyPatch,
):
    current = os.getpid()
    terminated: list[int] = []

    class ProcessHandle:
        def __init__(self, process: int) -> None:
            self.process = process

        def Close(self) -> None:
            pass

    class Api:
        @staticmethod
        def OpenProcess(access: int, inherit: bool, process: int) -> ProcessHandle:
            return ProcessHandle(process)

        @staticmethod
        def TerminateProcess(handle: ProcessHandle, status: int) -> None:
            terminated.append(handle.process)

    class Con:
        PROCESS_TERMINATE = 1
        PROCESS_QUERY_LIMITED_INFORMATION = 2

    class Job:
        JobObjectBasicProcessIdList = 3

        @staticmethod
        def QueryInformationJobObject(handle: int, information: int) -> tuple[int, ...]:
            return (current, 700)

        @staticmethod
        def IsProcessInJob(handle: ProcessHandle, job: Any) -> bool:
            return True

    monkeypatch.setattr(process_tree, "_IS_WINDOWS", True)
    monkeypatch.setattr(process_tree, "_adopted_windows_job", 123)
    monkeypatch.setattr(process_tree, "_live_windows_jobs", [])
    monkeypatch.setattr(
        process_tree, "_windows_modules", lambda: (Api(), Con(), Job(), object())
    )
    monkeypatch.setattr(process_tree.time, "sleep", lambda _seconds: None)

    assert (
        process_tree.drain_browser_process_marker(
            "browser", timeout=0.0, containment=_a_buried_browser_job()
        )
        is False
    )
    assert terminated == [700]


def test_linux_detached_discovery_does_not_need_ps(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(process_tree.sys, "platform", "linux")
    monkeypatch.setattr(
        process_tree,
        "_linux_process_rows",
        lambda: {
            10: (1, 10, "proc:owner", "S"),
            20: (10, 20, "proc:browser", "S"),
        },
    )
    monkeypatch.setattr(
        process_tree,
        "_ps_process_rows",
        lambda: pytest.fail("Linux hard exit called ps"),
    )

    assert process_tree._posix_detached_descendants(10, 10) == (
        (20, 20, "proc:browser"),
    )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux procfs")
def test_linux_snapshot_needs_no_ps_binary():
    rows = process_tree._linux_process_rows()

    assert rows[os.getpid()] == (
        os.getppid(),
        os.getpgrp(),
        process_tree._kernel_start_identity(os.getpid()),
        "R",
    )


@_POSIX_ONLY
def test_daemon_hard_exit_terminates_a_detached_descendant(tmp_path: Path):
    marker = tmp_path / "daemon-descendant.txt"
    script = r"""
import subprocess
import sys
from pathlib import Path

from linkedin_mcp_server import daemon_owner, process_tree

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(600)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
process_tree.remember_detached_process_groups()
# Through a rename, because the test waits for this path to exist and then
# reads it as an integer. A plain write creates the file before its bytes
# land, and a reader winning that gap reads an empty file or a truncated
# pid, which is still a valid integer naming some unrelated process.
marker = Path(sys.argv[1])
partial = marker.with_name(marker.name + ".partial")
partial.write_text(str(child.pid))
partial.replace(marker)
daemon_owner._exit_hard(None)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(marker)],
        cwd=_REPO_ROOT,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(500):
            if marker.exists():
                break
            if process.poll() is not None:
                stderr = process.stderr.read() if process.stderr is not None else ""
                pytest.fail(
                    "the daemon exited before creating its descendant: "
                    f"stderr={stderr!r}"
                )
            time.sleep(0.01)
        else:
            pytest.fail("the daemon did not create its descendant")

        descendant_pid = int(marker.read_text())
        assert process.wait(timeout=30) == -signal.SIGKILL
        assert _wait_gone(descendant_pid)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=30)
        descendant_pid = locals().get("descendant_pid")
        if isinstance(descendant_pid, int) and _alive(descendant_pid):
            os.kill(descendant_pid, signal.SIGKILL)


@_POSIX_ONLY
def test_crash_guardian_kills_the_owner_group_before_browser_drain(
    monkeypatch: pytest.MonkeyPatch,
):
    events: list[object] = []
    monkeypatch.setattr(process_guardian.sys, "argv", ["guardian", "10", "11", "456"])
    monkeypatch.setattr(
        process_guardian.os, "fdopen", lambda *_args, **_kwargs: io.BytesIO()
    )
    monkeypatch.setattr(process_guardian.os, "write", lambda *_args: 6)
    monkeypatch.setattr(process_guardian.os, "close", lambda _fd: None)
    monkeypatch.setattr(process_guardian.os, "getpgrp", lambda: 789)
    monkeypatch.setattr(
        process_guardian.os,
        "killpg",
        lambda group, sent: events.append(("kill", group, sent)),
    )
    monkeypatch.setattr(
        process_guardian,
        "_drain",
        lambda markers: events.append(("drain", markers)),
    )

    assert process_guardian.main() == 0
    assert events == [
        ("kill", 456, signal.SIGKILL),
        ("drain", set()),
    ]


@_POSIX_ONLY
def test_crash_guardian_requires_a_quiet_interval_after_an_empty_scan(
    monkeypatch: pytest.MonkeyPatch,
):
    snapshots = iter([set(), {456}, set()])
    now = [0.0]
    killed: list[int] = []

    def marked(_markers: set[str]) -> set[int]:
        return next(snapshots, set())

    def sleep(_seconds: float) -> None:
        now[0] += 0.5

    monkeypatch.setattr(process_guardian, "_marked_groups", marked)
    monkeypatch.setattr(process_guardian.os, "getpgrp", lambda: 100)
    monkeypatch.setattr(
        process_guardian.os,
        "killpg",
        lambda group, _signal: killed.append(group),
    )
    monkeypatch.setattr(process_guardian.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(process_guardian.time, "sleep", sleep)

    process_guardian._drain({"marker"})

    assert killed == [456]


@_POSIX_ONLY
def test_crash_guardian_holds_profile_until_detached_browser_is_gone(
    tmp_path: Path,
):
    auth_root = tmp_path / "auth"
    script = r"""
import os
import subprocess
import sys
import time
from pathlib import Path

from linkedin_mcp_server.process_tree import (
    new_browser_process_marker,
    remember_detached_process_groups,
    start_browser_guardian,
)
from linkedin_mcp_server.profile_lease import ProfileLease

auth_root = Path(sys.argv[1])
auth_root.mkdir(parents=True)
lease = ProfileLease(auth_root)
assert lease.try_acquire()
start_browser_guardian(lease.guardian_fd())
marker, marker_environment = new_browser_process_marker()
environment = dict(os.environ)
environment.update(marker_environment)
browser = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(600)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
    env=environment,
)
remember_detached_process_groups(marker)
print(os.getpid(), browser.pid, flush=True)
time.sleep(600)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(auth_root)],
        cwd=_REPO_ROOT,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    contender = None
    browser_pid: int | None = None
    try:
        assert process.stdout is not None
        reported_owner, browser_pid = map(int, process.stdout.readline().split())
        assert reported_owner == process.pid
        os.kill(process.pid, signal.SIGKILL)
        assert process.wait(timeout=30) == -signal.SIGKILL

        contender = ProfileLease(auth_root)
        acquired = False
        for _ in range(500):
            if contender.try_acquire():
                acquired = True
                assert not _alive(browser_pid)
                break
            time.sleep(0.01)
        assert acquired
        assert _wait_gone(browser_pid)
    finally:
        if contender is not None:
            contender.release()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=30)
        if browser_pid is not None and _alive(browser_pid):
            os.killpg(browser_pid, signal.SIGKILL)


@_POSIX_ONLY
def test_crash_guardian_stops_a_driver_before_its_delayed_browser_launch(
    tmp_path: Path,
):
    auth_root = tmp_path / "auth"
    launched = tmp_path / "browser-pid"
    driver_code = r"""
import os
import subprocess
import sys
import time
from pathlib import Path

time.sleep(0.25)
browser = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(600)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
    env=dict(os.environ),
)
marker = Path(sys.argv[1])
partial = marker.with_name(marker.name + ".partial")
partial.write_text(str(browser.pid))
partial.replace(marker)
time.sleep(600)
"""
    owner_code = r"""
import os
import subprocess
import sys
import time
from pathlib import Path

from linkedin_mcp_server.process_tree import (
    new_browser_process_marker,
    start_browser_guardian,
)
from linkedin_mcp_server.profile_lease import ProfileLease

auth_root = Path(sys.argv[1])
auth_root.mkdir(parents=True)
lease = ProfileLease(auth_root)
assert lease.try_acquire()
start_browser_guardian(lease.guardian_fd())
_marker, marker_environment = new_browser_process_marker()
environment = dict(os.environ)
environment.update(marker_environment)
driver = subprocess.Popen(
    [sys.executable, "-c", sys.argv[3], sys.argv[2]],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    env=environment,
)
print(os.getpid(), driver.pid, flush=True)
time.sleep(600)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", owner_code, str(auth_root), str(launched), driver_code],
        cwd=_REPO_ROOT,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    contender = None
    driver_pid: int | None = None
    try:
        assert process.stdout is not None
        reported_owner, driver_pid = map(int, process.stdout.readline().split())
        assert reported_owner == process.pid
        os.kill(process.pid, signal.SIGKILL)
        assert process.wait(timeout=30) == -signal.SIGKILL

        contender = ProfileLease(auth_root)
        for _ in range(500):
            if contender.try_acquire():
                break
            time.sleep(0.01)
        else:
            pytest.fail("the crash guardian did not release the profile")
        assert _wait_gone(driver_pid)
        assert not launched.exists()
    finally:
        if contender is not None:
            contender.release()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=30)
        if driver_pid is not None and _alive(driver_pid):
            os.kill(driver_pid, signal.SIGKILL)
        if launched.exists():
            browser_pid = int(launched.read_text())
            if _alive(browser_pid):
                os.killpg(browser_pid, signal.SIGKILL)


@_POSIX_ONLY
def test_process_marker_recovers_a_browser_after_driver_reparenting():
    marker = secrets.token_hex(32)
    driver_code = r"""
import os
import subprocess
import sys

environment = dict(os.environ)
environment[sys.argv[1]] = sys.argv[2]
browser = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(600)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
    env=environment,
)
print(browser.pid, flush=True)
"""
    driver = subprocess.Popen(
        [
            sys.executable,
            "-c",
            driver_code,
            process_tree._BROWSER_PROCESS_MARKER,
            marker,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert driver.stdout is not None
    browser_pid = int(driver.stdout.readline())
    driver.wait(timeout=30)
    try:
        process_tree.remember_detached_process_groups(marker)
        registration = process_tree._registered_posix_groups[browser_pid]
        assert marker in process_tree._registered_browser_markers
        assert registration.members[browser_pid] == process_tree._kernel_start_identity(
            browser_pid
        )
    finally:
        process_tree._registered_browser_markers.discard(marker)
        process_tree._registered_posix_groups.pop(browser_pid, None)
        if _alive(browser_pid):
            os.killpg(browser_pid, signal.SIGKILL)
        assert _wait_gone(browser_pid)


@_POSIX_ONLY
def test_registered_browser_group_survives_driver_reparenting(tmp_path: Path):
    marker = tmp_path / "browser-pid.txt"
    release = tmp_path / "release-driver"
    script = r"""
import subprocess
import sys
import time
from pathlib import Path

from linkedin_mcp_server import daemon_owner, process_tree

driver_code = r'''
import subprocess
import sys
import time
from pathlib import Path

browser = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(600)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
marker = Path(sys.argv[1])
partial = marker.with_name(marker.name + ".partial")
partial.write_text(str(browser.pid))
partial.replace(marker)
while not Path(sys.argv[2]).exists():
    time.sleep(0.01)
'''
driver = subprocess.Popen([sys.executable, "-c", driver_code, sys.argv[1], sys.argv[2]])
marker = Path(sys.argv[1])
while not marker.exists():
    time.sleep(0.01)
process_tree.remember_detached_process_groups()
Path(sys.argv[2]).touch()
driver.wait(timeout=30)
time.sleep(0.05)
daemon_owner._exit_hard(None)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(marker), str(release)],
        cwd=_REPO_ROOT,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(500):
            if marker.exists():
                break
            if process.poll() is not None:
                stderr = process.stderr.read() if process.stderr is not None else ""
                pytest.fail(f"the driver exited before browser launch: {stderr!r}")
            time.sleep(0.01)
        else:
            pytest.fail("the driver did not report its browser")

        browser_pid = int(marker.read_text())
        assert process.wait(timeout=30) == -signal.SIGKILL
        assert _wait_gone(browser_pid)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=30)
        browser_pid = locals().get("browser_pid")
        if isinstance(browser_pid, int) and _alive(browser_pid):
            os.kill(browser_pid, signal.SIGKILL)


def test_adopted_windows_job_drains_every_other_process(
    monkeypatch: pytest.MonkeyPatch,
):
    current = os.getpid()
    queries = iter([(current, 700), (current,)])
    terminated: list[int] = []
    closed: list[int] = []

    class ProcessHandle:
        def __init__(self, process: int) -> None:
            self.process = process

        def Close(self) -> None:
            closed.append(self.process)

    class Api:
        @staticmethod
        def OpenProcess(access: int, inherit: bool, process: int) -> ProcessHandle:
            assert access == 3
            assert inherit is False
            return ProcessHandle(process)

        @staticmethod
        def TerminateProcess(handle: ProcessHandle, status: int) -> None:
            assert status == 1
            terminated.append(handle.process)

    class Con:
        PROCESS_TERMINATE = 1
        PROCESS_QUERY_LIMITED_INFORMATION = 2

    class Job:
        JobObjectBasicProcessIdList = 3

        @staticmethod
        def QueryInformationJobObject(handle: int, information: int) -> tuple[int, ...]:
            assert handle == 123
            assert information == Job.JobObjectBasicProcessIdList
            return next(queries)

        @staticmethod
        def IsProcessInJob(handle: ProcessHandle, job: int) -> bool:
            assert job == 123
            return handle.process == 700

    original = process_tree._adopted_windows_job
    process_tree._adopted_windows_job = 123
    monkeypatch.setattr(
        process_tree, "_windows_modules", lambda: (Api(), Con(), Job(), object())
    )
    monkeypatch.setattr(process_tree.time, "sleep", lambda _seconds: None)
    try:
        process_tree._drain_adopted_windows_job()
    finally:
        process_tree._adopted_windows_job = original

    assert terminated == [700]
    assert closed == [700]


def test_adopted_windows_job_spares_the_gate_that_waits_on_it(
    monkeypatch: pytest.MonkeyPatch,
):
    """The gate is in the same Job and relays this owner's exit status.

    It spawns the owner and waits, so terminating it hands the frontend the
    termination code instead of whatever the owner exited with, and it does
    that while saying nothing about the browser the drain is aimed at.
    """
    current = os.getpid()
    gate = current + 1
    # A third answer so the mutation that drops the exclusion ends rather than
    # spinning: it terminates the gate, sees it once more, and stops on the
    # empty round, leaving the gate in what this asserts.
    queries = iter([(current, gate, 700), (current, gate), (current,)])
    terminated: list[int] = []

    class ProcessHandle:
        def __init__(self, process: int) -> None:
            self.process = process

        def Close(self) -> None:
            return None

    class Api:
        @staticmethod
        def OpenProcess(_access: int, _inherit: bool, process: int) -> ProcessHandle:
            return ProcessHandle(process)

        @staticmethod
        def TerminateProcess(handle: ProcessHandle, _status: int) -> None:
            terminated.append(handle.process)

    class Con:
        PROCESS_TERMINATE = 1
        PROCESS_QUERY_LIMITED_INFORMATION = 2

    class Job:
        JobObjectBasicProcessIdList = 3

        @staticmethod
        def QueryInformationJobObject(_handle: int, _information: int):
            return next(queries)

        @staticmethod
        def IsProcessInJob(_handle: ProcessHandle, _job: int) -> bool:
            return True

    monkeypatch.setattr(process_tree, "_adopted_windows_job", 123)
    monkeypatch.setattr(process_tree, "_adopted_windows_gate", gate)
    monkeypatch.setattr(
        process_tree, "_windows_modules", lambda: (Api(), Con(), Job(), object())
    )
    monkeypatch.setattr(process_tree.time, "sleep", lambda _seconds: None)

    process_tree._drain_adopted_windows_job()

    assert terminated == [700], "the browser went and the gate stayed"


def test_adopted_windows_job_revalidates_process_membership(
    monkeypatch: pytest.MonkeyPatch,
):
    current = os.getpid()
    queries = iter([(current, 700), (current,)])
    terminated: list[int] = []

    class ProcessHandle:
        def Close(self) -> None:
            pass

    class Api:
        @staticmethod
        def OpenProcess(access: int, inherit: bool, process: int) -> ProcessHandle:
            return ProcessHandle()

        @staticmethod
        def TerminateProcess(handle: ProcessHandle, status: int) -> None:
            terminated.append(status)

    class Con:
        PROCESS_TERMINATE = 1
        PROCESS_QUERY_LIMITED_INFORMATION = 2

    class Job:
        JobObjectBasicProcessIdList = 3

        @staticmethod
        def QueryInformationJobObject(handle: int, information: int) -> tuple[int, ...]:
            return next(queries)

        @staticmethod
        def IsProcessInJob(handle: ProcessHandle, job: int) -> bool:
            return False

    original = process_tree._adopted_windows_job
    process_tree._adopted_windows_job = 123
    monkeypatch.setattr(
        process_tree, "_windows_modules", lambda: (Api(), Con(), Job(), object())
    )
    monkeypatch.setattr(process_tree.time, "sleep", lambda _seconds: None)
    try:
        process_tree._drain_adopted_windows_job()
    finally:
        process_tree._adopted_windows_job = original

    assert terminated == []


def test_windows_hard_exit_drains_the_job_before_releasing_locks(
    monkeypatch: pytest.MonkeyPatch,
):
    events: list[object] = []
    monkeypatch.setattr(process_tree, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        process_tree,
        "_drain_adopted_windows_job",
        lambda: events.append("drain-job"),
    )
    monkeypatch.setattr(
        process_tree.os, "_exit", lambda status: events.append(("exit", status))
    )

    process_tree.hard_exit_process_tree(7)

    assert events == ["drain-job", ("exit", 7)]


class TestWindowsJobObject:
    @_WINDOWS_ONLY
    def test_owner_adoption_survives_parent_close_and_hard_exit_drains(
        self, tmp_path: Path
    ):
        script = r"""
import os
import subprocess
import sys
import time

from linkedin_mcp_server.process_tree import WindowsJob, hard_exit_process_tree

name = sys.argv[1]
WindowsJob.verify_current_process(name)
WindowsJob.adopt_current_process(name)
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(600)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(os.getpid(), child.pid, flush=True)
hard_exit_process_tree(7)
"""
        job = process_tree.WindowsJob.named("owner-integration")
        nonce = process_tree.release_nonce()
        process = subprocess.Popen(
            process_tree.windows_gate_command(
                [sys.executable, "-c", script, cast(str, job.name)], nonce
            ),
            cwd=_REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        owner_pid: int | None = None
        descendant_pid: int | None = None
        try:
            job.assign_popen(process)
            assert process.stdin is not None
            process_tree.release_windows_gate(process.stdin, nonce)
            assert process.stdout is not None
            owner_pid, descendant_pid = map(int, process.stdout.readline().split())

            job.close()
            # Not the owner's status. The gate is a member of this Job, so once
            # the owner's own adopted handle is the last one and the owner
            # leaves, kill-on-close reaches the gate before it can mirror
            # anything. What survives that is the containment itself, which is
            # what this launch is here to prove.
            process.wait(timeout=30)
            assert _wait_gone(owner_pid, descendant_pid)
        finally:
            if not job.closed:
                job.terminate()
                process.wait(timeout=30)
                job.release_popen_handle(process)
                job.wait_until_empty(timeout=30)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)
            for pid in (owner_pid, descendant_pid):
                if isinstance(pid, int) and _alive(pid):
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        check=False,
                    )

    @_WINDOWS_ONLY
    @pytest.mark.parametrize("held_stream", ["stdout", "stderr"])
    def test_normal_gate_exit_drains_descendants_holding_a_stream(
        self, held_stream: str
    ):
        script = r"""
import os
import subprocess
import sys

held = sys.argv[1]
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(600)"],
    stdin=subprocess.DEVNULL,
    stdout=sys.stdout if held == "stdout" else subprocess.DEVNULL,
    stderr=sys.stderr if held == "stderr" else subprocess.DEVNULL,
)
control = sys.stderr if held == "stdout" else sys.stdout
print(os.getpid(), child.pid, file=control, flush=True)
"""
        job = process_tree.WindowsJob.anonymous()
        nonce = process_tree.release_nonce()
        process = subprocess.Popen(
            process_tree.windows_gate_command(
                [sys.executable, "-c", script, held_stream], nonce
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        target_pid: int | None = None
        descendant_pid: int | None = None
        try:
            job.assign_popen(process)
            assert process.stdin is not None
            process_tree.release_windows_gate(process.stdin, nonce)
            control = process.stderr if held_stream == "stdout" else process.stdout
            held = process.stdout if held_stream == "stdout" else process.stderr
            assert control is not None and held is not None
            target_pid, descendant_pid = map(int, control.readline().split())

            assert process.wait(timeout=30) == 0
            assert _alive(descendant_pid)
            job.terminate()
            job.release_popen_handle(process)
            job.wait_until_empty(timeout=30)
            assert held.read() == b""
            assert _wait_gone(target_pid, descendant_pid)
        finally:
            if not job.closed:
                job.terminate()
                process.wait(timeout=30)
                job.release_popen_handle(process)
                job.wait_until_empty(timeout=30)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)
            for pid in (target_pid, descendant_pid):
                if isinstance(pid, int) and _alive(pid):
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        check=False,
                    )

    @_WINDOWS_ONLY
    async def test_asyncio_assignment_uses_the_real_popen_handle(self, tmp_path: Path):
        marker = tmp_path / "asyncio-members.txt"
        script = r"""
import os
import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(600)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
# Through a rename, for the reason given at the detached-group fixture
# above: existence has to imply the whole line. Measured on Windows CI,
# where the reader saw the file between its creation and its content and
# failed unpacking two pids out of nothing.
marker = Path(sys.argv[1])
partial = marker.with_name(marker.name + ".partial")
partial.write_text(f"{os.getpid()} {child.pid}")
partial.replace(marker)
time.sleep(600)
"""
        job = process_tree.WindowsJob.anonymous()
        nonce = process_tree.release_nonce()
        process = await asyncio.create_subprocess_exec(
            *process_tree.windows_gate_command(
                [sys.executable, "-c", script, str(marker)], nonce
            ),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        from linkedin_mcp_server import bootstrap

        target_pid: int | None = None
        descendant_pid: int | None = None
        managed: bootstrap._InstallerProcess | None = None
        try:
            popen = job.assign_asyncio_process(process)
            wait = bootstrap._capture_windows_process_wait(process, popen)
            managed = bootstrap._InstallerProcess(
                process=process,
                windows_job=job,
                windows_popen=popen,
                windows_wait=wait,
                assigned=True,
            )
            assert getattr(popen, "_handle", None) is not None
            assert process.stdin is not None
            process_tree.release_windows_gate(process.stdin, nonce)
            await process.stdin.drain()

            for _ in range(500):
                if marker.exists():
                    break
                if process.returncode is not None:
                    stderr = (
                        await process.stderr.read()
                        if process.stderr is not None
                        else b""
                    )
                    pytest.fail(
                        "the assigned target exited before creating descendants: "
                        f"stderr={stderr!r}"
                    )
                await asyncio.sleep(0.01)
            else:
                pytest.fail("the assigned target did not create its descendants")
            target_pid, descendant_pid = map(int, marker.read_text().split())

            deadline = asyncio.get_running_loop().time() + 30
            await asyncio.shield(bootstrap._assigned_windows_cleanup(managed, deadline))
            assert _wait_gone(target_pid, descendant_pid)
        finally:
            if managed is not None and managed.assigned:
                deadline = asyncio.get_running_loop().time() + 30
                await asyncio.shield(
                    bootstrap._assigned_windows_cleanup(managed, deadline)
                )
            elif managed is None:
                if process.returncode is None:
                    if process.stdin is not None:
                        process.stdin.close()
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=5)
                job.close()
            for pid in (target_pid, descendant_pid):
                if isinstance(pid, int) and _alive(pid):
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        check=False,
                    )

    @_WINDOWS_ONLY
    def test_a_compatible_outer_job_allows_the_managed_inner_job(self, tmp_path: Path):
        """Nesting, proved against an outer Job this test owns.

        Every managed Job this server creates is an inner one wherever the host
        already contains the server: a CI runner, a service wrapper, a terminal
        that groups its children. Whether Windows lets the browser and the
        installer keep their own Job in there is not something the code can
        decide, and it is the difference between contained and uncontained.

        This used to ask the runner whether it happened to be in a Job and skip
        when it was not, so the contract was only tested where nobody had
        arranged for it. The outer Job is built here instead: the helper below
        is assigned to it before it is released, so it always runs nested, and
        anything that stops it running nested fails rather than skips.
        """
        script = r"""
import importlib
import os
import subprocess
import sys
import time
from pathlib import Path

from linkedin_mcp_server import process_tree

win32api = importlib.import_module("win32api")
win32job = importlib.import_module("win32job")

print("pid", os.getpid(), flush=True)
# The parent checks this pid against its own outer Job handle and drops the
# file below once it holds, so everything after this point is nested and not
# merely hoping to be.
confirmed = Path(sys.argv[1])
for _ in range(6000):
    if confirmed.exists():
        break
    time.sleep(0.01)
else:
    raise SystemExit("the parent never confirmed the outer Job")
if not win32job.IsProcessInJob(win32api.GetCurrentProcess(), None):
    raise SystemExit("the helper is not contained in any Job")

job = process_tree.WindowsJob.anonymous()
nonce = process_tree.release_nonce()
target = subprocess.Popen(
    process_tree.windows_gate_command(
        [sys.executable, "-c", "import time; time.sleep(600)"], nonce
    ),
    stdin=subprocess.PIPE,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
assigned = False
try:
    job.assign_popen(target)
    assigned = True
    handle = target._handle
    if not win32job.IsProcessInJob(handle, job.job_handle):
        raise SystemExit("the target did not join the managed inner Job")
    if not win32job.IsProcessInJob(handle, None):
        raise SystemExit("the target left the outer Job on the way in")
    process_tree.release_windows_gate(target.stdin, nonce)
    job.terminate()
    if target.wait(timeout=30) == 0:
        raise SystemExit("the inner Job did not terminate its target")
    job.release_popen_handle(target)
    job.wait_until_empty(timeout=30)
    print("nested inner job drained", flush=True)
finally:
    if not job.closed:
        if assigned:
            job.terminate()
            target.wait(timeout=30)
            job.release_popen_handle(target)
        elif target.poll() is None:
            target.kill()
            target.wait(timeout=5)
        job.close()
"""
        win32api = importlib.import_module("win32api")
        win32con = importlib.import_module("win32con")
        win32job = importlib.import_module("win32job")

        confirmed = tmp_path / "outer-job-confirmed"
        outer = process_tree.WindowsJob.anonymous()
        nonce = process_tree.release_nonce()
        helper = subprocess.Popen(
            process_tree.windows_gate_command(
                [sys.executable, "-c", script, str(confirmed)], nonce
            ),
            cwd=_REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assigned = False
        try:
            outer.assign_popen(helper)
            assigned = True
            assert helper.stdin is not None and helper.stdout is not None
            process_tree.release_windows_gate(helper.stdin, nonce)

            # The gate spawns the helper as its own child, so the helper is in
            # this Job by inheritance rather than by assignment. Confirmed by
            # pid against this Job's handle, because ``IsProcessInJob(_, None)``
            # inside the helper would also answer yes to a Job the runner
            # arranged, which is the ambient condition this test replaced.
            reported = helper.stdout.readline().split()
            assert reported[:1] == [b"pid"], reported
            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, int(reported[1])
            )
            try:
                assert win32job.IsProcessInJob(handle, outer.job_handle)
            finally:
                handle.Close()

            confirmed.write_text("go", encoding="ascii")
            stdout, stderr = helper.communicate(timeout=180)
            assert helper.returncode == 0, stderr.decode("utf-8", "replace")
            assert b"nested inner job drained" in stdout
        finally:
            if helper.poll() is None:
                helper.kill()
                helper.wait(timeout=30)
            if not outer.closed:
                if assigned:
                    outer.terminate()
                    outer.release_popen_handle(helper)
                    outer.wait_until_empty(timeout=30)
                else:
                    outer.close()


# --------------------------------------------------------------------------
# Per-launch browser containment
# --------------------------------------------------------------------------


def _fake_patchright(process: Any) -> Any:
    """A ``Playwright`` handle shaped like the one patchright hands out.

    Only ever a convenience for the negative cases below. The shape itself is a
    claim about patchright and is measured against the real driver by
    ``test_the_patchright_driver_process_is_a_popen_this_module_can_assign``.
    """
    return SimpleNamespace(
        _impl_obj=SimpleNamespace(
            _connection=SimpleNamespace(_transport=SimpleNamespace(_proc=process))
        )
    )


def test_a_windows_launch_without_a_job_cannot_prove_its_shutdown(
    monkeypatch: pytest.MonkeyPatch,
):
    """The false proof this containment replaced.

    The Windows drain used to answer from the daemon owner's adopted Job and
    report "empty" whenever there was none. Direct mode never adopts one, and
    direct mode launches browsers: a residual Chromium after a graceful close
    therefore read as a clean shutdown, and the profile was released under it.
    """
    monkeypatch.setattr(process_tree, "_IS_WINDOWS", True)
    monkeypatch.setattr(process_tree, "_adopted_windows_job", None)

    assert (
        process_tree.drain_browser_process_marker("browser", containment=None) is False
    )


class TestTheBrowserLaunchJob:
    """One Job per browser, created before the driver can spawn anything."""

    _modules = TestWindowsJobSetup._modules
    _patch_modules = TestWindowsJobSetup._patch_modules

    def _windows(
        self,
        monkeypatch: pytest.MonkeyPatch,
        events: list[tuple[str, Any]],
        handle: _JobHandle,
        *,
        active: Iterator[object] | None = None,
    ) -> None:
        self._patch_modules(monkeypatch, self._modules(events, handle, active=active))
        monkeypatch.setattr(process_tree, "_IS_WINDOWS", True)
        monkeypatch.setattr(process_tree, "_adopted_windows_job", None)
        monkeypatch.setattr(process_tree, "_live_windows_jobs", [])
        monkeypatch.setattr(process_tree, "_retained_windows_jobs", [])

    def test_the_driver_is_assigned_before_it_launches_anything(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        handle = _JobHandle()
        self._windows(monkeypatch, events, handle)
        popen = type("Popen", (), {"_handle": 777})()
        driver = SimpleNamespace(
            _transport=SimpleNamespace(get_extra_info=lambda _name: popen)
        )

        job = process_tree.contain_browser_launch(_fake_patchright(driver))

        assert job is not None and not job.closed
        assert any(event[0] == "assign" and event[1][1] == 777 for event in events), (
            "the driver process never reached the Job"
        )
        # Kill-on-close, so a crash of this process ends the browser rather than
        # leaving it sitting on the profile.
        limits = next(event for event in events if event[0] == "limits")
        configured = cast(dict[str, Any], limits[1][2])
        assert configured["BasicLimitInformation"]["LimitFlags"] == 20

    def test_posix_gets_no_job_at_all(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(process_tree, "_IS_WINDOWS", False)

        assert process_tree.contain_browser_launch(object()) is None

    def test_a_launch_that_cannot_be_contained_closes_the_job_it_opened(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        handle = _JobHandle()
        self._windows(monkeypatch, events, handle)
        driver = SimpleNamespace(
            _transport=SimpleNamespace(get_extra_info=lambda _name: None)
        )

        with pytest.raises(process_tree.ProcessTreeError, match="underlying Popen"):
            process_tree.contain_browser_launch(_fake_patchright(driver))

        assert handle.closed, "the unusable Job handle was leaked"
        assert process_tree._live_windows_jobs == []

    def test_a_driver_that_names_no_process_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(process_tree, "_IS_WINDOWS", True)

        with pytest.raises(process_tree.ProcessTreeError, match="no driver process"):
            process_tree._patchright_driver_process(SimpleNamespace())

    def test_a_drained_job_is_ended_and_its_handle_released(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        handle = _JobHandle()
        self._windows(monkeypatch, events, handle, active=iter([0]))
        job = process_tree.WindowsJob.anonymous()

        assert (
            process_tree.drain_browser_process_marker("browser", containment=job)
            is True
        )
        assert any(event[0] == "terminate" for event in events)
        assert handle.closed
        assert job.drained

    def test_a_job_that_will_not_empty_keeps_its_handle_and_stays_unproven(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        handle = _JobHandle()
        self._windows(monkeypatch, events, handle, active=repeat(2))
        job = process_tree.WindowsJob.anonymous()

        assert (
            process_tree.drain_browser_process_marker(
                "browser", containment=job, timeout=0.0
            )
            is False
        )
        assert not handle.closed, "containment was released without proof"
        assert not job.drained
        assert job in process_tree._retained_windows_jobs

    def test_a_retry_answers_from_the_buried_jobs_own_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A cancel can lose a proved drain's result; the Job cannot be re-asked.

        Once the handle is gone every Job query fails, so a retry that went back
        to the API would report an unproven shutdown and keep a profile that is
        provably free.
        """
        events: list[tuple[str, Any]] = []
        handle = _JobHandle()
        self._windows(monkeypatch, events, handle, active=iter([0]))
        job = process_tree.WindowsJob.anonymous()
        assert (
            process_tree.drain_browser_process_marker("browser", containment=job)
            is True
        )
        terminations = sum(1 for event in events if event[0] == "terminate")

        assert (
            process_tree.drain_browser_process_marker("browser", containment=job)
            is True
        )
        assert sum(1 for event in events if event[0] == "terminate") == terminations

    def test_an_abandoned_job_never_claims_it_drained(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[tuple[str, Any]] = []
        handle = _JobHandle()
        self._windows(monkeypatch, events, handle)
        job = process_tree.WindowsJob.anonymous()
        job.close()

        assert (
            process_tree.drain_browser_process_marker("browser", containment=job)
            is False
        )


async def test_the_patchright_driver_process_is_a_popen_this_module_can_assign():
    """The dependency shape the Windows containment is built on, measured.

    Everything above this line reaches into patchright privates
    (``_impl_obj._connection._transport._proc``) and then into asyncio's
    (``_transport.get_extra_info("subprocess")``). A hand-written double for
    either would freeze an assumption rather than check one, so this draws the
    real driver and compares what came back.
    """
    from patchright.async_api import async_playwright

    playwright = await async_playwright().start()
    try:
        process = process_tree._patchright_driver_process(playwright)
        assert isinstance(process, asyncio.subprocess.Process)
        assert process.returncode is None, "the driver was not running"

        transport = getattr(process, "_transport", None)
        assert transport is not None
        popen = transport.get_extra_info("subprocess")
        assert isinstance(popen, subprocess.Popen)
        assert popen.pid == process.pid

        # ``WindowsJob.assign_popen`` reaches for exactly this attribute, and
        # only Windows has it: on POSIX the same ``Popen`` carries no handle,
        # which is why containment there is the environment marker instead.
        assert (getattr(popen, "_handle", None) is not None) is (os.name == "nt")
    finally:
        await playwright.stop()


@_WINDOWS_ONLY
def test_a_real_job_takes_a_grandchild_and_leaves_another_job_alone():
    """Real Windows Jobs: inheritance downwards, isolation sideways.

    The browser case in one shape. A Job assigned to a process it did not create
    still contains what that process spawns afterwards, which is what makes
    assigning the Node driver enough for the Chromium it launches next. And
    terminating that Job reaches nothing in the separate Job the installer runs
    under.
    """
    spawner = (
        "import subprocess, sys, time\n"
        'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])\n'
        "print(child.pid, flush=True)\n"
        "time.sleep(300)\n"
    )
    browser_job = process_tree.WindowsJob.anonymous()
    installer_job = process_tree.WindowsJob.anonymous()
    parent = subprocess.Popen(
        [sys.executable, "-c", spawner],
        stdout=subprocess.PIPE,
        text=True,
    )
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        browser_job.assign_popen(parent)
        installer_job.assign_popen(bystander)
        assert parent.stdout is not None
        grandchild = int(parent.stdout.readline().strip())
        assert _alive(grandchild)

        assert (
            process_tree.drain_browser_process_marker(
                "browser", containment=browser_job, timeout=30.0
            )
            is True
        )

        assert _wait_gone(parent.pid, grandchild)
        assert _alive(bystander.pid), "the installer Job was caught in the drain"
    finally:
        for process, job in ((parent, browser_job), (bystander, installer_job)):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=30)
            if not job.closed:
                job.close()


@_WINDOWS_ONLY
async def test_a_real_patchright_driver_is_ended_by_its_own_launch_job():
    from patchright.async_api import async_playwright

    playwright = await async_playwright().start()
    job = process_tree.contain_browser_launch(playwright)
    assert job is not None
    driver = process_tree._patchright_driver_process(playwright).pid
    try:
        assert _alive(driver)

        assert (
            process_tree.drain_browser_process_marker(
                "driver", containment=job, timeout=30.0
            )
            is True
        )

        assert _wait_gone(driver)
    finally:
        if not job.closed:
            job.close()
        try:
            await asyncio.wait_for(playwright.stop(), timeout=10)
        except BaseException:  # noqa: BLE001 - the driver was terminated on purpose
            pass


#: A parent that forks a grandchild into a session of its own and then never
#: waits for it, which is what makes the zombie stick. The grandchild announces
#: itself *after* ``setsid``, so a reader of that line is guaranteed to see it
#: already leading its own process group rather than still sharing its
#: parent's.
_ZOMBIE_HOLDER = (
    "import os, sys, time\n"
    "pid = os.fork()\n"
    "if pid == 0:\n"
    "    os.setsid()\n"
    "    print(os.getpid(), flush=True)\n"
    "    time.sleep(300)\n"
    "    os._exit(0)\n"
    "time.sleep(300)\n"
)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux procfs zombie states"
)
class TestALinuxZombieDoesNotHoldTheLocks:
    """A grandchild nobody will reap must not stop the hard-exit drain.

    ``/proc`` keeps a zombie's PGID and its start time, so every identity check
    in this module still recognises it, and ``waitpid`` cannot collect it when
    its parent is somebody else -- a container's PID 1, or any parent that does
    not reap. ``_wait_for_process_groups`` takes no deadline on the hard-exit
    path by design, because it holds the daemon and profile locks while it
    waits, so before the run-state check it spun here forever: the owner never
    killed its own group and never released the locks.
    """

    @staticmethod
    def _a_zombie_in_its_own_group() -> tuple[int, str, subprocess.Popen]:
        """An unreapable zombie's pid and start identity, plus the holder to kill."""
        holder = subprocess.Popen(
            [sys.executable, "-c", _ZOMBIE_HOLDER],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert holder.stdout is not None
        zombie = int(holder.stdout.readline().strip())
        identity = process_tree._kernel_start_identity(zombie)
        assert identity is not None
        assert os.getpgid(zombie) == zombie, "the helper is not its own group leader"
        os.kill(zombie, signal.SIGKILL)
        for _ in range(500):
            fields = process_tree._stat_fields(zombie)
            if fields and fields[0] == process_tree._ZOMBIE_STATE:
                return zombie, identity, holder
            time.sleep(0.01)
        holder.kill()
        holder.wait(timeout=10)
        pytest.fail("no zombie was produced")

    @staticmethod
    def _wait_in_a_thread(group: int) -> tuple[threading.Thread, list[bool]]:
        answers: list[bool] = []
        thread = threading.Thread(
            target=lambda: answers.append(
                process_tree._wait_for_process_groups((group,))
            ),
            daemon=True,
        )
        thread.start()
        return thread, answers

    @staticmethod
    def _register(monkeypatch: pytest.MonkeyPatch, zombie: int, identity: str) -> None:
        monkeypatch.setattr(process_tree, "_registered_browser_markers", set())
        monkeypatch.setattr(
            process_tree,
            "_registered_posix_groups",
            {
                zombie: process_tree._PosixGroupRegistration(
                    leader_identity=identity,
                    members={zombie: identity},
                )
            },
        )

    def test_the_hard_exit_drain_stops_waiting_for_it(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        zombie, identity, holder = self._a_zombie_in_its_own_group()
        try:
            self._register(monkeypatch, zombie, identity)

            thread, answers = self._wait_in_a_thread(zombie)
            thread.join(timeout=30)

            assert not thread.is_alive(), "the drain is still waiting for a zombie"
            # True, because the group *is* gone: what is left of it can neither
            # open the profile nor be reaped by this owner. The hard exit goes
            # on to kill its own group and release the locks.
            assert answers == [True]
        finally:
            holder.kill()
            holder.wait(timeout=10)

    def test_without_the_run_state_check_the_same_zombie_hangs_it(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The mutation, run rather than argued.

        Turning the one new check off restores the hang on exactly the setup
        above, which is what makes the test above a test.
        """
        zombie, identity, holder = self._a_zombie_in_its_own_group()
        thread: threading.Thread | None = None
        try:
            self._register(monkeypatch, zombie, identity)
            monkeypatch.setattr(
                process_tree, "_has_exited_unreaped", lambda _pid, _state: False
            )

            thread, answers = self._wait_in_a_thread(zombie)
            thread.join(timeout=2)

            assert thread.is_alive(), "the zombie no longer holds the drain"
            assert answers == []
        finally:
            # Releasing the zombie ends the spinning thread: killing its holder
            # reparents it to init, which reaps it at once.
            holder.kill()
            holder.wait(timeout=10)
            if thread is not None:
                thread.join(timeout=30)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux procfs run states"
)
def test_the_linux_snapshot_reports_a_zombies_run_state():
    """The bulk scan has to carry the state, or every check pays a second read."""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, time\n"
                "pid = os.fork()\n"
                "if pid == 0:\n"
                "    os._exit(0)\n"
                "print(pid, flush=True)\n"
                "time.sleep(300)\n"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        zombie = int(holder.stdout.readline().strip())
        for _ in range(500):
            rows = process_tree._linux_process_rows()
            if rows.get(zombie, (0, 0, None, None))[3] == process_tree._ZOMBIE_STATE:
                break
            time.sleep(0.01)
        else:  # pragma: no cover - the helper failed to leave a zombie
            pytest.fail("the snapshot never reported a zombie state")

        assert process_tree._has_exited_unreaped(zombie, rows[zombie][3])
        assert not process_tree._has_exited_unreaped(holder.pid, rows[holder.pid][3])
    finally:
        holder.kill()
        holder.wait(timeout=10)


@_POSIX_ONLY
def test_a_live_process_is_never_mistaken_for_a_zombie():
    """Only the zombie state is discounted, and only for what it proves.

    A stopped or uninterruptible process can still come back and still holds
    what it opened, so the locks stay. Reading any state but ``Z`` as gone is
    the failure this rules out.
    """
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert not process_tree._has_exited_unreaped(live.pid, None)
        for state in ("S", "R", "T", "D", "I"):
            assert not process_tree._has_exited_unreaped(live.pid, state)
        assert process_tree._has_exited_unreaped(live.pid, "Z")
    finally:
        live.kill()
        live.wait(timeout=10)
