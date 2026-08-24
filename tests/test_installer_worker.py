"""The worker pins the installer group until its supervisor cleans it."""

from __future__ import annotations

import os
import signal
from typing import Any

import pytest

import linkedin_mcp_server.installer_worker as worker

_NONCE = "0123456789abcdef" * 4


class _ImmediateThread:
    def __init__(self, *, target: Any, args: tuple[object, ...], **kwargs: object):
        self.target = target
        self.args = args

    def start(self) -> None:
        self.target(*self.args)


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
    class _LiveSupervisor:
        def is_set(self) -> bool:
            return False

        def wait(self, timeout: float | None = None) -> bool:
            return False

    class _IdleThread:
        def __init__(self, **_kwargs: object):
            pass

        def start(self) -> None:
            return None

    def denied(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("target access denied")

    monkeypatch.setattr(worker, "_await_nonce", lambda: _NONCE)
    monkeypatch.setattr(worker.threading, "Event", _LiveSupervisor)
    monkeypatch.setattr(worker.threading, "Thread", _IdleThread)
    monkeypatch.setattr(worker.subprocess, "Popen", denied)

    assert worker.main(["worker", "--", "target"]) == 70
    captured = capsys.readouterr()
    assert "Patchright target could not start: target access denied" in captured.out
    assert captured.err == ""


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
