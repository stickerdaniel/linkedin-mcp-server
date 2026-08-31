"""The supervisor gate prevents targets before the parent owns its pipes."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, BinaryIO, cast

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
_REPO_ROOT = Path(__file__).resolve().parents[1]


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
    monkeypatch.setattr(supervisor, "_await_nonce", lambda: _NONCE)
    monkeypatch.setattr(supervisor, "_await_start", lambda nonce: False)
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
    monkeypatch.setattr(supervisor, "_await_nonce", lambda: _NONCE)
    monkeypatch.setattr(supervisor, "_await_start", lambda nonce: nonce == _NONCE)
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

    reports: list[str] = []
    monkeypatch.setattr(
        "builtins.print", lambda message, **kwargs: reports.append(message)
    )
    monkeypatch.setattr(supervisor, "new_nonce", lambda: _NONCE)
    monkeypatch.setenv("PYTHONPATH", ".")
    monkeypatch.setattr(supervisor, "_await_nonce", lambda: _NONCE)
    monkeypatch.setattr(supervisor, "_await_start", lambda nonce: nonce == _NONCE)
    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, "/private/installer")
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
    assert reports == [f"armed {_NONCE}", f"started {_NONCE} {spawned[0].pid}"]
    environment = cast(dict[str, str], options[0]["env"])
    assert "PYTHONPATH" not in environment
    assert {environment[name] for name in ("TMPDIR", "TMP", "TEMP")} == {
        "/private/installer"
    }


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


def test_worker_status_extracts_a_frame_after_an_unterminated_startup_hook():
    reached = supervisor.threading.Event()
    frame: list[bytes] = []
    authentic = f"finished {_NONCE} 7\n".encode()

    # This is the real shape produced by a sitecustomize hook that calls
    # ``sys.stderr.write`` and flushes without a newline. The worker's first
    # module-level status write continues on that same physical line.
    supervisor._worker_status(
        cast(Any, _Pipe(b"sitecustomize: checking interpreter" + authentic)),
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


def _user_site_with_hook(tmp_path: Path, hook: str) -> tuple[str, dict[str, str], Path]:
    """Install *hook* as a real ``sitecustomize`` an interpreter would execute.

    Returns the interpreter that honours it, the environment that points at it,
    and the user-site directory. The base interpreter is needed because a
    virtual environment disables user site outright, so the venv Python that
    runs the suite would never load the hook and every assertion about it would
    hold for the wrong reason.
    """
    base_executable = getattr(sys, "_base_executable", sys.executable)
    environment = os.environ.copy()
    environment.pop("PYTHONNOUSERSITE", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONUSERBASE"] = str(tmp_path / "user-base")

    located = subprocess.run(
        [base_executable, "-c", "import site; print(site.getusersitepackages())"],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=True,
    )
    user_site = Path(located.stdout.strip())
    user_site.mkdir(parents=True)
    # The worker resolves the package through ordinary startup, so it needs a
    # path entry. The supervisor does not: it runs the file by absolute path.
    (user_site / "linkedin-mcp-source.pth").write_text(f"{_REPO_ROOT}\n")
    (user_site / "sitecustomize.py").write_text(hook)
    return base_executable, environment, user_site


def _venv_with_site_hook(tmp_path: Path, hook: str) -> tuple[str, dict[str, str]]:
    """Install *hook* where only ``-S`` can stop it: a real environment's site.

    A ``sitecustomize`` in the interpreter's own ``site-packages`` is what a
    virtual environment or a system install ships, and it survives ``-P``,
    ``PYTHONNOUSERSITE``, a popped ``PYTHONPATH`` and ``-I`` alike, because all
    of those act on the user-site copy or on path entries. The environment
    keeps the source root on a ``.pth`` for the worker, which runs with
    ordinary startup.
    """
    venv = tmp_path / "hooked-venv"
    subprocess.run(
        [
            getattr(sys, "_base_executable", sys.executable),
            "-m",
            "venv",
            "--without-pip",
            str(venv),
        ],
        check=True,
        timeout=120,
        capture_output=True,
    )
    interpreter = venv / "bin" / "python"
    located = subprocess.run(
        [str(interpreter), "-c", "import site; print(site.getsitepackages()[0])"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    site_packages = Path(located.stdout.strip())
    (site_packages / "linkedin-mcp-source.pth").write_text(f"{_REPO_ROOT}\n")
    (site_packages / "sitecustomize.py").write_text(hook)

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("VIRTUAL_ENV", None)
    return str(interpreter), environment


def _production_supervisor_command(interpreter: str, target: list[str]) -> list[str]:
    """The command production builds, run by the interpreter that has the hook."""
    from linkedin_mcp_server.bootstrap import _installer_supervisor_command

    command = _installer_supervisor_command(target)
    assert command[0] == sys.executable
    return [interpreter, *command[1:]]


def _kill_recorded_helpers(sentinel: Path, marker: str) -> None:
    """Stop the helpers a hook recorded, identified rather than assumed.

    A recorded pid is only a pid: the process behind it may have exited and the
    number been handed to something else on this machine, so each one is read
    back and killed only while it still carries *marker*.
    """
    if not sentinel.exists():
        return
    for line in sentinel.read_text().splitlines():
        for pid in line.split()[1:]:
            listed = subprocess.run(
                ["ps", "-o", "command=", "-p", pid],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if marker in listed.stdout:
                with contextlib.suppress(OSError):
                    os.kill(int(pid), signal.SIGKILL)


def _reaches_eof(stream: Any, timeout: float) -> bool:
    """Whether *stream* closes, rather than staying open on an inherited pipe."""
    reader = threading.Thread(target=stream.read, daemon=True)
    reader.start()
    reader.join(timeout)
    return not reader.is_alive()


@pytest.mark.skipif(os.name == "nt", reason="the cleanup uses POSIX process groups")
def test_real_user_site_hook_cannot_join_the_completion_frame(tmp_path: Path):
    base_executable, environment, _ = _user_site_with_hook(
        tmp_path,
        "import sys\n"
        "sys.stderr.write('sitecustomize: checking interpreter')\n"
        "sys.stderr.flush()\n",
    )

    enabled = subprocess.run(
        [base_executable, "-c", "import sys; print('sitecustomize' in sys.modules)"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert enabled.stdout.strip() == "True"
    assert "sitecustomize: checking interpreter" in enabled.stderr

    # The production shape keeps the hook out of the supervisor, so the noise
    # this frame has to survive now comes from the worker, which keeps ordinary
    # startup because its whole group is contained.
    process = subprocess.Popen(
        _production_supervisor_command(
            base_executable,
            [base_executable, "-s", "-c", "raise SystemExit(7)"],
        ),
        cwd=_REPO_ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdin is not None and process.stderr is not None
    try:
        from linkedin_mcp_server.process_protocol import read_authenticated_status

        process.stdin.write(f"{_NONCE}\n".encode())
        process.stdin.flush()
        armed = f"armed {_NONCE}\n".encode()
        assert read_authenticated_status(
            cast(BinaryIO, process.stderr),
            marker=armed[:-1],
            parse=lambda frame: frame if frame == armed else None,
        ) == (armed, armed)

        process.stdin.write(f"start {_NONCE}\n".encode())
        process.stdin.flush()
        started_marker = f"started {_NONCE} ".encode()
        started = read_authenticated_status(
            cast(BinaryIO, process.stderr),
            marker=started_marker,
            parse=lambda frame: frame if frame.endswith(b"\n") else None,
        )
        assert started is not None
        assert started[0].startswith(started_marker)
        assert process.wait(timeout=10) == 7
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=30)


@pytest.mark.skipif(os.name == "nt", reason="the cleanup uses POSIX process groups")
def test_a_startup_hook_cannot_spawn_a_pipe_holder_in_the_supervisor(tmp_path: Path):
    """A hook helper in the supervisor's own group would outlive every drain.

    The supervisor leads its session and the worker leads a separate group, so
    the parent kills only the supervisor pid and the supervisor drains only the
    worker group. Anything a startup hook spawns before ``main`` runs sits in
    the gap: it inherits the installer output pipes, holds their EOF open after
    the supervisor exits, and no cancellation reaches it.
    """
    sentinel = tmp_path / "hooks.txt"
    helper_marker = "installer-hook-helper"
    interpreter, environment = _venv_with_site_hook(
        tmp_path,
        "import os, subprocess, sys\n"
        # Inherits stdout and stderr, and outlives anything that only signals
        # the process that started it.
        "helper = subprocess.Popen([sys.executable, '-S', '-c',"
        f" 'import time; time.sleep(600)  # {helper_marker}'])\n"
        f"open({str(sentinel)!r}, 'a').write("
        "f'{os.getpid()} {helper.pid}\\n')\n",
    )

    # This hook is unavoidable for anything the interpreter starts normally,
    # ``-I`` included, which is what the supervisor launch has to answer for.
    # Its output goes to a file rather than a pipe, because the helper it
    # spawns inherits that pipe and would hold the read open for ten minutes.
    control_output = tmp_path / "control.txt"
    with control_output.open("wb") as sink:
        subprocess.run(
            [
                interpreter,
                "-I",
                "-c",
                "import sys; print('sitecustomize' in sys.modules)",
            ],
            env=environment,
            stdout=sink,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=True,
        )
    assert control_output.read_text().strip() == "True"
    control_lines = sentinel.read_text().splitlines()
    assert len(control_lines) == 1

    process = subprocess.Popen(
        _production_supervisor_command(
            interpreter,
            [interpreter, "-s", "-S", "-c", "raise SystemExit(7)"],
        ),
        cwd=_REPO_ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None and process.stderr is not None
    try:
        from linkedin_mcp_server.process_protocol import read_authenticated_status

        process.stdin.write(f"{_NONCE}\n".encode())
        process.stdin.flush()
        armed = f"armed {_NONCE}\n".encode()
        # The supervisor still imports its package under ``-S``, where nothing
        # but its own path bootstrap can reach it.
        assert read_authenticated_status(
            cast(BinaryIO, process.stderr),
            marker=armed[:-1],
            parse=lambda frame: frame if frame == armed else None,
        ) == (armed, armed)

        process.stdin.write(f"start {_NONCE}\n".encode())
        process.stdin.flush()
        started_marker = f"started {_NONCE} ".encode()
        started = read_authenticated_status(
            cast(BinaryIO, process.stderr),
            marker=started_marker,
            parse=lambda frame: frame if frame.endswith(b"\n") else None,
        )
        assert started is not None
        assert process.wait(timeout=30) == 7

        lines = sentinel.read_text().splitlines()
        # The worker keeps ordinary startup on purpose, so the hook still runs
        # somewhere under this launch and the run is not silently inert.
        assert len(lines) > len(control_lines)
        hook_pids = {line.split()[0] for line in lines}
        assert str(process.pid) not in hook_pids

        # Nothing the hook spawned still holds the installer output open.
        assert _reaches_eof(process.stdout, 20.0)
        assert _reaches_eof(process.stderr, 20.0)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=30)
        _kill_recorded_helpers(sentinel, helper_marker)


@pytest.mark.skipif(os.name == "nt", reason="the cleanup uses POSIX process groups")
def test_an_inherited_path_cannot_answer_the_supervisor_import(tmp_path: Path):
    """The re-added import root is the only one, so no path entry precedes it.

    The supervisor adds its own directory back by hand because ``-S`` took
    ``site`` away, and an appended entry loses to anything ``PYTHONPATH`` puts
    in front of it. The launch pops that variable, so this is the second layer:
    it holds for a supervisor started from an environment that carries one
    regardless.
    """
    sentinel = tmp_path / "shadow-loaded.txt"
    shadow = tmp_path / "shadow" / "linkedin_mcp_server"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text(
        f"open({str(sentinel)!r}, 'a').write('loaded\\n')\n"
        "raise ImportError('the shadow package answered')\n"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(tmp_path / "shadow")

    process = subprocess.Popen(
        _production_supervisor_command(
            sys.executable,
            [sys.executable, "-c", "raise SystemExit(7)"],
        ),
        cwd=_REPO_ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdin is not None and process.stderr is not None
    try:
        from linkedin_mcp_server.process_protocol import read_authenticated_status

        process.stdin.write(f"{_NONCE}\n".encode())
        process.stdin.flush()
        armed = f"armed {_NONCE}\n".encode()
        assert read_authenticated_status(
            cast(BinaryIO, process.stderr),
            marker=armed[:-1],
            parse=lambda frame: frame if frame == armed else None,
        ) == (armed, armed)

        process.stdin.write(f"start {_NONCE}\n".encode())
        process.stdin.flush()
        started_marker = f"started {_NONCE} ".encode()
        assert (
            read_authenticated_status(
                cast(BinaryIO, process.stderr),
                marker=started_marker,
                parse=lambda frame: frame if frame.endswith(b"\n") else None,
            )
            is not None
        )
        assert process.wait(timeout=30) == 7
        assert not sentinel.exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=30)


def test_the_armed_frame_is_not_platform_translated(monkeypatch: pytest.MonkeyPatch):
    """The parent compares this frame byte for byte.

    Checked at the argument, because a POSIX host writes LF either way. What
    decides the Windows outcome is that the stream is reconfigured at all:
    measured there, a genuine armed frame arrived as CRLF, was refused, and the
    supervisor was reported as never becoming ready.
    """
    asked: list[object] = []

    class _Stream:
        @staticmethod
        def reconfigure(**kwargs: object) -> None:
            asked.append(kwargs.get("newline"))

    monkeypatch.setattr(supervisor.sys, "stderr", _Stream())

    supervisor._write_frames_without_translation()

    assert asked == ["\n"]


def test_a_stream_that_cannot_be_reconfigured_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
):
    class _Stream:
        @staticmethod
        def reconfigure(**_kwargs: object) -> None:
            raise ValueError("already detached")

    monkeypatch.setattr(supervisor.sys, "stderr", _Stream())

    supervisor._write_frames_without_translation()
