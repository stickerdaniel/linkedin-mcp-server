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
import secrets
import stat
from collections.abc import Sequence
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
TOKEN_OWNER_CLASS = 4  # TOKEN_INFORMATION_CLASS.TokenOwner

NO_MULTIPLE_TRUSTEE = 0
TRUSTEE_IS_SID = 0
TRUSTEE_IS_USER = 1
SET_ACCESS = 2

NO_INHERITANCE = 0x00
OBJECT_INHERIT_ACE = 0x01
CONTAINER_INHERIT_ACE = 0x02
INHERIT_ONLY_ACE = 0x08
INHERITED_ACE = 0x10
CONTAINER_INHERITANCE = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE

ACCESS_ALLOWED_ACE_TYPE = 0x00
ACCESS_DENIED_ACE_TYPE = 0x01
SE_FILE_OBJECT = 1

OWNER_SECURITY_INFORMATION = 0x00000001
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
# Replace the DACL and stop the parent's entries being inherited back in.
# Ownership is deliberately excluded. Content created by another account does
# not become trusted merely because that account granted this process enough
# access to rewrite the security descriptor.
REPLACE_PROTECTED_DACL = DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION

SE_DACL_PROTECTED = 0x1000
SECURITY_DESCRIPTOR_REVISION = 1
FILE_ALL_ACCESS = 0x001F01FF
FILE_WRITE_DATA = 0x00000002
FILE_APPEND_DATA = 0x00000004
FILE_ADD_FILE = FILE_WRITE_DATA
FILE_ADD_SUBDIRECTORY = FILE_APPEND_DATA
FILE_DELETE_CHILD = 0x00000040
DELETE = 0x00010000
WRITE_DAC = 0x00040000
WRITE_OWNER = 0x00080000
GENERIC_WRITE = 0x40000000
GENERIC_ALL = 0x10000000
_FILE_REPLACEMENT_RIGHTS = (
    FILE_WRITE_DATA
    | FILE_APPEND_DATA
    | DELETE
    | WRITE_DAC
    | WRITE_OWNER
    | GENERIC_WRITE
    | GENERIC_ALL
)
_DIRECTORY_REPLACEMENT_RIGHTS = (
    FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY | FILE_DELETE_CHILD | _FILE_REPLACEMENT_RIGHTS
)
_CREATOR_OWNER_SID = "S-1-3-0"
#: OWNER RIGHTS, which is not a principal at all. An allow entry naming it
#: grants its mask to whichever account owns the object being checked, decided
#: at access-check time against that object's own security descriptor, and
#: Windows documents no path by which a token failing that owner comparison
#: takes anything from it. So the entry says nothing about who may act and only
#: restates what the owner may do, which is why both walks below skip it *after*
#: they have judged the owner of the same object and never before.
#:
#: An inherited copy lands in a child's DACL and is then read against that
#: child's owner rather than against this one's. That is not a gap here: every
#: directory this server goes on to create carries a protected DACL, so the copy
#: is dropped at creation, and every one it accepts instead is put through
#: ``verify_owner_only``. What the copy does grant is a foreign account rights
#: over an object that same account created and already owns.
#:
#: Measured on Windows Server 2025, and the source is CPython rather than
#: Windows: a directory created with ``mode=0o700`` carries SYSTEM,
#: Administrators and this entry, and nothing naming the user at all, because
#: this is how the restricted access list added in 3.12.4 grants the creator its
#: own directory. ``%TEMP%`` itself carries no such entry. So every private
#: directory this server creates has one, inheritable, and refusing it refused
#: the server its own state.
_OWNER_RIGHTS_SID = "S-1-3-4"
#: What an ACE has to grant before an account can take an *existing, named*
#: directory away from the path that sits on it: delete it, or rewrite who may.
#:
#: Deliberately narrower than ``_DIRECTORY_REPLACEMENT_RIGHTS``, which is the
#: question asked of the immediate parent. The rights to add a file or a
#: subdirectory are not here, because creating a new name beside ``Temp`` does
#: not replace ``Temp``, and a default Windows install grants exactly that on
#: ``C:\`` to Authenticated Users. Asking the ancestry the parent's question
#: would refuse every standard machine.
#:
#: ``FILE_DELETE_CHILD`` is the same question one level down: an account holding
#: it on a container deletes the component below without ever needing ``DELETE``
#: on that component itself. ``GENERIC_WRITE`` is absent because it maps to
#: ``FILE_GENERIC_WRITE``, which carries neither ``DELETE`` nor ``WRITE_DAC``;
#: ``GENERIC_ALL`` maps to everything, and is here.
_ANCESTOR_REPLACEMENT_RIGHTS = (
    DELETE | WRITE_DAC | WRITE_OWNER | FILE_DELETE_CHILD | GENERIC_ALL
)
#: The servicing stack, which owns much of what it installs. A default machine
#: has it as the owner of directories in the chain a ``%TEMP%`` under ``C:\``
#: hangs from, so leaving it out would refuse ordinary Windows. It is a virtual
#: service account: no interactive login can assume it, and only components
#: already running as the system can act as it.
_TRUSTED_INSTALLER_SID = (
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
)
#: Accounts whose control over a path this process is willing to inherit. An
#: owner holds ``READ_CONTROL`` and ``WRITE_DAC`` with no ACE saying so, which
#: is why ownership is a separate question from the DACL everywhere below.
_TRUSTED_SYSTEM_SIDS = frozenset(
    {
        "S-1-5-18",  # LocalSystem
        "S-1-5-32-544",  # BUILTIN\\Administrators
        _TRUSTED_INSTALLER_SID,
    }
)

FILE_PERSISTENT_ACLS = 0x00000008
ERROR_FILE_EXISTS = 80
ERROR_ALREADY_EXISTS = 183
_PRIVATE_DIRECTORY_ATTEMPTS = 16

FILE_READ_ATTRIBUTES = 0x00000080
#: ``FILE_EXECUTE`` under its directory name: the right to pass through this
#: directory to something below it. Requested for what it does to the *share*
#: mode rather than for the access, and it is the whole reason the pin holds:
#: Windows enters its sharing check only for an open whose desired access names
#: one of ``FILE_READ_DATA``, ``FILE_EXECUTE``, ``FILE_WRITE_DATA``,
#: ``FILE_APPEND_DATA`` or ``DELETE`` (MS-FSA 2.1.5.1.2.2). Attribute rights are
#: in none of those categories, so withholding ``FILE_SHARE_DELETE`` beside them
#: restricts nothing at all, and a handle opened for ``FILE_READ_ATTRIBUTES``
#: alone let its own directory be renamed out from under it. Measured on
#: Windows Server 2025 by the test named for it.
#:
#: The narrowest right that does the job. ``FILE_LIST_DIRECTORY`` would work
#: equally and grants the contents as well, which this has no use for.
FILE_TRAVERSE = 0x00000020
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", _PSID), ("Attributes", wintypes.DWORD)]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


class _TOKEN_OWNER(ctypes.Structure):
    _fields_ = [("Owner", _PSID)]


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


class _SECURITY_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("Revision", wintypes.BYTE),
        ("Sbz1", wintypes.BYTE),
        ("Control", wintypes.WORD),
        ("Owner", _PSID),
        ("Group", _PSID),
        ("Sacl", _PACL),
        ("Dacl", _PACL),
    ]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
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

    _advapi32.InitializeSecurityDescriptor.argtypes = [
        _PSECURITY_DESCRIPTOR,
        wintypes.DWORD,
    ]
    _advapi32.InitializeSecurityDescriptor.restype = wintypes.BOOL

    _advapi32.SetSecurityDescriptorOwner.argtypes = [
        _PSECURITY_DESCRIPTOR,
        _PSID,
        wintypes.BOOL,
    ]
    _advapi32.SetSecurityDescriptorOwner.restype = wintypes.BOOL

    _advapi32.SetSecurityDescriptorDacl.argtypes = [
        _PSECURITY_DESCRIPTOR,
        wintypes.BOOL,
        _PACL,
        wintypes.BOOL,
    ]
    _advapi32.SetSecurityDescriptorDacl.restype = wintypes.BOOL

    _advapi32.SetSecurityDescriptorControl.argtypes = [
        _PSECURITY_DESCRIPTOR,
        wintypes.WORD,
        wintypes.WORD,
    ]
    _advapi32.SetSecurityDescriptorControl.restype = wintypes.BOOL

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

    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE

    _kernel32.CreateDirectoryW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
    ]
    _kernel32.CreateDirectoryW.restype = wintypes.BOOL

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


def default_owner_sid() -> tuple[_PSID, ctypes.Array]:
    """Return the SID Windows assigns to objects created by this token."""
    _advapi32, _kernel32 = _load()

    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(
        _kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        _fail("OpenProcessToken")

    try:
        needed = wintypes.DWORD(0)
        _clear_last_error()
        if _advapi32.GetTokenInformation(
            token, TOKEN_OWNER_CLASS, None, 0, ctypes.byref(needed)
        ):
            raise PrivateStateError("GetTokenInformation sizing call succeeded")
        code = _last_error()
        if code != ERROR_INSUFFICIENT_BUFFER:
            _fail("GetTokenInformation", code)

        buffer = ctypes.create_string_buffer(needed.value)
        if not _advapi32.GetTokenInformation(
            token, TOKEN_OWNER_CLASS, buffer, needed.value, ctypes.byref(needed)
        ):
            _fail("GetTokenInformation")

        token_owner = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_OWNER)).contents
        if not token_owner.Owner:
            raise PrivateStateError("The process token carries no default owner SID")
        return _PSID(token_owner.Owner), buffer
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


def create_owner_only_directory(
    parent: Path, *, prefix: str
) -> tuple[Path, wintypes.HANDLE]:
    """Create and pin a random child with its final owner-only ACL.

    The whole chain from the drive root down to *parent* is pinned before any of
    it is judged, and stays pinned until the child exists, has been verified and
    has a pin of its own. What that buys is stated in :func:`pin_directory_chain`
    and :func:`verify_ancestry_cannot_be_replaced`: a temporary parent that is
    itself unimpeachable is worth nothing while a directory above it can hand an
    untrusted account the right to delete what is created here.
    """
    _advapi32, _kernel32 = _load()
    pins = pin_directory_chain(parent)
    sid_buffer: ctypes.Array | None = None
    acl = _PACL()
    try:
        verify_ancestry_cannot_be_replaced(parent)
        # ``require_protected=False`` is what the ancestry walk pays for. A
        # protected DACL on the temporary parent would answer this by itself,
        # and demanding one would refuse every ordinary ``%TEMP%``, which
        # inherits from the profile root by design.
        verify_children_cannot_be_replaced(parent, require_protected=False)
        _require_acl_capable_volume(parent)
        sid, sid_buffer = current_user_sid()
        acl = _build_owner_only_acl(sid, directory=True)

        descriptor_buffer = _SECURITY_DESCRIPTOR()
        descriptor = ctypes.cast(ctypes.byref(descriptor_buffer), _PSECURITY_DESCRIPTOR)
        if not _advapi32.InitializeSecurityDescriptor(
            descriptor, SECURITY_DESCRIPTOR_REVISION
        ):
            _fail("InitializeSecurityDescriptor")
        if not _advapi32.SetSecurityDescriptorOwner(descriptor, sid, False):
            _fail("SetSecurityDescriptorOwner")
        if not _advapi32.SetSecurityDescriptorDacl(descriptor, True, acl, False):
            _fail("SetSecurityDescriptorDacl")
        if not _advapi32.SetSecurityDescriptorControl(
            descriptor,
            SE_DACL_PROTECTED,
            SE_DACL_PROTECTED,
        ):
            _fail("SetSecurityDescriptorControl")

        attributes = _SECURITY_ATTRIBUTES(
            ctypes.sizeof(_SECURITY_ATTRIBUTES),
            descriptor,
            False,
        )
        for _ in range(_PRIVATE_DIRECTORY_ATTEMPTS):
            path = parent / f"{prefix}{secrets.token_hex(16)}"
            _clear_last_error()
            if _kernel32.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                break
            code = _last_error()
            if code not in (ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS):
                _fail("CreateDirectoryW", code)
        else:
            raise PrivateStateError(
                f"Could not create a unique private directory below {parent}"
            )

        try:
            verify_owner_only(path, directory=True)
            child_pin = pin_directory(path)
        except BaseException:
            try:
                path.rmdir()
            except OSError:
                pass
            raise
        return path, child_pin
    finally:
        if acl:
            _kernel32.LocalFree(acl)
        if sid_buffer is not None:
            del sid_buffer
        # Last, and only here: the child above is created, verified and pinned
        # before its ancestry stops being held still.
        _release_directory_pins(pins)


def restrict_to_current_user(path: Path, *, directory: bool) -> None:
    """Give only this account access to its own *path*, and verify it.

    A new Windows object belongs to the token's *default owner*, which is not
    always the token user: the policy "System objects: Default owner for
    objects created by members of the Administrators group" makes it the
    Administrators group instead, and GitHub's own Windows runners ship that
    way. So that owner is accepted here alongside the user SID, and the owner
    is then normalized to the user SID together with the protected DACL.

    Accepting it is not a hole, because under that policy it is not a
    distinction Windows offers. Every administrator's objects are owned by the
    same group there, so refusing the group would only refuse this account its
    own state; and an administrator who could have planted that directory can
    take ownership of anything, read any protected file and open this process
    anyway. What the check still refuses is the case it exists for: content
    owned by some *other*, non-administrative account, whose objects carry
    their own user SID and match neither of these.

    Which is why the default owner is accepted only when it is one of the
    identities in :data:`_TRUSTED_SYSTEM_SIDS`. A token's default owner is any
    group it carries with the owner attribute, and the argument above holds for
    an administrative one alone: taking a shared non-administrative group on
    trust would hand every member of it the same directory, which is the
    boundary this function exists to keep.
    """
    _advapi32, _kernel32 = _load()
    _require_acl_capable_volume(path)

    sid, sid_buffer = current_user_sid()
    expected_owner = _sid_to_string(sid)
    accepted_owners = {expected_owner}
    default_sid, default_buffer = default_owner_sid()
    try:
        default_owner = _sid_to_string(default_sid)
    finally:
        del default_buffer
    if default_owner in _TRUSTED_SYSTEM_SIDS:
        accepted_owners.add(default_owner)

    actual_owner = read_owner(path)
    if actual_owner not in accepted_owners:
        del sid_buffer
        raise PrivateStateError(
            f"{path} is owned by {actual_owner}, which is neither this account "
            f"nor a trusted system owner. Refusing to convert another "
            f"account's content into trusted private state."
        )

    replace_owner = actual_owner != expected_owner
    acl = _build_owner_only_acl(sid, directory=directory)
    try:
        security_information = REPLACE_PROTECTED_DACL
        if replace_owner:
            security_information |= OWNER_SECURITY_INFORMATION
        code = _advapi32.SetNamedSecurityInfoW(
            str(path),
            SE_FILE_OBJECT,
            security_information,
            sid if replace_owner else None,
            None,
            acl,
            None,
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


def _can_replace_child(entry: AccessEntry) -> bool:
    """Whether an ACE grants replacement rights on this directory or its children."""
    applies_here = not entry.flags & INHERIT_ONLY_ACE
    if applies_here and entry.mask & _DIRECTORY_REPLACEMENT_RIGHTS:
        return True
    if entry.flags & OBJECT_INHERIT_ACE and entry.mask & _FILE_REPLACEMENT_RIGHTS:
        return True
    return bool(
        entry.flags & CONTAINER_INHERIT_ACE
        and entry.mask & _DIRECTORY_REPLACEMENT_RIGHTS
    )


def _trusted_sids() -> set[str]:
    """This account plus the identities a local account cannot become."""
    sid, sid_buffer = current_user_sid()
    try:
        current_user = _sid_to_string(sid)
    finally:
        del sid_buffer
    return {current_user, *_TRUSTED_SYSTEM_SIDS}


def _refuse_a_reparse_point(path: Path) -> None:
    """Refuse a component whose name does not lead where its permissions do.

    ``pin_directory`` opens with ``FILE_FLAG_OPEN_REPARSE_POINT`` and therefore
    pins the link, while ``read_owner`` and ``describe_dacl`` follow it and
    answer about the target. A junction in the chain would leave the pin holding
    one object and the verdict describing another, and repointing it afterwards
    needs neither of the rights the DACL check looks for.
    """
    details = path.lstat()
    attributes = getattr(details, "st_file_attributes", None)
    if attributes is None:
        raise PrivateStateError(f"Windows did not report file attributes for {path}")
    if stat.S_ISLNK(details.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise PrivateStateError(
            f"{path} is a Windows reparse point, so what was pinned is the link "
            f"rather than the directory whose permissions were read"
        )
    if not stat.S_ISDIR(details.st_mode):
        raise PrivateStateError(f"Installer temporary path is not a directory: {path}")


def _verify_ancestor(path: Path, trusted: set[str]) -> None:
    """Refuse one container above the temporary parent."""
    owner = read_owner(path)
    if owner not in trusted:
        raise PrivateStateError(
            f"{path} is owned by {owner}, which can rewrite the permissions that "
            f"every directory below it inherits"
        )

    for entry in describe_dacl(path).entries:
        if entry.type == ACCESS_DENIED_ACE_TYPE:
            continue
        if entry.type != ACCESS_ALLOWED_ACE_TYPE:
            raise PrivateStateError(
                f"{path} carries an unsupported permission entry for {entry.sid}"
            )
        if entry.sid in trusted:
            continue
        # Read against the owner this function judged a moment ago, so it adds
        # no reach beyond what that verdict already accepted. See
        # ``_OWNER_RIGHTS_SID``; the walk reaches every container in the chain,
        # so each one's entry is skipped only on its own owner.
        if entry.sid == _OWNER_RIGHTS_SID:
            continue
        # An inherit-only entry grants nothing on this directory. Where it does
        # land is a directory this walk visits too, and it is judged there
        # against what that directory actually carries rather than guessed at
        # from here. ``C:\`` ships one of these, granting Modify to
        # Authenticated Users, and every standard component below it is
        # protected against exactly that.
        if entry.flags & INHERIT_ONLY_ACE:
            continue
        if entry.mask & _ANCESTOR_REPLACEMENT_RIGHTS:
            raise PrivateStateError(
                f"{path} grants {entry.sid} permission to remove or re-permission "
                f"the installer path below it"
            )


def verify_ancestry_cannot_be_replaced(path: Path) -> None:
    """Refuse a directory whose ancestry an untrusted account can rewrite.

    ``verify_children_cannot_be_replaced`` asks only about *path*, and a
    temporary parent is almost never protected: ``%TEMP%`` inherits from the
    profile root, which inherits from ``C:\\Users``. The DACL deciding who may
    delete the installer root is therefore assembled from every directory above
    it, and an account that can rewrite any of those propagates
    ``FILE_DELETE_CHILD`` into a parent that passed its own check a moment
    earlier, then replaces the root before its child pin is taken.

    Protection higher up is not a stopping condition. Whoever can rewrite a
    protected ancestor's DACL can also clear its protection, which makes Windows
    re-propagate everything above it down to the first descendant that is still
    protected. So the whole chain is walked, root first, and the question asked
    of each container is narrower than the one asked of the parent: see
    ``_ANCESTOR_REPLACEMENT_RIGHTS`` for why an ordinary ``C:\\`` passes it.
    """
    trusted = _trusted_sids()
    for container in reversed(path.parents):
        _verify_ancestor(container, trusted)


def verify_children_cannot_be_replaced(
    path: Path, *, require_protected: bool = True
) -> None:
    """Refuse a parent that lets an unprivileged account replace its children."""
    trusted = _trusted_sids()
    owner = read_owner(path)
    if owner not in trusted:
        raise PrivateStateError(
            f"{path} is owned by {owner}, which can rewrite its child permissions"
        )

    described = describe_dacl(path)
    if require_protected and not described.protected:
        raise PrivateStateError(
            f"{path} still inherits permissions that can change after verification"
        )

    for entry in described.entries:
        if entry.type == ACCESS_DENIED_ACE_TYPE:
            continue
        if entry.type != ACCESS_ALLOWED_ACE_TYPE:
            raise PrivateStateError(
                f"{path} carries an unsupported permission entry for {entry.sid}"
            )
        if entry.sid in trusted:
            continue
        # Same reasoning as in ``_verify_ancestor`` and the same ordering: the
        # owner of this directory was accepted above, and this entry names
        # nobody else. See ``_OWNER_RIGHTS_SID`` for why an inheritable one is
        # not a way around the question this function asks.
        if entry.sid == _OWNER_RIGHTS_SID:
            continue
        if (
            entry.sid == _CREATOR_OWNER_SID
            and entry.flags & INHERIT_ONLY_ACE
            and entry.flags & (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE)
        ):
            continue
        if _can_replace_child(entry):
            raise PrivateStateError(
                f"{path} grants {entry.sid} permission to replace private state below it"
            )


def pin_directory(path: Path) -> wintypes.HANDLE:
    """Open *path* while denying replacement until the handle is closed.

    What the handle denies is an open asking for ``DELETE``, which is what a
    rename of this directory and a removal of it both need. It denies nothing
    about the contents, the permissions or the attributes, all of which stay
    editable by whoever the DACL already allowed; this is a pin on the *name*
    and on nothing else. See ``FILE_TRAVERSE`` for why the access mask decides
    whether the share mode is consulted at all.
    """
    _, kernel32 = _load()
    handle = kernel32.CreateFileW(
        str(path),
        FILE_TRAVERSE | FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        _fail("CreateFileW")
    return handle


def close_directory_pin(handle: wintypes.HANDLE) -> None:
    """Close a handle returned by :func:`pin_directory`."""
    _, kernel32 = _load()
    if not kernel32.CloseHandle(handle):
        _fail("CloseHandle")


def pin_directory_chain(path: Path) -> tuple[wintypes.HANDLE, ...]:
    """Pin *path* and every directory above it, root first.

    The pins are what make the verification worth anything. ``read_owner`` and
    ``describe_dacl`` answer about a *name*, and between the answer and
    ``CreateDirectoryW`` any component of that name can be renamed away and a
    directory the attacker controls put in its place, so the check would have
    described one object and the creation landed in another. A handle held
    without ``FILE_SHARE_DELETE`` refuses both the rename and the delete for as
    long as it is open, which is why every component is pinned before any of
    them is judged rather than one at a time as the walk reaches it.

    It is a user-mode boundary. A kernel-mode filter opening with
    ``IO_IGNORE_SHARE_ACCESS_CHECK`` is outside it, and so is anything that
    happens after these handles close, including a rename registered for the
    next boot. Neither is reachable by the unprivileged account this defends
    against, and both are already able to do worse.
    """
    if not path.is_absolute():
        raise PrivateStateError(f"Refusing to pin a relative installer path: {path}")
    pins: list[wintypes.HANDLE] = []
    try:
        for component in (*reversed(path.parents), path):
            pins.append(pin_directory(component))
            _refuse_a_reparse_point(component)
    except BaseException:
        _release_directory_pins(pins)
        raise
    return tuple(pins)


def _release_directory_pins(handles: Sequence[wintypes.HANDLE]) -> None:
    """Close every pin, deepest first, and report the first failure afterwards.

    Stopping on the first failure would leak every handle above it, and these
    are held on directories as ordinary as ``C:\\Users``.
    """
    failure: BaseException | None = None
    for handle in reversed(handles):
        try:
            close_directory_pin(handle)
        except BaseException as exc:
            failure = failure if failure is not None else exc
    if failure is not None:
        raise failure


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
