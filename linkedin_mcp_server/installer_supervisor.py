"""Gate and supervise the managed browser installer process boundary."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
from typing import BinaryIO

if not __package__:
    # Production starts this file by absolute path under ``-I -S`` so that no
    # interpreter startup hook runs before the code below claims the control
    # pipes. ``site`` never ran, so the only import root is the one this file
    # was loaded from, which the parent picked next to its own module rather
    # than from anything the environment could redirect. Appended rather than
    # inserted: ``site`` would place the same directory after the standard
    # library, and going in front of it would let a sibling of this package
    # shadow a standard-library module.
    _PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _PACKAGE_ROOT not in sys.path:
        sys.path.append(_PACKAGE_ROOT)

from linkedin_mcp_server.process_protocol import (  # noqa: E402
    NONCE_LENGTH,
    new_nonce,
    read_authenticated_status,
    valid_nonce,
)
from linkedin_mcp_server.process_tree import (  # noqa: E402
    child_exited_without_reaping,
    terminate_process_group,
)

_ARMED = "armed"
_STARTED = "started"
_FINISHED = "finished"


def _parent_eof(reached: threading.Event) -> None:
    """Set *reached* when the process holding our stdin lease is gone."""
    try:
        os.read(sys.stdin.fileno(), 1)
    finally:
        reached.set()


def _worker_status(
    stream: BinaryIO, reached: threading.Event, frame: list[bytes], nonce: str
) -> None:
    marker = f"{_FINISHED} {nonce} ".encode("ascii")
    try:
        reported = read_authenticated_status(
            stream,
            marker=marker,
            parse=lambda candidate: _reported_returncode(candidate, nonce),
        )
        if reported is not None:
            frame.append(reported[0])
    finally:
        reached.set()


def _target_command(argv: list[str]) -> list[str]:
    args = argv[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    if not args:
        raise ValueError("The installer supervisor was started without a command")
    return args


def _read_control_frame(max_bytes: int) -> bytes | None:
    """Read one bounded control frame without buffering past its newline."""
    frame = bytearray()
    while len(frame) <= max_bytes:
        piece = os.read(sys.stdin.fileno(), 1)
        if not piece:
            return None
        frame.extend(piece)
        if piece == b"\n":
            return bytes(frame)
    return None


def _await_nonce() -> str | None:
    frame = _read_control_frame(NONCE_LENGTH + 1)
    if frame is None or not frame.endswith(b"\n"):
        return None
    try:
        nonce = frame[:-1].decode("ascii")
    except UnicodeDecodeError:
        return None
    return nonce if valid_nonce(nonce) else None


def _await_start(nonce: str) -> bool:
    """Read the authenticated launch gate without buffering past it."""
    expected = f"start {nonce}\n".encode("ascii")
    return _read_control_frame(len(expected)) == expected


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


def _write_frames_without_translation() -> None:
    """Stop Windows turning this channel's line endings into CRLF.

    The parent authenticates the armed frame by comparing exact bytes, and
    ``print`` through the default text layer writes ``os.linesep``. Measured on
    a Windows runner: a genuine frame arrived as CRLF, was refused, and the
    supervisor was reported as never becoming ready.
    """
    reconfigure = getattr(sys.stderr, "reconfigure", None)
    if reconfigure is None:
        return
    with contextlib.suppress(OSError, ValueError):
        reconfigure(newline="\n")


def main(argv: list[str] | None = None) -> int:
    """Wait until the parent owns our pipes, then start the leased worker."""
    argv = sys.argv if argv is None else argv

    _write_frames_without_translation()

    # The parent may be cancelled while asyncio is still constructing its
    # subprocess transport. No managed target exists until it has delivered the
    # post-Popen nonce and then opened the authenticated start gate, so transport
    # teardown can only end an empty supervisor.
    nonce = _await_nonce()
    if nonce is None:
        return 1
    try:
        print(f"{_ARMED} {nonce}", file=sys.stderr, flush=True)
    except OSError:
        return 1
    if not _await_start(nonce):
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
    worker_nonce = new_nonce()
    try:
        worker.stdin.write(f"{worker_nonce}\n".encode("ascii"))
        worker.stdin.flush()
    except OSError:
        if os.name == "nt":
            return 1
        return _terminate_worker(worker, result=1)

    worker_done = threading.Event()
    worker_frame: list[bytes] = []
    threading.Thread(
        target=_worker_status,
        args=(worker.stderr, worker_done, worker_frame, worker_nonce),
        name="installer-worker-status",
        daemon=True,
    ).start()

    if parent_gone.is_set():
        if os.name == "nt":
            return 1
        return _terminate_worker(worker, result=1)

    try:
        print(f"{_STARTED} {nonce} {worker.pid}", file=sys.stderr, flush=True)
    except OSError:
        if os.name == "nt":
            return 1
        return _terminate_worker(worker, result=1)

    while True:
        if worker_done.is_set():
            reported = (
                _reported_returncode(worker_frame[0], worker_nonce)
                if worker_frame
                else None
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
