"""The non-announcing lease contender, on temporary locks only.

Held by the product's own lease in this process, free after its release,
unknown for everything that is not an answer: a missing, linked or replaced
file, a directory, an errno other than contention, a helper that does not
answer. Nothing the probe does creates, rewrites or announces anything.
"""

from __future__ import annotations

import contextlib
import errno
import os
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


def test_a_helper_that_never_answers_is_unknown_and_reaped(tmp_path):
    silent = tmp_path / "silent-python"
    pid_file = tmp_path / "helper.pid"
    silent.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 30\n")
    silent.chmod(0o700)
    answer = run_probe(str(tmp_path / "profile.lock"), python=str(silent), timeout=1.0)
    assert answer["state"] == UNKNOWN and "did not answer" in answer["reason"]
    helper = int(pid_file.read_text())
    try:
        # Reaped: not even a zombie is left of this test's own helper.
        with pytest.raises(ProcessLookupError):
            os.kill(helper, 0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(helper, 9)


def test_the_helper_runs_without_site_processing(tmp_path):
    # Whatever overlay an interpreter carries, -I -S processes none of it.
    path = tmp_path / "profile.lock"
    path.write_text("")
    answer = run_probe(str(path))
    assert answer["state"] == FREE, answer
