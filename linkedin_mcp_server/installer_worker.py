"""Run Patchright inside the worker group owned by the supervisor."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from typing import NoReturn

from linkedin_mcp_server.process_protocol import NONCE_LENGTH, valid_nonce

_FINISHED = "finished"


def _supervisor_eof(reached: threading.Event) -> None:
    try:
        os.read(sys.stdin.fileno(), 1)
    finally:
        reached.set()


def _await_nonce() -> str | None:
    """Consume the status token without buffering past the stdin lease frame."""
    frame = bytearray()
    while len(frame) <= NONCE_LENGTH:
        piece = os.read(sys.stdin.fileno(), 1)
        if not piece:
            return None
        if piece == b"\n":
            try:
                nonce = frame.decode("ascii")
            except UnicodeDecodeError:
                return None
            return nonce if valid_nonce(nonce) else None
        frame.extend(piece)
    return None


def _command(argv: list[str]) -> list[str]:
    args = argv[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    if not args:
        raise ValueError("The installer worker was started without a command")
    return args


def _stop_without_supervisor(returncode: int) -> NoReturn:
    """End the installer group when the process owning our lease is gone."""
    if os.name != "nt":
        os.killpg(os.getpgrp(), signal.SIGKILL)
    os._exit(returncode)


def main(argv: list[str] | None = None) -> int:
    """Run Patchright while the supervisor owns our private control pipe."""
    argv = sys.argv if argv is None else argv
    nonce = _await_nonce()
    if nonce is None:
        return 1

    supervisor_gone = threading.Event()
    threading.Thread(
        target=_supervisor_eof,
        args=(supervisor_gone,),
        name="installer-supervisor-lease",
        daemon=True,
    ).start()
    if supervisor_gone.wait(0):
        return 1

    # The supervisor launched this worker as the process-group leader. Keeping
    # Patchright in that group lets the supervisor pin the PGID with this
    # worker's unreaped exit status before sending any numeric group signal.
    try:
        target = subprocess.Popen(
            _command(argv),
            # Never the lease. The thread above holds a blocking read on this
            # process's stdin, and on Windows the parent's stdin is an asyncio
            # named pipe whose child end is a synchronous handle: a target that
            # inherits it queues its own first operation behind that read, which
            # only ends when the parent lets go. Measured on a Windows runner,
            # the target process existed for the whole run and never executed a
            # line, writing no marker and no output. Withholding the handle
            # fixes it, and so does dropping the read; the target has no use for
            # a control channel either way.
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
            close_fds=True,
        )
    except OSError as exc:
        try:
            print(
                f"Patchright target could not start: {exc}",
                file=sys.stdout,
                flush=True,
            )
        except OSError:
            pass
        return 70
    if supervisor_gone.is_set():
        _stop_without_supervisor(1)

    while True:
        returncode = target.poll()
        if returncode is not None:
            if supervisor_gone.is_set():
                _stop_without_supervisor(returncode)
            try:
                print(
                    f"{_FINISHED} {nonce} {returncode}",
                    file=sys.stderr,
                    flush=True,
                )
            except OSError:
                _stop_without_supervisor(returncode)
            # Stay alive as the process-group identity until the supervisor has
            # killed and reaped the whole group. Its control pipe also closes if
            # it dies between receiving the status and doing that cleanup.
            supervisor_gone.wait()
            _stop_without_supervisor(returncode)
        if supervisor_gone.wait(0.05):
            _stop_without_supervisor(1)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
