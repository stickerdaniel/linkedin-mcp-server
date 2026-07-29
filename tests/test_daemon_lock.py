"""The lock that decides which process owns the shared browser.

Mutual exclusion cannot be shown inside one interpreter, so the tests that
matter here spawn real processes and let the kernel arbitrate. The rest pin
behaviour that is easy to write correctly and just as easy to break later.
"""

from __future__ import annotations

import os
import stat
import subprocess
import threading
import sys
import textwrap
import time
from pathlib import Path

import pytest

import linkedin_mcp_server.daemon_descriptor as daemon_descriptor_module
import linkedin_mcp_server.daemon_lock as daemon_lock_module
from linkedin_mcp_server.daemon_descriptor import daemon_dir, daemon_state_root
from linkedin_mcp_server.daemon_lock import (
    DaemonLock,
    DaemonLockError,
    daemon_is_running,
    daemon_lock_path,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_daemon_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # The child interpreters inherit HOME and patch the account lookup below, so
    # every process contends on the same state without touching the real user.
    monkeypatch.setattr(daemon_descriptor_module, "_account_home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))


def _run_child(source: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run *source* in a separate interpreter, so the kernel decides."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        # The child's working directory outranks PYTHONPATH on its import path,
        # so running the suite from another checkout would have these children
        # mix modules from both. Pinning it keeps them on the tree under test.
        cwd=_REPO_ROOT,
        timeout=30,
    )


_TRY_ACQUIRE = """
    import os
    import sys
    from pathlib import Path
    import linkedin_mcp_server.daemon_descriptor as daemon_descriptor
    from linkedin_mcp_server.daemon_lock import DaemonLock

    daemon_descriptor._account_home = lambda: Path(os.environ["HOME"])
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
    def test_the_lock_lives_in_private_daemon_state(self, tmp_path: Path):
        # The path remains keyed by the auth root, so every profile under that
        # root shares one election, while different roots cannot collide.
        first = daemon_lock_path(tmp_path)
        second = daemon_lock_path(tmp_path / "other-root")

        assert first.parent == daemon_dir(tmp_path)
        assert first.parent.parent == daemon_state_root()
        assert second.parent.parent == daemon_state_root()
        assert first != second

    @pytest.mark.skipif(
        os.name == "nt", reason="POSIX permission bits do not exist on Windows"
    )
    def test_acquiring_does_not_harden_the_configured_auth_root(self, tmp_path: Path):
        # Measured before the fix: acquiring changed any existing auth root from
        # 0755 to 0700. A custom profile directly under /tmp would try to chmod
        # /tmp itself; under a home directory it would silently change the home.
        auth_root = tmp_path / "shared-parent"
        auth_root.mkdir(mode=0o755)
        auth_root.chmod(0o755)
        lock = DaemonLock(auth_root)

        assert lock.try_acquire()
        try:
            assert stat.S_IMODE(auth_root.stat().st_mode) == 0o755
            assert not (auth_root / "daemon").exists()
            assert stat.S_IMODE(daemon_state_root().stat().st_mode) == 0o700
            assert stat.S_IMODE(daemon_dir(auth_root).stat().st_mode) == 0o700
        finally:
            lock.release()

    def test_a_fresh_auth_root_keeps_one_lock_identity(self, tmp_path: Path):
        # Measured before the fix: the first wrapper cached a path-derived key,
        # then acquisition created the auth root and every later lookup used its
        # inode. The live lock appeared free and a second owner was elected.
        auth_root = tmp_path / "fresh" / ".linkedin-mcp"
        first = DaemonLock(auth_root)

        assert first.try_acquire()
        try:
            assert daemon_is_running(auth_root)
            second = DaemonLock(auth_root)
            assert not second.try_acquire()
            second.release()
        finally:
            first.release()

    @pytest.mark.skipif(
        os.name == "nt", reason="creating directory symlinks needs extra privileges"
    )
    def test_an_auth_root_symlink_cannot_split_the_election(self, tmp_path: Path):
        # Measured before the fix: the default auth root also held daemon state,
        # so retargeting its daemon link let another owner lock a second inode.
        auth_root = tmp_path / ".linkedin-mcp"
        first_target = tmp_path / "first-target"
        second_target = tmp_path / "second-target"
        auth_root.mkdir()
        first_target.mkdir()
        second_target.mkdir()
        planted = auth_root / "daemon"
        planted.symlink_to(first_target, target_is_directory=True)

        first = DaemonLock(auth_root)
        assert first.try_acquire()
        planted.unlink()
        planted.symlink_to(second_target, target_is_directory=True)

        second = DaemonLock(auth_root)
        try:
            assert not second.try_acquire()
            assert not (first_target / "daemon.lock").exists()
            assert not (second_target / "daemon.lock").exists()
        finally:
            second.release()
            first.release()

    def test_two_locks_on_one_root_are_the_same_lock(self, tmp_path: Path):
        lock = DaemonLock(tmp_path)
        assert lock.try_acquire()
        try:
            assert not DaemonLock(tmp_path).try_acquire()
        finally:
            lock.release()


posix_handoff = pytest.mark.skipif(
    os.name == "nt",
    reason="Measured on Windows: an inherited handle does not carry the lock",
)


class TestHandoff:
    @posix_handoff
    def test_a_duplicate_survives_the_original_being_closed(self, tmp_path: Path):
        # How an owner is launched: it inherits a copy, and the process that
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

    @posix_handoff
    def test_the_copy_is_inheritable(self, tmp_path: Path):
        # The original is opened close-on-exec so a launched Chromium cannot
        # hold the lock open. That same flag would stop an owner inheriting
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

    @posix_handoff
    def test_handing_over_a_lock_we_do_not_hold_refuses(self, tmp_path: Path):
        with pytest.raises(DaemonLockError, match="does not hold"):
            DaemonLock(tmp_path).inheritable_copy()

    @posix_handoff
    def test_a_descriptor_for_another_auth_root_is_not_adopted(self, tmp_path: Path):
        # Measured before the check: it was adopted without complaint, so this
        # process reported it owned a browser nobody had elected it for, while
        # another process took the real lock unopposed.
        other = tmp_path / "other"
        other.mkdir()
        mine = tmp_path / "mine"
        mine.mkdir()
        elsewhere = DaemonLock(other)
        assert elsewhere.try_acquire()
        inherited = elsewhere.inheritable_copy()
        elsewhere.release()
        # Give this auth root a lock file of its own, so the refusal has to come
        # from comparing the two rather than from one of them being absent.
        own = DaemonLock(mine)
        assert own.try_acquire()
        own.release()

        try:
            with pytest.raises(DaemonLockError, match="not this auth root's"):
                DaemonLock(mine).adopt(inherited)
        finally:
            os.close(inherited)

    @posix_handoff
    def test_a_lock_file_replaced_mid_adoption_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The descriptor is compared against the path before the lock is held,
        # so an unlink and recreate in between leaves adoption holding an
        # orphaned inode while the live path sits free. Measured before the
        # check: adopter and contender each reported ownership, on two inodes.
        seed = DaemonLock(tmp_path)
        assert seed.try_acquire()
        inherited = seed.inheritable_copy()
        seed.release()
        path = daemon_lock_path(tmp_path)

        real = daemon_lock_module.try_lock

        def replace_then_lock(fd: int, *, exclusive: bool) -> bool:
            monkeypatch.setattr(daemon_lock_module, "try_lock", real)
            path.unlink()
            path.touch()
            return real(fd, exclusive=exclusive)

        monkeypatch.setattr(daemon_lock_module, "try_lock", replace_then_lock)

        try:
            with pytest.raises(DaemonLockError, match="was replaced"):
                DaemonLock(tmp_path).adopt(inherited)
        finally:
            os.close(inherited)

    @posix_handoff
    def test_a_lock_file_renamed_mid_adoption_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A rename keeps the inode's one link, so a link count still reads as
        # one while the name now refers to something else. Measured with a
        # count-based check: the rename passed and a contender locked the live
        # path alongside the adopter.
        seed = DaemonLock(tmp_path)
        assert seed.try_acquire()
        inherited = seed.inheritable_copy()
        seed.release()
        path = daemon_lock_path(tmp_path)
        real = daemon_lock_module.try_lock

        def rename_then_lock(fd: int, *, exclusive: bool) -> bool:
            monkeypatch.setattr(daemon_lock_module, "try_lock", real)
            path.rename(path.parent / "moved")
            path.touch()
            return real(fd, exclusive=exclusive)

        monkeypatch.setattr(daemon_lock_module, "try_lock", rename_then_lock)

        try:
            with pytest.raises(DaemonLockError, match="was replaced"):
                DaemonLock(tmp_path).adopt(inherited)
        finally:
            os.close(inherited)

    @posix_handoff
    def test_a_failed_handover_does_not_leak_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The duplicate already shares the kernel lock, so leaking it holds the
        # lock for this process's whole life: release() closes the original and
        # the lock survives in the copy. Measured before the fix: the daemon
        # still read as running after release and nobody could take over.
        lock = DaemonLock(tmp_path)
        assert lock.try_acquire()

        def refuse(*args: object, **kwargs: object) -> None:
            raise OSError("cannot mark inheritable")

        monkeypatch.setattr(os, "set_inheritable", refuse)
        with pytest.raises(OSError):
            lock.inheritable_copy()
        monkeypatch.undo()

        lock.release()

        successor = DaemonLock(tmp_path)
        assert successor.try_acquire(), "the duplicate leaked and kept the lock"
        successor.release()

    @posix_handoff
    def test_an_adoption_that_cannot_complete_gives_the_lock_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # By the time inheritance is cleared the lock is held through this
        # descriptor, and nothing has recorded that yet. Measured before the
        # cleanup: the failure left it held with no object able to release it,
        # and only a manual close freed it.
        elector = DaemonLock(tmp_path)
        assert elector.try_acquire()
        inherited = elector.inheritable_copy()
        elector.release()

        def refuse(*args: object, **kwargs: object) -> None:
            raise OSError("cannot clear inheritance")

        monkeypatch.setattr(os, "set_inheritable", refuse)
        with pytest.raises(DaemonLockError, match="could not be taken over"):
            DaemonLock(tmp_path).adopt(inherited)
        monkeypatch.undo()

        successor = DaemonLock(tmp_path)
        assert successor.try_acquire(), "the lock was left held by nobody"
        successor.release()

    @posix_handoff
    def test_adopting_a_descriptor_that_holds_no_lock_still_excludes(
        self, tmp_path: Path
    ):
        # An owner launched with a descriptor that was never locked, or
        # whose lock was already released. Measured before the fix: it reported
        # ownership while daemon_is_running said no daemon was there and a
        # contender took the lock alongside it. Adoption now asks for the lock,
        # which is granted against our own open file description when we already
        # hold it and takes it when it is free. Either way exactly one holder.
        creator = DaemonLock(tmp_path)
        assert creator.try_acquire()
        creator.release()
        unlocked = os.open(daemon_lock_path(tmp_path), os.O_RDWR)

        adopter = DaemonLock(tmp_path)
        adopter.adopt(unlocked)
        try:
            assert adopter.held
            assert daemon_is_running(tmp_path)
            contender = DaemonLock(tmp_path)
            assert not contender.try_acquire()
        finally:
            adopter.release()

    @posix_handoff
    def test_an_adopted_lock_is_held_without_reacquiring(self, tmp_path: Path):
        # An owner is launched already holding a copy. Acquiring again would
        # fail on POSIX, where the process already holds it, so adoption records
        # ownership rather than taking it.
        original = DaemonLock(tmp_path)
        assert original.try_acquire()
        inherited = original.inheritable_copy()
        original.release()

        owner = DaemonLock(tmp_path)
        owner.adopt(inherited)
        try:
            assert owner.held
            assert daemon_is_running(tmp_path)
        finally:
            owner.release()

        assert not daemon_is_running(tmp_path)


class TestRegistry:
    def test_a_held_lock_survives_losing_its_last_reference(self, tmp_path: Path):
        # Measured before the fix: the registry was weak throughout, so a lock
        # acquired and then dropped left the descriptor open with nothing left
        # to close it. Another process could not take the lock, and no object
        # remained through which to release it or clean it up after a fork.
        import gc

        from linkedin_mcp_server.daemon_lock import _held_locks

        DaemonLock(tmp_path).try_acquire()
        gc.collect()

        assert daemon_is_running(tmp_path)
        held = [
            lock for lock in _held_locks if lock.path.parent == daemon_dir(tmp_path)
        ]
        assert held, "a held lock must stay reachable"

        for lock in held:
            lock.release()
        assert not daemon_is_running(tmp_path)

    def test_an_unheld_lock_is_not_kept_alive(self, tmp_path: Path):
        # Only held locks are worth pinning. One that was built and never used
        # owns nothing, so keeping it would be a leak for no benefit.
        import gc

        from linkedin_mcp_server.daemon_lock import _held_locks

        DaemonLock(tmp_path)
        gc.collect()

        assert not [lock for lock in _held_locks if lock.path.parent == tmp_path]


class TestPlatformDifference:
    @pytest.mark.skipif(
        os.name != "nt", reason="the refusal only applies where handoff cannot work"
    )
    def test_windows_refuses_to_hand_a_lock_over(self, tmp_path: Path):
        # Measured on a Windows runner: a child holding the inherited handle did
        # not hold the lock once the parent had closed its handles and exited,
        # in 20 of 20 runs, while a third process took the byte range.
        # Returning a descriptor that looks like a transferred lock and is not
        # would put a second owner on a live browser, so it refuses instead.
        lock = DaemonLock(tmp_path)
        assert lock.try_acquire()
        try:
            with pytest.raises(DaemonLockError, match="POSIX mechanism"):
                lock.inheritable_copy()
        finally:
            lock.release()


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


class TestAdoptedDescriptors:
    @posix_handoff
    def test_an_adopted_lock_does_not_leak_to_later_children(self, tmp_path: Path):
        # The descriptor is marked inheritable for exactly one launch. Measured
        # before the fix: it stayed that way, so an unrelated child launched
        # afterwards inherited the lock and kept it held after the owner exited,
        # leaving every client looking at a daemon that was no longer there.
        original = DaemonLock(tmp_path)
        assert original.try_acquire()
        inherited = original.inheritable_copy()
        original.release()

        adopter = DaemonLock(tmp_path)
        adopter.adopt(inherited)

        # Launched the way a browser would be, inheriting open descriptors.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(3)"], close_fds=False
        )
        try:
            adopter.release()

            # Asked by electing rather than by probing. daemon_is_running takes
            # a lock to answer, so under a loaded parallel test run it competes
            # with whatever else is touching this file and can report a holder
            # that is really another probe. Election is the question that
            # matters anyway: if the child still held the lock, this would fail.
            successor = DaemonLock(tmp_path)
            assert successor.try_acquire(), "the child kept the lock alive"
            successor.release()
        finally:
            child.kill()
            child.wait()


class TestReleaseSemantics:
    @posix_handoff
    def test_releasing_closes_rather_than_unlocks(self, tmp_path: Path):
        # The measured trap. flock belongs to the open file description, which
        # every inherited copy shares, so unlocking would release the lock for
        # the owner too. Closing drops only this descriptor.
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
        # exclude an owner that is starting up.
        lock = DaemonLock(tmp_path)
        assert lock.try_acquire()
        try:
            assert daemon_is_running(tmp_path)
            assert daemon_is_running(tmp_path)
            assert lock.held
        finally:
            lock.release()

    def test_concurrent_probes_do_not_see_each_other(self, tmp_path: Path):
        # The half of the trade the shared probe buys. Measured with an
        # exclusive probe: 43 of 400 concurrent probes reported a daemon that
        # did not exist, because one briefly held the lock and the other read
        # its own sibling as an owner. Inventing an owner is the unrecoverable
        # direction, which is why shared is preferred despite the cost pinned
        # in the test below.
        creator = DaemonLock(tmp_path)
        assert creator.try_acquire()
        creator.release()

        answers: list[bool] = []
        start = threading.Barrier(2)

        def probe() -> None:
            start.wait()
            answers.extend(daemon_is_running(tmp_path) for _ in range(200))

        threads = [threading.Thread(target=probe) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert answers and not any(answers)

    def test_the_probe_is_an_observation_not_a_decision(self, tmp_path: Path):
        # The other half, and the reason the docstring calls this a hint. There
        # is no way to ask flock whether a lock is held without taking one, so a
        # probe contends with a concurrent election: measured, 11 of 3000
        # elections failed against a probe while no owner existed. Ownership is
        # therefore decided by try_acquire alone, never by asking this first.
        creator = DaemonLock(tmp_path)
        assert creator.try_acquire()
        creator.release()

        # An election is never wrong when nothing competes with it, which is
        # the property callers may rely on.
        for _ in range(50):
            lock = DaemonLock(tmp_path)
            assert lock.try_acquire()
            lock.release()

        # And the probe agrees with a settled state in both directions.
        assert not daemon_is_running(tmp_path)
        owner = DaemonLock(tmp_path)
        assert owner.try_acquire()
        try:
            assert daemon_is_running(tmp_path)
        finally:
            owner.release()

    def test_a_stale_lock_file_does_not_look_alive(self, tmp_path: Path):
        # The file is never unlinked, so it outlives the daemon that made it.
        # Its presence must therefore mean nothing on its own: what counts is
        # whether the lock inside it can be taken.
        lock = DaemonLock(tmp_path)
        assert lock.try_acquire()
        lock.release()

        assert daemon_lock_path(tmp_path).exists()
        assert not daemon_is_running(tmp_path)
