"""The non-announcing lease contender, on temporary locks only.

Held by the product's own lease in this process, free after its release,
unknown for everything that is not an answer: a missing, linked or replaced
file, a directory, an errno other than contention, a helper that does not
answer. Nothing the probe does creates, rewrites or announces anything, and no
helper it starts outlives it unowned, however the probe ends.
"""

from __future__ import annotations

import contextlib
import errno
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from differential import lease_probe
from differential.lease_probe import FREE, HELD, UNKNOWN, probe, run_probe
from linkedin_mcp_server.profile_lease import get_profile_lease

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the contender is POSIX flock; R7 is Linux-only"
)


@pytest.fixture
def lease(tmp_path):
    """The product's lease on a temporary auth root, held by this process."""
    held = get_profile_lease(tmp_path / "auth" / "profile")
    assert held.try_acquire()
    yield held
    if held.held:
        held.release()


def _entries(directory: Path) -> dict[str, tuple[int, int]]:
    return {
        entry.name: (entry.stat().st_ino, entry.stat().st_mtime_ns)
        for entry in directory.iterdir()
    }


def test_a_held_lease_is_held_and_a_released_one_free(lease):
    path = str(lease._lease_path)
    before = _entries(lease.auth_root)
    held = run_probe(path)
    assert held["state"] == HELD, held
    lease.release()
    free = run_probe(path)
    assert free["state"] == FREE, free
    assert (held["device"], held["inode"]) == (free["device"], free["inode"])
    # Nothing created, replaced or rewritten, and no waiter announced.
    assert _entries(lease.auth_root) == before
    assert not (lease.auth_root / "profile.handoff").exists()
    # And the probe held nothing: the product can take it again at once.
    assert lease.try_acquire()


def test_a_missing_lock_is_unknown_and_stays_missing(tmp_path):
    path = tmp_path / "profile.lock"
    assert run_probe(str(path))["state"] == UNKNOWN
    assert not path.exists()


def test_a_linked_lock_is_unknown_and_its_target_untouched(tmp_path, lease):
    link = tmp_path / "linked.lock"
    link.symlink_to(lease._lease_path)
    answer = run_probe(str(link))
    assert answer["state"] == UNKNOWN and "not a regular file" in answer["reason"]


def test_a_directory_is_unknown(tmp_path):
    assert run_probe(str(tmp_path))["state"] == UNKNOWN


def test_an_errno_other_than_contention_is_unknown_never_held(tmp_path, monkeypatch):
    path = tmp_path / "profile.lock"
    path.write_text("")
    import fcntl

    def unsupported(descriptor, operation):
        raise OSError(errno.EOPNOTSUPP, "Operation not supported")

    monkeypatch.setattr(fcntl, "flock", unsupported)
    answer = probe(str(path))
    assert answer["state"] == UNKNOWN and "could not be asked" in answer["reason"]


def test_a_lock_replaced_while_asking_is_unknown(tmp_path, monkeypatch):
    path = tmp_path / "profile.lock"
    path.write_text("")
    real = os.lstat
    looks = {"n": 0}

    def replaced(target, *args, **kwargs):
        looks["n"] += 1
        result = real(target, *args, **kwargs)
        if looks["n"] == 2:
            return os.stat_result(
                (result.st_mode, result.st_ino + 1, *tuple(result)[2:])
            )
        return result

    monkeypatch.setattr(lease_probe.os, "lstat", replaced)
    answer = probe(str(path))
    assert answer["state"] == UNKNOWN and "replaced while asking" in answer["reason"]


# --- Owning the helper ------------------------------------------------------------------
#
# Each helper is followed through the Popen that started it, never by its
# number: once it is reaped, the number may name anyone.


@pytest.fixture(autouse=True)
def owned(monkeypatch) -> list[subprocess.Popen]:
    """A fresh registry, so one test's leftover never blocks the next."""
    fresh: list[subprocess.Popen] = []
    monkeypatch.setattr(lease_probe, "_OWNED", fresh)
    return fresh


@pytest.fixture
def silent(tmp_path) -> str:
    """A helper that never answers: it sleeps until it is killed."""
    script = tmp_path / "silent-python"
    script.write_text("#!/bin/sh\nexec sleep 30\n")
    script.chmod(0o700)
    return str(script)


class _Spy:
    """Every helper started, as its own Popen, and the faults planted in it.

    ``forked`` breaks ``Popen`` itself once the child exists, ``returned``
    breaks the start after ``Popen`` returned, ``collecting`` breaks
    ``communicate`` while the helper runs, and ``unkillable`` makes the kill
    fail silently.
    """

    def __init__(self) -> None:
        self.created: list[subprocess.Popen] = []
        self.forked: BaseException | None = None
        self.returned: BaseException | None = None
        self.collecting: BaseException | None = None
        self.unkillable = False


def _end(created: list[subprocess.Popen]) -> None:
    """Emergency cleanup: end and reap, through its own Popen, what still runs.

    A helper already reaped is left alone, whatever its number names now.
    """
    for process in created:
        if getattr(process, "pid", None) is not None and process.poll() is None:
            subprocess.Popen.kill(process)  # past a planted refusal to die
            subprocess.Popen.wait(process, timeout=5)
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()


@contextlib.contextmanager
def _spying(monkeypatch):
    spy = _Spy()

    class Recording(lease_probe._Helper):
        def __init__(self, *args, **kwargs):
            spy.created.append(self)
            super().__init__(*args, **kwargs)
            if spy.returned is not None:
                raise spy.returned

        def _execute_child(self, *args, **kwargs):
            # Private to subprocess, so absent from its stubs: the step that
            # forks, inside Popen.__init__.
            super()._execute_child(*args, **kwargs)  # ty: ignore[unresolved-attribute]
            if spy.forked is not None:
                raise spy.forked

        def communicate(self, input=None, timeout=None):
            if spy.collecting is None:
                return super().communicate(input, timeout)
            # Collection has begun, and the helper is alive when it breaks.
            with contextlib.suppress(subprocess.TimeoutExpired):
                super().communicate(input, 0.2)
            assert self.poll() is None
            raise spy.collecting

        def kill(self):
            if not spy.unkillable:
                super().kill()

    monkeypatch.setattr(lease_probe, "_Helper", Recording)
    try:
        yield spy
    finally:
        _end(spy.created)


def _reaped(process: subprocess.Popen, returncode: int) -> bool:
    pipes = (process.stdout, process.stderr)
    return process.returncode == returncode and all(
        pipe is not None and pipe.closed for pipe in pipes
    )


def test_an_answered_probe_reaps_its_helper(tmp_path, monkeypatch, owned):
    path = tmp_path / "profile.lock"
    path.write_text("")
    with _spying(monkeypatch) as spy:
        assert run_probe(str(path))["state"] == FREE
        (process,) = spy.created
        assert _reaped(process, 0) and owned == []


def test_a_helper_that_never_answers_is_unknown_and_reaped(
    tmp_path, monkeypatch, silent, owned
):
    with _spying(monkeypatch) as spy:
        answer = run_probe(str(tmp_path / "profile.lock"), python=silent, timeout=1.0)
        assert answer["state"] == UNKNOWN and "did not answer" in answer["reason"]
        (process,) = spy.created
        assert _reaped(process, -signal.SIGKILL) and owned == []


def test_a_helper_that_cannot_start_is_unknown_and_leaves_nothing(
    tmp_path, monkeypatch, owned
):
    missing = str(tmp_path / "no-such-python")
    with _spying(monkeypatch) as spy:
        answer = run_probe(str(tmp_path / "profile.lock"), python=missing)
        assert answer["state"] == UNKNOWN and "could not start" in answer["reason"]
        (process,) = spy.created
        assert process.pid is None or process.returncode is not None
        assert owned == []


def test_a_helper_refused_before_the_fork_leaves_nothing_owned(
    tmp_path, monkeypatch, owned
):
    with _spying(monkeypatch) as spy:
        with pytest.raises(ValueError, match="null"):
            run_probe(str(tmp_path / "profile.lock"), python="python\0")
        (process,) = spy.created
        assert process.pid is None and owned == []


def _pipe_failure() -> OSError:
    return OSError(errno.EPIPE, "planted pipe failure")


@pytest.mark.parametrize(
    "planted",
    [
        pytest.param(_pipe_failure, id="failed"),
        pytest.param(KeyboardInterrupt, id="interrupted"),
    ],
)
def test_a_broken_collection_reaps_the_helper_and_raises_its_failure(
    tmp_path, monkeypatch, silent, owned, planted
):
    fault = planted()
    with _spying(monkeypatch) as spy:
        spy.collecting = fault
        with pytest.raises(type(fault)) as raised:
            run_probe(str(tmp_path / "profile.lock"), python=silent)
        assert raised.value is fault and not getattr(fault, "__notes__", None)
        (process,) = spy.created
        assert _reaped(process, -signal.SIGKILL) and owned == []


@pytest.mark.parametrize("stage", ["forked", "returned"])
def test_an_interrupt_as_the_helper_starts_still_reaps_it(
    tmp_path, monkeypatch, silent, owned, stage
):
    # Once the child exists: inside Popen, or before the caller holds it.
    fault = KeyboardInterrupt()
    with _spying(monkeypatch) as spy:
        setattr(spy, stage, fault)
        with pytest.raises(KeyboardInterrupt) as raised:
            run_probe(str(tmp_path / "profile.lock"), python=silent)
        assert raised.value is fault
        (process,) = spy.created
        assert _reaped(process, -signal.SIGKILL) and owned == []


@pytest.mark.parametrize("stage", [None, "forked", "collecting"])
def test_a_helper_that_outlives_its_kill_blocks_every_later_probe(
    tmp_path, monkeypatch, silent, owned, stage
):
    path = tmp_path / "profile.lock"
    path.write_text("")
    fault = None if stage is None else _pipe_failure()
    with _spying(monkeypatch) as spy:
        spy.unkillable = True
        if stage is not None:
            setattr(spy, stage, fault)
        # A timeout says so; a failure stays itself, not an unknown answer.
        with pytest.raises(lease_probe.UnsettledHelper if fault is None else OSError):
            run_probe(str(path), python=silent, timeout=0.5, grace=0.5)
        if fault is not None:
            assert any("not settled" in note for note in fault.__notes__)
        (process,) = spy.created
        # Not forgotten: still running, and still owned through its Popen.
        assert process.poll() is None and owned == [process]
        # Nothing more is asked while it lives: no second helper starts.
        with pytest.raises(lease_probe.UnsettledHelper):
            run_probe(str(path), grace=0.5)
        assert len(spy.created) == 1
        # Once it can die, settling reaps it and the next probe is asked.
        spy.unkillable = False
        spy.forked = spy.collecting = None
        lease_probe.settle()
        assert _reaped(process, -signal.SIGKILL) and owned == []
        assert run_probe(str(path))["state"] == FREE


def test_the_cleanup_never_signals_a_reaped_helpers_number(tmp_path, monkeypatch):
    path = tmp_path / "profile.lock"
    path.write_text("")
    sent: list[tuple[int, int]] = []
    with monkeypatch.context() as scoped:
        with _spying(scoped) as spy:
            assert run_probe(str(path))["state"] == FREE
            (process,) = spy.created
            assert process.returncode == 0
            # The number now names someone else's live process.
            scoped.setattr(os, "kill", lambda pid, number: sent.append((pid, number)))
    assert sent == []


def test_the_cleanup_ends_a_live_helper_after_a_failed_assertion(
    tmp_path, monkeypatch, silent
):
    with pytest.raises(AssertionError, match="planted"):
        with _spying(monkeypatch) as spy:
            spy.unkillable = True
            with pytest.raises(lease_probe.UnsettledHelper):
                run_probe(
                    str(tmp_path / "profile.lock"),
                    python=silent,
                    timeout=0.5,
                    grace=0.5,
                )
            assert spy.created[0].poll() is None
            raise AssertionError("planted: the primary assertion failed")
    (process,) = spy.created
    assert _reaped(process, -signal.SIGKILL)


def test_the_helper_runs_without_site_processing(tmp_path):
    # Whatever overlay an interpreter carries, -I -S processes none of it.
    path = tmp_path / "profile.lock"
    path.write_text("")
    answer = run_probe(str(path))
    assert answer["state"] == FREE, answer
