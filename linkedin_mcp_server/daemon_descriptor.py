"""What a running daemon publishes about itself, and what a client checks.

A client that finds a descriptor is about to send a bearer token to whatever
address it names and then drive a logged-in LinkedIn session through it. So
nothing here is taken on trust: the endpoint is checked to be loopback before
the token goes anywhere, the profile path is compared exactly, and the
configuration is compared through a fingerprint.

Two fields exist because of measurements rather than tidiness:

``profile_path``
    ``auth_root_dir`` returns the profile's *parent*, so ``.../profile`` and
    ``.../profile2`` share one auth root and therefore one lock. Without an
    exact comparison here, a client configured for the second would attach to
    the first one's browser and never notice.

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
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from linkedin_mcp_server.common_utils import secure_write_text, utcnow_iso
from linkedin_mcp_server.config.schema import AppConfig, is_loopback_host
from linkedin_mcp_server.private_state import harden_directory, harden_file

#: Bumped when the shape below changes incompatibly. A client that does not
#: recognise the value refuses rather than guessing at fields it cannot read.
SCHEMA_VERSION = 1

#: Bumped when the daemon's own protocol changes: the control routes, the call
#: metadata, or the ping contract. Compatibility keys on this rather than on the
#: package version, because the documented install is ``@latest`` and this
#: project releases often. Matching exact versions would mean a manual shutdown
#: after almost every release, which is precisely the friction the daemon exists
#: to remove.
PROTOCOL_VERSION = 1

_DESCRIPTOR_FILE = "daemon.json"
_DAEMON_DIR = "daemon"

# Enough that guessing is not a strategy. Read straight from the OS source.
_TOKEN_BYTES = 32

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


def _number(raw: Mapping[Any, Any], name: str, *, default: int | None = None) -> int:
    value = raw.get(name, default)
    # bool is an int as far as isinstance is concerned, and a port of True is
    # not a port.
    if isinstance(value, bool) or not isinstance(value, int):
        raise DescriptorError(
            f"The daemon descriptor field {name} is missing or not a number"
        )
    return value


def daemon_dir(auth_root: Path) -> Path:
    """Where a daemon's descriptor and token live for *auth_root*."""
    return auth_root.expanduser().resolve() / _DAEMON_DIR


def descriptor_path(auth_root: Path) -> Path:
    return daemon_dir(auth_root) / _DESCRIPTOR_FILE


def token_path(auth_root: Path, instance_id: str) -> Path:
    """Where the bearer token lives, named for the instance that owns it.

    Named rather than fixed so a descriptor and a token can never belong to
    different generations. With one shared name, a client that read the old
    descriptor and then the new token would find a digest that does not match
    and, following the discovery rules, call it corruption instead of a restart.

    The identifier is checked to be a UUID before it becomes part of a path.
    It arrives from a file anything with write access to the auth root can
    edit, and building a filename from it unchecked would let ``..`` walk out
    of the daemon directory and turn any readable file into what this client
    sends to the endpoint as its bearer token.
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
    if value is None:
        return None
    if name in ("user_data_dir", "chrome_path"):
        path = Path(str(value)).expanduser().resolve()
        # Windows paths differ only in case, so comparing them literally would
        # split one owner into several.
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
            host=_text(raw, "host"),
            port=_number(raw, "port"),
            path=_text(raw, "path"),
            token_sha256=_text(raw, "token_sha256"),
            config_fingerprint=_text(raw, "config_fingerprint"),
            started_at=_text(raw, "started_at"),
            log_path=_text(raw, "log_path"),
            pid=_number(raw, "pid", default=0),
        )
        descriptor.check_endpoint_is_local()
        return descriptor

    def check_endpoint_is_local(self) -> None:
        """Refuse an endpoint that is not this machine, before sending a token.

        The descriptor is a file on disk, so it can be edited. Reading a host
        from it and posting a bearer token there without checking would turn any
        write access to the auth root into a way to collect the token and,
        through it, the LinkedIn session behind it.
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

    def matches_token(self, token: str) -> bool:
        """Whether *token* is the one this descriptor was published with."""
        return hmac.compare_digest(self.token_sha256, _digest(token))

    def serves(self, profile: Path) -> bool:
        """Whether this daemon owns exactly *profile*.

        Exact, because the lock is shared by every profile under one auth root.
        Two directories side by side elect one owner between them, and a client
        that only checked the auth root would be served the wrong browser.
        """
        wanted = os.path.normcase(str(profile.expanduser().resolve()))
        return wanted == os.path.normcase(self.profile_path)


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
    harden_directory(directory)

    token_file = token_path(auth_root, descriptor.instance_id)
    secure_write_text(token_file, token)
    harden_file(token_file)

    descriptor_file = descriptor_path(auth_root)
    secure_write_text(descriptor_file, descriptor.to_json())
    return descriptor_file, token_file


#: A descriptor is a few hundred bytes of JSON. Bounded for the same reason the
#: token read is: whatever sits at the path may not be what this wrote.
_MAX_DESCRIPTOR_BYTES = 64 * 1024


def _read_own_file(path: Path, limit: int, *, missing_is_none: bool) -> str | None:
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
    is not.
    """
    # O_NOFOLLOW fails on a symlink rather than resolving it, so the check and
    # the read cannot disagree about which file they mean. O_NONBLOCK has no
    # effect on the regular file this expects.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise DescriptorError(
                    f"{path} is not a regular file, so it is not something this "
                    f"daemon wrote"
                )
            raw = os.read(fd, limit)
        finally:
            os.close(fd)
    except FileNotFoundError:
        if missing_is_none:
            return None
        raise DescriptorError(
            "The daemon is publishing a descriptor with no token beside it"
        ) from None
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
    except json.JSONDecodeError as exc:
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
    text = _read_own_file(path, _MAX_TOKEN_BYTES, missing_is_none=False)
    assert text is not None  # missing_is_none=False raises rather than returning
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
        host=host,
        port=port,
        path=path,
        token_sha256=_digest(token),
        config_fingerprint=config_fingerprint(config, key=token),
        started_at=utcnow_iso(),
        log_path=str(log_path),
        pid=os.getpid(),
    )
