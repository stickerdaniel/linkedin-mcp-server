"""Tell local storage apart from storage something else also writes to.

The shared-browser daemon coordinates through a lock, a descriptor and a token
file that several processes on one machine must agree on. A network filesystem
does not promise those semantics, and a sync client copies, replaces and
restores files behind the processes that own them. Either can hand a stale
descriptor or a second lock to a process that then drives the same profile, so
only storage this module can show to be local qualifies. Everything else keeps
the Direct server it had before the daemon existed.

The policy is in ``docs/decisions/2026-09-26-daemon-default-on-contract.md``:
local filesystem types are allow-listed, known sync providers are refused, and
what cannot be classified is refused too. Sync that leaves no trace this module
can read, such as Syncthing or rsync, is a documented limitation. So is iCloud's
"Desktop & Documents" on macOS: those folders stay where they are, and asking
iCloud Drive which folders it mirrors would itself raise a privacy prompt.

The one entry point, :func:`classify`, never raises. A failure anywhere inside
is an answer, ``UNKNOWN``, because the caller's only safe reaction to "could
not tell" is the same as to "not local".
"""

from __future__ import annotations

import enum
import json
import logging
import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath

from linkedin_mcp_server.session_state import canonical

logger = logging.getLogger(__name__)


class StorageClass(enum.Enum):
    """Where a path's bytes live, as far as the daemon is concerned."""

    LOCAL = "local"
    NONLOCAL = "non-local"
    SYNCED = "synced"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Classification:
    """A storage class and the reason for it.

    The reason is written for a warning a user may paste into an issue, so it
    names a filesystem or a provider and never a path.
    """

    storage_class: StorageClass
    reason: str


def classify(path: Path) -> Classification:
    """Classify the storage *path* is on, or would be on once created.

    Resolved first, with :func:`session_state.canonical`, so that a symlink
    into a synced folder is judged by where it leads. A path that does not
    exist yet is judged by its nearest existing ancestor, which is where it
    would be created.
    """
    try:
        return _classify(path, sys.platform)
    except Exception as exc:
        # The boundary that makes this total. An OS reader that fails is not
        # evidence of local storage, and an exception reaching the caller would
        # skip the Direct fallback it exists to choose.
        logger.debug("Storage classification failed", exc_info=True)
        return _unknown(f"classifying it failed ({type(exc).__name__})")


def _classify(path: Path, platform: str) -> Classification:
    resolved = canonical(path)
    existing = _nearest_existing(resolved)
    fold_case = platform in ("darwin", "win32")

    roots, unreadable = _provider_roots(platform)
    for root, provider in roots:
        if is_within(resolved, root, fold_case=fold_case):
            return Classification(StorageClass.SYNCED, f"inside {provider}")
    if unreadable:
        # Checked after the roots that could be read, so that a path inside a
        # known provider still says which one.
        return _unknown(unreadable[0])
    return _filesystem_class(existing, platform)


def _unknown(reason: str) -> Classification:
    return Classification(StorageClass.UNKNOWN, reason)


def _nearest_existing(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    raise FileNotFoundError("no ancestor of the path exists")


def is_within(path: PurePath, root: PurePath, *, fold_case: bool) -> bool:
    """Whether *path* is *root* or below it, compared by whole components.

    Never by string prefix: ``Dropbox-archive`` is not inside ``Dropbox``.
    *fold_case* is for filesystems that ignore case, where ``~/library`` names
    the same folder as ``~/Library``.
    """
    path_parts = path.parts
    root_parts = root.parts
    if not root_parts or len(root_parts) > len(path_parts):
        return False
    head = path_parts[: len(root_parts)]
    if fold_case:
        return [part.casefold() for part in head] == [
            part.casefold() for part in root_parts
        ]
    return tuple(head) == tuple(root_parts)


# --- Known sync providers ---------------------------------------------------

#: Every macOS File Provider client (OneDrive, Google Drive, the current
#: Dropbox, Box) mounts under ``~/Library/CloudStorage``; iCloud Drive lives in
#: ``~/Library/Mobile Documents``.
_DARWIN_PROVIDER_FOLDERS = (
    (("Library", "CloudStorage"), "a cloud storage folder"),
    (("Library", "Mobile Documents"), "iCloud Drive"),
)

#: Set by the OneDrive client for each signed-in account.
_ONEDRIVE_VARIABLES = ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")

#: Far above any real ``info.json``; a larger file is not one Dropbox wrote.
_DROPBOX_INFO_LIMIT = 1024 * 1024
_ERROR_NO_MORE_ITEMS = 259
#: Where Windows Dropbox keeps ``info.json``: the variable first, then the
#: ``CSIDL`` Windows answers for it (``CSIDL_APPDATA``, ``CSIDL_LOCAL_APPDATA``).
_WINDOWS_DROPBOX_FOLDERS = (("APPDATA", 0x001A), ("LOCALAPPDATA", 0x001C))


class _ProviderUnreadable(Exception):
    """A provider's configuration exists but says nothing usable."""


def _provider_roots(platform: str) -> tuple[list[tuple[Path, str]], list[str]]:
    """Return every known synced root, and why any configuration was unreadable."""
    roots: list[tuple[Path, str]] = []
    unreadable: list[str] = []

    homes = _homes()
    if platform == "darwin":
        if not homes:
            unreadable.append("the home directory could not be determined")
        for home in homes:
            for parts, provider in _DARWIN_PROVIDER_FOLDERS:
                roots.append((_resolved(home.joinpath(*parts)), provider))
    if platform == "win32":
        # The variables alone are not enough: an MCP host may start this
        # server with a reduced environment that leaves them out, while the
        # client's own registration in the registry is still there.
        folders = [os.environ.get(name, "") for name in _ONEDRIVE_VARIABLES]
        try:
            folders.extend(_onedrive_registered_folders())
        except OSError:
            unreadable.append("OneDrive's configuration could not be read")
        for folder in folders:
            if folder and Path(folder).is_absolute():
                roots.append((_resolved(Path(folder)), "OneDrive"))

    info_files = _dropbox_info_files(platform, homes)
    if info_files is None:
        unreadable.append("Dropbox's configuration could not be located")
        info_files = []
    for info in info_files:
        try:
            accounts = dropbox_roots(info)
        except _ProviderUnreadable:
            unreadable.append("Dropbox's configuration could not be read")
            continue
        roots.extend((_resolved(root), "a Dropbox folder") for root in accounts)
    return roots, unreadable


def _resolved(root: Path) -> Path:
    # The candidate path is resolved, so a root reached through a link has to be
    # too, or a Dropbox folder that is itself a symlink would never match.
    return canonical(root)


def _homes() -> list[Path]:
    """Every directory that may be this account's home.

    Both the environment's and the account database's, because a launcher can
    override ``HOME`` for one process while the sync client still writes its
    configuration under the real one.
    """
    homes: list[Path] = []
    try:
        homes.append(Path.home())
    except (RuntimeError, KeyError, OSError):
        pass
    if os.name != "nt":
        import pwd

        try:
            homes.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
        except KeyError:
            pass
    distinct: list[Path] = []
    for home in homes:
        if home.is_absolute() and home not in distinct:
            distinct.append(home)
    return distinct


def _dropbox_info_files(platform: str, homes: Sequence[Path]) -> list[Path] | None:
    """Where Dropbox records its accounts, per its own documentation.

    None when one of those places cannot be named: an ``info.json`` that could
    be there is not an ``info.json`` known to be absent.
    """
    if platform == "win32":
        files: list[Path] = []
        for variable, csidl in _WINDOWS_DROPBOX_FOLDERS:
            folder = _absolute(os.environ.get(variable, ""))
            # An MCP host may start this server without these variables. The
            # folders still exist, and Windows names them itself.
            folder = folder or _absolute(_known_folder(csidl) or "")
            if folder is None:
                return None
            files.append(folder / "Dropbox" / "info.json")
        return files
    if not homes:
        return None
    return [home / ".dropbox" / "info.json" for home in homes]


def _absolute(value: str) -> Path | None:
    return Path(value) if value and Path(value).is_absolute() else None


def _known_folder(csidl: int) -> str | None:  # pragma: no cover - Windows only
    """Ask Windows for one of the current account's folders, or None.

    The same call ``daemon_descriptor._account_home`` makes, for a different
    folder. None on any failure: the caller decides what not knowing means.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = getattr(ctypes, "WinDLL")("shell32", use_last_error=True)
        get_folder = shell32.SHGetFolderPathW
        get_folder.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
        ]
        get_folder.restype = ctypes.c_long
        buffer = ctypes.create_unicode_buffer(32768)
        if get_folder(None, csidl, None, 0, buffer) != 0:
            return None
        return buffer.value or None
    except Exception:
        logger.debug("SHGetFolderPathW failed", exc_info=True)
        return None


def _onedrive_registered_folders() -> list[str]:  # pragma: no cover - Windows only
    """Every OneDrive account's folder, from the client's registry entries."""
    if sys.platform != "win32":
        return []
    import winreg

    try:
        accounts = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\OneDrive\Accounts"
        )
    except FileNotFoundError:
        return []
    folders: list[str] = []
    with accounts:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(accounts, index)
            except OSError as exc:
                if getattr(exc, "winerror", None) == _ERROR_NO_MORE_ITEMS:
                    break
                raise
            index += 1
            try:
                with winreg.OpenKey(accounts, name) as account:
                    value, kind = winreg.QueryValueEx(account, "UserFolder")
            except FileNotFoundError:
                # An account entry that was never set up has no folder.
                continue
            if kind == winreg.REG_EXPAND_SZ and isinstance(value, str):
                value = os.path.expandvars(value)
            if isinstance(value, str) and value:
                folders.append(value)
    return folders


def dropbox_roots(info: Path) -> list[Path]:
    """Return every account's Dropbox folder recorded in *info*.

    An absent file means no Dropbox and is not a reason to refuse: a machine
    without Dropbox must not become unclassifiable. A file that is there but
    cannot be read or parsed is different. Dropbox is installed, and where it
    syncs is unknown.

    Raises:
        _ProviderUnreadable: the file exists but does not answer.
    """
    try:
        with info.open("rb") as handle:
            raw = handle.read(_DROPBOX_INFO_LIMIT + 1)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as exc:
        raise _ProviderUnreadable(type(exc).__name__) from exc
    if len(raw) > _DROPBOX_INFO_LIMIT:
        raise _ProviderUnreadable("too large")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _ProviderUnreadable("not JSON") from exc
    if not isinstance(document, dict):
        raise _ProviderUnreadable("not an object")

    # One entry per signed-in account, keyed "personal" or "business". Every
    # one is a synced root; reading only the first would miss the other.
    roots: list[Path] = []
    for account in document.values():
        location = account.get("path") if isinstance(account, dict) else None
        if not isinstance(location, str) or not Path(location).is_absolute():
            raise _ProviderUnreadable("an account without an absolute path")
        roots.append(Path(location))
    return roots


# --- Filesystems --------------------------------------------------------------


def _filesystem_class(existing: Path, platform: str) -> Classification:
    """Ask the platform what *existing* is on.

    A reader that fails says so by name. It is still ``UNKNOWN``, but a broken
    probe is not evidence about the storage, and a report that read "unknown
    filesystem" would send the diagnosis to the wrong place.
    """
    if platform.startswith("linux"):
        try:
            mounts = parse_mountinfo(_read_mountinfo())
        except Exception as exc:
            return _reader_failed("reading /proc/self/mountinfo", exc)
        return linux_class(existing, mounts)
    if platform == "darwin":
        try:
            raw_name, flags = _darwin_statfs(existing)
        except Exception as exc:
            return _reader_failed("macOS statfs probe", exc)
        name = darwin_fstypename(raw_name)
        if name is None:
            return _unknown("macOS statfs returned an unreadable result")
        return darwin_class(name, flags)
    if platform == "win32":
        return _windows_class(existing)
    return _unknown(f"storage is not classified on {platform}")


def _reader_failed(what: str, exc: Exception) -> Classification:
    logger.debug("%s failed", what, exc_info=True)
    return _unknown(f"{what} failed ({type(exc).__name__})")


# Linux --------------------------------------------------------------------------

_LINUX_LOCAL = frozenset(
    ("ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "zfs", "tmpfs", "bcachefs")
)
_LINUX_NONLOCAL = frozenset(
    ("cifs", "9p", "virtiofs", "sshfs", "overlay", "fuse", "ceph", "glusterfs")
)
_LINUX_NONLOCAL_PREFIXES = ("nfs", "smb", "fuse.")
_MOUNTINFO_ESCAPE = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True)
class Mount:
    mount_point: PurePosixPath
    fstype: str


def _read_mountinfo() -> str:
    with open("/proc/self/mountinfo", "rb") as handle:
        return os.fsdecode(handle.read())


def parse_mountinfo(text: str) -> list[Mount]:
    """Parse ``/proc/self/mountinfo`` into mount points and filesystem types.

    A line that does not parse raises instead of being skipped: it might be
    the mount the path is on, and skipping it would judge the path by its
    parent's filesystem.
    """
    mounts: list[Mount] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split(" ")
        # Optional fields end at a lone "-", followed by type, source, options.
        try:
            separator = fields.index("-", 6)
            mount_point = fields[4]
            fstype = fields[separator + 1]
        except (ValueError, IndexError) as exc:
            raise ValueError("a mountinfo line does not parse") from exc
        decoded = _MOUNTINFO_ESCAPE.sub(
            lambda match: chr(int(match.group(1), 8)), mount_point
        )
        mounts.append(Mount(PurePosixPath(decoded), fstype))
    return mounts


def linux_class(path: PurePath, mounts: Iterable[Mount]) -> Classification:
    """Classify *path* by the mount with the longest matching mount point.

    Later lines win a tie: a mount stacked on the same point hides the one
    listed before it.
    """
    best: Mount | None = None
    for mount in mounts:
        if not is_within(path, mount.mount_point, fold_case=False):
            continue
        if best is None or len(mount.mount_point.parts) >= len(best.mount_point.parts):
            best = mount
    if best is None:
        return _unknown("no mount covers it")
    return linux_fstype_class(best.fstype)


def linux_fstype_class(fstype: str) -> Classification:
    if fstype in _LINUX_LOCAL:
        return Classification(StorageClass.LOCAL, f"local {fstype} filesystem")
    if fstype in _LINUX_NONLOCAL or fstype.startswith(_LINUX_NONLOCAL_PREFIXES):
        return Classification(StorageClass.NONLOCAL, f"{fstype} filesystem")
    return _unknown(f"{fstype} is not a filesystem known to be local")


# macOS -------------------------------------------------------------------------

_DARWIN_LOCAL = frozenset(("apfs", "hfs"))
#: ``MNT_LOCAL`` from ``<sys/mount.h>``.
_MNT_LOCAL = 0x00001000


#: ``MFSTYPENAMELEN``: the name and its terminating NUL.
_MFSTYPENAMELEN = 16


def darwin_fstypename(raw: bytes) -> str | None:
    """The ``f_fstypename`` field as a name, or None if it cannot be one.

    The kernel writes a short NUL-terminated ASCII name here. Anything else
    means the structure was read at the wrong offsets, and those bytes say
    nothing about the filesystem.
    """
    if len(raw) != _MFSTYPENAMELEN or b"\0" not in raw:
        return None
    name = raw.split(b"\0", 1)[0]
    if not name or any(byte < 0x21 or byte > 0x7E for byte in name):
        return None
    return name.decode("ascii")


def darwin_class(fstypename: str, flags: int) -> Classification:
    if not flags & _MNT_LOCAL:
        return Classification(StorageClass.NONLOCAL, f"{fstypename} filesystem")
    if fstypename in _DARWIN_LOCAL:
        return Classification(StorageClass.LOCAL, f"local {fstypename} filesystem")
    return _unknown(f"{fstypename} is not a filesystem known to be local")


def _darwin_statfs(path: Path) -> tuple[bytes, int]:  # pragma: no cover - macOS
    """Return the raw ``f_fstypename`` bytes and ``f_flags`` from ``statfs(2)``.

    ``os.statvfs`` carries neither. The structure is the 64-bit-inode layout
    from ``<sys/mount.h>``, which arm64 exports as ``statfs`` and x86_64 as
    ``statfs$INODE64``; the plain x86_64 symbol has an older layout. The name
    is returned raw so :func:`darwin_fstypename` can tell a layout read at the
    wrong offsets from a real filesystem name.
    """
    import ctypes

    class _StatFs(ctypes.Structure):
        _fields_ = [
            ("f_bsize", ctypes.c_uint32),
            ("f_iosize", ctypes.c_int32),
            ("f_blocks", ctypes.c_uint64),
            ("f_bfree", ctypes.c_uint64),
            ("f_bavail", ctypes.c_uint64),
            ("f_files", ctypes.c_uint64),
            ("f_ffree", ctypes.c_uint64),
            ("f_fsid", ctypes.c_int32 * 2),
            ("f_owner", ctypes.c_uint32),
            ("f_type", ctypes.c_uint32),
            ("f_flags", ctypes.c_uint32),
            ("f_fssubtype", ctypes.c_uint32),
            ("f_fstypename", ctypes.c_char * 16),
            ("f_mntonname", ctypes.c_char * 1024),
            ("f_mntfromname", ctypes.c_char * 1024),
            ("f_flags_ext", ctypes.c_uint32),
            ("f_reserved", ctypes.c_uint32 * 7),
        ]

    libc = ctypes.CDLL(None, use_errno=True)
    symbol = "statfs" if os.uname().machine == "arm64" else "statfs$INODE64"
    function = libc[symbol]
    function.argtypes = [ctypes.c_char_p, ctypes.POINTER(_StatFs)]
    function.restype = ctypes.c_int
    result = _StatFs()
    if function(os.fsencode(path), ctypes.byref(result)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    # All sixteen bytes, not ``.value``, which stops at the first NUL and so
    # would hide a name that never ends.
    offset = _StatFs.f_fstypename.offset
    raw_name = bytes(result)[offset : offset + _MFSTYPENAMELEN]
    return raw_name, result.f_flags


# Windows -----------------------------------------------------------------------

_DRIVE_REMOTE = 4
_DRIVE_FIXED = 3
_DRIVE_RAMDISK = 6
_WINDOWS_LOCAL = frozenset(("ntfs", "refs"))

_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
#: ``IO_REPARSE_TAG_CLOUD`` and its sixteen ``CLOUD_n`` variants, which differ
#: only in the bits ``IO_REPARSE_TAG_CLOUD_MASK`` covers.
_IO_REPARSE_TAG_CLOUD = 0x9000001A
_IO_REPARSE_TAG_CLOUD_MASK = 0x0000F000
_IO_REPARSE_TAG_ONEDRIVE = 0x80000021


def windows_volume_class(drive_type: int, filesystem: str | None) -> Classification:
    """Classify a volume by drive type, then by filesystem name.

    *filesystem* is only asked for on a fixed drive: querying a network share
    for its filesystem is a network round trip for an answer already known.
    """
    if drive_type == _DRIVE_REMOTE:
        return Classification(StorageClass.NONLOCAL, "network drive")
    if drive_type not in (_DRIVE_FIXED, _DRIVE_RAMDISK):
        return _unknown(f"drive type {drive_type} is not a fixed drive")
    name = filesystem or ""
    if name.casefold() in _WINDOWS_LOCAL:
        return Classification(StorageClass.LOCAL, f"local {name} volume")
    return _unknown(f"{name or 'an unnamed filesystem'} is not known to be local")


def windows_cloud_marker(
    path: PurePath, lstat: Callable[[PurePath], os.stat_result]
) -> str | None:
    """Name the cloud-files marker on *path* or any ancestor, if there is one.

    Two different things, and both count. The recall attributes are file
    attributes: they say reading the data may fetch it from elsewhere. The
    cloud reparse tags are reparse metadata a sync engine puts on its
    placeholders. Neither being absent proves a folder is unsynced, which is
    why the drive and filesystem checks still run.
    """
    for candidate in (path, *path.parents):
        info = lstat(candidate)
        attributes = getattr(info, "st_file_attributes", 0)
        if attributes & (
            _FILE_ATTRIBUTE_RECALL_ON_OPEN | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        ):
            return "a folder whose data is recalled from the cloud"
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            tag = getattr(info, "st_reparse_tag", 0)
            if (tag & ~_IO_REPARSE_TAG_CLOUD_MASK) == _IO_REPARSE_TAG_CLOUD or (
                tag == _IO_REPARSE_TAG_ONEDRIVE
            ):
                return "a cloud-files placeholder"
    return None


def _windows_class(existing: Path) -> Classification:
    try:
        marker = windows_cloud_marker(existing, lambda candidate: os.lstat(candidate))
    except Exception as exc:
        return _reader_failed("reading Windows file attributes", exc)
    if marker is not None:
        return Classification(StorageClass.SYNCED, f"inside {marker}")
    try:
        root = _windows_volume_root(existing)
        drive_type = _windows_drive_type(root)
        filesystem = (
            _windows_filesystem(root)
            if drive_type in (_DRIVE_FIXED, _DRIVE_RAMDISK)
            else None
        )
    except Exception as exc:
        return _reader_failed("Windows volume query", exc)
    return windows_volume_class(drive_type, filesystem)


def _kernel32():  # pragma: no cover - Windows only
    import ctypes

    # WinDLL exists only on Windows, and a type checker running elsewhere
    # would otherwise reject the attribute.
    return getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)


def _windows_error() -> OSError:  # pragma: no cover - Windows only
    import ctypes

    return getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())


def _windows_volume_root(path: Path) -> str:  # pragma: no cover - Windows only
    """The mount point *path* is on, which for a mounted folder is not the drive."""
    import ctypes
    from ctypes import wintypes

    function = _kernel32().GetVolumePathNameW
    function.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    function.restype = wintypes.BOOL
    buffer = ctypes.create_unicode_buffer(32768)
    if not function(str(path), buffer, len(buffer)):
        raise _windows_error()
    return buffer.value


def _windows_drive_type(root: str) -> int:  # pragma: no cover - Windows only
    from ctypes import wintypes

    function = _kernel32().GetDriveTypeW
    function.argtypes = [wintypes.LPCWSTR]
    function.restype = wintypes.UINT
    return int(function(root))


def _windows_filesystem(root: str) -> str:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    function = _kernel32().GetVolumeInformationW
    function.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    function.restype = wintypes.BOOL
    name = ctypes.create_unicode_buffer(261)
    if not function(root, None, 0, None, None, None, name, len(name)):
        raise _windows_error()
    return name.value
