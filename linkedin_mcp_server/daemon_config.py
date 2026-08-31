"""The configuration a frontend hands to the owner it starts.

The owner cannot simply reload the configuration for itself. Half of it arrives
on the command line of the process the MCP client spawned, and that process is
the frontend, not the owner. An owner that re-read only the environment would
come up with a different browser than the client asked for, and the frontend
would then refuse to attach to it: ``config_fingerprint`` compares exactly the
fields that would differ. The result is not a wrong browser but a client that
elects an owner and then declines to use it, forever.

So the resolved configuration is handed over explicitly, on the child's standard
input, together with the post-spawn nonce that authenticates its startup verdict.
Not the environment and not the command line, because both are readable by
anything running as this account (``/proc/<pid>/environ`` and ``ps``), and what
travels here includes ``proxy_password``. A pipe is read once by one process and
never lands anywhere.

A long-running frontend can start an owner after the package was replaced on
disk, so the handover carries a startup-protocol version. Missing means the
pre-commit predecessor protocol. Input is still parsed as untrusted-shaped data
because the reconstructed values decide which browser opens against a logged-in
session.

Three versions are understood, and an owner has to serve all three because the
frontend on the other side is whatever was installed when it started. Windows
is the exception and refuses the first of them: the predecessor there holds a
legacy state directory this owner may not adopt, so it stops before touching
any state rather than serving a frontend it cannot share a namespace with
(#810):

1. No commit boundary. The frontend closes the pipe once written and kills a
   child that stays silent, so the owner's only safe handoff is ``READY``
   before it publishes.
2. Commit authorization on the configuration pipe, which the frontend holds
   open past the record for exactly that purpose.
3. Commit authorization on a separate loopback channel named here, so the
   configuration pipe can be closed at once and a predecessor owner started by
   a current frontend still reaches its end of file. See
   :mod:`linkedin_mcp_server.process_control`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from types import UnionType
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.process_protocol import valid_nonce

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

#: Server settings an owner needs. Deliberately a short list rather than the
#: whole section: transport, host, port and the one-shot flags all describe the
#: *frontend's* invocation, and an owner that adopted them would try to run the
#: client's transport or re-run its ``--login``.
_SERVER_FIELDS = ("tool_timeout_seconds", "log_level")
STARTUP_PROTOCOL_VERSION = 3
_PREDECESSOR_STARTUP_PROTOCOL_VERSION = 1
_PIPE_COMMIT_STARTUP_PROTOCOL_VERSION = 2
_SUPPORTED_STARTUP_PROTOCOLS = (
    _PREDECESSOR_STARTUP_PROTOCOL_VERSION,
    _PIPE_COMMIT_STARTUP_PROTOCOL_VERSION,
    STARTUP_PROTOCOL_VERSION,
)


def authorizes_commit(startup_protocol: int) -> bool:
    """Whether a frontend speaking *startup_protocol* can authorize a commit.

    The one distinction the owner acts on. Which channel carries the record is
    the handover's business; whether there is a record at all decides between
    publishing on the parent's authority and publishing on its own.
    """
    return startup_protocol >= _PIPE_COMMIT_STARTUP_PROTOCOL_VERSION


@dataclass(frozen=True)
class ControlEndpoint:
    """Where the parent is listening for its child's commit channel."""

    host: str
    port: int


@dataclass(frozen=True)
class OwnerHandover:
    """Configuration and the post-spawn token for its startup verdict."""

    config: AppConfig
    handshake_nonce: str
    startup_protocol: int = STARTUP_PROTOCOL_VERSION
    control: ControlEndpoint | None = None


def _encoded(config: AppConfig) -> dict[str, object]:
    return {
        # Every browser field, not just the fingerprinted ones. The fingerprint
        # says which differences make two clients unable to share an owner; it
        # does not say which settings the owner needs to do its job. Handing over
        # only the shared subset would leave the owner on default handoff and idle
        # timings while the user had configured others.
        "browser": {
            field.name: getattr(config.browser, field.name)
            for field in fields(config.browser)
        },
        "server": {name: getattr(config.server, name) for name in _SERVER_FIELDS},
    }


def encode(config: AppConfig) -> str:
    """Serialise the parts of *config* that an owner has to agree on."""
    return json.dumps(_encoded(config))


def encode_handover(
    config: AppConfig, handshake_nonce: str, control: ControlEndpoint
) -> str:
    """Serialise owner settings with a nonce created after its process exists.

    The version and the rendezvous are separate top-level names rather than a
    nested section, and that is what makes a predecessor owner able to read this
    record at all: every version of :func:`_decoded` reads ``browser`` and
    ``server`` by name and ignores what it has never heard of, while an unknown
    name *inside* one of those sections is refused.
    """
    if not valid_nonce(handshake_nonce):
        raise ValueError("The owner handshake nonce is invalid")
    payload = _encoded(config)
    payload["handshake_nonce"] = handshake_nonce
    payload["startup_protocol"] = STARTUP_PROTOCOL_VERSION
    payload["control_host"] = control.host
    payload["control_port"] = control.port
    return json.dumps(payload)


def _parsed(raw: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"The owner configuration is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("The owner configuration is not an object")
    return parsed


def _decoded(parsed: dict[str, object]) -> AppConfig:
    config = AppConfig()
    _apply(config.browser, parsed.get("browser"), "browser")
    _apply(config.server, parsed.get("server"), "server", allowed=_SERVER_FIELDS)
    # The same validation the frontend's own configuration went through. An
    # owner is the process that opens the browser, so a value that would be
    # refused there must be refused here rather than reaching Chromium by the
    # back door.
    config.validate()
    return config


def decode(raw: str) -> AppConfig:
    """Rebuild the configuration an owner was started with.

    Unknown names are refused rather than skipped. The two ends are one
    installation, so a name this does not recognise is not a version difference
    but a blob that did not come from :func:`encode`, and the quiet reading of
    it would be an owner running on settings nobody chose.
    """
    return _decoded(_parsed(raw))


def decode_handover(raw: str) -> OwnerHandover:
    """Rebuild owner settings and authenticate its future startup verdict."""
    parsed = _parsed(raw)
    handshake_nonce = parsed.get("handshake_nonce")
    if not valid_nonce(handshake_nonce):
        raise ValueError("The owner configuration has no valid handshake nonce")
    assert isinstance(handshake_nonce, str)
    startup_protocol = parsed.get(
        "startup_protocol", _PREDECESSOR_STARTUP_PROTOCOL_VERSION
    )
    if (
        not isinstance(startup_protocol, int)
        or isinstance(startup_protocol, bool)
        or startup_protocol not in _SUPPORTED_STARTUP_PROTOCOLS
    ):
        raise ValueError(
            "The owner configuration names an unsupported startup protocol"
        )
    control = _control_endpoint(parsed, startup_protocol)
    return OwnerHandover(_decoded(parsed), handshake_nonce, startup_protocol, control)


def _control_endpoint(
    parsed: dict[str, object], startup_protocol: int
) -> ControlEndpoint | None:
    """The rendezvous this protocol version requires, and no other version may name.

    Refused both ways round. A current record without an address would leave the
    owner with no way to be authorized, and an older version naming one would be
    a frontend asking for a channel its own protocol never establishes; neither
    is a version difference, because the value comes from one function.
    """
    host = parsed.get("control_host")
    port = parsed.get("control_port")
    if startup_protocol != STARTUP_PROTOCOL_VERSION:
        if host is not None or port is not None:
            raise ValueError(
                "The owner configuration names a control channel its startup "
                "protocol does not use"
            )
        return None
    if not isinstance(host, str) or not host or any(c.isspace() for c in host):
        raise ValueError("The owner configuration has no valid control host")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("The owner configuration has no valid control port")
    return ControlEndpoint(host, port)


def _apply(
    section: DataclassInstance,
    values: object,
    label: str,
    *,
    allowed: tuple[str, ...] | None = None,
) -> None:
    if not isinstance(values, dict):
        raise ValueError(f"The owner configuration has no {label} section")

    declared = get_type_hints(type(section))
    known = {field.name for field in fields(section)}
    if allowed is not None:
        known &= set(allowed)

    for name, value in values.items():
        if not isinstance(name, str) or name not in known:
            raise ValueError(
                f"The owner configuration names an unknown {label} setting"
            )
        if not _matches(value, declared[name]):
            # Named without its value: this section carries proxy_password and
            # the path to someone's browser profile, and the failure is
            # diagnosable from the field alone.
            raise ValueError(
                f"The owner configuration gives {label}.{name} the wrong type"
            )
        setattr(section, name, value)


def _matches(value: object, declared: Any) -> bool:
    """Whether *value* is usable where something of type *declared* belongs.

    Checked against the declared type rather than against the default's runtime
    type, because the optional settings default to ``None`` and every value
    would look wrong beside one. JSON is the other half of the reason: it cannot
    tell seconds from a count, so a string where a float belongs would otherwise
    surface deep inside a browser launch rather than here.
    """
    origin = get_origin(declared)
    if origin is Literal:
        # An exact member, so a log level or transport that this build does not
        # know is refused here rather than at whatever reads it later.
        return value in get_args(declared)
    if origin in (Union, UnionType):
        return any(_matches(value, member) for member in get_args(declared))
    if declared is type(None):
        return value is None
    if declared is float:
        # JSON writes 0 for 0.0, so an int has to be accepted where a float is
        # declared. bool is excluded explicitly: it is a subclass of int, and
        # accepting True as a timeout is exactly the silent nonsense this
        # function exists to stop.
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(declared, type):
        return isinstance(value, declared)
    # An annotation this does not understand. Refusing is the safe direction:
    # the alternative is a value nothing checked reaching a browser launch.
    return False
