"""Owner-only file permissions on Windows, via the Win32 security APIs.

``os.chmod`` on Windows sets the read-only attribute and nothing else, so the
``0600`` that protects a secret on POSIX protects nothing here. Access is
decided by the DACL instead, and setting one means calling Win32 directly.

Two details do the real work. The DACL is replaced rather than merged, so
whatever the parent granted does not survive; and it is marked *protected*, so
the parent's inheritable entries are not reapplied afterwards. A directory's
single entry is marked inheritable, which means files created inside it are
already owner-only at the moment they exist rather than from whenever something
gets around to hardening them.

Everything is read back and checked before returning. A caller only gets a
quiet return when the descriptor on disk actually says what it should.

``ctypes`` rather than ``pywin32``: this is the only place in the project that
needs Win32, and a dependency that installs on one platform to serve one module
is a poor trade. Deliberately no fallback path. If any of this fails, the
caller must not write the secret.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from linkedin_mcp_server.private_state import PrivateStateError

# Importable anywhere so tests can reach the helpers; every entry point refuses
# to run off Windows rather than failing obscurely inside a missing DLL.
_IS_WINDOWS = os.name == "nt"

# Loaded on first use rather than at import, so this module can be imported and
# type-checked on any platform while the libraries themselves only ever exist on
# Windows. Cached: binding the signatures is not free, and every call needs them.
_libraries: tuple[ctypes.CDLL, ctypes.CDLL] | None = None

_PSID = ctypes.c_void_p
_PACL = ctypes.c_void_p
_PSECURITY_DESCRIPTOR = ctypes.c_void_p
_HLOCAL = ctypes.c_void_p

ERROR_SUCCESS = 0
ERROR_INSUFFICIENT_BUFFER = 122

TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1  # TOKEN_INFORMATION_CLASS.TokenUser

NO_MULTIPLE_TRUSTEE = 0
TRUSTEE_IS_SID = 0
TRUSTEE_IS_USER = 1
SET_ACCESS = 2

NO_INHERITANCE = 0x00
OBJECT_INHERIT_ACE = 0x01
CONTAINER_INHERIT_ACE = 0x02
INHERITED_ACE = 0x10
CONTAINER_INHERITANCE = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE

ACCESS_ALLOWED_ACE_TYPE = 0x00
SE_FILE_OBJECT = 1

OWNER_SECURITY_INFORMATION = 0x00000001
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
# Replace the DACL *and* stop the parent's entries being inherited back in.
# Without the second flag the first one is close to cosmetic.
#
# The owner goes with them, because a DACL is only as good as who may rewrite
# it. Windows grants an object's owner READ_CONTROL and WRITE_DAC implicitly,
# with no ACE saying so, so a directory this account merely had permission to
# modify would keep its previous owner and that owner could widen the DACL
# straight back. Taking ownership is what makes the entry we just wrote the
# last word.
REPLACE_PROTECTED_DACL = (
    OWNER_SECURITY_INFORMATION
    | DACL_SECURITY_INFORMATION
    | PROTECTED_DACL_SECURITY_INFORMATION
)

SE_DACL_PROTECTED = 0x1000
FILE_ALL_ACCESS = 0x001F01FF

FILE_PERSISTENT_ACLS = 0x00000008


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", _PSID), ("Attributes", wintypes.DWORD)]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


class _ACL(ctypes.Structure):
    _fields_ = [
        ("AclRevision", wintypes.BYTE),
        ("Sbz1", wintypes.BYTE),
        ("AclSize", wintypes.WORD),
        ("AceCount", wintypes.WORD),
        ("Sbz2", wintypes.WORD),
    ]


class _ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", wintypes.BYTE),
        ("AceFlags", wintypes.BYTE),
        ("AceSize", wintypes.WORD),
    ]


class _ACCESS_ALLOWED_ACE(ctypes.Structure):
    # SidStart is the first word of a variable-length SID that continues past
    # the end of this struct, so the SID is read from its offset rather than
    # from the field.
    _fields_ = [
        ("Header", _ACE_HEADER),
        ("Mask", wintypes.DWORD),
        ("SidStart", wintypes.DWORD),
    ]


class _TRUSTEE_W(ctypes.Structure):
    pass


_TRUSTEE_W._fields_ = [
    ("pMultipleTrustee", ctypes.POINTER(_TRUSTEE_W)),
    ("MultipleTrusteeOperation", ctypes.c_int),
    ("TrusteeForm", ctypes.c_int),
    ("TrusteeType", ctypes.c_int),
    # Typed as a string in the SDK, but for TRUSTEE_IS_SID it carries a SID.
    ("ptstrName", ctypes.c_void_p),
]


@dataclass(frozen=True)
class AccessEntry:
    """One permission entry, as Windows reports it."""

    sid: str
    type: int
    flags: int
    mask: int

    @property
    def inherited(self) -> bool:
        return bool(self.flags & INHERITED_ACE)


@dataclass(frozen=True)
class Dacl:
    """What a path's permissions currently say."""

    #: Whether the parent's inheritable entries are kept out. Without this the
    #: entries below are only what is there until the next inheritance pass.
    protected: bool
    entries: tuple[AccessEntry, ...]


class _EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", wintypes.DWORD),
        ("grfAccessMode", ctypes.c_int),
        ("grfInheritance", wintypes.DWORD),
        ("Trustee", _TRUSTEE_W),
    ]


def _load() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    """Return the two security libraries, binding their signatures once.

    Declaring the signatures is not optional bookkeeping: ctypes otherwise
    assumes every argument is int-sized, which silently truncates pointers in a
    64-bit process. What that produces is not a crash but an unrelated error
    code from a function that never received the arguments meant for it.
    """
    global _libraries
    if _libraries is not None:
        return _libraries
    if not _IS_WINDOWS:
        raise PrivateStateError("Windows ACL support was called off Windows")

    # Resolved through getattr for the same reason profile_lease does it:
    # WinDLL exists only on Windows, and a type checker running elsewhere
    # reports the module as having no such member.
    _win_dll = getattr(ctypes, "WinDLL")
    # use_last_error keeps GetLastError from being clobbered between a call and
    # our read of it.
    _advapi32 = _win_dll("advapi32", use_last_error=True)
    _kernel32 = _win_dll("kernel32", use_last_error=True)

    _advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL

    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL

    _advapi32.SetEntriesInAclW.argtypes = [
        wintypes.ULONG,
        ctypes.POINTER(_EXPLICIT_ACCESS_W),
        _PACL,
        ctypes.POINTER(_PACL),
    ]
    _advapi32.SetEntriesInAclW.restype = wintypes.DWORD

    _advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        _PSID,
        _PSID,
        _PACL,
        _PACL,
    ]
    _advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD

    _advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(_PSID),
        ctypes.POINTER(_PSID),
        ctypes.POINTER(_PACL),
        ctypes.POINTER(_PACL),
        ctypes.POINTER(_PSECURITY_DESCRIPTOR),
    ]
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD

    _advapi32.GetSecurityDescriptorControl.argtypes = [
        _PSECURITY_DESCRIPTOR,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL

    _advapi32.GetAce.argtypes = [
        _PACL,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    _advapi32.GetAce.restype = wintypes.BOOL

    _advapi32.EqualSid.argtypes = [_PSID, _PSID]
    _advapi32.EqualSid.restype = wintypes.BOOL

    _advapi32.ConvertSidToStringSidW.argtypes = [
        _PSID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _kernel32.LocalFree.argtypes = [_HLOCAL]
    _kernel32.LocalFree.restype = _HLOCAL

    _kernel32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    _kernel32.GetVolumeInformationW.restype = wintypes.BOOL

    _libraries = (_advapi32, _kernel32)
    return _libraries


def _last_error() -> int:
    """This thread's last Win32 error code."""
    return getattr(ctypes, "get_last_error")()


def _clear_last_error() -> None:
    """Reset the error code, so a stale one cannot be read as a fresh result."""
    getattr(ctypes, "set_last_error")(0)


def _fail(api: str, code: int | None = None) -> None:
    """Raise for a failed Win32 call, naming the call that failed."""
    if code is None:
        code = _last_error() or 1
    raise PrivateStateError(f"{api} failed: {getattr(ctypes, 'WinError')(code)}")


def current_user_sid() -> tuple[_PSID, ctypes.Array]:
    """Return this process's account SID and the buffer that backs it.

    The buffer must outlive the pointer, hence returning both: the SID lives
    inside that allocation, and letting it be collected leaves a pointer into
    freed memory.

    Read from the process token rather than looked up by name, which is what
    makes it right for a service account or SYSTEM as well as a normal login,
    and avoids a name lookup that can reach for a domain controller.
    """
    _advapi32, _kernel32 = _load()

    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(
        _kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        _fail("OpenProcessToken")

    try:
        needed = wintypes.DWORD(0)
        _clear_last_error()
        # Expected to fail: this asks how large the buffer has to be.
        if _advapi32.GetTokenInformation(
            token, TOKEN_USER_CLASS, None, 0, ctypes.byref(needed)
        ):
            raise PrivateStateError("GetTokenInformation sizing call succeeded")
        code = _last_error()
        if code != ERROR_INSUFFICIENT_BUFFER:
            _fail("GetTokenInformation", code)

        buffer = ctypes.create_string_buffer(needed.value)
        if not _advapi32.GetTokenInformation(
            token, TOKEN_USER_CLASS, buffer, needed.value, ctypes.byref(needed)
        ):
            _fail("GetTokenInformation")

        token_user = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER)).contents
        if not token_user.User.Sid:
            raise PrivateStateError("The process token carries no user SID")
        return _PSID(token_user.User.Sid), buffer
    finally:
        _kernel32.CloseHandle(token)


def _build_owner_only_acl(sid: _PSID, *, directory: bool) -> _PACL:
    """Build a one-entry ACL granting full access to *sid* and nobody else."""
    _advapi32, _ = _load()

    entry = _EXPLICIT_ACCESS_W()
    entry.grfAccessPermissions = FILE_ALL_ACCESS
    entry.grfAccessMode = SET_ACCESS
    # An inheritable entry on the directory is what makes a file created inside
    # it owner-only from the instant it exists, rather than from whenever it is
    # hardened afterwards.
    entry.grfInheritance = CONTAINER_INHERITANCE if directory else NO_INHERITANCE
    entry.Trustee.pMultipleTrustee = None
    entry.Trustee.MultipleTrusteeOperation = NO_MULTIPLE_TRUSTEE
    entry.Trustee.TrusteeForm = TRUSTEE_IS_SID
    entry.Trustee.TrusteeType = TRUSTEE_IS_USER
    entry.Trustee.ptstrName = sid.value

    acl = _PACL()
    # A null "old ACL" builds a fresh one instead of merging into what is
    # already there, which is the whole point: merging would keep entries this
    # is meant to remove.
    code = _advapi32.SetEntriesInAclW(1, ctypes.byref(entry), None, ctypes.byref(acl))
    if code != ERROR_SUCCESS:
        _fail("SetEntriesInAclW", code)
    if not acl:
        raise PrivateStateError("SetEntriesInAclW returned no ACL")
    return acl


def restrict_to_current_user(path: Path, *, directory: bool) -> None:
    """Give only this account access to *path*, and verify it afterwards."""
    _advapi32, _kernel32 = _load()
    _require_acl_capable_volume(path)

    sid, sid_buffer = current_user_sid()
    acl = _build_owner_only_acl(sid, directory=directory)
    try:
        code = _advapi32.SetNamedSecurityInfoW(
            str(path), SE_FILE_OBJECT, REPLACE_PROTECTED_DACL, sid, None, acl, None
        )
        # This one returns the error code rather than setting the thread's last
        # error, so checking GetLastError here would read a stale value from
        # some earlier call and report success for a failure.
        if code != ERROR_SUCCESS:
            _fail("SetNamedSecurityInfoW", code)
    finally:
        _kernel32.LocalFree(acl)
        del sid_buffer

    verify_owner_only(path, directory=directory)


def _require_acl_capable_volume(path: Path) -> None:
    """Refuse a volume that cannot keep permissions at all.

    FAT and exFAT accept the call and store nothing. Checking the volume first
    turns that into a clear refusal before the secret is written, rather than a
    file that looks protected and is not.
    """
    _, _kernel32 = _load()

    drive = os.path.splitdrive(os.path.abspath(str(path)))[0]
    if not drive:
        # A UNC path has no drive letter. The volume query does not answer for
        # remote storage, so the read-back verification below is what has to
        # carry it.
        return
    flags = wintypes.DWORD(0)
    if not _kernel32.GetVolumeInformationW(
        f"{drive}\\", None, 0, None, None, ctypes.byref(flags), None, 0
    ):
        _fail("GetVolumeInformationW")
    if not flags.value & FILE_PERSISTENT_ACLS:
        raise PrivateStateError(
            f"The volume holding {path} does not keep file permissions, so a "
            f"secret stored there would be readable by any local account. Move "
            f"the LinkedIn MCP data directory to an NTFS volume."
        )


def describe_dacl(path: Path) -> Dacl:
    """Read back what the DACL actually says. Used by hardening and by tests."""
    _advapi32, _kernel32 = _load()

    dacl = _PACL()
    descriptor = _PSECURITY_DESCRIPTOR()
    code = _advapi32.GetNamedSecurityInfoW(
        str(path),
        SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if code != ERROR_SUCCESS:
        _fail("GetNamedSecurityInfoW", code)

    try:
        if not descriptor:
            raise PrivateStateError(f"{path} has no security descriptor")
        # A null DACL is not an empty one: it grants everyone full access.
        if not dacl:
            raise PrivateStateError(f"{path} has a null DACL, which grants everyone")

        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not _advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            _fail("GetSecurityDescriptorControl")

        header = ctypes.cast(dacl, ctypes.POINTER(_ACL)).contents
        entries: list[AccessEntry] = []
        for index in range(header.AceCount):
            ace = wintypes.LPVOID()
            if not _advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                _fail("GetAce")
            if not ace.value:
                # GetAce reported success without filling in the entry. Nothing
                # can be said about who has access, so say nothing rather than
                # returning a list that looks complete.
                raise PrivateStateError(f"{path} returned an empty permission entry")
            ace_header = ctypes.cast(ace, ctypes.POINTER(_ACE_HEADER)).contents
            # The SID runs past the end of the struct, so it is read from the
            # offset of its first word rather than from the field itself.
            ace_sid = _PSID(ace.value + _ACCESS_ALLOWED_ACE.SidStart.offset)
            allowed = ctypes.cast(ace, ctypes.POINTER(_ACCESS_ALLOWED_ACE)).contents
            entries.append(
                AccessEntry(
                    sid=_sid_to_string(ace_sid),
                    type=ace_header.AceType,
                    flags=ace_header.AceFlags,
                    mask=allowed.Mask,
                )
            )

        return Dacl(
            protected=bool(control.value & SE_DACL_PROTECTED),
            entries=tuple(entries),
        )
    finally:
        _kernel32.LocalFree(descriptor)


def _sid_to_string(sid: _PSID) -> str:
    _advapi32, _kernel32 = _load()

    text = wintypes.LPWSTR()
    if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        _fail("ConvertSidToStringSidW")
    try:
        return text.value or ""
    finally:
        _kernel32.LocalFree(ctypes.cast(text, _HLOCAL))


def read_owner(path: Path) -> str:
    """Return the SID string of *path*'s owner."""
    _advapi32, _kernel32 = _load()

    owner = _PSID()
    descriptor = _PSECURITY_DESCRIPTOR()
    code = _advapi32.GetNamedSecurityInfoW(
        str(path),
        SE_FILE_OBJECT,
        OWNER_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if code != ERROR_SUCCESS:
        _fail("GetNamedSecurityInfoW", code)
    try:
        if not owner:
            raise PrivateStateError(f"{path} has no owner")
        return _sid_to_string(owner)
    finally:
        if descriptor:
            _kernel32.LocalFree(descriptor)


def verify_owner_only(path: Path, *, directory: bool) -> None:
    """Raise unless *path*'s DACL grants this account and nothing else."""
    sid, sid_buffer = current_user_sid()
    try:
        expected_sid = _sid_to_string(sid)
    finally:
        del sid_buffer

    # Checked first, because it decides what the rest is worth. Windows grants
    # an owner READ_CONTROL and WRITE_DAC with no ACE saying so, so a DACL that
    # names only this account still means nothing while someone else owns the
    # path and can rewrite it at will.
    actual_owner = read_owner(path)
    if actual_owner != expected_sid:
        raise PrivateStateError(
            f"{path} is owned by {actual_owner}, not by this account, so that "
            f"owner can widen its permissions again at any time"
        )

    described = describe_dacl(path)
    if not described.protected:
        raise PrivateStateError(
            f"{path} still inherits permissions from its parent directory"
        )

    if len(described.entries) != 1:
        granted = ", ".join(entry.sid for entry in described.entries) or "nobody"
        raise PrivateStateError(
            f"{path} grants access to more than this account ({granted})"
        )

    entry = described.entries[0]
    if entry.sid != expected_sid:
        raise PrivateStateError(
            f"{path} grants access to {entry.sid}, not to this account"
        )
    if entry.type != ACCESS_ALLOWED_ACE_TYPE:
        raise PrivateStateError(f"{path} carries an unexpected permission entry")
    if entry.inherited:
        raise PrivateStateError(f"{path} still carries an inherited permission entry")
    expected_flags = CONTAINER_INHERITANCE if directory else 0
    if entry.flags != expected_flags:
        raise PrivateStateError(
            f"{path} has inheritance flags {entry.flags:#04x}, expected "
            f"{expected_flags:#04x}"
        )
    # The entry names the right account and nobody else, which is half the
    # question; the other half is what it actually grants. A backend that
    # accepts the call and stores a reduced mask would leave this account
    # unable to read its own token, or unable to create files in its own state
    # directory, while everything above still looked correct. Measured with a
    # read-back of zero: verification passed.
    if entry.mask != FILE_ALL_ACCESS:
        raise PrivateStateError(
            f"{path} grants this account {entry.mask:#010x} rather than the "
            f"{FILE_ALL_ACCESS:#010x} that was asked for, so the filesystem "
            f"stored something other than what was requested"
        )
