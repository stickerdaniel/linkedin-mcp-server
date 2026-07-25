"""Tests for cross-process ownership of the shared browser profile.

The interesting cases here need real operating-system processes. A single-loop
asyncio test cannot reproduce two server processes opening one Chromium profile,
which is the failure the lease exists to prevent, so the multi-process cases
spawn ``tests/helpers/profile_lease_worker.py`` with ``subprocess``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from linkedin_mcp_server.profile_lease import (
    ProfileLease,
    ProfileLeaseUnavailableError,
    get_profile_lease,
)

_WORKER = Path(__file__).parent / "helpers" / "profile_lease_worker.py"


def _spawn(*args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(_WORKER), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _await_line(process: subprocess.Popen[str], expected: str) -> None:
    assert process.stdout is not None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if expected in line:
            return
        if line == "" and process.poll() is not None:
            break
    stderr = process.stderr.read() if process.stderr else ""
    raise AssertionError(f"worker never reported {expected!r}: {stderr}")


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
        lease = ProfileLease(tmp_path)
        lease.try_acquire()
        waiters = [_spawn("announce", str(tmp_path), "10") for _ in range(3)]
        try:
            for waiter in waiters:
                _await_line(waiter, "ANNOUNCED")
            assert lease.handoff_requested()
        finally:
            for waiter in waiters:
                waiter.kill()
                waiter.wait(timeout=10)
            lease.release()

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

    def test_kernel_releases_the_lease_when_the_holder_dies(
        self, tmp_path: Path
    ) -> None:
        """No stale-lock recovery needed, unlike Chromium's SingletonLock."""
        victim = _spawn("die-holding", str(tmp_path))
        _await_line(victim, "HELD")
        victim.wait(timeout=10)

        lease = ProfileLease(tmp_path)
        assert lease.try_acquire(), "a dead holder still blocks the lease"
        lease.release()

    def test_kill_nine_releases_the_lease(self, tmp_path: Path) -> None:
        holder = _spawn("hold", str(tmp_path), "30")
        _await_line(holder, "HELD")
        holder.kill()
        holder.wait(timeout=10)

        lease = ProfileLease(tmp_path)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if lease.try_acquire():
                lease.release()
                return
            time.sleep(0.05)
        raise AssertionError("SIGKILL did not free the lease")

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

    @pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
    def test_fork_does_not_inherit_ownership(self, tmp_path: Path) -> None:
        """A forked child must not believe it owns the parent's lease."""
        if not hasattr(os, "fork"):  # pragma: no cover - Windows
            pytest.skip("fork is unavailable on this platform")

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
