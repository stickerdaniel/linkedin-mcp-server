"""A login attempt's own directory, its sweep, and its promotion to the source."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from linkedin_mcp_server import login_attempt
from linkedin_mcp_server.bootstrap import _auth_ready
from linkedin_mcp_server.exceptions import ProfileRootRefusedError
from linkedin_mcp_server.login_attempt import (
    PendingLogin,
    attempts_root,
    open_pending_login,
    promote,
    sweep,
)
from linkedin_mcp_server.private_state import PrivateStateError
from linkedin_mcp_server.session_state import (
    advance_logout_epoch,
    clear_auth_state,
    load_source_state,
    portable_cookie_path,
    quarantine_dirs,
    source_state_path,
    write_source_state,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="symbolic links")


def _write_session(profile: Path, content: str = "previous") -> str:
    (profile / "Default").mkdir(parents=True, exist_ok=True)
    (profile / "Default" / "Cookies").write_text(content)
    portable_cookie_path(profile).write_text(f'["{content}"]')
    return write_source_state(profile).login_generation


def _sign_in(pending: PendingLogin, content: str = "fresh") -> None:
    (pending.profile_dir / "Default").mkdir(parents=True)
    (pending.profile_dir / "Default" / "Cookies").write_text(content)
    pending.cookie_path.write_text(f'["{content}"]')


def _entries(root: Path) -> list[str]:
    return sorted(entry.name for entry in root.iterdir()) if root.is_dir() else []


@pytest.fixture
def opened(isolate_profile_dir) -> Iterator[list[PendingLogin]]:
    """Attempts a test opened, discarded afterwards so their guardians end."""
    attempts: list[PendingLogin] = []
    yield attempts
    for pending in attempts:
        if pending.lock_path.exists() or pending.guardian is not None:
            pending.discard()


def _open(opened: list[PendingLogin], profile: Path) -> PendingLogin:
    pending = open_pending_login(profile)
    opened.append(pending)
    return pending


def _hold_lock(path: Path) -> subprocess.Popen[str]:
    """A second process holding *path* the way an attempt's owner does."""
    code = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        from linkedin_mcp_server.profile_lease import acquire_locked_fd
        fd = acquire_locked_fd(Path(sys.argv[1]), exclusive=True)
        print("HELD" if fd is not None else "BUSY", flush=True)
        sys.stdin.readline()
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(path)],
        cwd=_REPO_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "HELD"
    return process


def _abandon(root: Path) -> str:
    """An attempt whose owner is gone: a lock nobody holds and its directory."""
    name = str(uuid4())
    (root / name / "profile").mkdir(parents=True)
    (root / f"{name}.lock").write_text("")
    return name


class TestPromotion:
    def test_it_installs_profile_cookies_and_state_at_the_source(
        self, isolate_profile_dir, opened
    ):
        profile = isolate_profile_dir
        pending = _open(opened, profile)
        _sign_in(pending)

        assert promote(pending, superseded_by=None) == "promoted"

        state = load_source_state(profile)
        assert state is not None
        assert state.profile_path == str(profile)
        assert state.cookies_path == str(portable_cookie_path(profile))
        assert (profile / "Default" / "Cookies").read_text() == "fresh"
        assert portable_cookie_path(profile).read_text() == '["fresh"]'
        assert _auth_ready(profile)
        pending.discard()
        assert _entries(attempts_root(profile)) == []

    def test_a_peer_session_is_left_as_it_is(self, isolate_profile_dir, opened):
        profile = isolate_profile_dir
        pending = _open(opened, profile)
        _sign_in(pending)
        peer = _write_session(profile, "peer")

        assert promote(pending, superseded_by=None) == "peer"

        state = load_source_state(profile)
        assert state is not None and state.login_generation == peer
        assert (profile / "Default" / "Cookies").read_text() == "peer"
        assert portable_cookie_path(profile).read_text() == '["peer"]'
        assert quarantine_dirs(profile) == []

    def test_an_explicit_login_replaces_what_it_finds(
        self, isolate_profile_dir, opened
    ):
        profile = isolate_profile_dir
        pending = _open(opened, profile)
        _sign_in(pending)
        previous = _write_session(profile)

        assert promote(pending) == "promoted"

        state = load_source_state(profile)
        assert state is not None and state.login_generation != previous
        assert (profile / "Default" / "Cookies").read_text() == "fresh"
        assert len(quarantine_dirs(profile)) == 1

    def test_a_logout_since_the_attempt_started_fences_it(
        self, isolate_profile_dir, opened
    ):
        profile = isolate_profile_dir
        pending = _open(opened, profile)
        _sign_in(pending)
        clear_auth_state(profile)

        assert promote(pending, superseded_by=None) == "fenced"

        assert not profile.exists()
        assert not portable_cookie_path(profile).exists()
        assert not source_state_path(profile).exists()

    @pytest.mark.parametrize("step", ["profile", "cookies", "state"])
    @pytest.mark.parametrize("prior", [True, False], ids=["prior", "first"])
    def test_a_failed_step_leaves_the_old_session_or_none(
        self, isolate_profile_dir, opened, monkeypatch, step, prior
    ):
        profile = isolate_profile_dir
        pending = _open(opened, profile)
        _sign_in(pending)
        previous = _write_session(profile) if prior else None

        real_rename, real_replace = os.rename, os.replace

        def rename(src, dst, *args, **kwargs):
            if step == "profile" and Path(src) == pending.profile_dir:
                raise OSError("injected rename failure")
            return real_rename(src, dst, *args, **kwargs)

        def replace(src, dst, *args, **kwargs):
            if step == "cookies" and Path(src) == pending.cookie_path:
                raise OSError("injected replace failure")
            return real_replace(src, dst, *args, **kwargs)

        def write_state(_profile):
            raise OSError("injected state failure")

        monkeypatch.setattr(login_attempt.os, "rename", rename)
        monkeypatch.setattr(login_attempt.os, "replace", replace)
        if step == "state":
            monkeypatch.setattr(login_attempt, "write_source_state", write_state)

        with pytest.raises(OSError, match="injected"):
            promote(pending, superseded_by=previous)

        state = load_source_state(profile)
        if prior:
            assert state is not None and state.login_generation == previous
            assert (profile / "Default" / "Cookies").read_text() == "previous"
            assert portable_cookie_path(profile).read_text() == '["previous"]'
            assert _auth_ready(profile)
        else:
            assert not source_state_path(profile).exists()
            assert not _auth_ready(profile)

    def test_a_death_inside_rotation_leaves_readiness_false_and_the_next_login_wins(
        self, isolate_profile_dir, opened
    ):
        profile = isolate_profile_dir
        previous = _write_session(profile)
        code = textwrap.dedent(
            """
            import os, shutil, sys
            from pathlib import Path
            from linkedin_mcp_server import session_state
            real_move = shutil.move

            def move_once_then_die(src, dst, *args, **kwargs):
                real_move(src, dst, *args, **kwargs)
                os._exit(17)

            session_state.shutil.move = move_once_then_die
            session_state.rotate_source_profile(Path(sys.argv[1]))
            """
        )
        died = subprocess.run(
            [sys.executable, "-c", code, str(profile)], cwd=_REPO_ROOT, check=False
        )
        assert died.returncode == 17
        assert not _auth_ready(profile)

        pending = _open(opened, profile)
        _sign_in(pending)
        assert promote(pending, superseded_by=previous) == "promoted"

        state = load_source_state(profile)
        assert state is not None and state.login_generation != previous
        assert _auth_ready(profile)


class TestCreation:
    def test_an_unclaimed_root_is_refused_and_nothing_is_created(self, tmp_path):
        foreign = tmp_path / "foreign" / "profile"
        foreign.parent.mkdir()

        with pytest.raises(ProfileRootRefusedError):
            open_pending_login(foreign)

        assert _entries(foreign.parent) == []

    @_POSIX_ONLY
    def test_a_symlinked_attempts_root_is_refused(self, isolate_profile_dir, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        planted = _abandon(elsewhere)
        attempts_root(isolate_profile_dir).symlink_to(elsewhere)

        with pytest.raises(PrivateStateError):
            open_pending_login(isolate_profile_dir)

        assert _entries(elsewhere) == sorted([planted, f"{planted}.lock"])

    def test_the_attempt_is_inside_the_auth_root_with_its_lock_beside_it(
        self, isolate_profile_dir, opened
    ):
        pending = _open(opened, isolate_profile_dir)

        root = attempts_root(isolate_profile_dir)
        assert pending.directory.parent == root
        assert pending.lock_path == root / f"{pending.directory.name}.lock"
        assert pending.profile_dir.parent == pending.directory
        assert not isolate_profile_dir.exists()


class TestSweep:
    def test_an_abandoned_attempt_and_its_lock_are_removed(self, isolate_profile_dir):
        root = attempts_root(isolate_profile_dir)
        root.mkdir()
        _abandon(root)

        sweep(root)

        assert _entries(root) == []

    def test_an_attempt_whose_lock_another_process_holds_is_kept(
        self, isolate_profile_dir
    ):
        root = attempts_root(isolate_profile_dir)
        root.mkdir()
        name = _abandon(root)
        holder = _hold_lock(root / f"{name}.lock")
        try:
            sweep(root)
            assert _entries(root) == sorted([name, f"{name}.lock"])
        finally:
            assert holder.stdin is not None
            holder.stdin.close()
            holder.wait(timeout=30)

        sweep(root)
        assert _entries(root) == []

    def test_entries_it_did_not_create_are_left_alone(
        self, isolate_profile_dir, tmp_path
    ):
        root = attempts_root(isolate_profile_dir)
        root.mkdir()
        (root / "not-a-uuid").mkdir()
        (root / "not-a-uuid.lock").write_text("")
        orphan = str(uuid4())
        (root / orphan).mkdir()
        expected = ["not-a-uuid", "not-a-uuid.lock", orphan]
        if os.name != "nt":
            target = tmp_path / "target"
            (target / "keep").mkdir(parents=True)
            linked = str(uuid4())
            (root / linked).symlink_to(target)
            (root / f"{linked}.lock").write_text("")
            lock_target = tmp_path / "lock-target"
            lock_target.write_text("keep")
            linked_lock = str(uuid4())
            (root / linked_lock).mkdir()
            (root / f"{linked_lock}.lock").symlink_to(lock_target)
            expected += [linked, f"{linked}.lock", linked_lock, f"{linked_lock}.lock"]

        sweep(root)

        assert _entries(root) == sorted(expected)
        if os.name != "nt":
            assert (tmp_path / "target" / "keep").is_dir()
            assert (tmp_path / "lock-target").read_text() == "keep"

    def test_two_sweepers_over_the_same_abandoned_attempts_both_finish(
        self, isolate_profile_dir
    ):
        root = attempts_root(isolate_profile_dir)
        root.mkdir()
        for _ in range(40):
            _abandon(root)
        code = "import sys; from pathlib import Path; from linkedin_mcp_server.login_attempt import sweep; sweep(Path(sys.argv[1]))"
        sweepers = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(root)],
                cwd=_REPO_ROOT,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        results = [
            (sweeper.wait(timeout=60), sweeper.communicate()[1]) for sweeper in sweepers
        ]

        assert results == [(0, ""), (0, "")]
        assert _entries(root) == []

    def test_a_new_attempt_sweeps_before_it_starts(self, isolate_profile_dir, opened):
        root = attempts_root(isolate_profile_dir)
        root.mkdir()
        abandoned = _abandon(root)

        pending = _open(opened, isolate_profile_dir)

        assert abandoned not in _entries(root)
        assert _entries(root) == sorted(
            [pending.directory.name, pending.lock_path.name]
        )


class TestLogout:
    def test_logout_removes_abandoned_attempts_and_keeps_a_held_one(
        self, isolate_profile_dir
    ):
        _write_session(isolate_profile_dir)
        root = attempts_root(isolate_profile_dir)
        root.mkdir()
        abandoned = _abandon(root)
        (root / abandoned / "cookies.json").write_text('["li_at"]')
        held = _abandon(root)
        holder = _hold_lock(root / f"{held}.lock")
        try:
            assert clear_auth_state(isolate_profile_dir)
            assert _entries(root) == sorted([held, f"{held}.lock"])
        finally:
            assert holder.stdin is not None
            holder.stdin.close()
            holder.wait(timeout=30)

    def test_logout_with_no_stored_session_still_removes_abandoned_attempts(
        self, isolate_profile_dir
    ):
        root = attempts_root(isolate_profile_dir)
        root.mkdir()
        abandoned = _abandon(root)
        (root / abandoned / "cookies.json").write_text('["li_at"]')

        assert advance_logout_epoch(isolate_profile_dir, expect_empty=True)

        assert _entries(root) == []

    @_POSIX_ONLY
    def test_logout_does_not_follow_a_linked_attempts_directory(
        self, isolate_profile_dir, tmp_path
    ):
        _write_session(isolate_profile_dir)
        target = tmp_path / "elsewhere"
        target.mkdir()
        _abandon(target)
        attempts_root(isolate_profile_dir).symlink_to(target)

        clear_auth_state(isolate_profile_dir)

        assert len(_entries(target)) == 2


class TestEndStates:
    def test_a_discarded_attempt_leaves_nothing(self, isolate_profile_dir, opened):
        pending = _open(opened, isolate_profile_dir)
        _sign_in(pending)

        pending.discard()

        assert _entries(attempts_root(isolate_profile_dir)) == []

    def test_a_promoted_attempt_leaves_nothing(self, isolate_profile_dir, opened):
        pending = _open(opened, isolate_profile_dir)
        _sign_in(pending)
        assert promote(pending) == "promoted"

        pending.discard()

        assert _entries(attempts_root(isolate_profile_dir)) == []

    def test_an_attempt_still_held_by_its_owner_survives_another_sweep(
        self, isolate_profile_dir, opened
    ):
        pending = _open(opened, isolate_profile_dir)
        _sign_in(pending)
        code = "import sys; from pathlib import Path; from linkedin_mcp_server.login_attempt import sweep; sweep(Path(sys.argv[1]))"

        subprocess.run(
            [sys.executable, "-c", code, str(pending.directory.parent)],
            cwd=_REPO_ROOT,
            check=True,
        )

        assert (pending.profile_dir / "Default" / "Cookies").read_text() == "fresh"
