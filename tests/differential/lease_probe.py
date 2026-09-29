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
(:func:`run_probe`), so no overlay reaches it. It is a checkpoint, not
continuous telemetry of the lock.

The helper is owned from before it exists until it is reaped, through its own
``Popen`` and never by its number. Whatever ends :func:`run_probe` (an answer,
the timeout, a failed collection, an interrupt) kills and reaps it within a
bound; one that outlives the bound stays owned, and :func:`settle` refuses
every later probe with :class:`UnsettledHelper` until it is gone.

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


class UnsettledHelper(RuntimeError):
    """A helper this process started outlived its kill: nothing more is asked."""


# Every helper started and not yet reaped, each with its pipes. Only a reaped
# helper leaves; a helper that will not die stays here, not forgotten.
_OWNED: list[subprocess.Popen] = []


class _Helper(subprocess.Popen):
    """A ``Popen`` owned before its child exists.

    Registering first leaves no moment in which a started child has no owner:
    not a failure inside ``Popen`` after the fork, and not an interrupt between
    its return and the caller's assignment.
    """

    def __init__(self, *args, **kwargs):
        _OWNED.append(self)
        super().__init__(*args, **kwargs)


def _finish(process: subprocess.Popen, grace: float) -> bool:
    """Kill *process* if it still runs and reap it; False if *grace* ran out."""
    if getattr(process, "pid", None) is not None:
        # Popen polls before it signals: a child it has reaped is never
        # signalled, and one nobody has reaped still holds its number.
        process.kill()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            return False
    for pipe in (getattr(process, "stdout", None), getattr(process, "stderr", None)):
        if pipe is not None:
            pipe.close()
    return True


def settle(grace: float = 5.0) -> None:
    """Reap every owned helper, or raise :class:`UnsettledHelper`.

    Nothing may be probed or measured while one is left: its lock question may
    still be in flight. :func:`run_probe` asks this first.
    """
    for process in list(_OWNED):
        if _finish(process, grace):
            _OWNED.remove(process)
    if _OWNED:
        raise UnsettledHelper(
            f"helper(s) {[process.pid for process in _OWNED]} outlived their "
            f"kill by {grace}s; they stay owned, and nothing more is asked until "
            f"settle() reaps them"
        )


def _settle_after(failure: BaseException, grace: float) -> bool:
    """Settle after *failure* without replacing it; note on it what is left."""
    try:
        settle(grace)
    except BaseException as problem:
        failure.add_note(f"and the helper was not settled: {problem!r}")
        return False
    return True


def run_probe(
    path: str,
    *,
    python: str = sys.executable,
    timeout: float = 10.0,
    grace: float = 5.0,
) -> dict:
    """Ask from a fresh process with no site processing; see the module docstring.

    Returns only once the helper is reaped. A failure while starting or
    collecting it propagates unchanged after the cleanup, which only adds a
    note if the helper is left owned.
    """
    settle(grace)
    command = [python, "-I", "-S", os.path.abspath(__file__), path]
    try:
        process = _Helper(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            close_fds=True,
        )
    except BaseException as failure:
        if _settle_after(failure, grace) and isinstance(failure, OSError):
            return _answer(UNKNOWN, f"the helper could not start: {failure!r}")
        raise
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        settle(grace)
        return _answer(
            UNKNOWN, f"the helper did not answer within {timeout}s and was reaped"
        )
    except BaseException as failure:
        _settle_after(failure, grace)
        raise
    settle(grace)
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
