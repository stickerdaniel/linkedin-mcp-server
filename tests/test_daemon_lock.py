"""The lock that decides which process owns the shared browser.

Mutual exclusion cannot be shown inside one interpreter, so the tests that
matter here spawn real processes and let the kernel arbitrate. The rest pin
behaviour that is easy to write correctly and just as easy to break later.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
import subprocess
import threading
import sys
import textwrap
import time
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

import linkedin_mcp_server.daemon.descriptor as daemon_descriptor_module
import linkedin_mcp_server.daemon.lock as daemon_lock_module
from linkedin_mcp_server.daemon import owner as daemon_owner
from linkedin_mcp_server.daemon.descriptor import daemon_dir, daemon_state_root
from linkedin_mcp_server.daemon.lock import (
    DaemonLock,
    DaemonLockError,
    daemon_lock_path,
)
from linkedin_mcp_server.private_state import PrivateStateError

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


def _held(auth_root: Path) -> bool:
    """Whether a contender is refused the lock, as an election would be."""
    contender = DaemonLock(auth_root)
    try:
        return not contender.try_acquire()
    finally:
        contender.release()


_TRY_ACQUIRE = """
    import os
    import sys
    from pathlib import Path
    import linkedin_mcp_server.daemon.descriptor as daemon_descriptor
    from linkedin_mcp_server.daemon.lock import DaemonLock

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
    def test_fresh_lock_state_is_private_under_umask_zero(self, tmp_path: Path):
        previous = os.umask(0)
        lock = DaemonLock(tmp_path / "auth")
        try:
            assert lock.try_acquire()
        finally:
            os.umask(previous)
            lock.release()

        assert stat.S_IMODE(daemon_state_root().stat().st_mode) == 0o700
        assert stat.S_IMODE(lock.path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(lock.path.stat().st_mode) == 0o600

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks are required")
    def test_a_planted_lock_symlink_is_refused(self, tmp_path: Path):
        path = daemon_lock_path(tmp_path / "auth")
        path.parent.mkdir(parents=True, mode=0o777)
        path.parent.chmod(0o777)
        victim = tmp_path / "victim.lock"
        victim.write_text("untouched")
        victim.chmod(0o644)
        path.symlink_to(victim)

        with pytest.raises(PrivateStateError, match="symbolic link"):
            DaemonLock(tmp_path / "auth").try_acquire()

        assert stat.S_IMODE(victim.stat().st_mode) == 0o644

    @pytest.mark.skipif(
        not hasattr(os, "mkfifo"), reason="named pipes are a POSIX mechanism"
    )
    def test_a_planted_lock_fifo_is_refused_without_blocking(self, tmp_path: Path):
        path = daemon_lock_path(tmp_path / "auth")
        path.parent.mkdir(parents=True, mode=0o777)
        path.parent.chmod(0o777)
        os.mkfifo(path)
        raised: list[BaseException] = []

        def acquire() -> None:
            try:
                DaemonLock(tmp_path / "auth").try_acquire()
            except BaseException as exc:
                raised.append(exc)

        # Opening a FIFO for reading waits for a writer, and none ever comes, so
        # a lock that opened it would wait forever. The join only turns that
        # into a failure: how long the refusal takes on a loaded host is not the
        # claim, and a wall-clock bound on it failed a correct refusal.
        worker = threading.Thread(target=acquire, daemon=True)
        worker.start()
        worker.join(timeout=30)
        if worker.is_alive():
            # A writer releases the blocked open, so the thread ends with the
            # test. It is offered until the thread is gone: one offered before
            # the open is reached finds no reader (ENXIO) and releases nothing.
            deadline = time.monotonic() + 30
            while worker.is_alive() and time.monotonic() < deadline:
                with contextlib.suppress(OSError):
                    os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
                worker.join(timeout=0.05)
            pytest.fail("acquiring the lock blocked on the planted FIFO")

        assert len(raised) == 1
        assert isinstance(raised[0], PrivateStateError)
        assert "not a regular file" in str(raised[0])
        assert path.is_fifo()

    @pytest.mark.skipif(
        os.name == "nt", reason="POSIX permission bits do not exist on Windows"
    )
    def test_an_existing_regular_lock_is_hardened_before_use(self, tmp_path: Path):
        path = daemon_lock_path(tmp_path / "auth")
        path.parent.mkdir(parents=True, mode=0o777)
        path.parent.chmod(0o777)
        path.touch(mode=0o666)
        path.chmod(0o666)
        lock = DaemonLock(tmp_path / "auth")

        assert lock.try_acquire()
        try:
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        finally:
            lock.release()

    def test_failed_fresh_lock_hardening_removes_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        def refuse(_path: Path) -> None:
            raise PrivateStateError("ACL failure")

        monkeypatch.setattr(daemon_lock_module, "harden_created_file", refuse)
        lock = DaemonLock(tmp_path / "auth")

        with pytest.raises(PrivateStateError, match="ACL failure"):
            lock.try_acquire()

        assert not lock.path.exists()

    @pytest.mark.skipif(os.name != "nt", reason="Windows ACLs are required")
    def test_lock_file_uses_the_private_file_acl(self, tmp_path: Path):
        from linkedin_mcp_server.windows_acl import describe_dacl

        lock = DaemonLock(tmp_path / "auth")
        assert lock.try_acquire()
        try:
            described = describe_dacl(lock.path)
            assert described.protected is True
            assert len(described.entries) == 1
        finally:
            lock.release()

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


class TestRegistry:
    def test_a_held_lock_survives_losing_its_last_reference(self, tmp_path: Path):
        # Measured before the fix: the registry was weak throughout, so a lock
        # acquired and then dropped left the descriptor open with nothing left
        # to close it. Another process could not take the lock, and no object
        # remained through which to release it or clean it up after a fork.
        import gc

        from linkedin_mcp_server.daemon.lock import _held_locks

        DaemonLock(tmp_path).try_acquire()
        gc.collect()

        assert _held(tmp_path)
        held = [
            lock for lock in _held_locks if lock.path.parent == daemon_dir(tmp_path)
        ]
        assert held, "a held lock must stay reachable"

        for lock in held:
            lock.release()
        assert not _held(tmp_path)

    def test_an_unheld_lock_is_not_kept_alive(self, tmp_path: Path):
        # Only held locks are worth pinning. One that was built and never used
        # owns nothing, so keeping it would be a leak for no benefit.
        import gc

        from linkedin_mcp_server.daemon.lock import _held_locks

        DaemonLock(tmp_path)
        gc.collect()

        assert not [lock for lock in _held_locks if lock.path.parent == tmp_path]


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

            assert not _held(tmp_path)
        finally:
            os.close(read_fd)
            os.waitpid(pid, 0)


class TestReleaseSemantics:
    @pytest.mark.skipif(
        os.name == "nt", reason="flock belongs to the open file description"
    )
    @pytest.mark.parametrize("release", ["release", "release_for_exit"])
    def test_releasing_closes_rather_than_unlocks(self, tmp_path: Path, release: str):
        # The measured trap. flock belongs to the open file description, which
        # a fork child's copy shares, so unlocking would release the lock for
        # every copy. Closing drops only this descriptor.
        lock = DaemonLock(tmp_path)
        assert lock.try_acquire()
        assert lock._fd is not None
        copy = os.dup(lock._fd)

        getattr(lock, release)()

        try:
            # Still held, through the copy. An unlock-then-close release would
            # have freed it here.
            assert _held(tmp_path)
        finally:
            os.close(copy)


class _Exited(BaseException):
    """Raised by the ``os._exit`` double, so the test outlives the exit."""


class _DiagnosticSink(logging.Handler):
    """A DEBUG sink that, once armed, fails or stalls on every record.

    The stall is bounded, so a regression costs this test two seconds rather
    than the suite. Unarmed, it only records, which is how the test proves the
    sink is reachable before it relies on the sink staying silent.
    """

    def __init__(self, mode: str) -> None:
        super().__init__(logging.DEBUG)
        self.mode = mode
        self.armed = False
        self.seen: list[str] = []
        self.entered: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if not self.armed:
            self.seen.append(record.getMessage())
            return
        self.entered.append(record.getMessage())
        if self.mode == "raises":
            raise RuntimeError("the diagnostic sink failed")
        threading.Event().wait(2.0)


@contextlib.contextmanager
def _debug_sink(mode: str) -> Generator[_DiagnosticSink]:
    """Route every DEBUG record from the lock and the owner into one sink."""
    sink = _DiagnosticSink(mode)
    root = logging.getLogger()
    loggers = (root, daemon_lock_module.logger, daemon_owner.logger)
    saved = [
        (logger, logger.level, logger.propagate, logger.disabled) for logger in loggers
    ]
    disabled = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    for logger in loggers:
        logger.setLevel(logging.DEBUG)
        logger.propagate = True
        logger.disabled = False
    root.addHandler(sink)
    try:
        yield sink
    finally:
        sink.armed = False
        root.removeHandler(sink)
        for logger, level, propagate, was_disabled in saved:
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = was_disabled
        logging.disable(disabled)


def _exit_hard_status(lock: Any) -> int:
    """Run the real ``_exit_hard`` and return the status it exits with."""

    def exit_now(status: int) -> None:
        raise _Exited(status)

    # A patch scoped to this call alone. The autouse fixture's isolation must
    # outlive it: the kernel probe below runs a child against the same home.
    with (
        mock.patch.object(daemon_owner.os, "_exit", exit_now),
        pytest.raises(_Exited) as exited,
    ):
        daemon_owner._exit_hard(lock)
    return exited.value.args[0]


class TestTheTerminalRelease:
    """``_exit_hard`` gives up the election and exits with no diagnostic between.

    A log handler that blocks or raises after the lock is gone would hold the
    exit, or divert it into ordinary unwinding, while a successor can already
    take the election. The lock here is real, and whether it is free is asked
    of the kernel from another process rather than read off ``lock.held``.
    """

    @pytest.mark.parametrize("mode", ["raises", "blocks"])
    def test_no_diagnostic_stands_between_release_and_exit(
        self, tmp_path: Path, mode: str
    ):
        with _debug_sink(mode) as sink:
            lock = DaemonLock(tmp_path)
            assert lock.try_acquire()
            if not any("Daemon lock acquired" in line for line in sink.seen):
                pytest.fail(f"the DEBUG sink never saw the lock: {sink.seen!r}")
            sink.armed = True

            status = _exit_hard_status(lock)

        assert sink.entered == [], "a diagnostic ran between release and exit"
        assert status == 1
        free = _run_child(_TRY_ACQUIRE, str(tmp_path))
        assert free.stdout.strip() == "ACQUIRED", free.stderr

    def test_a_close_that_fails_still_exits_in_silence(self, tmp_path: Path):
        lock = DaemonLock(tmp_path)
        assert lock.try_acquire()
        descriptor = lock._fd
        assert descriptor is not None
        real_close = os.close

        def close(fd: int) -> None:
            if fd == descriptor:
                raise OSError("the close did not happen")
            real_close(fd)

        try:
            with (
                _debug_sink("raises") as sink,
                mock.patch.object(daemon_lock_module.os, "close", close),
            ):
                sink.armed = True
                status = _exit_hard_status(lock)
        finally:
            real_close(descriptor)

        assert sink.entered == [], "a failed close was reported before exit"
        assert status == 1
        assert not lock.held

    def test_a_release_that_raises_still_exits(self):
        def release_for_exit() -> None:
            raise RuntimeError("the release failed")

        lock = SimpleNamespace(release_for_exit=release_for_exit)

        assert _exit_hard_status(lock) == 1
