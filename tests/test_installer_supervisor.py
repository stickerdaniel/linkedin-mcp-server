"""The supervisor gate prevents targets before the parent owns its pipes."""

from __future__ import annotations

import os
from typing import Any, cast

import pytest

import linkedin_mcp_server.installer_supervisor as supervisor


class _IdleThread:
    def __init__(self, **kwargs: object):
        pass

    def start(self) -> None:
        return None


class _StatusThread:
    def __init__(
        self, *, target: Any, args: tuple[object, ...], name: str, **kwargs: object
    ):
        self.target = target
        self.args = args
        self.name = name

    def start(self) -> None:
        if self.name == "installer-worker-status":
            self.target(*self.args)


class _Pipe:
    def __init__(self, lines: bytes = b"") -> None:
        self.lines = lines.splitlines(keepends=True)
        self.written = bytearray()

    def readline(self, _size: int = -1) -> bytes:
        return self.lines.pop(0) if self.lines else b""

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def flush(self) -> None:
        return None


_NONCE = "0123456789abcdef" * 4


class _Worker:
    pid = 12345
    returncode: int | None = None

    def __init__(self, status: bytes = b"") -> None:
        self.stdin = _Pipe()
        self.stderr = _Pipe(status)

    def wait(self) -> int:
        self.returncode = 7
        return self.returncode


def test_parent_must_open_the_start_gate_before_worker_creation(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)
    monkeypatch.setattr(supervisor, "_await_start", lambda: False)
    monkeypatch.setattr(supervisor.threading, "Thread", _IdleThread)
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("a worker existed before the start gate"),
    )

    assert supervisor.main(["supervisor", "--", "target"]) == 1


def test_broken_started_frame_stops_the_worker_group(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = 0
    stopped: list[int] = []

    def report(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise BrokenPipeError

    monkeypatch.setattr("builtins.print", report)
    monkeypatch.setattr(supervisor, "_await_start", lambda: True)
    monkeypatch.setattr(supervisor.threading, "Thread", _IdleThread)
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *a, **k: _Worker())
    monkeypatch.setattr(
        supervisor,
        "_terminate_worker",
        lambda child, *, result: stopped.append(result) or result,
    )

    assert supervisor.main(["supervisor", "--", "target"]) == 1
    assert calls == 2
    assert stopped == ([] if os.name == "nt" else [1])


def test_worker_status_preserves_target_result_and_command(
    monkeypatch: pytest.MonkeyPatch,
):
    commands: list[list[str]] = []
    options: list[dict[str, object]] = []

    spawned: list[_Worker] = []

    def spawn(command: list[str], **kwargs: object) -> _Worker:
        commands.append(command)
        options.append(kwargs)
        child = _Worker(f"finished {_NONCE} 7\n".encode())
        spawned.append(child)
        return child

    monkeypatch.setattr("builtins.print", lambda *a, **k: None)
    monkeypatch.setattr(supervisor.secrets, "token_hex", lambda _size: _NONCE)
    monkeypatch.setenv("PYTHONPATH", ".")
    monkeypatch.setattr(supervisor, "_await_start", lambda: True)
    monkeypatch.setattr(supervisor.threading, "Thread", _StatusThread)
    monkeypatch.setattr(supervisor.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        supervisor, "_terminate_worker", lambda child, *, result: result
    )

    assert supervisor.main(["supervisor", "--", "target", "arg"]) == 7
    assert commands[0][1:4] == [
        "-P",
        "-m",
        "linkedin_mcp_server.installer_worker",
    ]
    assert commands[0][-2:] == ["target", "arg"]
    assert options[0]["stdin"] == supervisor.subprocess.PIPE
    assert options[0]["stderr"] == supervisor.subprocess.PIPE
    assert spawned[0].stdin.written == f"{_NONCE}\n".encode()
    environment = cast(dict[str, str], options[0]["env"])
    assert "PYTHONPATH" not in environment


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        (f"finished {_NONCE} -9\n".encode(), -9),
        (f"finished {_NONCE} 0".encode(), None),
        (f"wrong {_NONCE} 0\n".encode(), None),
        (f"finished {_NONCE} nope\n".encode(), None),
        (b"finished 0\n", None),
    ],
)
def test_worker_status_is_strictly_framed(frame: bytes, expected: int | None):
    assert supervisor._reported_returncode(frame, _NONCE) == expected


def test_worker_status_rejects_a_pre_token_completion_diagnostic():
    reached = supervisor.threading.Event()
    frame: list[bytes] = []
    authentic = f"finished {_NONCE} 7\n".encode()

    supervisor._worker_status(
        cast(Any, _Pipe(b"finished 0\n" + authentic)),
        reached,
        frame,
        _NONCE,
    )

    assert reached.is_set()
    assert frame == [authentic]


def test_worker_status_accepts_a_frame_after_large_startup_noise():
    reached = supervisor.threading.Event()
    frame: list[bytes] = []
    noise = b"diagnostic\n" * 500
    authentic = f"finished {_NONCE} 0\n".encode()

    supervisor._worker_status(
        cast(Any, _Pipe(noise + authentic)),
        reached,
        frame,
        _NONCE,
    )

    assert reached.is_set()
    assert frame == [authentic]
