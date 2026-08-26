"""Process containment against real descendants and deterministic race doubles."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from collections.abc import Iterator
from itertools import repeat
from types import SimpleNamespace
from typing import Any, cast

import pytest

import linkedin_mcp_server.process_gate as process_gate
from linkedin_mcp_server.process_protocol import new_nonce
import linkedin_mcp_server.process_tree as process_tree

_REPO_ROOT = Path(__file__).resolve().parents[1]
_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
_WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")


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
    assert stdout == b"stdout:target payload\n"
    assert stderr == b"stderr-only\n"
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
        monkeypatch.setattr(process_tree, "os", SimpleNamespace(name="nt"))
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
        self._patch_modules(monkeypatch, modules)

        process_tree.WindowsJob.verify_current_process("named-owner")
        process_tree.WindowsJob.adopt_current_process("named-owner")

        assert verified.closed
        assert not verified.detached
        assert adopted.detached
        assert not adopted.closed
        assert process_tree._adopted_windows_job == 2

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
def test_daemon_hard_exit_terminates_its_process_group(tmp_path: Path):
    marker = tmp_path / "daemon-descendant.txt"
    script = r"""
import subprocess
import sys
from pathlib import Path

from linkedin_mcp_server import daemon_owner

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(600)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
Path(sys.argv[1]).write_text(str(child.pid))
daemon_owner._exit_hard()
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

from linkedin_mcp_server.process_tree import WindowsJob

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
time.sleep(600)
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
            assert _alive(owner_pid)
            assert _alive(descendant_pid)

            subprocess.run(
                ["taskkill", "/PID", str(owner_pid), "/F"],
                check=True,
                capture_output=True,
            )
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
Path(sys.argv[1]).write_text(f"{os.getpid()} {child.pid}")
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
        target_pid: int | None = None
        descendant_pid: int | None = None
        try:
            popen = job.assign_asyncio_process(process)
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

            await asyncio.to_thread(job.terminate)
            while process.returncode is None:
                await asyncio.sleep(0.01)
            job.release_popen_handle(popen)
            await asyncio.to_thread(job.wait_until_empty, timeout=30)
            assert _wait_gone(target_pid, descendant_pid)
        finally:
            if not job.closed:
                await asyncio.to_thread(job.terminate)
                while process.returncode is None:
                    await asyncio.sleep(0.01)
                job.release_popen_handle(popen)
                await asyncio.to_thread(job.wait_until_empty, timeout=30)
            if process.returncode is None:
                process.kill()
                await process.wait()
            for pid in (target_pid, descendant_pid):
                if isinstance(pid, int) and _alive(pid):
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        check=False,
                    )

    @_WINDOWS_ONLY
    def test_a_compatible_outer_job_allows_the_managed_inner_job(self):
        win32api = importlib.import_module("win32api")
        win32job = importlib.import_module("win32job")
        if not win32job.IsProcessInJob(win32api.GetCurrentProcess(), None):
            pytest.skip("the Windows runner does not contain this test in an outer Job")

        job = process_tree.WindowsJob.anonymous()
        nonce = process_tree.release_nonce()
        process = subprocess.Popen(
            process_tree.windows_gate_command(
                [sys.executable, "-c", "import time; time.sleep(600)"], nonce
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            job.assign_popen(process)
            assert process.stdin is not None
            process_tree.release_windows_gate(process.stdin, nonce)
            job.terminate()
            assert process.wait(timeout=30) != 0
            job.release_popen_handle(process)
            job.wait_until_empty(timeout=30)
        finally:
            if not job.closed:
                job.terminate()
                process.wait(timeout=30)
                job.release_popen_handle(process)
                job.wait_until_empty(timeout=30)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)
