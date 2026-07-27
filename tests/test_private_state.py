"""Storage that only the current account can read.

The Windows tests here run only on Windows and are the reason the CI matrix
covers it: the ACL path cannot be exercised anywhere else, and mocking it would
only assert that the mock was called.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from linkedin_mcp_server.private_state import (
    PrivateStateError,
    harden_directory,
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

    def test_a_file_where_a_directory_belongs_refuses(self, tmp_path: Path):
        occupied = tmp_path / "daemon"
        occupied.write_text("not a directory")

        with pytest.raises(PrivateStateError, match="Not a directory"):
            harden_directory(occupied)

    @posix_only
    def test_a_filesystem_that_ignores_chmod_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A share or a FAT volume accepts chmod and keeps the old mode. Without
        # reading the mode back, the secret lands on disk readable and nothing
        # says so. Simulated, since neither can be mounted in a test run.
        target = tmp_path / "token"
        target.touch(mode=0o644)
        monkeypatch.setattr(Path, "chmod", lambda self, mode: None)

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
    def test_a_file_grants_only_this_account_and_inherits_nothing(self, tmp_path: Path):
        from linkedin_mcp_server.windows_acl import describe_dacl

        target = tmp_path / "token"
        target.touch()
        harden_file(target)

        described = describe_dacl(target)
        assert described.protected is True
        assert len(described.entries) == 1
        assert described.entries[0].flags == 0

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
    @posix_only
    def test_the_windows_helpers_refuse_rather_than_pretend(self, tmp_path: Path):
        # Importable everywhere so the module can be reasoned about and typed on
        # any platform, but every entry point refuses off Windows rather than
        # failing somewhere inside a DLL that is not there.
        from linkedin_mcp_server.windows_acl import current_user_sid

        with pytest.raises(PrivateStateError, match="off Windows"):
            current_user_sid()
