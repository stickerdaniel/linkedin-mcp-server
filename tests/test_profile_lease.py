"""Tests for cross-process ownership of the shared browser profile.

The interesting cases here need real operating-system processes. A single-loop
asyncio test cannot reproduce two server processes opening one Chromium profile,
which is the failure the lease exists to prevent, so the multi-process cases
spawn ``tests/helpers/profile_lease_worker.py`` with ``subprocess``.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from linkedin_mcp_server.profile_lease import (
    ProfileLease,
    ProfileLeaseUnavailableError,
    get_profile_lease,
)

_WORKER = Path(__file__).parent / "helpers" / "profile_lease_worker.py"

# Windows opens files without FILE_SHARE_DELETE, so a path cannot be unlinked
# while a descriptor on it is open. The inode race these tests provoke is
# therefore unreachable there, and the guard against it is POSIX-only.
_posix_unlink_race = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows refuses to unlink a file that is still open",
)

# The forked child inherits the parent's descriptor, which has no Windows
# equivalent: CreateProcess starts from nothing.
_posix_fork = pytest.mark.skipif(
    not hasattr(os, "fork"), reason="fork is unavailable on this platform"
)


def _spawn(*args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(_WORKER), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _await_line(process: subprocess.Popen[str], expected: str) -> None:
    """Block until *process* prints a line containing *expected*.

    Fails rather than hangs when the worker dies early: ``readline`` returns
    ``""`` forever at EOF, so a loop that only checked its content would spin
    until the CI job's own timeout with no explanation.
    """
    assert process.stdout is not None
    while True:
        line = process.stdout.readline()
        if expected in line:
            return
        if line == "":  # EOF: the worker will never say anything more
            break
    stderr = process.stderr.read() if process.stderr else ""
    raise AssertionError(
        f"worker exited (status {process.poll()}) without reporting "
        f"{expected!r}: {stderr}"
    )


class TestSingleProcess:
    def test_acquire_and_release(self, tmp_path: Path) -> None:
        lease = ProfileLease(tmp_path)
        assert lease.try_acquire()
        assert lease.held
        lease.release()
        assert not lease.held

    def test_reentrant_acquisition_does_not_self_deadlock(self, tmp_path: Path) -> None:
        """flock conflicts with itself across two open descriptions.

        The middleware acquires and then browser creation acquires again, so a
        naive implementation that opened the path twice would deadlock the
        process against itself on the very first tool call.
        """
        lease = ProfileLease(tmp_path)
        assert lease.try_acquire()
        assert lease.try_acquire()
        assert lease.try_acquire()

        lease.release()
        assert lease.held, "released too early: outer references still hold it"
        lease.release()
        assert lease.held
        lease.release()
        assert not lease.held

    def test_hold_context_manager_releases(self, tmp_path: Path) -> None:
        lease = ProfileLease(tmp_path)
        with lease.hold():
            assert lease.held
        assert not lease.held

    def test_a_hold_released_twice_drops_only_one_reference(
        self, tmp_path: Path
    ) -> None:
        """Explicit release plus context-manager exit must not double-count.

        Otherwise a handle released early would take a *second* caller's
        reference with it on the way out, freeing the profile while that caller
        still has a browser open on it.
        """
        lease = ProfileLease(tmp_path)
        outer = lease.hold()  # someone else's reference
        handle = lease.hold()

        handle.release()
        handle.release()  # idempotent, not a second decrement
        with handle:
            pass

        assert lease.held, "an extra release dropped another caller's reference"
        outer.release()
        assert not lease.held

    def test_hold_raises_when_another_process_owns_it(self, tmp_path: Path) -> None:
        holder = _spawn("hold", str(tmp_path), "10")
        try:
            _await_line(holder, "HELD")
            lease = ProfileLease(tmp_path)
            with pytest.raises(ProfileLeaseUnavailableError):
                lease.hold()
        finally:
            holder.kill()
            holder.wait(timeout=10)

    @_posix_unlink_race
    def test_a_renamed_lock_file_is_not_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rename keeps the link count, so counting links cannot see it.

        The inode holds its one link while the name comes to mean a different
        file, so a count-based check passes and two processes end up locking
        two inodes for one path. Comparing identity is what catches it.
        """
        from linkedin_mcp_server import profile_lease

        path = tmp_path / "profile.lock"
        real = profile_lease.try_lock

        def rename_then_lock(fd: int, *, exclusive: bool) -> bool:
            monkeypatch.setattr(profile_lease, "try_lock", real)
            path.rename(tmp_path / "moved")
            path.touch()
            return real(fd, exclusive=exclusive)

        monkeypatch.setattr(profile_lease, "try_lock", rename_then_lock)

        descriptor = profile_lease.acquire_locked_fd(path, exclusive=True)
        assert descriptor is not None
        try:
            assert os.fstat(descriptor).st_ino == path.stat().st_ino
        finally:
            os.close(descriptor)

    @_posix_unlink_race
    def test_contention_on_a_stale_inode_retries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Being refused the old file says nothing about the current one.

        Contention answers only about the file that was opened. If the path has
        moved on since, the holder holds something nobody will consult, and
        reporting busy refuses a lock that is free. Measured before the fix:
        None came back while the live path could be locked immediately.
        """
        from linkedin_mcp_server import profile_lease

        path = tmp_path / "profile.lock"
        holder = profile_lease.open_lock_file(path)
        assert profile_lease.try_lock(holder, exclusive=True)
        real = profile_lease.try_lock

        def rename_then_lock(fd: int, *, exclusive: bool) -> bool:
            monkeypatch.setattr(profile_lease, "try_lock", real)
            path.rename(tmp_path / "moved")
            path.touch()
            return real(fd, exclusive=exclusive)

        monkeypatch.setattr(profile_lease, "try_lock", rename_then_lock)

        try:
            descriptor = profile_lease.acquire_locked_fd(path, exclusive=True)
            assert descriptor is not None, "refused a lock nobody was holding"
            os.close(descriptor)
        finally:
            os.close(holder)

    def test_lock_file_is_never_unlinked(self, tmp_path: Path) -> None:
        """Unlinking on release splits contenders across inodes.

        The pinned filelock 3.29.1 does exactly that and loses mutual exclusion;
        this asserts we do not repeat it.
        """
        lease = ProfileLease(tmp_path)
        lease.try_acquire()
        inode = (tmp_path / "profile.lock").stat().st_ino
        lease.release()

        assert (tmp_path / "profile.lock").exists()
        lease.try_acquire()
        assert (tmp_path / "profile.lock").stat().st_ino == inode
        lease.release()

    def test_held_seconds_tracks_ownership(self, tmp_path: Path) -> None:
        lease = ProfileLease(tmp_path)
        assert lease.held_seconds == 0.0
        lease.try_acquire()
        time.sleep(0.05)
        assert lease.held_seconds >= 0.05
        lease.release()
        assert lease.held_seconds == 0.0


class TestHandoff:
    def test_no_waiter_means_no_handoff(self, tmp_path: Path) -> None:
        lease = ProfileLease(tmp_path)
        lease.try_acquire()
        assert not lease.handoff_requested()
        lease.release()

    def test_waiter_is_visible_to_the_owner(self, tmp_path: Path) -> None:
        lease = ProfileLease(tmp_path)
        lease.try_acquire()
        waiter = _spawn("announce", str(tmp_path), "10")
        try:
            _await_line(waiter, "ANNOUNCED")
            assert lease.handoff_requested()
        finally:
            waiter.kill()
            waiter.wait(timeout=10)
            lease.release()

    def test_handoff_clears_when_the_waiter_leaves(self, tmp_path: Path) -> None:
        lease = ProfileLease(tmp_path)
        lease.try_acquire()
        waiter = _spawn("announce", str(tmp_path), "0.3")
        try:
            _await_line(waiter, "ANNOUNCED")
            assert lease.handoff_requested()
            _await_line(waiter, "WITHDRAWN")
            assert not lease.handoff_requested()
        finally:
            waiter.kill()
            waiter.wait(timeout=10)
            lease.release()

    def test_several_waiters_can_announce_at_once(self, tmp_path: Path) -> None:
        """Shared locks must genuinely coexist, not merely appear to.

        The worker reports ANNOUNCE_FAILED rather than ANNOUNCED when it could
        not take the shared lock, because announcing degrades to a silent no-op
        by design. Without that distinction a platform whose "shared" locks are
        really exclusive would pass this test with only the first waiter holding
        anything, and the whole handoff protocol would rest on nothing.
        """
        lease = ProfileLease(tmp_path)
        lease.try_acquire()
        waiters = [_spawn("announce", str(tmp_path), "10") for _ in range(3)]
        try:
            for waiter in waiters:
                _await_line(waiter, "ANNOUNCED")
            assert lease.handoff_requested()
            # Still running means still holding: a worker that failed to
            # announce exits immediately with status 1.
            for waiter in waiters:
                assert waiter.poll() is None, "a waiter dropped its announcement"
        finally:
            for waiter in waiters:
                waiter.kill()
                waiter.wait(timeout=10)
            lease.release()

    def test_an_announcement_reports_whether_it_took_the_lock(
        self, tmp_path: Path
    ) -> None:
        """The signal the worker relies on, pinned in-process too."""
        lease = ProfileLease(tmp_path)
        with lease.announce() as announcement:
            assert announcement.holds_lock is True
        with lease.announce() as second:
            assert second.holds_lock is True, "shared locks must coexist serially"

    async def test_new_owner_does_not_hand_over_to_itself(self, tmp_path: Path) -> None:
        """A waiter must drop its announcement before it becomes the owner.

        Otherwise the new owner's first probe sees its own shared lock, concludes
        someone is waiting, and hands the browser straight back — forever.
        """
        lease = ProfileLease(tmp_path)
        holder = _spawn("hold", str(tmp_path), "0.5")
        try:
            _await_line(holder, "HELD")
            assert await lease.acquire(timeout=10)
            assert lease.held
            assert not lease.handoff_requested(), (
                "the new owner sees its own announcement and would loop"
            )
        finally:
            holder.wait(timeout=10)
            lease.release()


class TestAsyncAcquire:
    async def test_acquires_once_the_owner_releases(self, tmp_path: Path) -> None:
        lease = ProfileLease(tmp_path)
        holder = _spawn("hold", str(tmp_path), "0.5")
        try:
            _await_line(holder, "HELD")
            assert await lease.acquire(timeout=10)
        finally:
            holder.wait(timeout=10)
            lease.release()

    async def test_a_free_lease_is_taken_without_announcing(
        self, tmp_path: Path
    ) -> None:
        """The uncontended path must not touch the handoff file at all.

        Asserted by counting calls, not by inspecting the aftermath: an
        implementation that announced and then withdrew cleanly would leave
        exactly the same observable state, so checking ``handoff_requested()``
        afterwards would pass either way. What actually matters is that the
        handoff file is never touched, because announcing and withdrawing around
        every acquisition is what an owner's own next probe would misread as a
        waiter, and hand the browser straight back to nobody.
        """
        lease = ProfileLease(tmp_path)
        announcements = 0
        real_announce = lease.announce

        def counting_announce() -> object:
            nonlocal announcements
            announcements += 1
            return real_announce()

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(lease, "announce", counting_announce)
            assert await lease.acquire(timeout=0)

        assert announcements == 0, "the uncontended path announced"
        assert lease.held
        lease.release()

    async def test_times_out_while_the_owner_keeps_it(self, tmp_path: Path) -> None:
        lease = ProfileLease(tmp_path)
        holder = _spawn("hold", str(tmp_path), "10")
        try:
            _await_line(holder, "HELD")
            assert not await lease.acquire(timeout=0.5)
            assert not lease.held
        finally:
            holder.kill()
            holder.wait(timeout=10)

    async def test_timeout_leaves_no_announcement_behind(self, tmp_path: Path) -> None:
        lease = ProfileLease(tmp_path)
        holder = _spawn("hold", str(tmp_path), "10")
        try:
            _await_line(holder, "HELD")
            assert not await lease.acquire(timeout=0.3)
            assert not lease.handoff_requested()
        finally:
            holder.kill()
            holder.wait(timeout=10)

    async def test_cancellation_cannot_strand_the_lease(self, tmp_path: Path) -> None:
        """The reason acquisition polls instead of blocking in a thread.

        A blocking flock in ``asyncio.to_thread`` keeps running after the caller
        gives up, acquires the lease once the owner releases, and leaves nothing
        to release it — a permanent wedge from an ordinary timeout.
        """
        lease = ProfileLease(tmp_path)
        holder = _spawn("hold", str(tmp_path), "1.0")
        try:
            _await_line(holder, "HELD")
            task = asyncio.create_task(lease.acquire(timeout=30))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            holder.wait(timeout=10)
            await asyncio.sleep(0.3)

            assert not lease.held
            probe = ProfileLease(tmp_path)
            assert probe.try_acquire(), "the cancelled waiter stranded the lease"
            probe.release()
            assert not lease.handoff_requested()
        finally:
            holder.kill()


class TestCrossProcess:
    def test_mutual_exclusion_between_processes(self, tmp_path: Path) -> None:
        log = tmp_path / "critical.log"
        log.touch()
        workers = [_spawn("critical", str(tmp_path), str(log), "25") for _ in range(4)]
        for worker in workers:
            assert worker.wait(timeout=90) == 0

        tokens = log.read_text(encoding="utf-8").split()
        current: str | None = None
        for token in tokens:
            if token.startswith("IN"):
                assert current is None, f"overlapping critical sections: {token}"
                current = token[2:]
            else:
                assert current == token[3:], f"mismatched exit: {token}"
                current = None
        assert current is None
        assert len(tokens) == 200

    @staticmethod
    def _assert_freed_within(tmp_path: Path, message: str, seconds: float = 10) -> None:
        """Poll until the lease is free, rather than probing once.

        POSIX frees a ``flock`` synchronously with the dying process, but
        Windows documents lock release at process exit as depending on
        available resources, so a single immediate probe would flake there.
        """
        lease = ProfileLease(tmp_path)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if lease.try_acquire():
                lease.release()
                return
            time.sleep(0.05)
        raise AssertionError(message)

    def test_kernel_releases_the_lease_when_the_holder_dies(
        self, tmp_path: Path
    ) -> None:
        """No stale-lock recovery needed, unlike Chromium's SingletonLock."""
        victim = _spawn("die-holding", str(tmp_path))
        _await_line(victim, "HELD")
        victim.wait(timeout=10)

        self._assert_freed_within(tmp_path, "a dead holder still blocks the lease")

    def test_forced_termination_releases_the_lease(self, tmp_path: Path) -> None:
        """``Popen.kill`` is SIGKILL on POSIX and TerminateProcess on Windows.

        Neither gives the holder a chance to clean up, which is the point: the
        operating system has to free the lock on its own.
        """
        holder = _spawn("hold", str(tmp_path), "30")
        _await_line(holder, "HELD")
        holder.kill()
        holder.wait(timeout=10)

        self._assert_freed_within(tmp_path, "forced termination did not free the lease")

    def test_a_late_waiter_is_not_starved(self, tmp_path: Path) -> None:
        log = tmp_path / "starve.log"
        log.touch()
        busy = [_spawn("critical", str(tmp_path), str(log), "40") for _ in range(3)]
        try:
            time.sleep(0.1)
            lease = ProfileLease(tmp_path)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if lease.try_acquire():
                    lease.release()
                    return
                time.sleep(0.01)
            raise AssertionError("late waiter never acquired the lease")
        finally:
            for worker in busy:
                worker.kill()
                worker.wait(timeout=10)


class TestRegistry:
    def test_same_auth_root_returns_one_lease(self, tmp_path: Path) -> None:
        first = get_profile_lease(tmp_path / "profile")
        second = get_profile_lease(tmp_path / "profile")
        assert first is second

    @_posix_fork
    @pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
    def test_fork_does_not_inherit_ownership(self, tmp_path: Path) -> None:
        """A forked child must not believe it owns the parent's lease."""
        lease = ProfileLease(tmp_path)
        assert lease.try_acquire()
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            os.close(read_fd)
            os.write(write_fd, b"1" if lease.held else b"0")
            os.close(write_fd)
            os._exit(0)

        os.close(write_fd)
        child_says_held = os.read(read_fd, 1)
        os.close(read_fd)
        os.waitpid(pid, 0)
        lease.release()

        assert child_says_held == b"0", "child inherited ownership state"

    @_posix_fork
    @pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
    def test_the_reset_does_not_drop_the_living_parents_lock(
        self, tmp_path: Path
    ) -> None:
        """A child clearing its inherited state must not unlock for the parent.

        Both descriptions refer to the same open file, so unlocking in the child
        would release the lock the parent still believes it holds. Closing is
        safe; unlocking is not.
        """
        lease = ProfileLease(tmp_path)
        assert lease.try_acquire()
        lease.mark_browser_open()

        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            os.close(read_fd)
            # browser_open must reset too: the browser belongs to the parent.
            inherited_browser = lease.browser_open
            lease.release()  # a no-op; there is nothing of ours to release
            os.write(write_fd, b"1" if inherited_browser else b"0")
            os.close(write_fd)
            os._exit(0)

        os.close(write_fd)
        child_says_browser_open = os.read(read_fd, 1)
        os.close(read_fd)
        os.waitpid(pid, 0)

        assert child_says_browser_open == b"0"
        assert lease.held
        assert lease.browser_open is True
        # An outside process must still be locked out: the child's cleanup must
        # not have handed our profile away.
        assert not ProfileLease(tmp_path).try_acquire()
        lease.release()

    @_posix_fork
    @pytest.mark.parametrize(
        "trigger",
        [
            "lease._reset_if_forked()",
            "lease.held",
            "lease.held_seconds",
            "lease.browser_open",
            # Public methods that have no reason to check for a fork, and the
            # case that decides the design: a child touching the lease at all is
            # a coincidence, not something to rely on.
            "lease.handoff_requested()",
            "lease.mark_browser_closed()",
            "pass  # touches nothing",
        ],
        ids=[
            "explicit",
            "held",
            "held_seconds",
            "browser_open",
            "handoff_requested",
            "mark_browser_closed",
            "no_touch",
        ],
    )
    def test_a_forked_child_does_not_keep_the_lease_alive(
        self, tmp_path: Path, trigger: str
    ) -> None:
        """Clearing the bookkeeping is not enough; the descriptor must be closed.

        ``fork`` duplicates the descriptor, and the kernel lock survives as long
        as *any* copy is open. A child that only forgot it had inherited one
        would keep the parent's lease held after the parent died, locking every
        other process out until the child happened to exit, with nothing in
        the child's own state to explain why.

        The ``no_touch`` case is why this is handled at ``fork`` time rather
        than inside the accessors: a child that never mentions the lease has
        still inherited it, so no amount of checking on entry could ever cover
        every path.

        Every accessor a child could plausibly touch first is covered, not just
        the private reset: reading ``held`` and getting an honest ``False`` is
        exactly the case where nothing would otherwise prompt the cleanup.

        The child outlives the parent here, which is the shape of a server that
        spawns a helper and then exits.
        """
        # Through the registry, which is how every caller in the server obtains
        # a lease and the only place that can know what a fork must discard.
        script = textwrap.dedent(f"""
            import os, sys, time
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
            from pathlib import Path
            from linkedin_mcp_server.profile_lease import get_profile_lease

            lease = get_profile_lease(Path({str(tmp_path)!r}) / "profile")
            assert lease.try_acquire()
            lease.mark_browser_open()
            if os.fork() == 0:
                {trigger}
                print("CHILD_READY:%d" % os.getpid(), flush=True)
                time.sleep(30)
                os._exit(0)
            os._exit(0)  # parent dies still believing it holds the lease
        """)
        holder = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        child_pid: int | None = None
        try:
            assert holder.stdout is not None
            while True:
                line = holder.stdout.readline()
                if line.startswith("CHILD_READY:"):
                    child_pid = int(line.split(":", 1)[1])
                    break
                if line == "":
                    stderr = holder.stderr.read() if holder.stderr else ""
                    raise AssertionError(f"helper never forked a child: {stderr}")
            holder.wait(timeout=10)  # the parent exits; the child lives on

            lease = ProfileLease(tmp_path)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if lease.try_acquire():
                    lease.release()
                    return
                time.sleep(0.05)
            raise AssertionError("the forked child leaked its parent's kernel lock")
        finally:
            holder.kill()
            # The grandchild is not ours to wait on, but leaving it asleep for
            # 30s would hold the lock through the rest of the run on failure.
            if child_pid is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(child_pid, signal.SIGKILL)

    def test_stale_ownership_from_another_pid_is_discarded(
        self, tmp_path: Path
    ) -> None:
        """The fork reset, observable without forking.

        The child of a fork exercises this, but its coverage is invisible and
        its assertions have to travel back through a pipe. Driving it directly
        pins the behaviour: state recorded under a foreign pid is not ours.
        """
        lease = ProfileLease(tmp_path)
        assert lease.try_acquire()
        lease.mark_browser_open()

        lease._owner_pid = os.getpid() + 1_000_000  # as if inherited

        assert lease.browser_open is False  # the property runs the reset
        assert lease.held is False
        assert lease.held_seconds == 0.0
        assert lease._fd is None
        assert lease._refs == 0

    def test_an_already_closed_inherited_descriptor_is_tolerated(
        self, tmp_path: Path
    ) -> None:
        """The reset runs on paths where the descriptor may already be gone.

        Raising here would surface as a failure in whatever ordinary call
        happened to trigger the reset, long after the fork that caused it.
        """
        lease = ProfileLease(tmp_path)
        assert lease.try_acquire()
        assert lease._fd is not None
        os.close(lease._fd)
        lease._owner_pid = os.getpid() + 1_000_000  # as if inherited

        lease._reset_if_forked()  # must not raise

        assert lease._fd is None
        assert not lease.held

    def test_marking_the_browser_closed_clears_the_flag(self, tmp_path: Path) -> None:
        """Only ever called on a *confirmed* close; a timed-out one keeps it."""
        lease = ProfileLease(tmp_path)
        lease.mark_browser_open()
        assert lease.browser_open is True

        lease.mark_browser_closed()

        assert lease.browser_open is False

    def test_leases_are_reset_between_tests(self, tmp_path: Path) -> None:
        """``reset_leases_for_testing`` must free even a multiply-held lease.

        The autouse fixture calls it after every test. If it dropped only one
        reference, a leaked lease would silently wedge every later test that
        touches the same auth root.
        """
        from linkedin_mcp_server.profile_lease import reset_leases_for_testing

        lease = get_profile_lease(tmp_path / "profile")
        assert lease.try_acquire()
        assert lease.try_acquire()  # a second reference, as middleware + browser
        assert lease.held

        reset_leases_for_testing()

        assert not lease.held
        probe = ProfileLease(lease.auth_root)
        assert probe.try_acquire(), "reset left the kernel lock held"
        probe.release()


class TestDegradedSignals:
    """Behaviour when the lock files themselves misbehave.

    These paths cannot be reached by ordinary use, only by another process
    replacing or removing the files. They still have to fail in a defined
    direction: never silently, and never towards handing the profile away.
    """

    @_posix_unlink_race
    def test_an_unlinked_lock_file_is_reacquired(self, tmp_path: Path) -> None:
        """An orphaned inode protects nothing, so acquisition must retry.

        Without the identity check two processes end up holding locks on
        different inodes for the same path and both believe they are the owner.
        Unlinking is the easier half of that: it is also what a rename does,
        which the test below covers and a link count cannot see.
        """
        from linkedin_mcp_server import profile_lease as module

        lease = ProfileLease(tmp_path)
        lock_path = tmp_path / "profile.lock"
        real_open = module.open_lock_file
        attempts: list[int] = []

        def unlink_once(path: Path) -> int:
            fd = real_open(path)
            attempts.append(fd)
            if len(attempts) == 1:
                path.unlink()  # orphan the inode we just opened
            return fd

        # A private context rather than the shared fixture: undoing that one
        # would also revert the autouse profile-directory patches in conftest,
        # silently unpinning this test from its tmp_path for the rest of the run.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(module, "open_lock_file", unlink_once)
            assert lease.try_acquire()

        assert len(attempts) == 2, "the orphaned inode was accepted"
        assert lease._fd is not None
        # The descriptor is the file the path names, which is what the retry
        # was for. A link count would also pass here and miss the rename case.
        assert os.fstat(lease._fd).st_ino == lock_path.stat().st_ino
        assert lock_path.exists()
        lease.release()

    @_posix_unlink_race
    def test_a_path_that_keeps_being_replaced_gives_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retrying forever would hang a tool call; refuse instead."""
        from linkedin_mcp_server import profile_lease as module

        real_open = module.open_lock_file

        def always_unlink(path: Path) -> int:
            fd = real_open(path)
            path.unlink()
            return fd

        monkeypatch.setattr(module, "open_lock_file", always_unlink)
        with pytest.raises(ProfileLeaseUnavailableError, match="keeps being"):
            ProfileLease(tmp_path).try_acquire()

    @pytest.mark.parametrize(
        ("error_code", "is_contention"),
        [
            (33, True),  # ERROR_LOCK_VIOLATION: someone else holds it
            (6, False),  # ERROR_INVALID_HANDLE
            (5, False),  # ERROR_ACCESS_DENIED
            (87, False),  # ERROR_INVALID_PARAMETER
            (997, False),  # ERROR_IO_PENDING: async only, not our call
            (0, False),
        ],
    )
    def test_only_a_lock_violation_counts_as_contention(
        self, error_code: int, is_contention: bool
    ) -> None:
        """Windows reports "held" and "the call was wrong" the same way.

        Treating every failure as contention would make the lease report a
        permanently busy profile no client could ever use, and would make the
        handoff probe close a working browser on the strength of an error. The
        matrix job cannot catch a regression here, because a healthy run never
        produces anything but 33, so the classification is pinned directly.
        """
        from linkedin_mcp_server.profile_lease import _windows_failure_is_contention

        assert _windows_failure_is_contention(error_code) is is_contention

    @pytest.mark.skipif(os.name == "nt", reason="flock is the POSIX backend")
    @pytest.mark.parametrize(
        "code,is_contention",
        [
            (errno.EAGAIN, True),
            (errno.EWOULDBLOCK, True),
            (errno.EACCES, True),
            (errno.EOPNOTSUPP, False),
            (errno.EIO, False),
            (errno.EBADF, False),
        ],
    )
    def test_posix_failures_are_classified(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        code: int,
        is_contention: bool,
    ) -> None:
        """The POSIX half of the same question, and for the same reason.

        Only contention means somebody else holds the lock. Measured with
        EOPNOTSUPP before the fix: reported as busy, which on a filesystem
        without usable flock would leave every process seeing an owner that does
        not exist, no owner electable, and an error pointing at contention.
        """
        import fcntl

        from linkedin_mcp_server.profile_lease import (
            ProfileLeaseUnavailableError,
            open_lock_file,
            try_lock,
        )

        def failing(fd: int, operation: int) -> None:
            raise OSError(code, os.strerror(code))

        descriptor = open_lock_file(tmp_path / "probe.lock")
        monkeypatch.setattr(fcntl, "flock", failing)

        if is_contention:
            assert try_lock(descriptor, exclusive=True) is False
        else:
            with pytest.raises(ProfileLeaseUnavailableError, match="Could not lock"):
                try_lock(descriptor, exclusive=True)

    def test_a_failing_backend_does_not_leak_descriptors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lock backend that raises must not leave the file open.

        The handoff probe swallows that failure so a broken signal cannot make
        an owner give up a working browser, and the poller runs every second,
        so a descriptor escaping here accumulates one per poll for the life of
        the process.
        """
        from linkedin_mcp_server import profile_lease as module

        opened: list[int] = []
        real_open = module.open_lock_file

        def spy(path: Path) -> int:
            fd = real_open(path)
            opened.append(fd)
            return fd

        def refuse(fd: int, *, exclusive: bool) -> bool:
            raise ProfileLeaseUnavailableError("backend unavailable")

        monkeypatch.setattr(module, "open_lock_file", spy)
        monkeypatch.setattr(module, "try_lock", refuse)

        for _ in range(3):
            with pytest.raises(ProfileLeaseUnavailableError):
                module.acquire_locked_fd(tmp_path / "probe.lock", exclusive=True)

        assert opened, "the spy never ran, so this proves nothing"
        for fd in set(opened):
            with pytest.raises(OSError):
                os.fstat(fd)  # a live descriptor would not raise

    def test_cleanup_tolerates_an_already_closed_descriptor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cleanup must not raise on top of the failure that triggered it."""
        from linkedin_mcp_server import profile_lease as module

        def close_then_refuse(fd: int, *, exclusive: bool) -> bool:
            os.close(fd)  # as if something else had already reclaimed it
            raise ProfileLeaseUnavailableError("backend unavailable")

        monkeypatch.setattr(module, "try_lock", close_then_refuse)

        with pytest.raises(ProfileLeaseUnavailableError, match="backend unavailable"):
            module.acquire_locked_fd(tmp_path / "probe.lock", exclusive=True)

    def test_an_inconclusive_probe_does_not_hand_the_profile_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed: a broken signal is not evidence that anyone is waiting.

        Reporting a waiter here would make the owner close a working browser on
        the strength of a signal it could not actually read.
        """
        from linkedin_mcp_server import profile_lease as module

        lease = ProfileLease(tmp_path)

        def refuse(path: Path, *, exclusive: bool) -> int | None:
            raise ProfileLeaseUnavailableError("signal unreadable")

        monkeypatch.setattr(module, "acquire_locked_fd", refuse)
        assert lease.handoff_requested() is False

    def test_a_failed_announcement_degrades_to_waiting_quietly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not being able to announce costs a prompt handoff, not the call."""
        from linkedin_mcp_server import profile_lease as module

        lease = ProfileLease(tmp_path)

        def refuse(path: Path, *, exclusive: bool) -> int | None:
            raise ProfileLeaseUnavailableError("signal unreadable")

        monkeypatch.setattr(module, "acquire_locked_fd", refuse)
        with lease.announce() as announcement:
            assert announcement.holds_lock is False

    def test_no_locking_backend_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running unprotected is the one outcome that must never happen.

        Silently continuing would put us back where we started: two processes on
        one profile, one silently losing its cookie.
        """
        from linkedin_mcp_server import profile_lease as module

        fd = os.open(tmp_path / "probe", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            monkeypatch.setattr(module, "_HAS_FCNTL", False)
            monkeypatch.setattr(module, "_HAS_WINDOWS_LOCKS", False)
            with pytest.raises(
                ProfileLeaseUnavailableError, match="no usable file locking"
            ):
                module.try_lock(fd, exclusive=True)
            # Unlocking has nothing to undo and must stay silent, or a cleanup
            # path would raise on top of the original failure.
            module._unlock(fd)
        finally:
            os.close(fd)

    def test_releasing_an_unheld_lease_is_a_no_op(self, tmp_path: Path) -> None:
        """Release is called from ``finally`` blocks that may not have acquired."""
        lease = ProfileLease(tmp_path)
        lease.release()  # must not raise
        assert not lease.held
        assert lease.held_seconds == 0.0

    def test_a_closed_descriptor_does_not_break_release(self, tmp_path: Path) -> None:
        """Release runs during teardown, where descriptors may already be gone."""
        lease = ProfileLease(tmp_path)
        assert lease.try_acquire()
        assert lease._fd is not None
        os.close(lease._fd)

        lease.release()  # must not raise

        assert not lease.held
