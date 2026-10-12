"""What a running daemon publishes about itself, and what a client checks.

A client sends a bearer token to the address a descriptor names and drives a
logged-in session through it, so nothing here is taken on trust: the endpoint
must be loopback before the token goes anywhere, the profile is compared by
:func:`profile_identity` (``auth_root_dir`` is the profile's parent, so sibling
profiles share one lock), and the configuration through a keyed fingerprint,
keyed because ``proxy_password`` is part of it.

The token lives in its own file named after the instance, and the descriptor
carries only its digest, so no client pairs one generation's descriptor with the
next generation's token.

State lives under the account's home, not the configurable auth root. Two
containers sharing a bind-mounted auth root therefore do not share it, and only
the profile lease keeps them apart.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import stat
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from linkedin_mcp_server.common_utils import (
    is_still_at,
    secure_mkdir,
    secure_write_text,
    utcnow_iso,
)
from linkedin_mcp_server.config.schema import AppConfig, is_loopback_host
from linkedin_mcp_server.private_state import (
    PrivateStateError,
    harden_created_file,
    harden_directory,
    harden_directory_entry,
    harden_file,
)

#: Bumped when the shape below changes incompatibly. A client that does not
#: recognise the value refuses rather than guessing at fields it cannot read.
SCHEMA_VERSION = 1

#: Bumped when the daemon's tool protocol changes incompatibly: the call
#: metadata, the heartbeat contract, or the ping contract. Compatibility keys on
#: this rather than on the package version, so frequent ``@latest`` releases do
#: not each force a restart. The call marker is mandatory in both directions
#: (:mod:`linkedin_mcp_server.daemon.liveness`).
#:
#: A bump keeps older owners readable: :func:`read` parses any protocol at or
#: above :data:`CONTROL_FLOOR_PROTOCOL` and the caller may use a mismatched one
#: for control only (``daemon.discovery.Attachment.control_only``). An owner
#: nobody can read is an owner nobody can ask to stand down.
PROTOCOL_VERSION = 1

#: The oldest tool protocol whose owner this build can still ask to stand down.
#: Every protocol in this namespace accepts the same stand-down request
#: (``daemon.owner.STAND_DOWN_PATH``), so the floor is the first one.
CONTROL_FLOOR_PROTOCOL = 1

_DESCRIPTOR_FILE = "daemon.json"
_PENDING_DESCRIPTOR_PREFIX = "pending-"
_DAEMON_DIR = "daemon"


#: The account-level namespace every daemon of this account shares. Earlier
#: releases' namespaces are never touched
#: (docs/decisions/2026-10-11-daemon-clean-slate.md); the profile lease still
#: keeps their owners off this browser. On Windows this namespace is only
#: created through ``harden_directory_entry``, with Python 3.12.4+'s restricted
#: creation DACL, so no handle opened under a formerly permissive root can
#: reach it.
_APPLICATION_STATE_DIR = ".linkedin-mcp-daemon"
_WINDOWS = os.name == "nt"
_windows_account_home_pins: dict[str, tuple[tuple[int, int], tuple[Any, ...]]] = {}

# Enough that guessing is not a strategy. Read straight from the OS source.
_TOKEN_BYTES = 32

#: What a hex digest may contain, for checking one read from a file.
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

#: Configuration a client must share with the owner to be served by it. Each of
#: these either changes what the browser *is* (its fingerprint, its exit
#: address, where its profile lives) or how long the owner keeps it. Anything
#: not listed here may differ freely between clients.
#:
#: The login settings (``login_timeout_seconds``, ``login_inline_wait_seconds``,
#: ``auto_import_from_browser``) are left out on purpose. The owner never signs
#: in: ``bootstrap._start_login_if_needed`` refuses before the inline wait or a
#: login starts, and ``_auto_import_allowed`` answers False there. So they
#: change nothing the owner does, and each client applies its own when it signs
#: in itself.
SHARED_CONFIG_FIELDS = (
    "user_data_dir",
    "headless",
    "slow_mo",
    "viewport_width",
    "viewport_height",
    "default_timeout",
    "chrome_path",
    "proxy_server",
    "proxy_username",
    "proxy_password",
    "proxy_bypass",
    "browser_idle_timeout_seconds",
)


class DescriptorError(RuntimeError):
    """A descriptor could not be trusted, so nothing was sent to it."""


class CommitPreflightError(RuntimeError):
    """Descriptor publication stopped before the replacement was attempted."""


def _text(raw: Mapping[Any, Any], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str):
        raise DescriptorError(
            f"The daemon descriptor field {name} is missing or not text"
        )
    return value


def _digest_text(raw: Mapping[Any, Any], name: str) -> str:
    """Read a field that must be a SHA-256 digest, and check that it is one.

    Checked here because ``hmac.compare_digest`` raises ``TypeError`` on a
    non-ASCII string, outside the :class:`DescriptorError` callers expect.
    """
    value = _text(raw, name)
    if len(value) != 64 or any(character not in _HEX_DIGITS for character in value):
        raise DescriptorError(
            f"The daemon descriptor field {name} is not a SHA-256 digest"
        )
    return value


def _number(raw: Mapping[Any, Any], name: str, *, default: int | None = None) -> int:
    value = raw.get(name, default)
    # bool is an int as far as isinstance is concerned, and a port of True is
    # not a port.
    if isinstance(value, bool) or not isinstance(value, int):
        raise DescriptorError(
            f"The daemon descriptor field {name} is missing or not a number"
        )
    return value


#: The account home the test suite gives every process a test starts, the
#: detached owner included, which no in-process patch can reach. A variable of
#: this project's own and never ``HOME``: launchers rewrite ``HOME`` and
#: ``USERPROFILE`` for one process, which is why :func:`_account_home` ignores
#: them, and nothing but ``tests/conftest.py`` sets this one. The owner inherits
#: it from the frontend that starts it, so the two still agree on one root.
TEST_ACCOUNT_HOME_ENV = "LINKEDIN_MCP_TEST_ACCOUNT_HOME"


def _account_home() -> Path:
    """Return the operating system account's home, ignoring ``HOME`` overrides."""
    if test_home := os.environ.get(TEST_ACCOUNT_HOME_ENV):
        return _usable_home(test_home)
    if os.name == "nt":  # pragma: no cover - exercised on the Windows CI runner
        import ctypes
        from ctypes import wintypes

        # CSIDL_PROFILE asks Windows for the current account's profile directory.
        # Unlike Path.home(), this does not change when a launcher overrides
        # HOME or USERPROFILE for one process and would otherwise split one
        # account's daemon election into several roots.
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
        result = get_folder(None, 0x0028, None, 0, buffer)
        if result != 0 or not buffer.value:
            raise DescriptorError(
                "Windows could not identify the current account's profile directory"
            )
        return Path(buffer.value)

    import pwd

    try:
        entry = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError as exc:
        raise DescriptorError(
            "The operating system could not identify the current account's home directory"
        ) from exc
    return _usable_home(entry)


def _usable_home(entry: str) -> Path:
    """Check a home directory before daemon state is keyed under it."""
    # An empty or relative entry is not an answer. Path("") is ".", so two
    # processes of one account started from different working directories would
    # key their state under different roots and each elect its own owner.
    home = Path(entry)
    if not entry or not home.is_absolute():
        raise DescriptorError(
            "The current account has no absolute home directory, so daemon state "
            "has nowhere stable to live"
        )

    # The home has to be there already. Creating it would be wrong twice: a home
    # that is merely not mounted yet would be shadowed by an empty directory,
    # and the daemon that did so would key its state under that directory while
    # every process started after the mount keys it under the real one.
    if not home.is_dir():
        raise DescriptorError(
            f"The current account's home directory {home} does not exist. Daemon "
            f"state has nowhere to live until it does."
        )

    # Resolved, not merely checked for links. A home reached through a symlink
    # is an ordinary layout on POSIX, and refusing it would refuse a working
    # machine. What matters is that every process agrees, which resolving gives
    # and an is_symlink() refusal does not. Windows inspects every component
    # before following it instead (``daemon_state_root``).
    return home if os.name == "nt" else home.resolve()


def daemon_state_root() -> Path:
    """Return the private application root shared by every local daemon.

    Resolved on POSIX, so processes reaching it through different symlinks
    agree on one lock. A path changed underneath a running owner (a retargeted
    link, a renamed directory) splits the lock, as it does the profile lease;
    no lock addressed by path avoids that.
    """
    root = _account_home() / _APPLICATION_STATE_DIR / _DAEMON_DIR
    # Windows must inspect each application-owned component before following it.
    # Resolving here would turn a junction into an ordinary-looking target first.
    return root if _WINDOWS else root.resolve()


def _canonical_path(path: Path, label: str) -> Path:
    """Resolve *path*, raising :class:`DescriptorError` if it is unusable.

    ``expanduser`` and ``resolve`` raise ``RuntimeError``, ``ValueError`` or
    ``OSError`` on type-correct configuration, so those are refusals.
    """
    try:
        return path.expanduser().resolve()
    except DescriptorError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise DescriptorError(
            f"The {label} {path!r} is not a usable path: {exc}"
        ) from exc


def _auth_root_identity(auth_root: Path) -> bytes:
    """Return the stable filesystem identity of *auth_root*, creating it once.

    Created rather than inspected, so the first daemon on a fresh install
    never keys a missing path and then an inode. Device plus inode keeps case
    and Unicode aliases together without merging distinct directories; the
    auth root is never rotated, unlike the profile (:func:`profile_identity`).
    """
    canonical = _canonical_path(auth_root, "authentication root")
    # Before creating: secure_mkdir would refuse an existing file with a
    # NotADirectoryError rather than this module's DescriptorError.
    if canonical.exists() and not canonical.is_dir():
        raise DescriptorError(
            f"The authentication root is not a directory: {canonical}"
        )

    # Creating and reading back are one step to a caller; either may fail in
    # the operating system's terms, for example a root that vanished between.
    try:
        secure_mkdir(canonical, mode=0o700)
        info = canonical.stat()
    except OSError as exc:
        raise DescriptorError(
            f"The authentication root {canonical} could not be prepared: {exc}"
        ) from exc

    if not stat.S_ISDIR(info.st_mode):
        raise DescriptorError(
            f"The authentication root is not a directory: {canonical}"
        )
    if not info.st_ino:
        raise DescriptorError(
            f"The filesystem does not provide a stable identity for {canonical}, "
            "so browser ownership cannot be coordinated safely"
        )
    return f"inode\0{info.st_dev}\0{info.st_ino}".encode("ascii")


def profile_identity(profile: Path) -> str:
    """Return an identity for a browser profile, by location rather than inode.

    Not by inode: every new session replaces the profile directory, so an
    inode key would stop matching its own descriptor at the first login. Not
    by bare string either: the parent is keyed by inode and the final
    component is taken from the parent's listing, so spellings the filesystem
    treats as one directory agree. Creates nothing, since this also runs while
    assembling a refusal.
    """
    canonical = _canonical_path(profile, "browser profile")
    try:
        info = canonical.parent.stat()
    except OSError:
        # No parent to key on. Two clients naming the same absent directory
        # still agree with each other, which is all this can offer.
        return f"path\0{os.path.normcase(str(canonical))}"

    if not stat.S_ISDIR(info.st_mode) or not info.st_ino:
        return f"path\0{os.path.normcase(str(canonical))}"

    # Only the parent is consulted: the profile may not exist before the first
    # login, and the identity must not change when it appears.
    # A fold match is only a candidate: on a case-sensitive volume Profile and
    # profile are two directories, and merging them would serve one profile's
    # session to the other's client. casefold settles case; NFC settles
    # composition, which macOS also collapses.
    wanted = canonical.name
    folded = unicodedata.normalize("NFC", wanted).casefold()
    try:
        entries = [entry.name for entry in os.scandir(canonical.parent)]
    except OSError:
        # The parent stopped being readable between the two calls. The spelling
        # the caller gave still identifies it well enough to compare.
        entries = []
    if wanted not in entries:
        for name in entries:
            if unicodedata.normalize("NFC", name).casefold() != folded:
                continue
            # samefile confirms what folding only guesses. Keep looking when it
            # says no: several entries can fold alike on a volume that
            # distinguishes them, and a later one may be the match.
            try:
                if canonical.parent.joinpath(name).samefile(canonical):
                    wanted = name
                    break
            except OSError:
                continue
    return f"under\0{info.st_dev}\0{info.st_ino}\0{wanted}"


def _daemon_dir_name(auth_root: Path) -> str:
    return hashlib.sha256(_auth_root_identity(auth_root)).hexdigest()


def daemon_dir(auth_root: Path) -> Path:
    """Where daemon state for *auth_root* lives.

    Under the account's private application directory rather than the
    configurable auth root, which may be ``/tmp`` or another directory this
    application does not own. Keyed by the physical auth root, so sibling
    profiles share one election.
    """
    return daemon_state_root() / _daemon_dir_name(auth_root)


def _windows_directory_identity(path: Path) -> tuple[int, int]:
    entry = path.lstat()
    attributes = getattr(entry, "st_file_attributes", None)
    if attributes is None:
        raise PrivateStateError(
            f"Windows did not report file attributes for account-home path {path}"
        )
    if stat.S_ISLNK(entry.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise PrivateStateError(
            f"Account-home path {path} is a reparse point and cannot anchor daemon state"
        )
    if not stat.S_ISDIR(entry.st_mode):
        raise PrivateStateError(f"Account-home path is not a directory: {path}")
    return entry.st_dev, entry.st_ino


def _pin_windows_account_home(home: Path) -> None:
    """Keep every replaceable account-home path component fixed for this process."""
    key = os.path.normcase(str(home.absolute()))
    existing = _windows_account_home_pins.get(key)
    if existing is not None:
        if _windows_directory_identity(home) != existing[0]:
            raise PrivateStateError(
                f"Windows account-home path {home} changed while daemon state was in use"
            )
        return

    from linkedin_mcp_server.windows_acl import close_directory_pin, pin_directory

    chain: list[Path] = []
    current = home
    while current.parent != current:
        chain.append(current)
        current = current.parent

    pins: list[Any] = []
    try:
        # A volume root has no parent to be renamed inside, so the walk ends
        # empty and there is no edge to pin; its identity still has to be read,
        # or a home on `D:\` is refused as unusable on every start.
        home_identity: tuple[int, int] | None = (
            None if chain else _windows_directory_identity(home)
        )
        for path in reversed(chain):
            before = _windows_directory_identity(path)
            pin = pin_directory(path)
            pins.append(pin)
            if _windows_directory_identity(path) != before:
                raise PrivateStateError(
                    f"Windows account-home path {path} was replaced while it was pinned"
                )
            if path == home:
                home_identity = before
        if home_identity is None:
            raise PrivateStateError(f"Windows account-home path is unusable: {home}")

        retained = (home_identity, tuple(pins))
        prior = _windows_account_home_pins.setdefault(key, retained)
        if prior is not retained:
            for pin in reversed(pins):
                close_directory_pin(pin)
    except BaseException:
        for pin in reversed(pins):
            with contextlib.suppress(OSError, PrivateStateError):
                close_directory_pin(pin)
        raise


def reset_daemon_descriptor_for_testing() -> None:
    """Release process-lifetime Windows pins between isolated tests."""
    if not _windows_account_home_pins:
        return
    from linkedin_mcp_server.windows_acl import close_directory_pin

    retained = tuple(_windows_account_home_pins.values())
    _windows_account_home_pins.clear()
    for _identity, pins in retained:
        for pin in reversed(pins):
            with contextlib.suppress(OSError, PrivateStateError):
                close_directory_pin(pin)


def prepare_daemon_state(auth_root: Path) -> Path:
    """Establish owner-only storage before trusting any per-auth state entry.

    The shared root is hardened first. A permissive root may already hold a
    planted per-auth entry, so that entry is established without following a
    link and checked in its own right; files inside are hardened before use.
    """
    root = daemon_state_root()
    if _WINDOWS:
        home = _account_home()
        from linkedin_mcp_server.windows_acl import (
            verify_children_cannot_be_replaced,
        )

        if os.name == "nt":
            _pin_windows_account_home(home)
        verify_children_cannot_be_replaced(home)
        harden_directory_entry(home / _APPLICATION_STATE_DIR)
        harden_directory_entry(root)
    else:
        harden_directory(root)
    directory = root / _daemon_dir_name(auth_root)
    harden_directory_entry(directory)
    return directory


def descriptor_path(auth_root: Path) -> Path:
    return daemon_dir(auth_root) / _DESCRIPTOR_FILE


def pending_descriptor_path(auth_root: Path, instance_id: str) -> Path:
    """Where one uncommitted generation stages its descriptor."""
    checked = _checked_instance_id(instance_id)
    return daemon_dir(auth_root) / f"{_PENDING_DESCRIPTOR_PREFIX}{checked}.json"


def token_path(auth_root: Path, instance_id: str) -> Path:
    """Where the bearer token lives, named for the instance that owns it.

    Per instance, so a descriptor and a token never belong to different
    generations. The identifier comes from a file and is checked to be a UUID
    first; ``..`` in it would turn any readable file into the bearer token
    sent to the endpoint.
    """
    return daemon_dir(auth_root) / f"token-{_checked_instance_id(instance_id)}"


def _checked_instance_id(instance_id: str) -> str:
    """Return *instance_id* if it is a UUID, and refuse otherwise."""
    try:
        # str() of the parsed value, not the input: it rejects the separators
        # and casing that would otherwise give one instance several filenames.
        return str(uuid.UUID(instance_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise DescriptorError(
            "The daemon descriptor carries an instance id that is not a UUID"
        ) from exc


def new_instance_id() -> str:
    """A fresh identity for one daemon run."""
    return str(uuid.uuid4())


def new_token() -> str:
    """A fresh bearer token. One per daemon start, never reused."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    """Render *value* so that equal configuration always hashes equally."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


#: Fields where an empty string is a value rather than an absence. The proxy
#: credentials are the case that matters: ``proxy_settings`` compares the
#: password against None precisely so an empty one is still sent, so folding it
#: into "unset" would report a match between two clients whose browsers launch
#: with different options.
_EMPTY_IS_MEANINGFUL = frozenset({"proxy_username", "proxy_password"})


def _normalize(name: str, value: Any) -> Any:
    """Reduce a field to the form two processes should agree on.

    Only differences that would make the owner's browser wrong for a client
    count. When unsure, err towards a difference: that costs a refusal, while
    the opposite hands a client a differently configured browser.
    """
    if value is None:
        return None
    if name == "user_data_dir":
        # The profile is shared state, so compare the physical directory rather
        # than one spelling. macOS can preserve case and Unicode aliases in a
        # resolved path even when both names address the same directory.
        return profile_identity(Path(str(value)))
    if name == "chrome_path":
        path = Path(str(value)).expanduser().resolve()
        return os.path.normcase(str(path))
    if name == "proxy_bypass":
        # An ordered, comma-separated list where neither the order nor the
        # spacing changes which hosts bypass the proxy.
        hosts = sorted({host.strip().lower() for host in str(value).split(",")} - {""})
        return ",".join(hosts)
    if isinstance(value, str) and not value and name not in _EMPTY_IS_MEANINGFUL:
        return None
    return value


def config_fingerprint(config: AppConfig, *, key: str) -> str:
    """Digest the configuration an owner and its clients must agree on.

    Keyed with the daemon's token, because ``proxy_password`` is among the
    fields and an unkeyed digest of otherwise guessable values would allow
    offline password guessing.
    """
    material = {
        name: _normalize(name, getattr(config.browser, name, None))
        for name in SHARED_CONFIG_FIELDS
    }
    material["tool_timeout_seconds"] = config.server.tool_timeout_seconds
    return hmac.new(
        key.encode("utf-8"), _canonical(material).encode("utf-8"), hashlib.sha256
    ).hexdigest()


@dataclass(frozen=True)
class DaemonDescriptor:
    """Everything a client needs to decide whether to attach, and where."""

    instance_id: str
    schema_version: int
    protocol_version: int
    package_version: str
    runtime_id: str
    profile_path: str
    profile_identity: str
    host: str
    port: int
    path: str
    token_sha256: str
    config_fingerprint: str
    started_at: str
    log_path: str
    #: Diagnostics only, never a liveness test: a recycled process id reads as
    #: alive forever.
    pid: int = field(default=0)

    @property
    def url(self) -> str:
        # An IPv6 literal has to be bracketed, or its colons run into the port.
        # Bracketed already, or a name, it is left alone.
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}{self.path}"

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_mapping(cls, raw: object) -> DaemonDescriptor:
        """Build one from parsed JSON, refusing anything unusable.

        Field by field, because another process wrote the file; unpacking
        would accept a string port and fail much later.
        """
        if not isinstance(raw, Mapping):
            raise DescriptorError("The daemon descriptor is not an object")

        descriptor = cls(
            # Checked here as well as where it becomes a path, so a descriptor
            # is rejected on the way in rather than at whichever call site
            # happens to touch the identifier first.
            instance_id=_checked_instance_id(_text(raw, "instance_id")),
            schema_version=_number(raw, "schema_version"),
            protocol_version=_number(raw, "protocol_version"),
            package_version=_text(raw, "package_version"),
            runtime_id=_text(raw, "runtime_id"),
            profile_path=_text(raw, "profile_path"),
            profile_identity=_text(raw, "profile_identity"),
            host=_text(raw, "host"),
            port=_number(raw, "port"),
            path=_text(raw, "path"),
            token_sha256=_digest_text(raw, "token_sha256"),
            config_fingerprint=_digest_text(raw, "config_fingerprint"),
            started_at=_text(raw, "started_at"),
            log_path=_text(raw, "log_path"),
            pid=_number(raw, "pid", default=0),
        )
        descriptor.check_endpoint_is_local()
        return descriptor

    def check_endpoint_is_local(self) -> None:
        """Refuse an endpoint that is not this machine, before sending a token.

        The file says what was last written to it, and posting the bearer token
        to an unchecked host would hand over the LinkedIn session behind it.
        """
        if not is_loopback_host(self.host):
            raise DescriptorError(
                f"The daemon descriptor points at {self.host}, which is not "
                f"this machine. Refusing to send credentials there."
            )
        if not 1 <= self.port <= 65535:
            raise DescriptorError(
                f"The daemon descriptor names port {self.port}, which is not a "
                f"usable port number"
            )
        if not self.path.startswith("/"):
            raise DescriptorError("The daemon descriptor has no usable path")

        # A URL is printable ASCII. A range rather than a list of known-bad
        # characters, so nothing the client would reject gets past here.
        for name, value in (("host", self.host), ("path", self.path)):
            if any(character < "\x21" or character > "\x7e" for character in value):
                raise DescriptorError(
                    f"The daemon descriptor's {name} contains a character that "
                    f"cannot appear in a URL, so it cannot name an endpoint"
                )

        # Individually plausible fields must also compose into a URL a client
        # can use. urlsplit, so this does not depend on the caller's client.
        try:
            parsed = urlsplit(self.url)
            port_survived = parsed.port == self.port
            host_survived = (parsed.hostname or "") == self.host.strip("[]").lower()
        except ValueError as exc:
            raise DescriptorError(
                f"The daemon descriptor describes an endpoint that cannot be "
                f"used to reach anything: {exc}"
            ) from exc
        # The host is read back too: is_loopback_host judged the field, and a
        # space-padded address can pass it while the URL names another host.
        if not port_survived or not host_survived or parsed.scheme != "http":
            raise DescriptorError(
                f"The daemon descriptor's host, port and path do not compose "
                f"into a usable address: {self.url!r}"
            )

        # Finally, what the name resolves to: is_loopback_host accepts
        # ``localhost`` by name and trailing-dot spellings, which say nothing
        # about where this machine's resolver sends them.
        hostname = parsed.hostname or ""
        try:
            # A literal is its own answer, and the owner publishes one, so the
            # common path never reaches getaddrinfo, which takes no deadline.
            ipaddress.ip_address(hostname)
            return
        except ValueError:
            pass

        try:
            resolved = socket.getaddrinfo(hostname, self.port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as exc:
            raise DescriptorError(
                f"The daemon descriptor names host {self.host!r}, which does "
                f"not resolve to an address on this machine: {exc}"
            ) from exc

        # Every answer, not the first: one loopback answer says nothing about
        # the one a client picks. The client resolves again when it connects,
        # so this narrows the gap rather than closing it.
        for *_, address in resolved:
            if not is_loopback_host(str(address[0])):
                raise DescriptorError(
                    f"The daemon descriptor names host {self.host!r}, which "
                    f"resolves to {address[0]}, somewhere other than this "
                    f"machine. Refusing to send credentials there."
                )

    def matches_token(self, token: str) -> bool:
        """Whether *token* is the one this descriptor was published with."""
        return hmac.compare_digest(self.token_sha256, _digest(token))

    def serves(self, profile: Path) -> bool:
        """Whether this daemon owns exactly *profile*.

        Exact, because the lock is shared by every profile under one auth root.
        Two directories side by side elect one owner between them, and a client
        that only checked the auth root would be served the wrong browser.
        """
        return profile_identity(profile) == self.profile_identity


def prepare(
    auth_root: Path, descriptor: DaemonDescriptor, token: str
) -> tuple[Path, Path]:
    """Stage one generation without making it globally discoverable."""
    directory = prepare_daemon_state(auth_root)

    token_file = directory / f"token-{_checked_instance_id(descriptor.instance_id)}"
    secure_write_text(token_file, token)
    try:
        harden_created_file(token_file)
    except BaseException:
        with contextlib.suppress(OSError):
            token_file.unlink(missing_ok=True)
        raise

    pending_file = directory / (
        f"{_PENDING_DESCRIPTOR_PREFIX}{_checked_instance_id(descriptor.instance_id)}.json"
    )
    try:
        secure_write_text(pending_file, descriptor.to_json())
        harden_created_file(pending_file)
    except BaseException:
        with contextlib.suppress(OSError):
            pending_file.unlink(missing_ok=True)
            token_file.unlink(missing_ok=True)
        raise
    return pending_file, token_file


def discard_prepared(auth_root: Path, instance_id: str) -> None:
    """Remove only the uncommitted files belonging to *instance_id*."""
    directory = prepare_daemon_state(auth_root)
    checked = _checked_instance_id(instance_id)
    for path in (
        directory / f"{_PENDING_DESCRIPTOR_PREFIX}{checked}.json",
        directory / f"token-{checked}",
    ):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def commit_prepared(auth_root: Path, instance_id: str) -> Path:
    """Make a validated pending generation canonical with one replacement."""
    try:
        directory = prepare_daemon_state(auth_root)
        checked = _checked_instance_id(instance_id)
        pending = directory / f"{_PENDING_DESCRIPTOR_PREFIX}{checked}.json"
        published = directory / _DESCRIPTOR_FILE
        # Windows reports replacing a directory with a file as WinError 5, the same
        # code used for transient sharing conflicts. Normalize only an actual
        # directory entry before the syscall. Following a symlink here would reject
        # a link that ``os.replace`` can safely replace without touching its target.
        try:
            published_mode = published.stat(follow_symlinks=False).st_mode
        except FileNotFoundError:
            published_mode = None
        structural = published_mode is not None and stat.S_ISDIR(published_mode)
        if published_mode is not None and not structural and os.name == "nt":
            # A link naming a directory is refused by ``os.replace`` on Windows
            # with the same WinError 5, so it is the same structural problem
            # there and is normalized with it. POSIX replaces the link itself
            # and never reaches this, which is why the question is asked of the
            # running platform rather than of the flag the tests simulate with.
            structural = stat.S_ISLNK(published_mode) and published.is_dir()
        if structural:
            raise IsADirectoryError(
                f"The daemon descriptor path {published} is a directory"
            )
    except (CommitPreflightError, IsADirectoryError, NotADirectoryError):
        raise
    except BaseException as exc:
        raise CommitPreflightError(
            "The daemon descriptor replacement could not be prepared"
        ) from exc

    os.replace(pending, published)
    return published


def replace_definitely_failed(exc: BaseException) -> bool:
    """Whether *exc* proves that ``os.replace`` could not have taken effect.

    Permission and read-only errors are deliberately excluded. After an NFS
    server applied a rename and lost its reply, a retry can observe changed
    permissions or mount state and report one even though publication happened.
    Structural file-versus-directory mismatches cannot be produced that way.
    """
    return isinstance(
        exc, (CommitPreflightError, IsADirectoryError, NotADirectoryError)
    )


#: A descriptor is a few hundred bytes of JSON. Bounded for the same reason the
#: token read is: whatever sits at the path may not be what this wrote.
_MAX_DESCRIPTOR_BYTES = 64 * 1024


def _read_own_file(
    path: Path,
    limit: int,
    *,
    missing_is_none: bool,
    missing_message: str | None = None,
) -> str | None:
    """Read a file this daemon wrote, refusing anything that is not one.

    Before a byte is used, the entry must not be a symlink, must be a regular
    file, and the open must not block, so a link cannot aim the read elsewhere
    and a named pipe cannot hang the client. With *missing_is_none*, absence
    returns ``None``; otherwise it raises with *missing_message*.
    """
    # Windows has no O_NOFOLLOW, so the open there resolves a link and the
    # check below then sees a perfectly ordinary regular file at the other end.
    # Checking the entry itself is the only refusal available on that platform.
    # It is a check-then-open rather than an atomic one, so a link planted in
    # the gap still wins; closing that properly needs the Win32 reparse-point
    # flag, which is worth doing when the daemon actually runs there.
    if not hasattr(os, "O_NOFOLLOW"):  # pragma: no cover - Windows only
        try:
            if path.is_symlink():
                raise DescriptorError(
                    f"{path} is a symbolic link rather than a file this daemon "
                    f"wrote. Remove it and start the server again."
                )
        except OSError as exc:
            raise DescriptorError(f"{path} could not be read: {exc}") from exc

    # O_NOFOLLOW fails on a symlink rather than resolving it, so the check and
    # the read cannot disagree about which file they mean. O_NONBLOCK has no
    # effect on the regular file this expects.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise DescriptorError(
                    f"{path} is not a regular file, so it is not something this "
                    f"daemon wrote"
                )
            # A private parent prevents another account planting anything new, but
            # it says nothing about an entry left there while an older parent was
            # permissive. Establish this file's owner and access before using a
            # byte, then prove the descriptor still names the entry that was
            # hardened.
            try:
                harden_file(path)
            except PrivateStateError as exc:
                raise DescriptorError(str(exc)) from exc
            if not is_still_at(fd, path):
                raise DescriptorError(
                    f"{path} was replaced while its private access was being "
                    f"established"
                )
            # Size checked before reading rather than by noticing a full
            # buffer afterwards. Truncating instead would hand the caller a
            # fragment, and a fragment of JSON is reported as malformed, which
            # sends whoever reads that message looking for the wrong problem.
            if info.st_size > limit:
                raise DescriptorError(
                    f"{path} is {info.st_size} bytes, far larger than anything "
                    f"this daemon writes, so it is not one of its files"
                )
            raw = os.read(fd, limit)
        finally:
            os.close(fd)
    except FileNotFoundError:
        if missing_is_none:
            return None
        # Worded by the caller. Naming the token here would be right only for
        # as long as it stays the only caller that asks absence to raise, and
        # wrong without anything failing if that changes.
        raise DescriptorError(missing_message or f"{path} is missing") from None
    except OSError as exc:
        # ELOOP arrives here when the path is a symlink, which is never
        # something this wrote and never something to follow.
        raise DescriptorError(f"{path} could not be read: {exc}") from exc

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Wrapped, because a caller distinguishing absence from untrusted state
        # through DescriptorError would otherwise meet an unrelated exception
        # for what is simply a file this did not write.
        raise DescriptorError(f"{path} is not text this daemon wrote") from exc


def _decode_descriptor(path: Path, raw: str) -> DaemonDescriptor:
    """Parse and version-check one descriptor read from *path*.

    Enforces the schema (the file format) but not the tool protocol: a
    protocol mismatch names an owner this client may only ask to stand down,
    which the caller decides (``daemon.discovery._inspect``).
    """
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise DescriptorError(
            f"The daemon descriptor at {path} is not valid JSON: {exc}"
        ) from exc

    descriptor = DaemonDescriptor.from_mapping(parsed)
    if descriptor.schema_version != SCHEMA_VERSION:
        raise DescriptorError(
            f"The daemon descriptor is version {descriptor.schema_version}, and "
            f"this client understands version {SCHEMA_VERSION}"
        )
    if descriptor.protocol_version < CONTROL_FLOOR_PROTOCOL:
        raise DescriptorError(
            f"The running daemon speaks protocol {descriptor.protocol_version}, "
            f"which this client cannot even ask to stand down"
        )
    return descriptor


def _read_descriptor(path: Path, *, missing_is_none: bool) -> DaemonDescriptor | None:
    raw = _read_own_file(
        path,
        _MAX_DESCRIPTOR_BYTES,
        missing_is_none=missing_is_none,
        missing_message=f"The prepared daemon descriptor at {path} is missing",
    )
    if raw is None:
        return None
    return _decode_descriptor(path, raw)


def read(auth_root: Path) -> DaemonDescriptor | None:
    """Return the published descriptor, or None when there is none.

    Raises :class:`DescriptorError` for one that exists but cannot be trusted,
    which the caller must distinguish from absence: a corrupt descriptor beside
    a *held* lock means a live daemon this client cannot talk to. Whether the
    position is free remains the lock's answer.
    """
    directory = prepare_daemon_state(auth_root)
    return _read_descriptor(directory / _DESCRIPTOR_FILE, missing_is_none=True)


def validate_prepared(
    auth_root: Path,
    instance_id: str,
    *,
    profile: Path,
    config: AppConfig,
) -> DaemonDescriptor:
    """Validate one pending generation before the parent authorizes commit."""
    checked = _checked_instance_id(instance_id)
    directory = prepare_daemon_state(auth_root)
    pending = directory / f"{_PENDING_DESCRIPTOR_PREFIX}{checked}.json"
    descriptor = _read_descriptor(pending, missing_is_none=False)
    if descriptor is None:  # pragma: no cover - absence raises above
        raise DescriptorError(f"The prepared daemon descriptor at {pending} is missing")
    if descriptor.instance_id != checked:
        raise DescriptorError(
            "The prepared daemon descriptor belongs to another generation"
        )
    # Exact here, unlike ``read``: this is the generation this build is about
    # to publish as its own, not one it may merely ask to stand down.
    if descriptor.protocol_version != PROTOCOL_VERSION:
        raise DescriptorError(
            "The prepared daemon descriptor speaks another protocol than this build"
        )
    if not descriptor.serves(profile):
        raise DescriptorError("The prepared daemon descriptor serves another profile")

    token = read_token(auth_root, descriptor)
    if descriptor.config_fingerprint != config_fingerprint(config, key=token):
        raise DescriptorError(
            "The prepared daemon descriptor uses a different configuration"
        )
    return descriptor


#: A token is a few dozen characters. Reading is bounded anyway, so a file that
#: turns out to be something else cannot pull an unbounded amount into memory
#: before the checks below reject it.
_MAX_TOKEN_BYTES = 4096


def read_token(auth_root: Path, descriptor: DaemonDescriptor) -> str:
    """Read the token belonging to *descriptor*, checking it is the right one.

    The UUID check confines the filename, not what sits there, so the file
    goes through :func:`_read_own_file`.
    """
    directory = prepare_daemon_state(auth_root)
    path = directory / f"token-{_checked_instance_id(descriptor.instance_id)}"
    # A token missing beside a published descriptor raises. Checked rather
    # than asserted below, since -O strips assertions and this is a credential.
    text = _read_own_file(
        path,
        _MAX_TOKEN_BYTES,
        missing_is_none=False,
        missing_message=(
            "The daemon is publishing a descriptor with no token beside it"
        ),
    )
    if text is None:  # pragma: no cover - unreachable while the flag is False
        raise DescriptorError(f"{path} could not be read")
    token = text.strip()

    if not descriptor.matches_token(token):
        raise DescriptorError(
            "The daemon token does not match its descriptor, so they belong to "
            "different daemon generations"
        )
    return token


def build(
    *,
    instance_id: str,
    package_version: str,
    runtime_id: str,
    profile: Path,
    host: str,
    port: int,
    path: str,
    token: str,
    config: AppConfig,
    log_path: Path,
) -> DaemonDescriptor:
    """Assemble a descriptor for a daemon that is already listening."""
    return DaemonDescriptor(
        instance_id=instance_id,
        schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION,
        package_version=package_version,
        runtime_id=runtime_id,
        profile_path=str(profile.expanduser().resolve()),
        profile_identity=profile_identity(profile),
        host=host,
        port=port,
        path=path,
        token_sha256=_digest(token),
        config_fingerprint=config_fingerprint(config, key=token),
        started_at=utcnow_iso(),
        log_path=str(log_path),
        pid=os.getpid(),
    )
