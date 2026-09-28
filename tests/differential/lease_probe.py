"""Whether an auth root's ``profile.lock`` is held, asked without announcing.

A contender that tells nobody: it opens the *existing* lock file afresh (never
creating, truncating, replacing or unlinking it, and never touching
``profile.handoff``, whose shared lock is how a waiter announces itself), checks
that the path and the open file are one regular file on one device and inode
before and after, and asks for the same kernel lock the product takes
(``profile_lease.try_lock``: ``flock`` exclusive, non-blocking).

* ``free``: granted, and released at once;
* ``held``: refused with ``EAGAIN`` or ``EWOULDBLOCK``, the kernel's word for a
  lock someone else's open file holds;
* ``unknown``: anything else, including a missing, linked or replaced file, a
  failed open and every other errno. A failure is never contention.

``flock`` belongs to open file descriptions, so the fresh open is what makes
this an independent contender: a descriptor inherited from the holder would
be the holder. The helper runs as its own process with no site processing
(:func:`run_probe`), so no overlay reaches it, and it is always reaped. It is
a checkpoint, not continuous telemetry of the lock.

POSIX only; elsewhere every answer is ``unknown``.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys

HELD = "held"
FREE = "free"
UNKNOWN = "unknown"

_CONTENTION = frozenset({errno.EAGAIN, errno.EWOULDBLOCK})


def _answer(state: str, reason: str, identity=None) -> dict:
    device, inode = identity if identity is not None else (None, None)
    return {"state": state, "reason": reason, "device": device, "inode": inode}


def probe(path: str) -> dict:
    """Ask once whether the lock at *path* is held; see the module docstring."""
    try:
        import fcntl
    except ImportError:
        return _answer(UNKNOWN, "no flock on this platform")
    try:
        before = os.lstat(path)
    except OSError as exc:
        return _answer(UNKNOWN, f"the lock file cannot be examined: {exc!r}")
    if not stat.S_ISREG(before.st_mode):
        return _answer(UNKNOWN, "the lock path is not a regular file")
    identity = (before.st_dev, before.st_ino)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        return _answer(UNKNOWN, f"the lock file cannot be opened: {exc!r}", identity)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != identity:
            return _answer(
                UNKNOWN, "the lock file was replaced while opening", identity
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _CONTENTION:
                state, reason = HELD, "another open file holds the lock"
            else:
                state, reason = UNKNOWN, f"the lock could not be asked: {exc!r}"
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            state, reason = FREE, "granted and released at once"
        try:
            after = os.lstat(path)
        except OSError as exc:
            return _answer(UNKNOWN, f"the lock file went away: {exc!r}", identity)
        if (after.st_dev, after.st_ino) != identity:
            return _answer(UNKNOWN, "the lock file was replaced while asking", identity)
        return _answer(state, reason, identity)
    finally:
        os.close(descriptor)


def run_probe(
    path: str, *, python: str = sys.executable, timeout: float = 10.0
) -> dict:
    """Ask from a fresh process with no site processing, and always reap it."""
    command = [python, "-I", "-S", os.path.abspath(__file__), path]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            close_fds=True,
        )
    except OSError as exc:
        return _answer(UNKNOWN, f"the helper could not start: {exc!r}")
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return _answer(UNKNOWN, f"the helper did not answer within {timeout}s")
    if process.returncode != 0:
        return _answer(
            UNKNOWN, f"the helper failed ({process.returncode}): {err[-500:]}"
        )
    try:
        answer = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return _answer(UNKNOWN, "the helper's answer is unreadable")
    if not isinstance(answer, dict) or answer.get("state") not in (HELD, FREE, UNKNOWN):
        return _answer(UNKNOWN, "the helper's answer is not one of its three")
    return answer


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: lease_probe.py PATH", file=sys.stderr)
        return 2
    print(json.dumps(probe(argv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
