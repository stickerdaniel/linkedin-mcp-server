"""What a running daemon publishes about itself, and what a client checks.

A client that finds a descriptor is about to send a bearer token to whatever
address it names and then drive a logged-in LinkedIn session through it. So
nothing here is taken on trust: the endpoint is checked to be loopback before
the token goes anywhere, the profile path is compared exactly, and the
configuration is compared through a fingerprint.

Two fields exist because of measurements rather than tidiness:

``profile_path`` and ``profile_identity``
    ``auth_root_dir`` returns the profile's *parent*, so ``.../profile`` and
    ``.../profile2`` share one auth root and therefore one lock. The path is
    retained for diagnostics; the identity is what is compared. It is built
    from the parent's inode and the profile's own name rather than the
    profile's inode, because the profile directory is replaced whenever a new
    session is established: keyed by its inode, a descriptor would stop
    matching its own profile at the first login. The parent's inode still
    settles the case and Unicode aliases that a bare string comparison gets
    wrong on an insensitive volume.

``config_fingerprint``
    Two clients that disagree about ``headless``, the user agent or the proxy
    are not interchangeable, because the owner's browser can only be one of
    them. The fingerprint is keyed rather than a plain digest: ``proxy_password``
    is part of what must match, and a plain hash over otherwise-guessable
    configuration would be an offline check for password guesses.

The token itself is deliberately *not* here. It lives beside the descriptor in
its own file, named after the instance, and the descriptor carries only its
digest. Fixed names would let a client pair one generation's descriptor with the
next generation's token and read that mismatch as corruption rather than as the
restart it is.

State lives under the account's own directory rather than under the auth root,
which is configurable and may be a directory this application does not own. One
consequence is worth stating before anything is wired to it: two containers
sharing a bind-mounted auth root do *not* share this state, because each has its
own home. Measured with two containers on one mounted profile: both saw the same
auth root identity and both took the lock. Whatever adopts this has to either
keep containers on the existing per-operation lease or give them a shared state
directory of their own.
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

from linkedin_mcp_server.common_utils import secure_mkdir, secure_write_text, utcnow_iso
from linkedin_mcp_server.config.schema import AppConfig, is_loopback_host
from linkedin_mcp_server.private_state import harden_directory, harden_file

#: Bumped when the shape below changes incompatibly. A client that does not
#: recognise the value refuses rather than guessing at fields it cannot read.
SCHEMA_VERSION = 2

#: Bumped when the daemon's own protocol changes incompatibly: the control
#: routes, the call metadata, or the ping contract. Compatibility keys on this
#: rather than on the package version, because the documented install is
#: ``@latest`` and this project releases often. Matching exact versions would
#: mean a manual shutdown after almost every release, which is precisely the
#: friction the daemon exists to remove.
#:
#: **Incompatibly is doing the work in that sentence, and it was added rather
#: than assumed.** The heartbeat route and the call marker
#: (:mod:`linkedin_mcp_server.daemon_liveness`) are a control route and call
#: metadata both, and they did not bump this. A bump makes an old owner
#: unreadable — :func:`read` refuses the file before an attachment exists — and
#: an owner nobody can read is an owner nobody can ask to stand down, so the
#: turnover that an upgrade depends on would break for every owner already
#: installed. An addition that both sides can do without is therefore not a
#: reason to bump; one that either side would misread is. The test for it is
#: whether the two mixed pairings still work, and for the heartbeat they are
#: tested rather than asserted.
PROTOCOL_VERSION = 1

_DESCRIPTOR_FILE = "daemon.json"
_DAEMON_DIR = "daemon"
_APPLICATION_STATE_DIR = ".mcp-server-linkedin"

# Enough that guessing is not a strategy. Read straight from the OS source.
_TOKEN_BYTES = 32

#: What a hex digest may contain, for checking one read from a file.
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

#: Configuration a client must share with the owner to be served by it. Each of
#: these either changes what the browser *is* (its fingerprint, its exit
#: address, where its profile lives) or how long the owner keeps it. Anything
#: not listed here may differ freely between clients.
SHARED_CONFIG_FIELDS = (
    "user_data_dir",
    "headless",
    "slow_mo",
    "user_agent",
    "viewport_width",
    "viewport_height",
    "default_timeout",
    "chrome_path",
    "proxy_server",
    "proxy_username",
    "proxy_password",
    "proxy_bypass",
    "login_timeout_seconds",
    "login_inline_wait_seconds",
    "auto_import_from_browser",
    "eager_full_chromium",
    "browser_idle_timeout_seconds",
)


class DescriptorError(RuntimeError):
    """A descriptor could not be trusted, so nothing was sent to it."""


def _text(raw: Mapping[Any, Any], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str):
        raise DescriptorError(
            f"The daemon descriptor field {name} is missing or not text"
        )
    return value


def _digest_text(raw: Mapping[Any, Any], name: str) -> str:
    """Read a field that must be a SHA-256 digest, and check that it is one.

    ``token_sha256`` is compared with ``hmac.compare_digest``, which refuses a
    string carrying anything outside ASCII and raises TypeError doing so.
    Measured with an accented character: the descriptor was accepted here and
    the comparison failed later with a TypeError, outside the DescriptorError
    every caller of this module is written to expect.

    ``config_fingerprint`` is only produced and parsed so far, and is checked
    the same way because it is the same kind of value and will be compared the
    same way once there is something to compare it against. A digest has one
    shape, so establishing it costs nothing.
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


def _account_home() -> Path:
    """Return the operating system account's home, ignoring process overrides."""
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
    # and an is_symlink() refusal does not.
    return home.resolve()


def daemon_state_root() -> Path:
    """Return the private application root shared by every local daemon.

    Fully resolved, so that processes reaching this directory by different
    routes still agree on one lock. Symbolic links along the way are followed
    rather than refused: they are a normal way to lay out a home directory.

    What this cannot defend against is the path being *changed* underneath a
    running owner, by retargeting a link or renaming a directory. Measured: a
    second process then takes a lock on a different inode while the first still
    holds its own. The existing profile lease has exactly the same property, for
    the same reason, and no lock addressed by path can avoid it. The account
    that could do this is the one whose browser is at stake.
    """
    return (_account_home() / _APPLICATION_STATE_DIR / _DAEMON_DIR).resolve()


def _canonical_path(path: Path, label: str) -> Path:
    """Resolve *path*, in this module's vocabulary rather than the runtime's.

    expanduser and resolve are the first thing every caller reaches, and they
    have failure modes of their own: a home directory that cannot be
    determined raises RuntimeError, an embedded NUL raises ValueError, and an
    unreadable parent raises PermissionError. All three arrive from
    type-correct configuration, so they are refusals rather than defects, and
    they belong in the error every caller of this module is written to expect.
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

    Created rather than merely inspected, because this runs in the process that
    is establishing state: the first daemon on a fresh install must not key a
    missing path and then switch to an inode after creating it, which would
    leave its own lock at an address nothing else computes. ``secure_mkdir``
    creates only what is missing and deliberately leaves permissions on
    existing parents alone.

    Device plus inode identifies the physical directory rather than one
    spelling of it, which keeps case and Unicode aliases together on an
    insensitive volume without conflating distinct directories on a sensitive
    one. The auth root can be keyed this way because nothing rotates it; the
    profile inside it cannot, and :func:`profile_identity` says why.
    """
    canonical = _canonical_path(auth_root, "authentication root")
    # Checked before creating, not after. secure_mkdir refuses an existing
    # non-directory itself, with a NotADirectoryError that reaches the caller
    # instead of the DescriptorError this module is read through, and the check
    # below could then never run: measured, a file where the auth root belongs
    # surfaced as NotADirectoryError.
    if canonical.exists() and not canonical.is_dir():
        raise DescriptorError(
            f"The authentication root is not a directory: {canonical}"
        )

    # Creating and then reading it back are one operation as far as a caller is
    # concerned, and both can fail in ways the operating system describes in its
    # own vocabulary. Measured: an auth root that vanished between the two
    # surfaced as FileNotFoundError, several layers from anything that could
    # explain it as a descriptor problem.
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

    Deliberately not the device and inode that address daemon state. A profile
    directory is *replaced* in normal operation: every path that establishes a
    new session rotates the old one into quarantine and Chromium creates a fresh
    directory at the same place. Keyed by inode, a descriptor published before
    the first login would stop matching its own profile the moment that login
    happened, and the owner would keep the lock while no client could recognise
    it. The auth root above it is not rotated, which is why the lock can use an
    inode and this cannot.

    Location, then, but not a bare string comparison: on a case-insensitive
    volume ``resolve()`` keeps whichever spelling the caller used, so two names
    for one profile would look like two profiles. The parent is identified by
    its inode, and the final component is taken from the parent's own directory
    listing rather than from the caller, which is what makes ``Profile`` and
    ``profile`` land on the same answer where the filesystem says they are one
    directory. ``normcase`` alone would not: it is a no-op on POSIX, and macOS
    is case-insensitive all the same.

    Creates nothing: this runs while assembling a refusal, and leaving a browser
    profile behind to explain one would be a strange thing for a client to
    discover afterwards.
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

    # The name as the filesystem holds it, where it holds one. Only the parent
    # is consulted, never the profile itself, because the profile legitimately
    # does not exist yet before the first login: an identity that changed when
    # it appeared would leave a descriptor published beforehand no longer
    # matching its own profile. Measured, with the auth root non-empty so the
    # scan had something to walk: serves() went from True to False across the
    # login that created the directory.
    # The name as the filesystem holds it, which is only a different name from
    # the caller's where the filesystem says the two are one directory. Asking
    # it directly rather than folding on principle: on a case-sensitive volume
    # Profile and profile are two directories, and treating a fold match as the
    # same entry there is wrong twice over. Measured on a case-sensitive APFS
    # volume, once with both present and once with only the sibling: two
    # distinct profiles shared an identity, which would have served a client
    # the other one's logged-in session.
    # Folded and normalised. casefold settles case; NFC settles composition,
    # which is a separate axis macOS also collapses: an accented name written
    # decomposed and composed is one directory there, and comparing the two
    # spellings without normalising gave them different identities.
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
            # samefile answers what folding only guesses at, and it is safe to
            # ask here: the candidate exists by definition, and the profile
            # need not, which is the case that made an earlier version of this
            # fall back to a path and change its answer at the first login.
            #
            # Keep looking when it says no. Several entries can fold alike on a
            # volume that distinguishes them, so stopping at the first
            # candidate would settle on a directory the filesystem had just
            # denied was the same one: measured with É and é present and é
            # asked for, the scan met É first, was told they differ, and gave
            # up before reaching the entry that did match.
            try:
                if canonical.parent.joinpath(name).samefile(canonical):
                    wanted = name
                    break
            except OSError:
                continue
    return f"under\0{info.st_dev}\0{info.st_ino}\0{wanted}"


def daemon_dir(auth_root: Path) -> Path:
    """Where daemon state for *auth_root* lives.

    The auth root is user-configurable and may be a shared directory such as
    ``/tmp`` or a home directory. Storing state below it would either change
    permissions on a directory this application does not own or trust a
    replaceable directory link for the ownership lock. Keep state under the
    operating system account's private application directory instead, keyed by
    the physical auth root so sibling profiles still share exactly one election.
    """
    key = hashlib.sha256(_auth_root_identity(auth_root)).hexdigest()
    return daemon_state_root() / key


def descriptor_path(auth_root: Path) -> Path:
    return daemon_dir(auth_root) / _DESCRIPTOR_FILE


def token_path(auth_root: Path, instance_id: str) -> Path:
    """Where the bearer token lives, named for the instance that owns it.

    Named rather than fixed so a descriptor and a token can never belong to
    different generations. With one shared name, a client that read the old
    descriptor and then the new token would find a digest that does not match
    and, following the discovery rules, call it corruption instead of a restart.

    The identifier is checked to be a UUID before it becomes part of a path.
    It arrives from a file, and a file is only ever as trustworthy as what can
    write to it. The state directory is owner-only now, so that is this account
    and whatever runs as it, but building a filename from the identifier
    unchecked would let ``..`` walk out of the daemon directory and turn any
    readable file into what this client sends to the endpoint as its bearer
    token. The check costs one parse.
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

    Only differences that would actually make the owner's browser wrong for a
    client should count. Two spellings of the same path are the same profile.
    Erring towards a false difference is safe here: it costs a refusal that
    names the field. Erring the other way hands a client a browser configured
    differently from what it asked for.
    """
    if name == "auto_import_from_browser":
        # Auto-import is enabled by default. None and True therefore ask for the
        # same browser behaviour; only an explicit False disables it.
        return value is not False
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

    Keyed with the daemon's own token. That is not belt and braces: the shared
    fields include ``proxy_password``, and the remaining fields are mostly
    guessable, so an unkeyed digest sitting in a world-readable descriptor would
    let anyone test password guesses offline. The key never leaves the token
    file, so only a process already entitled to talk to the daemon can compute
    or compare it.
    """
    material = {
        name: _normalize(name, getattr(config.browser, name, None))
        for name in SHARED_CONFIG_FIELDS
    }
    material["tool_timeout_seconds"] = config.server.tool_timeout_seconds
    return hmac.new(
        key.encode("utf-8"), _canonical(material).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def mismatched_fields(config: AppConfig, other: AppConfig) -> tuple[str, ...]:
    """Which shared fields differ, for an explanation that names no values.

    A mismatch message has to say enough to act on and nothing more: the values
    include a proxy password and the path to someone's browser profile.
    """
    differing = [
        name
        for name in SHARED_CONFIG_FIELDS
        if _normalize(name, getattr(config.browser, name, None))
        != _normalize(name, getattr(other.browser, name, None))
    ]
    if config.server.tool_timeout_seconds != other.server.tool_timeout_seconds:
        differing.append("tool_timeout_seconds")
    return tuple(differing)


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
    #: Diagnostics only. Never used to decide whether the daemon is alive: a
    #: recycled process id reads as alive forever, which is how a comparable
    #: server wedged itself permanently.
    pid: int = field(default=0)

    @property
    def url(self) -> str:
        # An IPv6 literal has to be bracketed, or the colons in the address run
        # into the one before the port and the whole thing parses as a bad
        # port. Measured: a valid ::1 endpoint produced a URL no client could
        # use. Bracketed already, or a name, it is left alone.
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}{self.path}"

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_mapping(cls, raw: object) -> DaemonDescriptor:
        """Build one from parsed JSON, refusing anything unusable.

        Field by field rather than by unpacking the mapping, because this is
        reading a file that another process wrote and that anything with write
        access could have edited. Unpacking would accept a port that is a string
        and fail much later, somewhere that cannot explain why.
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

        The descriptor is a file on disk, so it says what was last written to
        it rather than what this process believes. Reading a host from it and
        posting a bearer token there unchecked would turn a corrupted or
        stale file into a way to hand over the token and, through it, the
        LinkedIn session behind it. The file lives in owner-only state, so this
        is about what has gone wrong rather than about another account, and it
        is cheap enough to check either way.
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

        # A URL is printable ASCII. Stated as a rule rather than as a list of
        # the characters that happened to come up: an earlier version banned
        # CR, LF, tab and NUL by name and left thirty-six other code points
        # that were accepted here and rejected by the client, which is exactly
        # the shape of failure this check exists to move. Anything outside the
        # printable range goes, whether it is a control character, a
        # non-breaking space, or a lone surrogate.
        for name, value in (("host", self.host), ("path", self.path)):
            if any(character < "\x21" or character > "\x7e" for character in value):
                raise DescriptorError(
                    f"The daemon descriptor's {name} contains a character that "
                    f"cannot appear in a URL, so it cannot name an endpoint"
                )

        # The fields are individually plausible; whether they compose into
        # something a client can use is a separate question, and the only one
        # that finally matters. Measured accepting three such descriptors:
        # "[::1] ", a bracketed IPv4, and a path with a newline, each failing
        # later in the client rather than here where it could be explained as a
        # descriptor this did not write.
        #
        # urlsplit rather than an HTTP client's parser: this has no business
        # depending on which client the caller happens to use.
        try:
            parsed = urlsplit(self.url)
            port_survived = parsed.port == self.port
            host_survived = (parsed.hostname or "") == self.host.strip("[]").lower()
        except ValueError as exc:
            raise DescriptorError(
                f"The daemon descriptor describes an endpoint that cannot be "
                f"used to reach anything: {exc}"
            ) from exc
        # The host is read back, not just the port. is_loopback_host was asked
        # about the field; this asks about the string a client will actually
        # connect to, and the two came apart: a space-padded address passed as
        # loopback and produced a host of "%20127.0.0.1%20", so what was
        # verified and what would be contacted were not the same name.
        if not port_survived or not host_survived or parsed.scheme != "http":
            raise DescriptorError(
                f"The daemon descriptor's host, port and path do not compose "
                f"into a usable address: {self.url!r}"
            )

        # And finally: what does this name actually resolve to. Spelling rules
        # keep answering slightly the wrong question, because a host can be
        # well formed and still resolve to nothing, or to somewhere else.
        # is_loopback_host strips trailing dots before deciding whether it is
        # looking at a literal or a name, so "127.0.0.1." and "localhost.."
        # passed as loopback while getaddrinfo refused them outright; and it
        # accepts "localhost" by name, which says nothing about where the
        # resolver on this machine actually sends it.
        hostname = parsed.hostname or ""
        try:
            # An address literal is its own answer, and asking the resolver
            # about one would be a needless trip through a component that can
            # hang: getaddrinfo takes no deadline, so a wedged NSS or mDNS
            # responder blocks for as long as it likes. Measured with a stalled
            # resolver: two seconds for a two second stall, and unbounded in
            # principle. This is what a daemon publishes for itself, so the
            # common path never reaches the call below.
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

        # Every answer, not merely the first: a name can carry several, and one
        # of them being loopback says nothing about the one a client picks.
        # Measured with the resolver made to answer 203.0.113.9 for localhost:
        # the descriptor was accepted, and the token would have been posted off
        # this machine.
        #
        # This does not close the gap entirely, because the client resolves the
        # name again when it connects and can get a different answer. What it
        # does is stop a descriptor naming a host that resolves elsewhere right
        # now, which is the difference between a check and a decoration.
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


def publish(
    auth_root: Path, descriptor: DaemonDescriptor, token: str
) -> tuple[Path, Path]:
    """Write the token and then the descriptor, in that order.

    Order matters. The descriptor is what makes a daemon discoverable, so it is
    written last: a client that finds one can rely on the token beside it
    already existing. Written the other way round, discovery would race the
    token into existence.
    """
    directory = daemon_dir(auth_root)
    harden_directory(daemon_state_root())
    harden_directory(directory)

    # secure_write_text already creates the file 0600, inside a directory this
    # just made 0700, so the token is never on disk under wider permissions.
    # harden_file is what establishes that rather than assuming it: it reads
    # the mode, the owner and the access list back, and on Windows replaces the
    # access list outright. If it refuses, the token it refused to vouch for
    # goes away again rather than staying behind as state nothing verified.
    token_file = token_path(auth_root, descriptor.instance_id)
    secure_write_text(token_file, token)
    try:
        harden_file(token_file)
    except BaseException:
        # Best effort, and quiet about its own failure: this runs while another
        # error is on its way out, and replacing that one with a complaint
        # about the tidying would hide what actually went wrong. Measured with
        # the unlink refused: the caller saw PermissionError instead of the
        # verification failure that caused it.
        with contextlib.suppress(OSError):
            token_file.unlink(missing_ok=True)
        raise

    descriptor_file = descriptor_path(auth_root)
    secure_write_text(descriptor_file, descriptor.to_json())
    return descriptor_file, token_file


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

    Both files under the daemon directory go through here, because both are
    read by a client that is about to act on them and neither is safe to open
    by path alone. Three things are established before a byte is used:

    * it is not a symbolic link, so the path cannot aim the read elsewhere;
    * it is a regular file, so a named pipe cannot stand in for one;
    * the open does not block, so such a pipe cannot hang the client inside
      ``open`` before either check has run.

    *missing_is_none* separates the two callers. Absence of a descriptor is the
    ordinary first-start case; absence of a token beside a published descriptor
    is not, and *missing_message* is how that caller says why.
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


def read(auth_root: Path) -> DaemonDescriptor | None:
    """Return the published descriptor, or None when there is none.

    Raises :class:`DescriptorError` for one that exists but cannot be trusted,
    which the caller must distinguish from absence: a corrupt descriptor beside
    a *held* lock means a live daemon this client cannot talk to, and deleting
    it would strand every other client attached to that daemon.
    """
    path = descriptor_path(auth_root)
    raw = _read_own_file(path, _MAX_DESCRIPTOR_BYTES, missing_is_none=True)
    if raw is None:
        return None

    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        # ValueError rather than JSONDecodeError alone: a number with more
        # digits than sys.int_max_str_digits allows raises a plain ValueError
        # from inside the parser, and a file well under the size limit can
        # carry one. Measured with a five thousand digit integer: it crossed
        # the boundary as ValueError before anything could call it a bad
        # descriptor. JSONDecodeError is a ValueError, so this covers both.
        raise DescriptorError(
            f"The daemon descriptor is not valid JSON: {exc}"
        ) from exc

    descriptor = DaemonDescriptor.from_mapping(parsed)
    if descriptor.schema_version != SCHEMA_VERSION:
        raise DescriptorError(
            f"The daemon descriptor is version {descriptor.schema_version}, and "
            f"this client understands version {SCHEMA_VERSION}"
        )
    # Enforced, not merely recorded. This is the field compatibility is meant
    # to key on, and a client that attached across a protocol change would be
    # speaking to an owner whose control routes, call metadata and ping
    # contract it does not share.
    if descriptor.protocol_version != PROTOCOL_VERSION:
        raise DescriptorError(
            f"The running daemon speaks protocol {descriptor.protocol_version} "
            f"and this client speaks {PROTOCOL_VERSION}. Stop the running "
            f"daemon to let a compatible one start."
        )
    return descriptor


#: A token is a few dozen characters. Reading is bounded anyway, so a file that
#: turns out to be something else cannot pull an unbounded amount into memory
#: before the checks below reject it.
_MAX_TOKEN_BYTES = 4096


def read_token(auth_root: Path, descriptor: DaemonDescriptor) -> str:
    """Read the token belonging to *descriptor*, checking it is the right one.

    Opened without following links and confirmed to be a regular file before a
    byte is read. Confining the *filename* to the daemon directory, which the
    UUID check does, is not enough on its own: a link sitting at that name
    still points wherever it likes, and a special file there could block the
    client before any of the checks ran.
    """
    path = token_path(auth_root, descriptor.instance_id)
    # None only comes back for a missing file, and this caller asked for that
    # to raise instead: a token missing beside a published descriptor is a
    # daemon in a state it should not be in, not an ordinary absence. Checked
    # rather than asserted, since assertions are removed under -O and this one
    # guards what gets used as a credential.
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
