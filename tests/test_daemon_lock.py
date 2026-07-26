"""The lock that decides which process owns the shared browser.

Mutual exclusion cannot be shown inside one interpreter, so the tests that
matter here spawn real processes and let the kernel arbitrate. The rest pin
behaviour that is easy to write correctly and just as easy to break later.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from linkedin_mcp_server.daemon_lock import (
    DaemonLock,
    DaemonLockError,
    daemon_is_running,
    daemon_lock_path,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_child(source: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run *source* in a separate interpreter, so the kernel decides."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        timeout=30,
    )


_TRY_ACQUIRE = """
    import sys
    from pathlib import Path
    from linkedin_mcp_server.daemon_lock import DaemonLock

    lock = DaemonLock(Path(sys.argv[1]))
    print("ACQUIRED" if lock.try_acquire() else "BUSY")
"""


class TestOwnership:
    def test_one_process_at_a_time(self, tmp_path: Path):
        # The whole point: a second process must not be able to start a second
        # browser against a profile the first one already owns.
        lock = DaemonLock(tmp_path)
        assert lock.try_acquire()

        result = _run_child(_TRY_ACQUIRE, str(tmp_path))

        assert result.stdout.strip() == "BUSY", result.stderr
        lock.release()

    def test_the_lock_frees_when_the_holder_exits(self, tmp_path: Path):
        # A crashed daemon must not lock everyone out forever. The kernel
        # releases the lock on death, which is why this uses a file lock rather
        # than a pid file that would need a liveness check nobody can do safely.
        first = _run_child(_TRY_ACQUIRE, str(tmp_path))
        assert first.stdout.strip() == "ACQUIRED", first.stderr

        second = _run_child(_TRY_ACQUIRE, str(tmp_path))

        assert second.stdout.strip() == "ACQUIRED", second.stderr

    def test_acquiring_twice_in_one_process_is_a_mistake(self, tmp_path: Path):
        # Not reference counted, unlike the profile lease. A second acquire is a
        # caller that believes it is the first, and the lock means nothing if
        # two parts of one process can each think they own the daemon.
        lock = DaemonLock(tmp_path)
        assert lock.try_acquire()

        with pytest.raises(DaemonLockError, match="already holds"):
            lock.try_acquire()

        lock.release()

    def test_releasing_without_holding_is_harmless(self, tmp_path: Path):
        # Cleanup paths call this without always knowing whether the acquire
        # got far enough, and raising there would mask the original failure.
        DaemonLock(tmp_path).release()


class TestScope:
    def test_the_lock_covers_the_whole_auth_root(self, tmp_path: Path):
        # Every profile under one auth root shares one browser lease, so the
        # election has to be just as wide. Scoped per profile, two owners would
        # both start and then compete forever for a lease only one can hold.
        assert daemon_lock_path(tmp_path / "profile").parent != tmp_path
        assert daemon_lock_path(tmp_path).parent == tmp_path

    def test_two_locks_on_one_root_are_the_same_lock(self, tmp_path: Path):
        lock = DaemonLock(tmp_path)
        assert lock.try_acquire()
        try:
            assert not DaemonLock(tmp_path).try_acquire()
        finally:
            lock.release()


class TestHandoff:
    def test_a_duplicate_survives_the_original_being_closed(self, tmp_path: Path):
        # How a supervisor is launched: it inherits a copy, and the process that
        # elected it lets go. If the lock did not survive that, another client
        # would see it free and elect a second owner against a live browser.
        lock = DaemonLock(tmp_path)
        assert lock.try_acquire()
        inherited = lock.inheritable_copy()
        lock.release()

        try:
            assert daemon_is_running(tmp_path)
        finally:
            os.close(inherited)

        assert not daemon_is_running(tmp_path)

    def test_the_copy_is_inheritable(self, tmp_path: Path):
        # The original is opened close-on-exec so a launched Chromium cannot
        # hold the lock open. That same flag would stop a supervisor inheriting
        # it, so the duplicate has to clear it explicitly.
        lock = DaemonLock(tmp_path)
        assert lock.try_acquire()
        try:
            inherited = lock.inheritable_copy()
            try:
                assert os.get_inheritable(inherited)
            finally:
                os.close(inherited)
        finally:
            lock.release()

    def test_handing_over_a_lock_we_do_not_hold_refuses(self, tmp_path: Path):
        with pytest.raises(DaemonLockError, match="does not hold"):
            DaemonLock(tmp_path).inheritable_copy()

    def test_an_adopted_lock_is_held_without_reacquiring(self, tmp_path: Path):
        # A supervisor is launched already holding a copy. Acquiring again would
        # fail on POSIX, where the process already holds it, so adoption records
        # ownership rather than taking it.
        original = DaemonLock(tmp_path)
        assert original.try_acquire()
        inherited = original.inheritable_copy()
        original.release()

        supervisor = DaemonLock(tmp_path)
        supervisor.adopt(inherited)
        try:
            assert supervisor.held
            assert daemon_is_running(tmp_path)
        finally:
            supervisor.release()

        assert not daemon_is_running(tmp_path)


class TestFork:
    @pytest.mark.skipif(
        not hasattr(os, "fork"), reason="fork does not exist on Windows"
    )
    def test_a_child_that_never_touches_the_lock_does_not_hold_it(self, tmp_path: Path):
        # Measured before the fix: an untouched fork child kept the lock held
        # after the owner released, so every other process saw a daemon that
        # was no longer there and none could elect a replacement. Checking
        # inside the lock's own methods cannot catch this, because the child
        # never calls one.
        lock = DaemonLock(tmp_path)
        assert lock.try_acquire()

        # The child reports what it inherited rather than sleeping, so the
        # assertion below cannot run before the child has finished discarding
        # it. Timing the two against each other would make this flaky in
        # whichever direction the scheduler happened to go.
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - runs in the forked child
            os.close(read_fd)
            os.write(write_fd, b"x")
            os.close(write_fd)
            time.sleep(2)
            os._exit(0)

        os.close(write_fd)
        try:
            os.read(read_fd, 1)
            lock.release()

            assert not daemon_is_running(tmp_path)
        finally:
            os.close(read_fd)
            os.waitpid(pid, 0)


class TestReleaseSemantics:
    def test_releasing_closes_rather_than_unlocks(self, tmp_path: Path):
        # The measured trap. flock belongs to the open file description, which
        # every inherited copy shares, so unlocking would release the lock for
        # the supervisor too. Closing drops only this descriptor.
        lock = DaemonLock(tmp_path)
        assert lock.try_acquire()
        inherited = lock.inheritable_copy()

        lock.release()

        try:
            # Still held, through the copy. An unlock-then-close release would
            # have freed it here and let a second owner start.
            assert daemon_is_running(tmp_path)
        finally:
            os.close(inherited)


class TestLiveness:
    def test_no_lock_file_means_no_daemon(self, tmp_path: Path):
        assert not daemon_is_running(tmp_path)

    def test_probing_does_not_disturb_the_holder(self, tmp_path: Path):
        # Discovery calls this on every cold start, so it must not briefly
        # exclude a supervisor that is starting up.
        lock = DaemonLock(tmp_path)
        assert lock.try_acquire()
        try:
            assert daemon_is_running(tmp_path)
            assert daemon_is_running(tmp_path)
            assert lock.held
        finally:
            lock.release()

    def test_a_stale_lock_file_does_not_look_alive(self, tmp_path: Path):
        # The file is never unlinked, so it outlives the daemon that made it.
        # Its presence must therefore mean nothing on its own: what counts is
        # whether the lock inside it can be taken.
        lock = DaemonLock(tmp_path)
        assert lock.try_acquire()
        lock.release()

        assert daemon_lock_path(tmp_path).exists()
        assert not daemon_is_running(tmp_path)
