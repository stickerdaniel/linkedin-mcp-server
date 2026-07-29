"""The configuration a frontend hands to the owner it starts.

The owner cannot simply reload the configuration for itself. Half of it arrives
on the command line of the process the MCP client spawned, and that process is
the frontend, not the owner. An owner that re-read only the environment would
come up with a different browser than the client asked for, and the frontend
would then refuse to attach to it: ``config_fingerprint`` compares exactly the
fields that would differ. The result is not a wrong browser but a client that
elects an owner and then declines to use it, forever.

So the resolved configuration is handed over explicitly, on the child's standard
input. Not the environment and not the command line, because both are readable
by anything running as this account — ``/proc/<pid>/environ`` and ``ps`` — and
what travels here includes ``proxy_password``. A pipe is read once by one
process and never lands anywhere.

Both ends are the same installation: the frontend starts the owner with its own
interpreter and its own package, so this codec never crosses versions. It is
still a parse of untrusted-shaped input rather than a restore, because the thing
being reconstructed decides which browser opens against a logged-in session.
"""

from __future__ import annotations

import json
from dataclasses import fields
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


def encode(config: AppConfig) -> str:
    """Serialise the parts of *config* that an owner has to agree on."""
    return json.dumps(
        {
            # Every browser field, not just the fingerprinted ones. The
            # fingerprint says which differences make two clients unable to
            # share an owner; it does not say which settings the owner needs to
            # do its job. Handing over only the shared subset would leave the
            # owner on default handoff and idle timings while the user had
            # configured others.
            "browser": {
                field.name: getattr(config.browser, field.name)
                for field in fields(config.browser)
            },
            "server": {name: getattr(config.server, name) for name in _SERVER_FIELDS},
        }
    )


def decode(raw: str) -> AppConfig:
    """Rebuild the configuration an owner was started with.

    Unknown names are refused rather than skipped. The two ends are one
    installation, so a name this does not recognise is not a version difference
    but a blob that did not come from :func:`encode`, and the quiet reading of
    it would be an owner running on settings nobody chose.
    """
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"The owner configuration is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("The owner configuration is not an object")

    config = AppConfig()
    _apply(config.browser, parsed.get("browser"), "browser")
    _apply(config.server, parsed.get("server"), "server", allowed=_SERVER_FIELDS)
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
