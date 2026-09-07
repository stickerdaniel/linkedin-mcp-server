"""The worker pins the installer group until its supervisor cleans it."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from typing import Any, cast

import pytest

import linkedin_mcp_server.installer_worker as worker

_NONCE = "0123456789abcdef" * 4


class _ImmediateThread:
    def __init__(self, *, target: Any, args: tuple[object, ...], **kwargs: object):
        self.target = target
        self.args = args

    def start(self) -> None:
        self.target(*self.args)


class _IdleThread:
    def __init__(self, **kwargs: object):
        pass

    def start(self) -> None:
        return None


class _SupervisorEvent:
    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> bool:
        return timeout is None


class _FinishedTarget:
    def poll(self) -> int:
        return 0


def test_supervisor_lease_is_armed_before_patchright_creation(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(worker, "_await_nonce", lambda: _NONCE)
    monkeypatch.setattr(worker.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(worker, "_supervisor_eof", lambda reached: reached.set())
    monkeypatch.setattr(
        worker.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("Patchright escaped after supervisor EOF"),
    )

    assert worker.main(["worker", "--", "target"]) == 1


def test_nonce_frame_does_not_consume_the_following_lease_eof(
    monkeypatch: pytest.MonkeyPatch,
):
    pieces = iter([*(bytes([byte]) for byte in f"{_NONCE}\n".encode()), b""])

    class _Stdin:
        def fileno(self) -> int:
            return 0

    monkeypatch.setattr(worker.sys, "stdin", _Stdin())
    monkeypatch.setattr(worker.os, "read", lambda _fd, _size: next(pieces))

    assert worker._await_nonce() == _NONCE
    assert next(pieces) == b""


def test_finished_frame_carries_the_stdin_nonce(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    nonce = _NONCE

    class _LiveSupervisor:
        def is_set(self) -> bool:
            return False

        def wait(self, timeout: float | None = None) -> bool:
            return timeout is None

    class _IdleThread:
        def __init__(self, **_kwargs: object):
            pass

        def start(self) -> None:
            return None

    class _FinishedTarget:
        def poll(self) -> int:
            return 7

    monkeypatch.setattr(worker.threading, "Event", _LiveSupervisor)
    monkeypatch.setattr(worker.threading, "Thread", _IdleThread)
    monkeypatch.setattr(worker, "_await_nonce", lambda: nonce, raising=False)
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: _FinishedTarget())
    monkeypatch.setattr(
        worker,
        "_stop_without_supervisor",
        lambda returncode: (_ for _ in ()).throw(SystemExit(returncode)),
    )

    with pytest.raises(SystemExit, match="7"):
        worker.main(["worker", "--", "target"])

    captured = capsys.readouterr()
    assert captured.err == f"finished {nonce} 7\n"


def test_target_spawn_error_reaches_installer_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    def denied(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("target access denied")

    monkeypatch.setattr(worker, "_await_nonce", lambda: _NONCE)
    monkeypatch.setattr(worker.threading, "Event", _SupervisorEvent)
    monkeypatch.setattr(worker.threading, "Thread", _IdleThread)
    monkeypatch.setattr(worker.subprocess, "Popen", denied)

    assert worker.main(["worker", "--", "target"]) == 70
    captured = capsys.readouterr()
    assert "Patchright target could not start: target access denied" in captured.out
    assert captured.err == ""


def test_private_temp_environment_reaches_patchright(
    monkeypatch: pytest.MonkeyPatch,
):
    options: list[dict[str, object]] = []

    def spawn(*args: object, **kwargs: object) -> _FinishedTarget:
        options.append(kwargs)
        return _FinishedTarget()

    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, "/private/installer")
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_await_nonce", lambda: _NONCE)
    monkeypatch.setattr(worker.threading, "Event", _SupervisorEvent)
    monkeypatch.setattr(worker.threading, "Thread", _IdleThread)
    monkeypatch.setattr(worker.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        worker,
        "_stop_without_supervisor",
        lambda returncode: (_ for _ in ()).throw(SystemExit(returncode)),
    )

    with pytest.raises(SystemExit, match="0"):
        worker.main(["worker", "--", "target"])

    environment = cast(dict[str, str], options[0]["env"])
    assert {environment[name] for name in ("TMPDIR", "TMP", "TEMP")} == {
        "/private/installer"
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_orphaned_worker_kills_its_own_installer_group(
    monkeypatch: pytest.MonkeyPatch,
):
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(worker.os, "getpgrp", lambda: 12345)
    monkeypatch.setattr(
        worker.os, "killpg", lambda pgid, sent: signals.append((pgid, sent))
    )
    monkeypatch.setattr(
        worker.os,
        "_exit",
        lambda code: (_ for _ in ()).throw(SystemExit(code)),
    )

    with pytest.raises(SystemExit, match="1"):
        worker._stop_without_supervisor(1)

    assert signals == [(12345, signal.SIGKILL)]


def test_the_target_does_not_inherit_the_supervisor_lease():
    """The real worker, because the defect was in what it hands its target.

    The lease thread holds a blocking read on this worker's stdin, and a target
    that inherits the same handle waits behind it. On Windows that wait starts
    before the target runs a line: measured on a runner, the process existed for
    the whole run, wrote no marker and produced no output, and a real browser
    install therefore never returned. On POSIX the target reaches its own first
    read and blocks there instead. One handle, two shapes, and the same cause.

    Asserted through the target's own stdout rather than through the call, so a
    worker that goes back to handing the lease over fails here in bounded time
    instead of hanging the suite.
    """
    reports_its_stdin = (
        "import sys; print('stdin=' + repr(sys.stdin.read(1)), flush=True)"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-P",
            "-m",
            "linkedin_mcp_server.installer_worker",
            "--",
            sys.executable,
            "-P",
            "-c",
            reports_its_stdin,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # As the supervisor starts it, and not as a detail. Losing its lease
        # makes this worker signal its own process group, so a worker left in
        # the runner's group takes the test session with it: measured here, a
        # first draft of this test killed pytest between two assertions.
        start_new_session=os.name != "nt",
    )
    lease = process.stdin
    spoken_by_the_target = process.stdout
    assert lease is not None and spoken_by_the_target is not None
    spoken: list[bytes] = []
    try:
        lease.write(f"{_NONCE}\n".encode("ascii"))
        lease.flush()

        # Held open on purpose: closing it is what ends the lease, and the point
        # here is what the target sees while the lease is still live.
        listener = threading.Thread(
            target=lambda: spoken.append(spoken_by_the_target.readline()),
            daemon=True,
        )
        listener.start()
        listener.join(30)
        assert not listener.is_alive(), "the target never reported its own stdin"
        assert spoken[0].strip() == b"stdin=''"

        lease.close()
        assert process.wait(timeout=30) is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
