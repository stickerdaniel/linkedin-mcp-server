"""The configuration a frontend hands to the owner it starts.

The owner cannot reload it itself: part of it came from the frontend's command
line, and an owner with a different configuration would fail the frontend's
``config_fingerprint`` check forever. So the resolved configuration travels as
the CONFIG frame on the child's standard input
(:mod:`linkedin_mcp_server.daemon.startup`), never the environment or command
line, which other processes of this account can read and which would expose
``proxy_password``. It is parsed strictly, since it decides which browser opens
against a logged-in session.
"""

from __future__ import annotations

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

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

#: Server settings an owner needs. Deliberately a short list rather than the
#: whole section: transport, host, port and the one-shot flags all describe the
#: *frontend's* invocation, and an owner that adopted them would try to run the
#: client's transport or re-run its ``--login``.
_SERVER_FIELDS = ("tool_timeout_seconds", "log_level")


@dataclass(frozen=True)
class OwnerHandover:
    """Configuration and the post-spawn nonce every later startup frame carries."""

    config: AppConfig
    handshake_nonce: str


def encode(config: AppConfig) -> dict[str, object]:
    """The parts of *config* that an owner has to agree on, as a JSON object."""
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


def decode(payload: object) -> AppConfig:
    """Rebuild the configuration an owner was started with.

    Unknown names are refused rather than skipped: both ends are one build, so
    such a name means the payload did not come from :func:`encode`.
    """
    if not isinstance(payload, dict):
        raise ValueError("The owner configuration is not an object")
    config = AppConfig()
    _apply(config.browser, payload.get("browser"), "browser")
    _apply(config.server, payload.get("server"), "server", allowed=_SERVER_FIELDS)
    # The same validation the frontend's own configuration went through. An
    # owner is the process that opens the browser, so a value that would be
    # refused there must be refused here rather than reaching Chromium by the
    # back door.
    config.validate()
    return config


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

    Against the declared type, not the default's runtime type, which is
    ``None`` for optional settings. A wrong type is refused here rather than
    deep inside a browser launch.
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
