"""Hold a target behind a parent-controlled release frame."""

from __future__ import annotations

import os
import subprocess
import sys

_PREFIX = b"release "
_NONCE_BYTES = 32
_NONCE_LENGTH = _NONCE_BYTES * 2
_MAX_FRAME = len(_PREFIX) + _NONCE_LENGTH + 1


def _arguments(argv: list[str]) -> tuple[str, list[str]]:
    if len(argv) < 4 or argv[2] != "--":
        raise ValueError("The process gate was started without a target command")
    nonce = argv[1]
    try:
        decoded = bytes.fromhex(nonce)
    except ValueError as exc:
        raise ValueError("The process gate received an invalid release nonce") from exc
    if len(nonce) != _NONCE_LENGTH or len(decoded) != _NONCE_BYTES:
        raise ValueError("The process gate received an invalid release nonce")
    return nonce, argv[3:]


def _await_release(nonce: str) -> bool:
    """Read one bounded frame without consuming target stdin behind it."""
    expected = _PREFIX + nonce.encode("ascii") + b"\n"
    frame = bytearray()
    while len(frame) < _MAX_FRAME:
        piece = os.read(0, 1)
        if not piece:
            return False
        frame.extend(piece)
        if piece == b"\n":
            return bytes(frame) == expected
    return False


def main(argv: list[str] | None = None) -> int:
    """Release the exact target command and mirror its exit status."""
    nonce, command = _arguments(sys.argv if argv is None else argv)
    if not _await_release(nonce):
        return 70
    try:
        target = subprocess.Popen(
            command,
            stdin=0,
            stdout=1,
            stderr=2,
            close_fds=True,
        )
    except OSError:
        return 70
    return target.wait()


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
