"""Which storage the shared-browser daemon may coordinate on.

Most of these tests drive the pure halves of the classifier with fixture
inputs, so each platform's rules run on every runner. The native readers are
exercised once, at the end, against this runner's own temporary directory.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest

import linkedin_mcp_server.storage_class as storage_class
from linkedin_mcp_server.storage_class import (
    Classification,
    Mount,
    StorageClass,
    classify,
    darwin_class,
    dropbox_roots,
    is_within,
    linux_class,
    parse_mountinfo,
    windows_cloud_marker,
    windows_volume_class,
)

LOCAL = StorageClass.LOCAL
NONLOCAL = StorageClass.NONLOCAL
SYNCED = StorageClass.SYNCED
UNKNOWN = StorageClass.UNKNOWN

# Real mountinfo shape: optional fields (zero, one or two) before the lone "-",
# and the kernel's octal escapes in mount points.
_MOUNTINFO = "\n".join(
    [
        "22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw",
        "23 22 8:2 / /home rw,relatime shared:2 master:7 - xfs /dev/sda2 rw",
        "24 23 0:40 / /home/u/net rw - nfs4 server:/export rw,vers=4.2",
        "25 23 0:41 / /home/u/net\\040drive rw - cifs //nas/share rw",
        "26 22 0:42 / /var/lib/docker/overlay rw - overlay overlay rw,lowerdir=/a",
        "27 23 0:43 / /home/u/remote rw - fuse.sshfs u@host:/ rw",
        "28 22 0:44 / /mnt/nfs rw - nfs server:/nfs rw",
        "29 22 0:45 / /tmp rw,nosuid shared:5 - tmpfs tmpfs rw",
        "30 22 8:3 / /mnt/stick rw - vfat /dev/sdb1 rw",
        # Stacked on /mnt/stacked: the later mount hides the earlier one.
        "31 22 0:46 / /mnt/stacked rw - nfs server:/a rw",
        "32 22 8:4 / /mnt/stacked rw - ext4 /dev/sdc1 rw",
    ]
)


def _linux(path: str) -> Classification:
    return linux_class(PurePosixPath(path), parse_mountinfo(_MOUNTINFO))


class TestLinuxMounts:
    def test_escaped_spaces_are_decoded(self):
        points = {str(mount.mount_point) for mount in parse_mountinfo(_MOUNTINFO)}

        assert "/home/u/net drive" in points
        assert "/home/u/net\\040drive" not in points

    @pytest.mark.parametrize(
        ("path", "expected", "fstype"),
        [
            ("/home/u/.linkedin-mcp", LOCAL, "xfs"),
            ("/home/u/net/profile", NONLOCAL, "nfs4"),
            ("/home/u/net drive/profile", NONLOCAL, "cifs"),
            ("/var/lib/docker/overlay/x", NONLOCAL, "overlay"),
            ("/home/u/remote/x", NONLOCAL, "fuse.sshfs"),
            ("/tmp/pytest-of-u", LOCAL, "tmpfs"),
            ("/mnt/stick/profile", UNKNOWN, "vfat"),
            ("/mnt/stacked/profile", LOCAL, "ext4"),
            ("/etc", LOCAL, "ext4"),
        ],
    )
    def test_the_longest_mount_point_decides(
        self, path: str, expected: StorageClass, fstype: str
    ):
        verdict = _linux(path)

        assert verdict.storage_class is expected
        assert fstype in verdict.reason

    def test_mount_points_match_whole_components(self):
        # /mnt/nfs-local shares a string prefix with the NFS mount at /mnt/nfs
        # and nothing else: it is on the root filesystem.
        assert _linux("/mnt/nfs-local/profile").storage_class is LOCAL
        assert _linux("/mnt/nfs/profile").storage_class is NONLOCAL

    def test_no_covering_mount_is_unknown(self):
        mounts = [Mount(PurePosixPath("/home"), "ext4")]

        assert linux_class(PurePosixPath("/srv/x"), mounts).storage_class is UNKNOWN

    def test_a_line_that_does_not_parse_is_refused_rather_than_skipped(self):
        with pytest.raises(ValueError):
            parse_mountinfo(_MOUNTINFO + "\n33 22 0:47 / /mnt/x rw no-separator")

    def test_every_nfs_and_smb_variant_is_nonlocal(self):
        for fstype in ("nfs", "nfs4", "smb3", "smbfs", "9p", "virtiofs", "fuse"):
            verdict = storage_class.linux_fstype_class(fstype)
            assert verdict.storage_class is NONLOCAL, fstype


class TestMacFilesystems:
    MNT_LOCAL = 0x1000

    def test_a_local_apfs_volume_is_local(self):
        assert darwin_class("apfs", self.MNT_LOCAL).storage_class is LOCAL
        assert darwin_class("hfs", self.MNT_LOCAL | 0x1).storage_class is LOCAL

    def test_a_volume_without_mnt_local_is_nonlocal(self):
        assert darwin_class("smbfs", 0).storage_class is NONLOCAL
        assert darwin_class("nfs", 0).storage_class is NONLOCAL
        # The flag decides, not the name.
        assert darwin_class("apfs", 0).storage_class is NONLOCAL

    def test_a_local_filesystem_off_the_allow_list_is_unknown(self):
        assert darwin_class("msdos", self.MNT_LOCAL).storage_class is UNKNOWN
        assert darwin_class("devfs", self.MNT_LOCAL).storage_class is UNKNOWN


class TestWindowsVolumes:
    @pytest.mark.parametrize(
        ("drive_type", "filesystem", "expected"),
        [
            (3, "NTFS", LOCAL),
            (3, "ReFS", LOCAL),
            (6, "NTFS", LOCAL),
            (4, None, NONLOCAL),
            (2, None, UNKNOWN),
            (5, None, UNKNOWN),
            (0, None, UNKNOWN),
            (3, "FAT32", UNKNOWN),
            (3, "exFAT", UNKNOWN),
            (3, "", UNKNOWN),
        ],
    )
    def test_drive_type_then_filesystem(
        self, drive_type: int, filesystem: str | None, expected: StorageClass
    ):
        assert windows_volume_class(drive_type, filesystem).storage_class is expected

    @staticmethod
    def _lstat(entries: dict[str, tuple[int, int]]):
        def lstat(path: PurePath):
            attributes, tag = entries.get(str(path), (0x10, 0))
            return SimpleNamespace(st_file_attributes=attributes, st_reparse_tag=tag)

        return lstat

    @pytest.mark.parametrize(
        ("marked", "attributes", "tag"),
        [
            # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS on an ancestor: an attribute.
            (r"C:\Users\u\Cloud", 0x10 | 0x400000, 0),
            # FILE_ATTRIBUTE_RECALL_ON_OPEN on the path itself.
            (r"C:\Users\u\Cloud\profile", 0x10 | 0x40000, 0),
            # IO_REPARSE_TAG_CLOUD_3: reparse metadata, not an attribute.
            (r"C:\Users\u\Cloud", 0x10 | 0x400, 0x9000301A),
            (r"C:\Users\u\Cloud", 0x10 | 0x400, 0x9000001A),
        ],
    )
    def test_a_cloud_marker_on_the_path_or_an_ancestor_is_found(
        self, marked: str, attributes: int, tag: int
    ):
        lstat = self._lstat({marked: (attributes, tag)})

        found = windows_cloud_marker(
            PureWindowsPath(r"C:\Users\u\Cloud\profile"), lstat
        )

        assert found is not None

    def test_ordinary_reparse_points_are_not_cloud_markers(self):
        # A symlink (IO_REPARSE_TAG_SYMLINK) and a junction (MOUNT_POINT) are
        # layout, not sync.
        lstat = self._lstat(
            {
                r"C:\Users\u\link": (0x10 | 0x400, 0xA000000C),
                r"C:\Users\u": (0x10 | 0x400, 0xA0000003),
            }
        )

        assert windows_cloud_marker(PureWindowsPath(r"C:\Users\u\link"), lstat) is None

    def test_a_failing_attribute_read_is_not_an_answer(self):
        def lstat(path: PurePath):
            raise PermissionError(5, "Access is denied")

        with pytest.raises(PermissionError):
            windows_cloud_marker(PureWindowsPath(r"C:\x"), lstat)


class TestComponentMatching:
    def test_a_sibling_sharing_a_prefix_is_not_inside(self):
        root = PurePosixPath("/home/u/Dropbox")

        assert is_within(PurePosixPath("/home/u/Dropbox"), root, fold_case=False)
        assert is_within(PurePosixPath("/home/u/Dropbox/a"), root, fold_case=False)
        assert not is_within(
            PurePosixPath("/home/u/Dropbox-archive/a"), root, fold_case=False
        )
        assert not is_within(PurePosixPath("/home/u"), root, fold_case=False)

    def test_case_is_folded_only_where_the_filesystem_ignores_it(self):
        root = PureWindowsPath(r"C:\Users\U\OneDrive")
        path = PureWindowsPath(r"c:\users\u\onedrive\profile")

        assert is_within(path, root, fold_case=True)
        assert not is_within(
            PurePosixPath("/home/u/dropbox/x"),
            PurePosixPath("/home/u/Dropbox"),
            fold_case=False,
        )


def _write_info(path: Path, document: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class TestDropboxConfiguration:
    def test_every_account_is_a_root(self, tmp_path: Path):
        personal = tmp_path / "Dropbox (Personal)"
        business = tmp_path / "Dropbox (Acme)"
        info = _write_info(
            tmp_path / "info.json",
            {
                "personal": {"path": str(personal), "host": 1, "is_team": False},
                "business": {"path": str(business), "host": 2, "is_team": True},
            },
        )

        assert dropbox_roots(info) == [personal, business]

    def test_an_absent_file_means_no_dropbox(self, tmp_path: Path):
        assert dropbox_roots(tmp_path / "missing" / "info.json") == []

    @pytest.mark.parametrize(
        "content",
        [
            b"{not json",
            b"\xff\xfe",
            b"[]",
            b'{"personal": {"host": 1}}',
            b'{"personal": {"path": "relative/Dropbox"}}',
            b'{"personal": "x"}',
        ],
    )
    def test_a_present_file_that_does_not_parse_is_unreadable(
        self, tmp_path: Path, content: bytes
    ):
        info = tmp_path / "info.json"
        info.write_bytes(content)

        with pytest.raises(storage_class._ProviderUnreadable):
            dropbox_roots(info)

    def test_a_present_file_that_cannot_be_opened_is_unreadable(self, tmp_path: Path):
        # A directory where the file should be fails to open on every platform
        # (IsADirectoryError on POSIX, PermissionError on Windows), and neither
        # means Dropbox is absent.
        (tmp_path / "info.json").mkdir()

        with pytest.raises(storage_class._ProviderUnreadable):
            dropbox_roots(tmp_path / "info.json")


class _ReaderBroke(OSError):
    pass


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A home under tmp_path and a filesystem double that behaves like statfs.

    The double refuses a path that does not exist, as the real readers do, so
    skipping the nearest-ancestor step cannot pass by accident.
    """
    home = tmp_path / "home"
    home.mkdir()
    local = tmp_path / "local"
    local.mkdir()
    seen: list[Path] = []

    def filesystem(existing: Path, _platform: str) -> Classification:
        if not existing.exists():
            raise FileNotFoundError(2, "No such file or directory")
        seen.append(existing)
        return Classification(LOCAL, "local test filesystem")

    monkeypatch.setattr(storage_class, "_homes", lambda: [home])
    monkeypatch.setattr(storage_class, "_filesystem_class", filesystem)
    return SimpleNamespace(home=home, local=local, seen=seen, root=tmp_path)


def _dropbox(world: SimpleNamespace, *accounts: Path) -> None:
    names = ("personal", "business")
    _write_info(
        world.home / ".dropbox" / "info.json",
        {name: {"path": str(path)} for name, path in zip(names, accounts)},
    )
    for path in accounts:
        path.mkdir(parents=True, exist_ok=True)


class TestClassification:
    def test_a_nonexistent_child_of_a_local_dir_is_local(self, world):
        target = world.local / "not" / "yet" / "profile"

        verdict = storage_class._classify(target, "linux")

        assert verdict.storage_class is LOCAL
        # Judged where it would be created, not at a path that is not there.
        assert world.seen == [storage_class.canonical(world.local)]

    def test_a_nonexistent_child_of_a_synced_dir_is_synced(self, world):
        dropbox = world.root / "Dropbox"
        _dropbox(world, dropbox)

        verdict = storage_class._classify(dropbox / "new" / "profile", "linux")

        assert verdict.storage_class is SYNCED
        assert "Dropbox" in verdict.reason

    def test_the_second_dropbox_account_is_synced_too(self, world):
        personal = world.root / "Dropbox (Personal)"
        business = world.root / "Dropbox (Acme)"
        _dropbox(world, personal, business)

        first = storage_class._classify(personal / "profile", "linux")
        second = storage_class._classify(business / "profile", "linux")

        assert first.storage_class is SYNCED
        assert second.storage_class is SYNCED

    def test_a_sibling_of_a_dropbox_folder_is_not_synced(self, world):
        dropbox = world.root / "Dropbox"
        _dropbox(world, dropbox)
        sibling = world.root / "Dropbox-archive"
        sibling.mkdir()

        assert storage_class._classify(sibling, "linux").storage_class is LOCAL

    def test_no_dropbox_configuration_leaves_the_filesystem_to_decide(self, world):
        assert not (world.home / ".dropbox").exists()

        assert storage_class._classify(world.local, "linux").storage_class is LOCAL

    @pytest.mark.parametrize("content", [b"{not json", b'{"personal": {}}'])
    def test_an_unreadable_dropbox_configuration_is_unknown(self, world, content):
        info = world.home / ".dropbox" / "info.json"
        info.parent.mkdir()
        info.write_bytes(content)

        verdict = storage_class._classify(world.local, "linux")

        assert verdict.storage_class is UNKNOWN
        assert "Dropbox" in verdict.reason
        assert world.seen == []

    def test_no_home_means_dropbox_cannot_be_ruled_out(self, world, monkeypatch):
        monkeypatch.setattr(storage_class, "_homes", lambda: [])

        assert storage_class._classify(world.local, "linux").storage_class is UNKNOWN

    def test_a_symlink_into_cloud_storage_is_judged_by_its_target(self, world):
        provider = world.home / "Library" / "CloudStorage" / "OneDrive-Acme" / "x"
        provider.mkdir(parents=True)
        link = world.local / "linked"
        try:
            link.symlink_to(provider, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"this runner cannot create symlinks: {exc}")

        verdict = storage_class._classify(link / "profile", "darwin")

        assert verdict.storage_class is SYNCED
        assert "cloud storage" in verdict.reason

    @pytest.mark.parametrize(
        ("parts", "provider"),
        [
            (("Library", "Mobile Documents", "com~apple~CloudDocs", "p"), "iCloud"),
            (("library", "cloudstorage", "GoogleDrive-u", "p"), "cloud storage"),
        ],
    )
    def test_mac_provider_folders_are_synced(self, world, parts, provider):
        verdict = storage_class._classify(world.home.joinpath(*parts), "darwin")

        assert verdict.storage_class is SYNCED
        assert provider in verdict.reason

    def test_a_reader_failure_is_unknown_and_never_raises(self, world, monkeypatch):
        def broken(existing: Path, _platform: str) -> Classification:
            raise _ReaderBroke(5, "statfs failed")

        monkeypatch.setattr(storage_class, "_filesystem_class", broken)

        verdict = classify(world.local)

        assert verdict.storage_class is UNKNOWN
        assert "_ReaderBroke" in verdict.reason

    def test_a_failure_to_resolve_is_unknown_and_never_raises(self, monkeypatch):
        def loop(_path: Path) -> Path:
            raise RuntimeError("Symlink loop")

        monkeypatch.setattr(storage_class, "canonical", loop)

        verdict = classify(Path("anything"))

        assert verdict.storage_class is UNKNOWN
        assert "RuntimeError" in verdict.reason


class TestWindowsProviders:
    @pytest.fixture
    def windows(self, world, monkeypatch: pytest.MonkeyPatch):
        for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
            monkeypatch.delenv(name, raising=False)
        roaming = world.root / "AppData" / "Roaming"
        local_app = world.root / "AppData" / "Local"
        roaming.mkdir(parents=True)
        local_app.mkdir(parents=True)
        monkeypatch.setenv("APPDATA", str(roaming))
        monkeypatch.setenv("LOCALAPPDATA", str(local_app))
        registered: list[str] = []
        monkeypatch.setattr(
            storage_class, "_onedrive_registered_folders", lambda: list(registered)
        )
        world.roaming = roaming
        world.local_app = local_app
        world.registered = registered
        return world

    @pytest.mark.parametrize(
        "variable", ["OneDrive", "OneDriveCommercial", "OneDriveConsumer"]
    )
    def test_a_onedrive_variable_names_a_synced_root(
        self, windows, monkeypatch, variable
    ):
        onedrive = windows.root / "OneDrive - Acme"
        onedrive.mkdir()
        monkeypatch.setenv(variable, str(onedrive))

        verdict = storage_class._classify(onedrive / "profile", "win32")

        assert verdict.storage_class is SYNCED
        assert "OneDrive" in verdict.reason

    def test_a_registered_onedrive_folder_counts_without_the_variables(self, windows):
        onedrive = windows.root / "OneDrive"
        onedrive.mkdir()
        windows.registered.append(str(onedrive))

        verdict = storage_class._classify(onedrive / "profile", "win32")

        assert verdict.storage_class is SYNCED

    def test_an_unreadable_onedrive_registration_is_unknown(self, windows, monkeypatch):
        def denied() -> list[str]:
            raise PermissionError(5, "Access is denied")

        monkeypatch.setattr(storage_class, "_onedrive_registered_folders", denied)

        verdict = storage_class._classify(windows.local, "win32")

        assert verdict.storage_class is UNKNOWN
        assert "OneDrive" in verdict.reason

    @pytest.mark.parametrize("folder", ["roaming", "local_app"])
    def test_dropbox_is_read_from_both_windows_locations(self, windows, folder):
        dropbox = windows.root / "Dropbox"
        dropbox.mkdir()
        _write_info(
            getattr(windows, folder) / "Dropbox" / "info.json",
            {"personal": {"path": str(dropbox)}},
        )

        verdict = storage_class._classify(dropbox / "profile", "win32")

        assert verdict.storage_class is SYNCED

    def test_a_missing_appdata_means_dropbox_cannot_be_ruled_out(
        self, windows, monkeypatch
    ):
        monkeypatch.delenv("APPDATA")

        verdict = storage_class._classify(windows.local, "win32")

        assert verdict.storage_class is UNKNOWN

    def test_nothing_registered_leaves_the_volume_to_decide(self, windows):
        assert storage_class._classify(windows.local, "win32").storage_class is LOCAL


class TestNativeRunner:
    def test_this_runners_temporary_directory_classifies(self, tmp_path: Path):
        verdict = classify(tmp_path)

        assert isinstance(verdict.storage_class, StorageClass)
        assert verdict.reason

    def test_a_known_local_directory_is_local(self, tmp_path: Path):
        """The affirmative control: a classifier answering UNKNOWN for
        everything would keep every user on Direct and pass every refusal test.

        Asserted only where the platform guarantees the answer: a GitHub-hosted
        runner's temporary directory is on its system disk (ext4, APFS or
        NTFS), and so is macOS's per-user temporary directory. Elsewhere a
        developer's temporary directory may legitimately be anything.
        """
        hosted = os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
        mac_system_temp = sys.platform == "darwin" and str(
            storage_class.canonical(tmp_path)
        ).startswith("/private/var/folders/")
        if not (hosted or mac_system_temp):
            pytest.skip(
                "only GitHub-hosted runners and macOS's system temporary "
                "directory are known to be local"
            )

        verdict = classify(tmp_path)

        assert verdict.storage_class is LOCAL, verdict.reason
