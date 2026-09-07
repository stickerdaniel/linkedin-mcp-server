"""Storage that only the current account can read.

The Windows tests here run only on Windows and are the reason the CI matrix
covers it: the ACL path cannot be exercised anywhere else, and mocking it would
only assert that the mock was called.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from linkedin_mcp_server import private_state
from linkedin_mcp_server.private_state import (
    PrivateStateError,
    harden_created_file,
    harden_directory,
    harden_directory_entry,
    harden_file,
)

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits do not exist on Windows"
)
windows_only = pytest.mark.skipif(
    os.name != "nt", reason="Windows ACLs cannot be set or read on POSIX"
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestHardeningOnPosix:
    @posix_only
    def test_a_new_directory_is_owner_only(self, tmp_path: Path):
        target = tmp_path / "auth-root" / "daemon"

        harden_directory(target)

        assert _mode(target) == 0o700

    @posix_only
    def test_an_existing_wide_directory_is_narrowed(self, tmp_path: Path):
        # The case that actually happens: the auth root predates this code, or
        # the user made it, and it carries whatever the umask gave it. Creating
        # the token inside it with 0600 would look right while the directory
        # let anyone list and open it.
        target = tmp_path / "daemon"
        target.mkdir(mode=0o755)

        harden_directory(target)

        assert _mode(target) == 0o700

    @posix_only
    def test_hardening_works_outside_a_linkedin_mcp_tree(self, tmp_path: Path):
        # USER_DATA_DIR can point anywhere, and the older tree-hardening helper
        # deliberately does nothing outside a .linkedin-mcp directory. A token
        # stored under a custom root has to be protected just the same.
        target = tmp_path / "somewhere-else" / "daemon"
        target.parent.mkdir(mode=0o755)
        target.mkdir(mode=0o755)

        harden_directory(target)

        assert _mode(target) == 0o700

    @posix_only
    def test_a_file_is_owner_only(self, tmp_path: Path):
        target = tmp_path / "token"
        target.touch(mode=0o644)

        harden_file(target)

        assert _mode(target) == 0o600

    @posix_only
    def test_repeated_hardening_is_stable(self, tmp_path: Path):
        # Called on every daemon start, so it has to be safe to repeat.
        target = tmp_path / "daemon"

        harden_directory(target)
        harden_directory(target)

        assert _mode(target) == 0o700

    @posix_only
    def test_an_owned_child_directory_is_created_private_under_umask_zero(
        self, tmp_path: Path
    ):
        target = tmp_path / "state" / "per-auth"
        target.parent.mkdir()
        previous = os.umask(0)
        try:
            harden_directory_entry(target)
        finally:
            os.umask(previous)

        assert _mode(target) == 0o700

    @posix_only
    def test_an_owned_child_directory_refuses_a_planted_symlink(self, tmp_path: Path):
        parent = tmp_path / "state"
        parent.mkdir(mode=0o777)
        parent.chmod(0o777)
        target = parent / "per-auth"
        elsewhere = tmp_path / "attacker-state"
        elsewhere.mkdir(mode=0o777)
        elsewhere.chmod(0o777)
        target.symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(PrivateStateError, match="symbolic link"):
            harden_directory_entry(target)

        assert _mode(elsewhere) == 0o777


class TestExtendedAcls:
    """Access the permission bits do not describe.

    On macOS an ACL sits alongside the mode rather than inside it, so a file
    can report 0600 while another account still reads it. Checking only the
    mode is not checking who has access.
    """

    @pytest.mark.skipif(
        sys.platform != "darwin", reason="extended ACLs are a macOS mechanism here"
    )
    def test_an_inherited_acl_does_not_survive_hardening(self, tmp_path: Path):
        # Measured before the fix: an auth root carrying an inheritable
        # everyone entry produced a token reporting exactly 0600 that everyone
        # could still read, and hardening returned without complaint.
        subprocess.run(
            [
                "chmod",
                "+a",
                "everyone allow read,list,search,file_inherit,directory_inherit",
                str(tmp_path),
            ],
            check=True,
            capture_output=True,
        )

        target = tmp_path / "daemon"
        harden_directory(target)
        token = target / "token"
        token.touch()
        harden_file(token)

        for path in (target, token):
            listing = subprocess.run(
                ["ls", "-lde", str(path)], capture_output=True, text=True, check=True
            ).stdout
            entries = [
                line
                for line in listing.splitlines()[1:]
                if line.strip() and line.strip()[0].isdigit()
            ]
            assert entries == [], f"{path} still grants access outside its mode"

    @pytest.mark.skipif(
        sys.platform != "darwin", reason="extended ACLs are a macOS mechanism here"
    )
    def test_an_acl_that_cannot_be_cleared_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Fails closed. If the entries cannot be removed, the caller must not
        # go on to write a secret into a file other accounts can read.
        target = tmp_path / "daemon"
        target.mkdir()
        subprocess.run(
            ["chmod", "+a", "everyone allow read", str(target)],
            check=True,
            capture_output=True,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.private_state._drop_extended_acl", lambda path: None
        )

        with pytest.raises(PrivateStateError, match="access control list"):
            harden_directory(target)

    @pytest.mark.skipif(
        sys.platform != "darwin", reason="extended ACLs are a macOS mechanism here"
    )
    def test_hardening_does_not_depend_on_an_executable_being_findable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # This ran chmod and read ls once, and that failed open: measured with
        # no usable PATH, hardening reported success while everyone could still
        # read the token, because neither step could run and both treated that
        # as nothing to report. Going through libc removes the dependency
        # rather than adding a check for it.
        subprocess.run(
            [
                "chmod",
                "+a",
                "everyone allow read,list,search,file_inherit,directory_inherit",
                str(tmp_path),
            ],
            check=True,
            capture_output=True,
        )
        monkeypatch.setenv("PATH", "")

        target = tmp_path / "daemon"
        harden_directory(target)
        token = target / "token"
        token.touch()
        harden_file(token)

        listing = subprocess.run(
            ["/bin/ls", "-lde", str(token)], capture_output=True, text=True, check=True
        ).stdout
        entries = [
            line
            for line in listing.splitlines()[1:]
            if line.strip() and line.strip()[0].isdigit()
        ]
        assert entries == [], "the token is still readable outside this account"

    @pytest.mark.skipif(
        sys.platform != "darwin", reason="extended ACLs are a macOS mechanism here"
    )
    def test_concurrent_first_use_does_not_skip_the_access_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The library is resolved once and cached. Publishing "resolved" before
        # the cache was filled let a second thread read an empty cache, which
        # reads as "this platform has no access lists" everywhere below.
        # Measured: that thread hardened a directory which kept an inherited
        # everyone entry, and the call reported success.
        import threading
        import time

        from linkedin_mcp_server import private_state

        monkeypatch.setattr(private_state, "_libc_resolved", False)
        monkeypatch.setattr(private_state, "_libc_cache", None)
        real_find = private_state.ctypes.util.find_library
        monkeypatch.setattr(
            private_state.ctypes.util,
            "find_library",
            lambda name: (time.sleep(0.2), real_find(name))[1],
        )

        hardened: list[Path] = []

        def harden(index: int) -> None:
            root = tmp_path / f"root{index}"
            root.mkdir()
            subprocess.run(
                [
                    "chmod",
                    "+a",
                    "everyone allow list,file_inherit,directory_inherit",
                    str(root),
                ],
                check=True,
                capture_output=True,
            )
            target = root / "daemon"
            harden_directory(target)
            hardened.append(target)

        threads = [threading.Thread(target=harden, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(hardened) == 4
        for target in hardened:
            listing = subprocess.run(
                ["/bin/ls", "-lde", str(target)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            entries = [
                line
                for line in listing.splitlines()[1:]
                if line.strip() and line.strip()[0].isdigit()
            ]
            assert entries == [], f"{target} kept an inherited access list"

    def test_resolution_survives_an_audit_hook_reaching_back_in(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Loading a library raises a ctypes.dlopen audit event, and a hook on
        # it runs synchronously inside the resolution lock. Measured with a
        # plain lock: a hook that called back in left the resolving thread
        # waiting on itself and it never finished.
        import sys
        import threading

        from linkedin_mcp_server import private_state

        monkeypatch.setattr(private_state, "_libc_resolved", False)
        monkeypatch.setattr(private_state, "_libc_cache", None)

        reentered: list[str] = []

        def hook(event: str, args: object) -> None:
            if event == "ctypes.dlopen" and not reentered:
                reentered.append(event)
                private_state._libc()

        sys.addaudithook(hook)  # cannot be removed, hence the one-shot guard

        finished = threading.Event()

        def resolve() -> None:
            private_state._libc()
            finished.set()

        threading.Thread(target=resolve, daemon=True).start()

        assert finished.wait(5), "resolution deadlocked against its own lock"


class TestRefusals:
    def test_hardening_a_missing_file_refuses(self, tmp_path: Path):
        # Hardening has to happen before the secret is written. Being asked to
        # harden something that does not exist means the caller has the order
        # wrong, and staying quiet would hide it until the file was readable.
        with pytest.raises(PrivateStateError, match="does not exist"):
            harden_file(tmp_path / "absent")

    @posix_only
    def test_hardening_a_linked_file_refuses(self, tmp_path: Path):
        # chmod follows a link, so hardening one would set 0600 on whatever it
        # points at and report the secret's own file as private. Measured: an
        # unrelated 0644 file became 0600 while the caller was told its token
        # was protected.
        elsewhere = tmp_path / "someone-elses"
        elsewhere.write_text("not ours")
        elsewhere.chmod(0o644)
        link = tmp_path / "token"
        link.symlink_to(elsewhere)

        with pytest.raises(PrivateStateError, match="symbolic link"):
            harden_file(link)

        assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o644

    @posix_only
    def test_a_file_swapped_for_a_link_mid_hardening_is_not_followed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Checking the path and then chmodding it resolves the name twice, so a
        # swap in between is followed by the second resolution. Measured with a
        # path-based implementation: an unrelated file became 0600 while this
        # reported the token as private. Hardening through the descriptor keeps
        # the other file untouched, and the swap is then reported rather than
        # passed off as success.
        token = tmp_path / "token"
        token.touch()
        token.chmod(0o644)
        victim = tmp_path / "victim"
        victim.write_text("not ours")
        victim.chmod(0o644)
        real = os.fchmod

        def swap_then_chmod(fd: int, mode: int) -> None:
            if token.exists() and not token.is_symlink():
                token.unlink()
                token.symlink_to(victim)
            real(fd, mode)

        monkeypatch.setattr(os, "fchmod", swap_then_chmod)

        with pytest.raises(PrivateStateError, match="was replaced"):
            harden_file(token)

        assert stat.S_IMODE(victim.stat().st_mode) == 0o644

    @posix_only
    def test_a_failure_while_hardening_is_this_module_s_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The descriptor half can fail too: fstat, fchmod and the access list
        # calls each have their own OSError. Measured with fchmod answering EIO
        # before the translation covered them: it crossed the boundary as
        # itself.
        target = tmp_path / "token"
        target.touch()

        def failing(fd: int, mode: int) -> None:
            raise OSError(errno.EIO, "I/O error")

        monkeypatch.setattr(os, "fchmod", failing)

        with pytest.raises(PrivateStateError, match="make private"):
            harden_file(target)

    @pytest.mark.parametrize(
        "path",
        [Path("~definitely-no-such-user-xyz/state"), Path("/tmp/state\x00x")],
    )
    def test_a_path_that_cannot_be_resolved_is_refused(self, path: Path):
        # Both arrive from configuration a caller was entitled to supply, and
        # both used to leave this module as somebody else's exception: an
        # embedded NUL as ValueError, and an unknown user as a directory
        # literally named "~something" created wherever the process was
        # running. Measured: one appeared in the working directory.
        with pytest.raises(PrivateStateError):
            harden_directory(path)

        assert not path.exists()

    def test_a_file_where_a_directory_belongs_refuses(self, tmp_path: Path):
        occupied = tmp_path / "daemon"
        occupied.write_text("not a directory")

        with pytest.raises(PrivateStateError, match="Not a directory"):
            harden_directory(occupied)
        with pytest.raises(PrivateStateError, match="Not a directory"):
            harden_directory_entry(occupied)

    def test_a_windows_reparse_directory_is_refused_before_acl_hardening(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import windows_acl

        target = tmp_path / "per-auth"
        target.mkdir()
        entry = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700,
            st_dev=1,
            st_ino=2,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            st_reparse_tag=0x8000001B,
        )
        monkeypatch.setattr(private_state, "_WINDOWS", True)
        monkeypatch.setattr(Path, "lstat", lambda path: entry)
        monkeypatch.setattr(
            windows_acl,
            "restrict_to_current_user",
            lambda *args, **kwargs: pytest.fail("a reparse target was hardened"),
        )

        with pytest.raises(PrivateStateError, match="Windows reparse point"):
            harden_directory_entry(target)

    def test_windows_directory_replacement_during_acl_hardening_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import windows_acl

        target = tmp_path / "per-auth"
        target.mkdir()
        entries = iter(
            [
                SimpleNamespace(
                    st_mode=stat.S_IFDIR | 0o700,
                    st_dev=1,
                    st_ino=2,
                    st_file_attributes=0,
                ),
                SimpleNamespace(
                    st_mode=stat.S_IFDIR | 0o700,
                    st_dev=1,
                    st_ino=3,
                    st_file_attributes=0,
                ),
            ]
        )
        monkeypatch.setattr(private_state, "_WINDOWS", True)
        monkeypatch.setattr(Path, "lstat", lambda path: next(entries))
        monkeypatch.setattr(windows_acl, "verify_owner_only", lambda *a, **k: None)

        with pytest.raises(PrivateStateError, match="was replaced"):
            harden_directory_entry(target)

    def test_existing_windows_directory_is_verified_without_acl_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import windows_acl

        target = tmp_path / "daemon"
        target.mkdir()
        verified: list[tuple[Path, bool]] = []
        monkeypatch.setattr(private_state, "_WINDOWS", True)
        monkeypatch.setattr(
            windows_acl,
            "verify_owner_only",
            lambda path, *, directory: verified.append((path, directory)),
        )
        monkeypatch.setattr(
            windows_acl,
            "restrict_to_current_user",
            lambda *args, **kwargs: pytest.fail("an existing DACL was replaced"),
        )

        harden_directory(target)

        assert verified == [(target, True)]

    def test_existing_windows_file_is_verified_without_acl_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import windows_acl

        target = tmp_path / "token"
        target.touch()
        verified: list[tuple[Path, bool]] = []
        monkeypatch.setattr(private_state, "_WINDOWS", True)
        monkeypatch.setattr(
            windows_acl,
            "verify_owner_only",
            lambda path, *, directory: verified.append((path, directory)),
        )
        monkeypatch.setattr(
            windows_acl,
            "restrict_to_current_user",
            lambda *args, **kwargs: pytest.fail("an existing DACL was replaced"),
        )

        harden_file(target)

        assert verified == [(target, False)]

    def test_new_windows_file_replaces_its_inherited_acl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import windows_acl

        target = tmp_path / "token"
        target.touch()
        entry = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_dev=1,
            st_ino=2,
            st_file_attributes=0,
        )
        restricted: list[tuple[Path, bool]] = []
        monkeypatch.setattr(private_state, "_WINDOWS", True)
        monkeypatch.setattr(Path, "lstat", lambda path: entry)
        monkeypatch.setattr(
            windows_acl,
            "restrict_to_current_user",
            lambda path, *, directory: restricted.append((path, directory)),
        )
        monkeypatch.setattr(
            windows_acl,
            "verify_owner_only",
            lambda *args, **kwargs: pytest.fail("a created file used verify-only"),
        )

        harden_created_file(target)

        assert restricted == [(target, False)]

    def test_new_windows_file_replacement_during_acl_hardening_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import windows_acl

        target = tmp_path / "token"
        target.touch()
        entries = iter(
            [
                SimpleNamespace(
                    st_mode=stat.S_IFREG | 0o600,
                    st_dev=1,
                    st_ino=2,
                    st_file_attributes=0,
                ),
                SimpleNamespace(
                    st_mode=stat.S_IFREG | 0o600,
                    st_dev=1,
                    st_ino=3,
                    st_file_attributes=0,
                ),
            ]
        )
        monkeypatch.setattr(private_state, "_WINDOWS", True)
        monkeypatch.setattr(Path, "lstat", lambda path: next(entries))
        monkeypatch.setattr(
            windows_acl, "restrict_to_current_user", lambda *a, **k: None
        )

        with pytest.raises(PrivateStateError, match="was replaced"):
            harden_created_file(target)

    def test_new_windows_child_is_hardened_before_publication(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        target = tmp_path / "per-auth"
        hardened: list[Path] = []

        def harden(path: Path) -> None:
            assert path != target
            assert path.parent == target.parent
            assert not target.exists()
            hardened.append(path)

        monkeypatch.setattr(private_state, "_WINDOWS", True)
        monkeypatch.setattr(private_state, "harden_created_directory", harden)

        harden_directory_entry(target)

        assert len(hardened) == 1
        assert target.is_dir()
        assert not hardened[0].exists()

    def test_concurrent_windows_child_publication_verifies_the_winner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import windows_acl

        target = tmp_path / "per-auth"
        verified: list[Path] = []

        def harden(_path: Path) -> None:
            target.mkdir()

        real_rename = Path.rename

        def windows_rename(path: Path, destination: Path) -> Path:
            if destination == target and target.exists():
                raise FileExistsError(destination)
            return real_rename(path, destination)

        monkeypatch.setattr(private_state, "_WINDOWS", True)
        monkeypatch.setattr(Path, "rename", windows_rename)
        monkeypatch.setattr(private_state, "harden_created_directory", harden)
        monkeypatch.setattr(
            private_state, "_refuse_windows_reparse_point", lambda *args: None
        )
        monkeypatch.setattr(
            windows_acl,
            "verify_owner_only",
            lambda path, *, directory: verified.append(path),
        )

        harden_directory_entry(target)

        assert verified == [target]
        assert target.is_dir()
        assert list(tmp_path.glob(".private-*")) == []

    def test_failed_windows_child_hardening_removes_the_fresh_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        target = tmp_path / "per-auth"
        monkeypatch.setattr(private_state, "_WINDOWS", True)
        monkeypatch.setattr(
            private_state, "_refuse_windows_reparse_point", lambda *args: None
        )
        monkeypatch.setattr(
            private_state,
            "harden_created_directory",
            lambda _path: (_ for _ in ()).throw(PrivateStateError("ACL failure")),
        )

        with pytest.raises(PrivateStateError, match="ACL failure"):
            harden_directory_entry(target)

        assert not target.exists()
        assert list(tmp_path.glob(".private-*")) == []

    def test_old_windows_python_refuses_before_creating_a_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        target = tmp_path / "per-auth"
        monkeypatch.setattr(private_state, "_WINDOWS", True)
        monkeypatch.setattr(private_state.sys, "version_info", (3, 12, 3))

        with pytest.raises(PrivateStateError, match="3.12.4 or newer"):
            harden_directory_entry(target)

        assert not target.exists()

    def test_windows_missing_reparse_attributes_fail_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        target = tmp_path / "per-auth"
        target.mkdir()
        entry = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_dev=1, st_ino=2)
        monkeypatch.setattr(private_state, "_WINDOWS", True)
        monkeypatch.setattr(Path, "lstat", lambda path: entry)

        with pytest.raises(PrivateStateError, match="did not report file attributes"):
            harden_directory_entry(target)

    @posix_only
    def test_an_owned_child_directory_refuses_another_account_s_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        target = tmp_path / "per-auth"
        target.mkdir(mode=0o777)
        target.chmod(0o777)
        monkeypatch.setattr(os, "geteuid", lambda: target.stat().st_uid + 1)

        with pytest.raises(PrivateStateError, match="is owned by uid"):
            harden_directory_entry(target)

    @pytest.mark.skipif(
        sys.platform != "darwin", reason="macOS is where an access list needs libc"
    )
    def test_missing_access_list_functions_refuse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Measured before the check: with the bindings absent, a directory
        # carrying an inherited everyone entry came back from hardening
        # unchanged while the call reported success. On macOS these functions
        # are part of libc, so their absence is a broken system rather than a
        # platform without access lists.
        monkeypatch.setattr(private_state, "_libc", lambda: None)

        with pytest.raises(PrivateStateError, match="access list functions"):
            harden_directory(tmp_path / "daemon")

    @posix_only
    def test_an_unreadable_access_list_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # acl_get_file returns NULL for any failure and sets errno to say which.
        # Only the errnos meaning "there is no list" are an answer; reading
        # EACCES as one would report a path as private without having checked.
        target = tmp_path / "daemon"
        target.mkdir()

        class _Failing:
            def __call__(self, *args: object) -> None:
                ctypes.set_errno(errno.EACCES)
                return None

        library = private_state._libc()
        if library is None:
            pytest.skip("this platform has no access list functions to fail")
        monkeypatch.setattr(library, "acl_get_file", _Failing())

        with pytest.raises(PrivateStateError, match="Could not read the access list"):
            private_state._has_extended_acl(target)

    @posix_only
    def test_a_path_owned_by_another_account_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # 0600 says "only the owner", which is worth nothing when the owner is
        # somebody else: the bits grant them, and they can widen them again. It
        # takes a privileged process to reach this, which is exactly what a
        # container started against a pre-existing state tree is.
        target = tmp_path / "token"
        target.touch()
        monkeypatch.setattr(os, "geteuid", lambda: os.stat(target).st_uid + 1)

        with pytest.raises(PrivateStateError, match="is owned by uid"):
            harden_file(target)

    @posix_only
    def test_a_filesystem_that_ignores_chmod_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A share or a FAT volume accepts chmod and keeps the old mode. Without
        # reading the mode back, the secret lands on disk readable and nothing
        # says so. Simulated, since neither can be mounted in a test run.
        target = tmp_path / "token"
        target.touch(mode=0o644)
        # fchmod rather than Path.chmod: a file is hardened through the
        # descriptor it was opened on, so that is the call such a filesystem
        # would accept and ignore.
        monkeypatch.setattr(os, "fchmod", lambda fd, mode: None)

        with pytest.raises(PrivateStateError, match="owner-only"):
            harden_file(target)


class TestWindowsAcl:
    """Only meaningful on Windows, where a DACL rather than a mode decides."""

    @windows_only
    def test_a_directory_grants_only_this_account(self, tmp_path: Path):
        from linkedin_mcp_server.windows_acl import (
            CONTAINER_INHERITANCE,
            current_user_sid,
            describe_dacl,
            _sid_to_string,
        )

        target = tmp_path / "daemon"
        harden_directory(target)

        sid, buffer = current_user_sid()
        try:
            expected = _sid_to_string(sid)
        finally:
            del buffer

        described = describe_dacl(target)
        assert described.protected is True
        assert len(described.entries) == 1
        assert described.entries[0].sid == expected
        # Inheritable, so a file created inside is owner-only from the start
        # rather than from whenever it gets hardened.
        assert described.entries[0].flags == CONTAINER_INHERITANCE

    @windows_only
    def test_a_directory_can_be_private_from_creation(self, tmp_path: Path):
        from linkedin_mcp_server.windows_acl import (
            CONTAINER_INHERITANCE,
            create_owner_only_directory,
            close_directory_pin,
            current_user_sid,
            describe_dacl,
            _sid_to_string,
        )

        parent = tmp_path / "temporary"
        harden_directory(parent)
        target, pin = create_owner_only_directory(parent, prefix="installer-")
        try:
            sid, buffer = current_user_sid()
            try:
                expected = _sid_to_string(sid)
            finally:
                del buffer

            described = describe_dacl(target)
            assert described.protected is True
            assert [(entry.sid, entry.flags) for entry in described.entries] == [
                (expected, CONTAINER_INHERITANCE)
            ]
        finally:
            close_directory_pin(pin)
            target.rmdir()

    @windows_only
    def test_an_ordinary_inheriting_temp_layout_is_still_accepted(self, tmp_path: Path):
        """The layout every Windows machine has, end to end against the real one.

        ``%TEMP%`` inherits from the profile root, which inherits from
        ``C:\\Users``, and the walk runs all the way to the drive root. So this
        is not a fixture at all above ``tmp_path``: it is this machine, and it is
        the test that fails first if the ancestry rule is too strict to ship.
        """
        from linkedin_mcp_server.windows_acl import (
            close_directory_pin,
            create_owner_only_directory,
            describe_dacl,
            verify_ancestry_cannot_be_replaced,
        )

        # Plain ``mkdir``, so each level carries whatever it inherited rather
        # than a protected DACL that would answer the question by itself.
        parent = tmp_path / "profile" / "appdata" / "temp"
        parent.mkdir(parents=True)
        assert describe_dacl(parent).protected is False, "an inheriting parent"

        verify_ancestry_cannot_be_replaced(parent)
        target, pin = create_owner_only_directory(parent, prefix="installer-")
        try:
            assert describe_dacl(target).protected is True
        finally:
            close_directory_pin(pin)
            target.rmdir()

    @windows_only
    def test_owner_rights_in_the_chain_is_still_accepted(self, tmp_path: Path):
        """The entry CPython puts on every directory it creates for us.

        Granted explicitly here so the case does not depend on which Python
        made the fixture. Full control and inheritable, and still not a
        widening: ``S-1-3-4`` names whoever owns the object being checked
        rather than any account, and each container's owner is judged one step
        before its permissions are.
        """
        from linkedin_mcp_server.windows_acl import (
            close_directory_pin,
            create_owner_only_directory,
            describe_dacl,
            verify_ancestry_cannot_be_replaced,
        )

        ancestor = tmp_path / "profile"
        parent = ancestor / "temp"
        parent.mkdir(parents=True)
        subprocess.run(
            ["icacls", str(ancestor), "/grant", "*S-1-3-4:(OI)(CI)F"],
            check=True,
            capture_output=True,
        )
        granted = {entry.sid for entry in describe_dacl(parent).entries}
        assert "S-1-3-4" in granted, "the grant did not propagate to the parent"

        verify_ancestry_cannot_be_replaced(parent)
        target, pin = create_owner_only_directory(parent, prefix="installer-")
        try:
            # Gone again on the child, which is the other half of why an
            # inheritable owner-rights entry above cannot reach what is created
            # here: the child's protected DACL drops every inherited entry.
            assert "S-1-3-4" not in {
                entry.sid for entry in describe_dacl(target).entries
            }
        finally:
            close_directory_pin(pin)
            target.rmdir()

    @windows_only
    @pytest.mark.parametrize(
        "rights",
        [
            "D",  # delete the ancestor itself and put another one there
            "WDAC",  # or grant yourself the rest of it at leisure
            "WO",
            "DC",  # or delete the component below without touching this one
        ],
    )
    def test_an_ancestor_another_sid_controls_is_refused(
        self, tmp_path: Path, rights: str
    ):
        """The finding itself, against real ACLs.

        The temporary parent is beyond reproach and its grandparent is not, so
        every check that looks only at the parent passes while an untrusted
        account can still delete what is created inside it.
        """
        from linkedin_mcp_server.windows_acl import (
            create_owner_only_directory,
            describe_dacl,
        )

        ancestor = tmp_path / "ancestor"
        parent = ancestor / "temp"
        parent.mkdir(parents=True)
        # Everyone, which the current account is a member of and which is
        # therefore never in the trusted set.
        subprocess.run(
            ["icacls", str(ancestor), "/grant", f"*S-1-1-0:({rights})"],
            check=True,
            capture_output=True,
        )
        granted = {entry.sid for entry in describe_dacl(parent).entries}
        assert "S-1-1-0" not in granted, (
            "the grant must not reach the parent, or this would measure the "
            "parent check that already existed"
        )

        with pytest.raises(PrivateStateError, match="S-1-1-0"):
            create_owner_only_directory(parent, prefix="installer-")

        assert list(parent.iterdir()) == [], "refused before CreateDirectoryW"

    @windows_only
    def test_an_ancestor_that_propagates_full_control_is_refused(self, tmp_path: Path):
        """The mutation the pins and the walk exist for.

        An inheritable grant on a directory above an unprotected temporary
        parent lands in that parent, which is what makes ancestry a question
        about the parent at all rather than about somewhere else.
        """
        from linkedin_mcp_server.windows_acl import (
            create_owner_only_directory,
            describe_dacl,
        )

        ancestor = tmp_path / "ancestor"
        parent = ancestor / "temp"
        parent.mkdir(parents=True)
        subprocess.run(
            ["icacls", str(ancestor), "/grant", "*S-1-1-0:(OI)(CI)(F)"],
            check=True,
            capture_output=True,
        )
        assert "S-1-1-0" in {entry.sid for entry in describe_dacl(parent).entries}, (
            "the grant propagated, which is the point of the test"
        )

        with pytest.raises(PrivateStateError, match="S-1-1-0"):
            create_owner_only_directory(parent, prefix="installer-")

        assert list(parent.iterdir()) == []

    @windows_only
    def test_an_ancestor_owner_outside_the_trusted_set_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Ownership is asked separately because no ACE reports it.

        Taking the owner out of the trusted set rather than changing it on disk:
        ``/setowner`` to a foreign account needs a privilege a test account does
        not have, and what is under test is the verdict, not ``icacls``.
        """
        from linkedin_mcp_server import windows_acl

        parent = tmp_path / "ancestor" / "temp"
        parent.mkdir(parents=True)
        owner = windows_acl.read_owner(tmp_path)
        monkeypatch.setattr(
            windows_acl,
            "_trusted_sids",
            lambda: {"S-1-5-18", "S-1-5-32-544"} - {owner},
        )

        with pytest.raises(PrivateStateError, match="rewrite the permissions"):
            windows_acl.verify_ancestry_cannot_be_replaced(parent)

    @windows_only
    def test_a_reparse_point_in_the_chain_is_refused(self, tmp_path: Path):
        """A pin holds the link; the permissions were read from its target."""
        from linkedin_mcp_server.windows_acl import pin_directory_chain

        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(real)],
            check=True,
            capture_output=True,
        )

        with pytest.raises(PrivateStateError, match="reparse point"):
            pin_directory_chain(link)

    @windows_only
    def test_a_pinned_component_cannot_be_renamed_away(self, tmp_path: Path):
        """What the pin is for, measured rather than assumed.

        Without this the verification describes one directory and
        ``CreateDirectoryW`` lands in whatever answers to the name afterwards.
        """
        from linkedin_mcp_server.windows_acl import (
            _release_directory_pins,
            pin_directory_chain,
        )

        parent = tmp_path / "ancestor" / "temp"
        parent.mkdir(parents=True)
        pins = pin_directory_chain(parent)
        try:
            with pytest.raises(OSError):
                (tmp_path / "ancestor").rename(tmp_path / "moved")
        finally:
            _release_directory_pins(pins)

        # And released, so nothing is left holding the tree open.
        (tmp_path / "ancestor").rename(tmp_path / "moved")

    @windows_only
    def test_an_owned_child_directory_uses_the_same_acl_contract(self, tmp_path: Path):
        from linkedin_mcp_server.windows_acl import describe_dacl

        parent = tmp_path / "state"
        harden_directory(parent)
        target = parent / "per-auth"

        harden_directory_entry(target)

        described = describe_dacl(target)
        assert described.protected is True
        assert len(described.entries) == 1

    @windows_only
    def test_an_owned_child_directory_refuses_an_ntfs_junction(self, tmp_path: Path):
        from linkedin_mcp_server.windows_acl import describe_dacl

        parent = tmp_path / "state"
        harden_directory(parent)
        elsewhere = tmp_path / "redirected-state"
        elsewhere.mkdir()
        subprocess.run(
            ["icacls", str(elsewhere), "/grant", "*S-1-1-0:(OI)(CI)F"],
            check=True,
            capture_output=True,
        )
        junction = parent / "per-auth"
        subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(elsewhere)],
            check=True,
            capture_output=True,
        )
        try:
            assert junction.is_junction()
            assert "S-1-1-0" in {
                entry.sid for entry in describe_dacl(elsewhere).entries
            }

            with pytest.raises(PrivateStateError, match="Windows reparse point"):
                harden_directory_entry(junction)

            assert "S-1-1-0" in {
                entry.sid for entry in describe_dacl(elsewhere).entries
            }
        finally:
            junction.rmdir()

    @windows_only
    def test_a_file_grants_only_this_account_and_inherits_nothing(self, tmp_path: Path):
        from linkedin_mcp_server.windows_acl import describe_dacl

        parent = tmp_path / "state"
        harden_directory(parent)
        target = parent / "token"
        target.touch()
        harden_created_file(target)

        described = describe_dacl(target)
        assert described.protected is True
        assert len(described.entries) == 1
        assert described.entries[0].flags == 0

    @windows_only
    def test_an_existing_permissive_directory_is_refused_without_repair(
        self, tmp_path: Path
    ):
        from linkedin_mcp_server.windows_acl import describe_dacl

        target = tmp_path / "legacy-state"
        target.mkdir()
        subprocess.run(
            ["icacls", str(target), "/grant", "*S-1-1-0:(OI)(CI)F"],
            check=True,
            capture_output=True,
        )

        # Three refusals mean the same thing here, and which one arrives first
        # depends on the machine rather than on the code: where the token's
        # default owner is the Administrators group, a directory planted by
        # this test belongs to that group and the ownership check speaks before
        # the permissions are ever read. What the test is about is the line
        # below, that nothing was repaired on the way out.
        with pytest.raises(
            PrivateStateError, match="grants access|inherits permissions|is owned by"
        ):
            harden_directory(target)

        assert "S-1-1-0" in {entry.sid for entry in describe_dacl(target).entries}

    @windows_only
    def test_an_existing_permissive_file_is_refused_without_repair(
        self, tmp_path: Path
    ):
        from linkedin_mcp_server.windows_acl import describe_dacl

        target = tmp_path / "legacy-token"
        target.touch()
        subprocess.run(
            ["icacls", str(target), "/grant", "*S-1-1-0:F"],
            check=True,
            capture_output=True,
        )

        # Three refusals mean the same thing here, and which one arrives first
        # depends on the machine rather than on the code: where the token's
        # default owner is the Administrators group, a directory planted by
        # this test belongs to that group and the ownership check speaks before
        # the permissions are ever read. What the test is about is the line
        # below, that nothing was repaired on the way out.
        with pytest.raises(
            PrivateStateError, match="grants access|inherits permissions|is owned by"
        ):
            harden_file(target)

        assert "S-1-1-0" in {entry.sid for entry in describe_dacl(target).entries}

    @windows_only
    def test_a_permissive_parent_does_not_leak_in(self, tmp_path: Path):
        # The reason the DACL is replaced and marked protected rather than
        # merged: a parent granting Everyone would otherwise pass that straight
        # down to the token file.
        from linkedin_mcp_server.windows_acl import describe_dacl

        parent = tmp_path / "wide"
        parent.mkdir()
        # Argument list rather than a shell string: the path comes from a
        # fixture and could carry spaces or shell metacharacters.
        subprocess.run(
            ["icacls", str(parent), "/grant", "*S-1-1-0:(OI)(CI)F"],
            check=True,
            capture_output=True,
        )

        target = parent / "daemon"
        harden_directory(target)

        described = describe_dacl(target)
        granted = {entry.sid for entry in described.entries}
        assert "S-1-1-0" not in granted  # Everyone
        assert "S-1-5-32-545" not in granted  # BUILTIN\Users
        assert "S-1-5-11" not in granted  # Authenticated Users

    @windows_only
    def test_a_private_parent_prevents_child_replacement(self, tmp_path: Path):
        from linkedin_mcp_server.windows_acl import (
            verify_children_cannot_be_replaced,
        )

        parent = tmp_path / "account-home"
        harden_directory(parent)

        verify_children_cannot_be_replaced(parent)

    @windows_only
    def test_a_parent_granting_delete_child_is_refused(self, tmp_path: Path):
        from linkedin_mcp_server.windows_acl import (
            verify_children_cannot_be_replaced,
        )

        parent = tmp_path / "account-home"
        harden_directory(parent)
        subprocess.run(
            ["icacls", str(parent), "/grant", "*S-1-1-0:(DC)"],
            check=True,
            capture_output=True,
        )

        with pytest.raises(PrivateStateError, match="replace private state"):
            verify_children_cannot_be_replaced(parent)

    @windows_only
    def test_hardening_preserves_current_ownership(self, tmp_path: Path):
        # The entry is created by this account and remains owned by it. Hardening
        # refuses foreign content instead of laundering its ownership.
        from linkedin_mcp_server.windows_acl import (
            current_user_sid,
            read_owner,
            _sid_to_string,
        )

        target = tmp_path / "daemon"
        harden_directory(target)

        sid, buffer = current_user_sid()
        try:
            expected = _sid_to_string(sid)
        finally:
            del buffer

        assert read_owner(target) == expected

    @windows_only
    def test_a_foreign_initial_owner_is_refused_before_acl_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import windows_acl

        target = tmp_path / "daemon"
        target.mkdir()
        monkeypatch.setattr(
            windows_acl, "read_owner", lambda _path: "S-1-5-21-0-0-0-1234"
        )
        with pytest.raises(PrivateStateError, match="another account's content"):
            windows_acl.restrict_to_current_user(target, directory=True)

    @windows_only
    def test_a_pinned_directory_cannot_be_replaced(self, tmp_path: Path):
        from linkedin_mcp_server.windows_acl import (
            close_directory_pin,
            pin_directory,
        )

        target = tmp_path / "installer-root"
        target.mkdir()
        replacement = tmp_path / "moved"
        pin = pin_directory(target)
        try:
            with pytest.raises(OSError):
                target.rename(replacement)
            # Both, because a directory taken away by either route leaves the
            # name free for one an attacker controls.
            with pytest.raises(OSError):
                target.rmdir()
        finally:
            close_directory_pin(pin)

        target.rename(replacement)
        assert replacement.is_dir()

    @windows_only
    def test_a_pinned_chain_holds_its_ancestors_too(self, tmp_path: Path):
        """The component an attacker would actually move.

        Replacing the temporary root itself is the obvious attack and the
        useless one, since it is created and pinned in the same breath. Moving
        a directory *above* it is what turns a verified chain into an
        unverified one, and it is the reason the whole chain is held rather
        than the leaf.
        """
        from linkedin_mcp_server.windows_acl import (
            _release_directory_pins,
            pin_directory_chain,
        )

        ancestor = tmp_path / "profile"
        parent = ancestor / "temp"
        parent.mkdir(parents=True)

        pins = pin_directory_chain(parent)
        try:
            with pytest.raises(OSError):
                ancestor.rename(tmp_path / "moved")
        finally:
            _release_directory_pins(pins)

        ancestor.rename(tmp_path / "moved")
        assert (tmp_path / "moved" / "temp").is_dir()

    @windows_only
    def test_the_struct_layouts_match_the_windows_headers(self):
        # ctypes sizes a struct from its declared fields, so a wrong field type
        # produces a struct Windows reads at the wrong offsets and reports as
        # an unrelated error. These are the documented sizes on 64-bit.
        import ctypes

        from linkedin_mcp_server import windows_acl as acl

        assert ctypes.sizeof(acl._ACL) == 8
        assert ctypes.sizeof(acl._ACE_HEADER) == 4
        assert ctypes.sizeof(acl._ACCESS_ALLOWED_ACE) == 12
        assert ctypes.sizeof(acl._SID_AND_ATTRIBUTES) == 16
        assert ctypes.sizeof(acl._TRUSTEE_W) == 32
        assert ctypes.sizeof(acl._EXPLICIT_ACCESS_W) == 48


class TestWindowsAclOffWindows:
    def test_directory_pin_opens_the_entry_without_following_reparse_points(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import windows_acl

        captured: list[int] = []

        class Kernel:
            @staticmethod
            def CreateFileW(
                path: str,
                access: int,
                sharing: int,
                security: object,
                disposition: int,
                flags: int,
                template: object,
            ) -> int:
                captured.append(flags)
                return 123

        monkeypatch.setattr(windows_acl, "_load", lambda: (object(), Kernel()))

        assert windows_acl.pin_directory(tmp_path) == 123
        assert captured == [
            windows_acl.FILE_FLAG_BACKUP_SEMANTICS
            | windows_acl.FILE_FLAG_OPEN_REPARSE_POINT
        ]

    @pytest.mark.parametrize("directory", [False, True])
    def test_the_token_default_owner_is_normalized_to_the_token_user(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        directory: bool,
    ):
        """The Administrators-group owner policy, which GitHub's runners ship.

        Objects this account creates are owned by that group there, so refusing
        it would refuse this account its own daemon state. It is accepted and
        the owner is written back to the user SID in the same call.

        Named by its real SID rather than by a placeholder, because that is the
        whole claim: the policy can only ever put this one group there, and a
        stand-in string would pass a check that admits nothing else.
        """
        from linkedin_mcp_server import windows_acl

        user_sid = object()
        default_owner_sid = object()
        acl = object()
        calls: list[tuple[int, object]] = []

        class Advapi:
            @staticmethod
            def SetNamedSecurityInfoW(
                path: str,
                object_type: int,
                information: int,
                owner: object,
                group: object,
                dacl: object,
                sacl: object,
            ) -> int:
                calls.append((information, owner))
                return windows_acl.ERROR_SUCCESS

        class Kernel:
            @staticmethod
            def LocalFree(value: object) -> None:
                assert value is acl

        monkeypatch.setattr(windows_acl, "_load", lambda: (Advapi(), Kernel()))
        monkeypatch.setattr(
            windows_acl, "_require_acl_capable_volume", lambda _path: None
        )
        monkeypatch.setattr(
            windows_acl, "current_user_sid", lambda: (user_sid, object())
        )
        monkeypatch.setattr(
            windows_acl,
            "default_owner_sid",
            lambda: (default_owner_sid, object()),
        )
        monkeypatch.setattr(
            windows_acl,
            "_sid_to_string",
            lambda sid: {
                user_sid: "current-account",
                default_owner_sid: "S-1-5-32-544",
            }[sid],
        )
        monkeypatch.setattr(windows_acl, "read_owner", lambda _path: "S-1-5-32-544")
        monkeypatch.setattr(
            windows_acl, "_build_owner_only_acl", lambda *_args, **_kwargs: acl
        )
        monkeypatch.setattr(
            windows_acl, "verify_owner_only", lambda *_args, **_kwargs: None
        )

        windows_acl.restrict_to_current_user(tmp_path, directory=directory)

        assert calls == [
            (
                windows_acl.REPLACE_PROTECTED_DACL
                | windows_acl.OWNER_SECURITY_INFORMATION,
                user_sid,
            )
        ]

    def test_a_default_owner_no_policy_can_produce_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The other half of the rule above, and the reason it is a set.

        A token's default owner is whichever of its groups carries the owner
        attribute, and Windows' own policy can only ever put Administrators
        there. A token built by hand can name a shared ordinary group instead,
        and a directory owned by that group is every member's directory. Taking
        it as this account's own state is precisely the boundary crossing the
        owner check exists to refuse.
        """
        from linkedin_mcp_server import windows_acl

        user_sid = object()
        default_owner_sid = object()
        shared_group = "S-1-5-21-11-22-33-1007"
        assert shared_group not in windows_acl._TRUSTED_SYSTEM_SIDS

        def never(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("a refused owner reached the ACL construction")

        monkeypatch.setattr(windows_acl, "_load", lambda: (object(), object()))
        monkeypatch.setattr(
            windows_acl, "_require_acl_capable_volume", lambda _path: None
        )
        monkeypatch.setattr(
            windows_acl, "current_user_sid", lambda: (user_sid, object())
        )
        monkeypatch.setattr(
            windows_acl,
            "default_owner_sid",
            lambda: (default_owner_sid, object()),
        )
        monkeypatch.setattr(
            windows_acl,
            "_sid_to_string",
            lambda sid: {
                user_sid: "current-account",
                default_owner_sid: shared_group,
            }[sid],
        )
        monkeypatch.setattr(windows_acl, "read_owner", lambda _path: shared_group)
        monkeypatch.setattr(windows_acl, "_build_owner_only_acl", never)

        with pytest.raises(PrivateStateError, match="is owned by"):
            windows_acl.restrict_to_current_user(tmp_path, directory=True)

    @pytest.mark.parametrize(
        "right_name",
        ["FILE_ADD_FILE", "FILE_ADD_SUBDIRECTORY", "FILE_DELETE_CHILD", "DELETE"],
    )
    def test_untrusted_parent_replacement_permission_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        right_name: str,
    ):
        from linkedin_mcp_server import windows_acl

        monkeypatch.setattr(
            windows_acl, "current_user_sid", lambda: (object(), object())
        )
        monkeypatch.setattr(
            windows_acl, "_sid_to_string", lambda _sid: "current-account"
        )
        monkeypatch.setattr(windows_acl, "read_owner", lambda _path: "current-account")
        monkeypatch.setattr(
            windows_acl,
            "describe_dacl",
            lambda _path: windows_acl.Dacl(
                protected=True,
                entries=(
                    windows_acl.AccessEntry(
                        sid="another-account",
                        type=windows_acl.ACCESS_ALLOWED_ACE_TYPE,
                        flags=0,
                        mask=getattr(windows_acl, right_name),
                    ),
                ),
            ),
        )

        with pytest.raises(PrivateStateError, match="replace private state"):
            windows_acl.verify_children_cannot_be_replaced(tmp_path)

    def test_owner_rights_is_not_read_as_a_second_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Same mask, same flags, and the verdict turns on the SID alone.

        The refusing half is the parametrized test above; this is the pair to
        it. ``S-1-3-4`` grants to the owner of the directory being read, which
        the same call accepted a line earlier, so it can widen nothing.
        """
        from linkedin_mcp_server import windows_acl

        monkeypatch.setattr(
            windows_acl, "current_user_sid", lambda: (object(), object())
        )
        monkeypatch.setattr(
            windows_acl, "_sid_to_string", lambda _sid: "current-account"
        )
        monkeypatch.setattr(windows_acl, "read_owner", lambda _path: "current-account")
        monkeypatch.setattr(
            windows_acl,
            "describe_dacl",
            lambda _path: windows_acl.Dacl(
                protected=True,
                entries=(
                    windows_acl.AccessEntry(
                        sid="S-1-3-4",
                        type=windows_acl.ACCESS_ALLOWED_ACE_TYPE,
                        flags=windows_acl.CONTAINER_INHERIT_ACE
                        | windows_acl.OBJECT_INHERIT_ACE,
                        mask=windows_acl.GENERIC_ALL,
                    ),
                ),
            ),
        )

        windows_acl.verify_children_cannot_be_replaced(tmp_path)
        windows_acl.verify_ancestry_cannot_be_replaced(tmp_path / "child")

    @pytest.mark.parametrize(
        ("inheritance", "right_name"),
        [
            ("OBJECT_INHERIT_ACE", "FILE_WRITE_DATA"),
            ("OBJECT_INHERIT_ACE", "FILE_APPEND_DATA"),
            ("OBJECT_INHERIT_ACE", "DELETE"),
            ("OBJECT_INHERIT_ACE", "WRITE_DAC"),
            ("CONTAINER_INHERIT_ACE", "DELETE"),
            ("CONTAINER_INHERIT_ACE", "WRITE_DAC"),
        ],
    )
    def test_inherit_only_child_replacement_permission_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        inheritance: str,
        right_name: str,
    ):
        from linkedin_mcp_server import windows_acl

        monkeypatch.setattr(
            windows_acl, "current_user_sid", lambda: (object(), object())
        )
        monkeypatch.setattr(
            windows_acl, "_sid_to_string", lambda _sid: "current-account"
        )
        monkeypatch.setattr(windows_acl, "read_owner", lambda _path: "current-account")
        monkeypatch.setattr(
            windows_acl,
            "describe_dacl",
            lambda _path: windows_acl.Dacl(
                protected=True,
                entries=(
                    windows_acl.AccessEntry(
                        sid="another-account",
                        type=windows_acl.ACCESS_ALLOWED_ACE_TYPE,
                        flags=(
                            windows_acl.INHERIT_ONLY_ACE
                            | getattr(windows_acl, inheritance)
                        ),
                        mask=getattr(windows_acl, right_name),
                    ),
                ),
            ),
        )

        with pytest.raises(PrivateStateError, match="replace private state"):
            windows_acl.verify_children_cannot_be_replaced(tmp_path)

    def test_inheriting_home_permissions_are_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import windows_acl

        monkeypatch.setattr(
            windows_acl, "current_user_sid", lambda: (object(), object())
        )
        monkeypatch.setattr(
            windows_acl, "_sid_to_string", lambda _sid: "current-account"
        )
        monkeypatch.setattr(windows_acl, "read_owner", lambda _path: "current-account")
        monkeypatch.setattr(
            windows_acl,
            "describe_dacl",
            lambda _path: windows_acl.Dacl(protected=False, entries=()),
        )

        with pytest.raises(PrivateStateError, match="inherits permissions"):
            windows_acl.verify_children_cannot_be_replaced(tmp_path)

    def test_inherit_only_creator_owner_is_not_a_foreign_principal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import windows_acl

        monkeypatch.setattr(
            windows_acl, "current_user_sid", lambda: (object(), object())
        )
        monkeypatch.setattr(
            windows_acl, "_sid_to_string", lambda _sid: "current-account"
        )
        monkeypatch.setattr(windows_acl, "read_owner", lambda _path: "current-account")
        monkeypatch.setattr(
            windows_acl,
            "describe_dacl",
            lambda _path: windows_acl.Dacl(
                protected=True,
                entries=(
                    windows_acl.AccessEntry(
                        sid="S-1-3-0",
                        type=windows_acl.ACCESS_ALLOWED_ACE_TYPE,
                        flags=(
                            windows_acl.INHERIT_ONLY_ACE
                            | windows_acl.OBJECT_INHERIT_ACE
                            | windows_acl.CONTAINER_INHERIT_ACE
                        ),
                        mask=windows_acl.GENERIC_ALL,
                    ),
                ),
            ),
        )

        windows_acl.verify_children_cannot_be_replaced(tmp_path)

    def test_file_inheritance_does_not_treat_read_ea_as_delete_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import windows_acl

        monkeypatch.setattr(
            windows_acl, "current_user_sid", lambda: (object(), object())
        )
        monkeypatch.setattr(
            windows_acl, "_sid_to_string", lambda _sid: "current-account"
        )
        monkeypatch.setattr(windows_acl, "read_owner", lambda _path: "current-account")
        monkeypatch.setattr(
            windows_acl,
            "describe_dacl",
            lambda _path: windows_acl.Dacl(
                protected=True,
                entries=(
                    windows_acl.AccessEntry(
                        sid="another-account",
                        type=windows_acl.ACCESS_ALLOWED_ACE_TYPE,
                        flags=(
                            windows_acl.INHERIT_ONLY_ACE
                            | windows_acl.OBJECT_INHERIT_ACE
                        ),
                        mask=windows_acl.FILE_DELETE_CHILD,
                    ),
                ),
            ),
        )

        windows_acl.verify_children_cannot_be_replaced(tmp_path)

    def test_inheriting_temporary_parent_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import windows_acl

        monkeypatch.setattr(
            windows_acl, "current_user_sid", lambda: (object(), object())
        )
        monkeypatch.setattr(
            windows_acl, "_sid_to_string", lambda _sid: "current-account"
        )
        monkeypatch.setattr(windows_acl, "read_owner", lambda _path: "current-account")
        monkeypatch.setattr(
            windows_acl,
            "describe_dacl",
            lambda _path: windows_acl.Dacl(protected=False, entries=()),
        )

        with pytest.raises(PrivateStateError, match="inherits permissions"):
            windows_acl.verify_children_cannot_be_replaced(tmp_path)

        windows_acl.verify_children_cannot_be_replaced(
            tmp_path, require_protected=False
        )

    def test_security_descriptor_matches_the_native_pointer_width(self):
        from linkedin_mcp_server import windows_acl

        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 20
        assert ctypes.sizeof(windows_acl._SECURITY_DESCRIPTOR) == expected

    def test_private_directory_uses_its_final_acl_at_creation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import windows_acl

        acl = windows_acl._PACL(456)
        created: list[Path] = []
        parent_checked: list[tuple[Path, bool]] = []
        # One log rather than several, because the order is the claim: a pin
        # taken after the check it protects, or released before the child has
        # one of its own, is the whole defect and every count still matches.
        order: list[tuple[str, Path]] = []
        pinned: dict[object, Path] = {}
        verified: list[Path] = []

        class Advapi:
            InitializeSecurityDescriptor = staticmethod(lambda *_args: True)
            SetSecurityDescriptorOwner = staticmethod(lambda *_args: True)
            SetSecurityDescriptorDacl = staticmethod(lambda *_args: True)
            SetSecurityDescriptorControl = staticmethod(lambda *_args: True)

        class Kernel:
            @staticmethod
            def CreateDirectoryW(path: str, attributes: object) -> bool:
                assert attributes is not None
                target = Path(path)
                target.mkdir()
                created.append(target)
                order.append(("create", target))
                return True

            @staticmethod
            def LocalFree(value: object) -> None:
                assert value == acl

        monkeypatch.setattr(windows_acl, "_load", lambda: (Advapi(), Kernel()))

        def pin(path: Path) -> object:
            handle = object()
            pinned[handle] = path
            order.append(("pin", path))
            return handle

        monkeypatch.setattr(windows_acl, "pin_directory", pin)
        monkeypatch.setattr(
            windows_acl,
            "close_directory_pin",
            lambda handle: order.append(("close", pinned[handle])),
        )

        def check_parent(path: Path, *, require_protected: bool) -> None:
            parent_checked.append((path, require_protected))
            order.append(("parent", path))

        def check_child(path: Path, **_kwargs: object) -> None:
            verified.append(path)
            order.append(("verify", path))

        monkeypatch.setattr(
            windows_acl, "verify_children_cannot_be_replaced", check_parent
        )
        monkeypatch.setattr(
            windows_acl,
            "verify_ancestry_cannot_be_replaced",
            lambda path: order.append(("ancestry", path)),
        )
        # The reparse check reads ``st_file_attributes``, which no POSIX
        # ``stat_result`` carries. Its own behaviour is covered natively below.
        monkeypatch.setattr(windows_acl, "_refuse_a_reparse_point", lambda _path: None)
        monkeypatch.setattr(
            windows_acl, "_require_acl_capable_volume", lambda _path: None
        )
        monkeypatch.setattr(windows_acl, "_clear_last_error", lambda: None)
        monkeypatch.setattr(
            windows_acl,
            "current_user_sid",
            lambda: (windows_acl._PSID(123), object()),
        )
        monkeypatch.setattr(
            windows_acl, "_build_owner_only_acl", lambda *_args, **_kwargs: acl
        )
        monkeypatch.setattr(windows_acl, "verify_owner_only", check_child)

        target, child_pin = windows_acl.create_owner_only_directory(
            tmp_path, prefix="installer-"
        )

        assert parent_checked == [(tmp_path, False)]
        assert created == [target]
        assert verified == [target]
        assert pinned[child_pin] == target
        # The whole sequence, in order. Every component from the drive root down
        # is pinned before anything is judged, both checks then run against names
        # that can no longer move, and the ancestry is only released once the
        # child exists, has been verified and holds a pin of its own.
        assert order == [
            *(("pin", path) for path in (*reversed(tmp_path.parents), tmp_path)),
            ("ancestry", tmp_path),
            ("parent", tmp_path),
            ("create", target),
            ("verify", target),
            ("pin", target),
            *(("close", path) for path in (tmp_path, *tmp_path.parents)),
        ]

    def test_the_ancestry_is_pinned_before_any_of_it_is_judged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Order is the whole guarantee.

        A verdict about a path is a verdict about a name, and a name that is
        still free to move describes whatever is standing there when the check
        runs rather than what ``CreateDirectoryW`` lands in.
        """
        from linkedin_mcp_server import windows_acl

        events: list[tuple[str, Path]] = []

        monkeypatch.setattr(windows_acl, "_load", lambda: (object(), object()))
        monkeypatch.setattr(windows_acl, "_refuse_a_reparse_point", lambda _path: None)
        monkeypatch.setattr(
            windows_acl,
            "pin_directory",
            lambda path: events.append(("pin", path)) or object(),
        )
        monkeypatch.setattr(
            windows_acl, "read_owner", lambda path: events.append(("owner", path))
        )
        monkeypatch.setattr(windows_acl, "_trusted_sids", lambda: {None})
        monkeypatch.setattr(
            windows_acl,
            "describe_dacl",
            lambda _path: windows_acl.Dacl(protected=True, entries=()),
        )

        pins = windows_acl.pin_directory_chain(tmp_path)
        windows_acl.verify_ancestry_cannot_be_replaced(tmp_path)

        assert len(pins) == len(tmp_path.parents) + 1
        kinds = [kind for kind, _path in events]
        assert set(kinds[: len(pins)]) == {"pin"}, "every pin precedes every verdict"
        assert set(kinds[len(pins) :]) == {"owner"}
        # Root first, so nothing above a pinned component can still be moved.
        assert [path for kind, path in events if kind == "pin"] == [
            *reversed(tmp_path.parents),
            tmp_path,
        ]

    def test_a_relative_path_is_refused_rather_than_pinned(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """``Path.parents`` of a relative path stops at ``.``, not at a root."""
        from linkedin_mcp_server import windows_acl

        monkeypatch.setattr(windows_acl, "_load", lambda: (object(), object()))
        monkeypatch.setattr(
            windows_acl,
            "pin_directory",
            lambda _path: pytest.fail("a relative path reached the pin"),
        )

        with pytest.raises(PrivateStateError, match="relative installer path"):
            windows_acl.pin_directory_chain(Path("relative/temp"))

    def test_a_failed_pin_releases_the_ones_already_held(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """These are handles on ``C:\\`` and ``C:\\Users``. Leaking them is not free."""
        from linkedin_mcp_server import windows_acl

        opened: list[object] = []
        closed: list[object] = []

        def pin(path: Path) -> object:
            if path == tmp_path:
                raise PrivateStateError("CreateFileW failed")
            handle = object()
            opened.append(handle)
            return handle

        monkeypatch.setattr(windows_acl, "_load", lambda: (object(), object()))
        monkeypatch.setattr(windows_acl, "_refuse_a_reparse_point", lambda _path: None)
        monkeypatch.setattr(windows_acl, "pin_directory", pin)
        monkeypatch.setattr(
            windows_acl, "close_directory_pin", lambda handle: closed.append(handle)
        )

        with pytest.raises(PrivateStateError, match="CreateFileW failed"):
            windows_acl.pin_directory_chain(tmp_path)

        assert opened, "some of the chain was pinned before the failure"
        assert closed == list(reversed(opened))

    def test_every_pin_is_released_even_when_one_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import windows_acl

        handles = [ctypes.c_void_p(index) for index in (11, 22, 33)]
        closed: list[object] = []

        def close(handle: object) -> None:
            closed.append(handle)
            if handle is handles[1]:
                raise PrivateStateError("CloseHandle failed")

        monkeypatch.setattr(windows_acl, "close_directory_pin", close)

        with pytest.raises(PrivateStateError, match="CloseHandle failed"):
            windows_acl._release_directory_pins(handles)

        assert closed == list(reversed(handles)), "the failure stopped nothing"

    @pytest.mark.parametrize(
        ("mask", "refused"),
        [
            # What lets an account take a named directory away from its path.
            (0x00010000, True),  # DELETE
            (0x00040000, True),  # WRITE_DAC: grant yourself the rest later
            (0x00080000, True),  # WRITE_OWNER: same, one step further round
            (0x00000040, True),  # FILE_DELETE_CHILD: delete the component below
            (0x10000000, True),  # GENERIC_ALL
            # What an ordinary machine grants, and what does not replace a name.
            (0x00000004, False),  # FILE_ADD_SUBDIRECTORY, as ``C:\`` grants it
            (0x00000002, False),  # FILE_ADD_FILE
            (0x00000001, False),  # FILE_LIST_DIRECTORY
            (0x00020000, False),  # READ_CONTROL
            (0x40000000, False),  # GENERIC_WRITE maps to no delete and no WRITE_DAC
        ],
    )
    def test_an_ancestor_is_judged_on_replacement_rights_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mask: int, refused: bool
    ):
        """The narrower question, and why it has to be narrower.

        Asking the ancestry what is asked of the immediate parent would refuse
        every default Windows install: ``C:\\`` grants Authenticated Users the
        right to create a folder in it, and creating a folder beside ``Users``
        is not replacing ``Users``.
        """
        from linkedin_mcp_server import windows_acl

        # The masks above are literals so the table reads as Windows writes it;
        # this keeps them honest against the module's own constants.
        assert (windows_acl.DELETE, windows_acl.FILE_DELETE_CHILD) == (
            0x00010000,
            0x00000040,
        )
        monkeypatch.setattr(windows_acl, "_load", lambda: (object(), object()))
        monkeypatch.setattr(windows_acl, "read_owner", lambda _path: "S-1-5-18")
        monkeypatch.setattr(windows_acl, "_trusted_sids", lambda: {"S-1-5-18"})
        monkeypatch.setattr(
            windows_acl,
            "describe_dacl",
            lambda _path: windows_acl.Dacl(
                protected=False,
                entries=(
                    windows_acl.AccessEntry(
                        sid="S-1-5-11",  # Authenticated Users
                        type=windows_acl.ACCESS_ALLOWED_ACE_TYPE,
                        flags=0,
                        mask=mask,
                    ),
                ),
            ),
        )

        if refused:
            with pytest.raises(PrivateStateError, match="remove or re-permission"):
                windows_acl.verify_ancestry_cannot_be_replaced(tmp_path)
        else:
            windows_acl.verify_ancestry_cannot_be_replaced(tmp_path)

    def test_an_ancestor_owned_by_another_account_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """An owner rewrites the DACL whenever it likes, with no ACE saying so."""
        from linkedin_mcp_server import windows_acl

        monkeypatch.setattr(windows_acl, "_load", lambda: (object(), object()))
        monkeypatch.setattr(windows_acl, "_trusted_sids", lambda: {"S-1-5-18"})
        monkeypatch.setattr(
            windows_acl,
            "read_owner",
            lambda path: (
                "S-1-5-18" if path != tmp_path.parent else "S-1-5-21-1-2-3-1001"
            ),
        )
        monkeypatch.setattr(
            windows_acl,
            "describe_dacl",
            lambda _path: windows_acl.Dacl(protected=True, entries=()),
        )

        with pytest.raises(PrivateStateError, match="S-1-5-21-1-2-3-1001"):
            windows_acl.verify_ancestry_cannot_be_replaced(tmp_path)

    def test_an_inherit_only_entry_is_judged_where_it_lands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """``C:\\`` carries one of these, granting Modify to Authenticated Users.

        It grants nothing on ``C:\\`` itself, and every standard directory below
        is protected against it. Judging it here rather than where it applies
        would refuse an ordinary machine over an entry that reaches nothing.
        """
        from linkedin_mcp_server import windows_acl

        monkeypatch.setattr(windows_acl, "_load", lambda: (object(), object()))
        monkeypatch.setattr(windows_acl, "read_owner", lambda _path: "S-1-5-18")
        monkeypatch.setattr(windows_acl, "_trusted_sids", lambda: {"S-1-5-18"})
        entry = windows_acl.AccessEntry(
            sid="S-1-5-11",
            type=windows_acl.ACCESS_ALLOWED_ACE_TYPE,
            flags=windows_acl.INHERIT_ONLY_ACE | windows_acl.CONTAINER_INHERITANCE,
            mask=windows_acl.DELETE | windows_acl.WRITE_DAC,
        )
        monkeypatch.setattr(
            windows_acl,
            "describe_dacl",
            lambda _path: windows_acl.Dacl(protected=False, entries=(entry,)),
        )

        windows_acl.verify_ancestry_cannot_be_replaced(tmp_path)

        # And the same entry applying to the directory itself is refused, so
        # this is the flag doing the work rather than the mask being ignored.
        applied = windows_acl.AccessEntry(
            sid=entry.sid, type=entry.type, flags=0, mask=entry.mask
        )
        monkeypatch.setattr(
            windows_acl,
            "describe_dacl",
            lambda _path: windows_acl.Dacl(protected=False, entries=(applied,)),
        )
        with pytest.raises(PrivateStateError, match="remove or re-permission"):
            windows_acl.verify_ancestry_cannot_be_replaced(tmp_path)

    def test_an_unsupported_ancestor_entry_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Nothing can be said about a mask this cannot read, so nothing is."""
        from linkedin_mcp_server import windows_acl

        monkeypatch.setattr(windows_acl, "_load", lambda: (object(), object()))
        monkeypatch.setattr(windows_acl, "read_owner", lambda _path: "S-1-5-18")
        monkeypatch.setattr(windows_acl, "_trusted_sids", lambda: {"S-1-5-18"})
        monkeypatch.setattr(
            windows_acl,
            "describe_dacl",
            lambda _path: windows_acl.Dacl(
                protected=False,
                entries=(
                    windows_acl.AccessEntry(
                        sid="S-1-5-11",
                        type=0x05,  # ACCESS_ALLOWED_OBJECT_ACE_TYPE
                        flags=0,
                        mask=0,
                    ),
                ),
            ),
        )

        with pytest.raises(PrivateStateError, match="unsupported permission entry"):
            windows_acl.verify_ancestry_cannot_be_replaced(tmp_path)

    def test_the_trusted_owners_are_identities_no_login_can_assume(self):
        """A trusted owner is a hole exactly the size of who can become it."""
        from linkedin_mcp_server import windows_acl

        assert windows_acl._TRUSTED_SYSTEM_SIDS == {
            "S-1-5-18",
            "S-1-5-32-544",
            windows_acl._TRUSTED_INSTALLER_SID,
        }
        # Never the groups an ordinary account is a member of.
        for opened in ("S-1-1-0", "S-1-5-11", "S-1-5-32-545", "S-1-5-4"):
            assert opened not in windows_acl._TRUSTED_SYSTEM_SIDS

    def test_foreign_initial_owner_is_refused_before_acl_construction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import windows_acl

        target = tmp_path / "foreign"
        target.touch()
        monkeypatch.setattr(windows_acl, "_load", lambda: (object(), object()))
        monkeypatch.setattr(
            windows_acl, "_require_acl_capable_volume", lambda _path: None
        )
        user_sid = object()
        default_owner = object()
        monkeypatch.setattr(
            windows_acl, "current_user_sid", lambda: (user_sid, object())
        )
        # Given a SID of its own so the owner under test is a third party
        # rather than either accepted identity, which is the case this check
        # exists for. Untrusted, so it is refused in its own right too; the
        # test beside this one is the one that separates those two reasons.
        monkeypatch.setattr(
            windows_acl, "default_owner_sid", lambda: (default_owner, object())
        )
        monkeypatch.setattr(
            windows_acl,
            "_sid_to_string",
            lambda sid: {
                user_sid: "current-account",
                default_owner: "default-owner",
            }[sid],
        )
        monkeypatch.setattr(windows_acl, "read_owner", lambda _path: "another-account")
        monkeypatch.setattr(
            windows_acl,
            "_build_owner_only_acl",
            lambda *_args, **_kwargs: pytest.fail(
                "foreign content reached ACL replacement"
            ),
        )

        with pytest.raises(PrivateStateError, match="another account's content"):
            windows_acl.restrict_to_current_user(target, directory=False)

    @posix_only
    def test_the_windows_helpers_refuse_rather_than_pretend(self, tmp_path: Path):
        # Importable everywhere so the module can be reasoned about and typed on
        # any platform, but every entry point refuses off Windows rather than
        # failing somewhere inside a DLL that is not there.
        from linkedin_mcp_server.windows_acl import current_user_sid

        with pytest.raises(PrivateStateError, match="off Windows"):
            current_user_sid()

    def test_the_declared_floor_can_create_windows_state(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A runtime the package accepts has to be one Windows state can use.

        The refusal below is unconditional and arrives on a first ever start,
        before anything exists to remove, so an installable interpreter that
        trips it leaves the shared owner unavailable with nothing to undo.
        """
        import tomllib

        manifest = Path(__file__).resolve().parents[1] / "pyproject.toml"
        declared = tomllib.loads(manifest.read_text())["project"]["requires-python"]
        floor = tuple(
            int(part)
            for part in declared.split(",")[0].strip().removeprefix(">=").split(".")
        )
        monkeypatch.setattr(private_state, "_WINDOWS", True)
        monkeypatch.setattr(private_state.sys, "version_info", floor)

        assert private_state._windows_private_creation_supported(), (
            f"the package offers Python {declared}, which cannot create it"
        )
