"""Gate and supervise the managed browser installer process boundary."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
from typing import BinaryIO

from linkedin_mcp_server.process_tree import (
    child_exited_without_reaping,
    terminate_process_group,
)

_ARMED = "armed"
_STARTED = "started"
_FINISHED = "finished"
_START = b"start\n"
_NONCE_BYTES = 32


def _parent_eof(reached: threading.Event) -> None:
    """Set *reached* when the process holding our stdin lease is gone."""
    try:
        os.read(sys.stdin.fileno(), 1)
    finally:
        reached.set()


def _worker_status(
    stream: BinaryIO, reached: threading.Event, frame: list[bytes], nonce: str
) -> None:
    try:
        while line := stream.readline(128):
            if _reported_returncode(line, nonce) is not None:
                frame.append(line)
                return
    finally:
        reached.set()


def _target_command(argv: list[str]) -> list[str]:
    args = argv[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    if not args:
        raise ValueError("The installer supervisor was started without a command")
    return args


def _await_start() -> bool:
    """Read the exact launch gate without buffering past it."""
    frame = bytearray()
    while len(frame) <= len(_START):
        piece = os.read(sys.stdin.fileno(), 1)
        if not piece:
            return False
        frame.extend(piece)
        if piece == b"\n":
            return bytes(frame) == _START
    return False


def _terminate_worker(worker: subprocess.Popen[bytes], *, result: int) -> int:
    """Stop a POSIX worker group while its child identity is still pinned."""
    terminated = terminate_process_group(worker.pid, timeout=10.0, child=worker)
    return result if terminated else 70


def _reported_returncode(frame: bytes, nonce: str) -> int | None:
    if not frame.endswith(b"\n"):
        return None
    try:
        label, reported_nonce, value = frame.decode("ascii").strip().split(maxsplit=2)
        returncode = int(value)
    except (UnicodeDecodeError, ValueError):
        return None
    if label != _FINISHED or reported_nonce != nonce:
        return None
    return returncode


def main(argv: list[str] | None = None) -> int:
    """Wait until the parent owns our pipes, then start the leased worker."""
    argv = sys.argv if argv is None else argv

    # The parent may be cancelled while asyncio is still constructing its
    # subprocess transport. No managed target exists until it has received this
    # frame and replied through stdin, so transport teardown can only end an
    # empty supervisor.
    try:
        print(_ARMED, file=sys.stderr, flush=True)
    except OSError:
        return 1
    if not _await_start():
        return 1

    parent_gone = threading.Event()
    threading.Thread(
        target=_parent_eof,
        args=(parent_gone,),
        name="installer-parent-lease",
        daemon=True,
    ).start()
    if parent_gone.wait(0):
        return 1

    worker_command = [
        sys.executable,
        "-P",
        "-m",
        "linkedin_mcp_server.installer_worker",
        "--",
        *_target_command(argv),
    ]
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    worker = subprocess.Popen(
        worker_command,
        stdin=subprocess.PIPE,
        stdout=None,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=os.name != "nt",
        close_fds=True,
    )
    assert worker.stdin is not None and worker.stderr is not None

    # Generated only after the process exists and delivered through the pipe that
    # remains its lifetime lease. Interpreter startup diagnostics run before the
    # worker module and can imitate a plain status line, but cannot know this frame.
    nonce = secrets.token_hex(_NONCE_BYTES)
    try:
        worker.stdin.write(f"{nonce}\n".encode("ascii"))
        worker.stdin.flush()
    except OSError:
        if os.name == "nt":
            return 1
        return _terminate_worker(worker, result=1)

    worker_done = threading.Event()
    worker_frame: list[bytes] = []
    threading.Thread(
        target=_worker_status,
        args=(worker.stderr, worker_done, worker_frame, nonce),
        name="installer-worker-status",
        daemon=True,
    ).start()

    if parent_gone.is_set():
        if os.name == "nt":
            return 1
        return _terminate_worker(worker, result=1)

    try:
        print(f"{_STARTED} {worker.pid}", file=sys.stderr, flush=True)
    except OSError:
        if os.name == "nt":
            return 1
        return _terminate_worker(worker, result=1)

    while True:
        if worker_done.is_set():
            reported = (
                _reported_returncode(worker_frame[0], nonce) if worker_frame else None
            )
            result = reported if reported is not None else 70
            if os.name == "nt":
                return result
            return _terminate_worker(worker, result=result)

        if child_exited_without_reaping(worker):
            if os.name == "nt":
                returncode = worker.wait()
                return returncode if returncode != 0 else 70
            returncode = worker.returncode
            result = returncode if returncode not in (None, 0) else 70
            return _terminate_worker(worker, result=result)

        if parent_gone.wait(0.05):
            if os.name == "nt":
                # The parent owns the only Job handle. Its teardown contains the
                # worker tree; leave this supervisor promptly when its lease ends.
                os._exit(1)
            return _terminate_worker(worker, result=1)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
