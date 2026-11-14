"""
Configuration loading and argument parsing for LinkedIn MCP Server.

Loads settings from CLI arguments and environment variables.
"""

import argparse
import logging
import math
import os
import stat
import sys
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Never, NoReturn
from urllib.parse import unquote, urlsplit

from dotenv import load_dotenv

from .schema import AppConfig, ConfigurationError

# Load .env file if present
load_dotenv()

logger = logging.getLogger(__name__)

# Boolean value mappings for environment variable parsing
TRUTHY_VALUES = ("1", "true", "yes", "on")
FALSY_VALUES = ("0", "false", "no", "off")


def _normalize_env(value: str) -> str:
    """Normalize environment variable values for tolerant parsing."""
    return value.strip().lower()


def positive_int(value: str) -> int:
    """Argparse type for positive integers."""
    ivalue = int(value)
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"must be positive, got {value}")
    return ivalue


def positive_float(value: str) -> float:
    """Argparse type for positive finite floats."""
    fvalue = float(value)
    if not (math.isfinite(fvalue) and fvalue > 0):
        raise argparse.ArgumentTypeError(
            f"must be a positive finite number, got {value}"
        )
    return fvalue


def non_negative_float(value: str) -> float:
    """Argparse type for non-negative finite floats (0 allowed as a sentinel)."""
    fvalue = float(value)
    if not (math.isfinite(fvalue) and fvalue >= 0):
        raise argparse.ArgumentTypeError(
            f"must be a non-negative finite number, got {value}"
        )
    return fvalue


def credential_free_url(value: str) -> str:
    """Argparse type for a proxy URL that carries no credentials.

    A password in a command-line argument is readable by every user on the
    machine through the process list, which is the whole reason there is no
    ``--proxy-password`` flag. Accepting it inside ``--proxy-server`` would give
    the secret back the same exposure, so it is refused here rather than split
    out later. The value is never echoed, since it is the secret itself.
    """
    candidate = value if "://" in value else f"http://{value}"
    try:
        parsed = urlsplit(candidate)
        # The encoded form counts too: "user%3Apass%40host" parses as a plain
        # hostname, so the credentials would survive in the visible address.
        has_credentials = bool(parsed.username or parsed.password) or (
            "@" in unquote(parsed.hostname or "")
        )
    except ValueError:
        # Leave the shape of the URL to BrowserConfig.validate(), which can
        # explain the problem properly.
        return value
    if has_credentials:
        raise argparse.ArgumentTypeError(
            "must not contain credentials. Pass the bare scheme://host:port "
            "here and supply the password via the PROXY_PASSWORD environment "
            "variable, so it is not exposed in the process list."
        )
    return value


# Refused rather than ignored. A user who set this did so to change how the
# browser presents itself, and dropping it silently would leave them believing
# it still applies. The setting never worked the way it reads: Patchright only
# overrides the user-agent string, so the client hints kept reporting the real
# browser and the page saw two different answers to the same question. Service
# workers never received the override at all
# (https://github.com/microsoft/playwright/issues/5237, closed upstream).
#
# The second sentence about a running server is not padding. Only a release
# from before this refusal could start a shared owner with a user agent, and
# those releases keep their daemon state in a directory this version never
# reads (docs/decisions/2026-10-11-daemon-clean-slate.md), so that owner can
# never be asked to stand down: it keeps running and keeps the profile.
# Removing the setting is therefore only half the fix, and the other half is
# invisible unless this message says it.
_USER_AGENT_REMOVED = (
    "{setting} is no longer supported and the server will not start with it "
    "set. Overriding the user agent left the browser contradicting itself: the "
    "string changed but the client hints did not, and service workers kept the "
    "original either way. The browser now reports its own identity "
    "consistently. Remove the setting to start. If a shared server from an "
    "earlier version is still running with it, stop that process too: it "
    "cannot be retired automatically."
)


class EnvironmentKeys:
    """Environment variable names used by the application."""

    HEADLESS = "HEADLESS"
    LOG_LEVEL = "LOG_LEVEL"
    TRANSPORT = "TRANSPORT"
    TIMEOUT = "TIMEOUT"
    USER_AGENT = "USER_AGENT"
    HOST = "HOST"
    PORT = "PORT"
    HTTP_PATH = "HTTP_PATH"
    SLOW_MO = "SLOW_MO"
    VIEWPORT = "VIEWPORT"
    CHROME_PATH = "CHROME_PATH"
    PROXY_SERVER = "PROXY_SERVER"
    PROXY_USERNAME = "PROXY_USERNAME"
    PROXY_PASSWORD = "PROXY_PASSWORD"
    PROXY_BYPASS = "PROXY_BYPASS"
    USER_DATA_DIR = "USER_DATA_DIR"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    LOGIN_TIMEOUT = "LOGIN_TIMEOUT"
    LOGIN_INLINE_WAIT = "LOGIN_INLINE_WAIT"
    BROWSER_WAIT = "BROWSER_WAIT"
    BROWSER_MIN_HOLD = "BROWSER_MIN_HOLD"
    BROWSER_IDLE_TIMEOUT = "BROWSER_IDLE_TIMEOUT"
    IMPORT_FROM_BROWSER = "IMPORT_FROM_BROWSER"
    AUTO_IMPORT_FROM_BROWSER = "AUTO_IMPORT_FROM_BROWSER"
    DAEMON_ENABLED = "DAEMON_ENABLED"
    INSTALLER_TEMP_DIR = "INSTALLER_TEMP_DIR"
    LINKEDIN_TRACE_MODE = "LINKEDIN_TRACE_MODE"
    LINKEDIN_MCP_CHECK_FOR_UPDATES = "LINKEDIN_MCP_CHECK_FOR_UPDATES"
    LINKEDIN_MCP_HTTP_TOKEN = "LINKEDIN_MCP_HTTP_TOKEN"
    LINKEDIN_MCP_ALLOW_UNAUTHENTICATED_HTTP = "LINKEDIN_MCP_ALLOW_UNAUTHENTICATED_HTTP"


# What ``manifest.json`` fills from ``user_config``, and the exact string each
# mapping leaves behind when the host does not substitute it.
#
# An MCPB host builds its replacement map from the manifest's ``default`` values
# overlaid with the answers the user gave, then rewrites only the ``${...}``
# occurrences it has a key for. A field with neither a default nor an answer is
# absent from that map, so its placeholder is handed to the process verbatim.
# Measured against Claude Desktop's own substitution routine.
#
# One exact string per variable, not a pattern for the shape. A pattern would
# also swallow a password that happens to read ``${user_config.token}``, which
# is a legal password and would leave the browser authenticating with nothing.
# Spelling the mapping out costs a line each and cannot do that.
# ``tests/test_manifest.py`` checks this table against the manifest itself, so
# the two cannot drift.
#
# One collision survives and is not fixable here: a password whose value is
# its own variable's literal. Substituted and unsubstituted are then the same
# string, and nothing downstream can tell them apart.
_MCPB_PLACEHOLDERS = {
    EnvironmentKeys.CHROME_PATH: "${user_config.chrome_path}",
    EnvironmentKeys.PROXY_SERVER: "${user_config.proxy_server}",
    EnvironmentKeys.PROXY_USERNAME: "${user_config.proxy_username}",
    EnvironmentKeys.PROXY_PASSWORD: "${user_config.proxy_password}",
    EnvironmentKeys.PROXY_BYPASS: "${user_config.proxy_bypass}",
}


def _env(key: str) -> str | None:
    """Read an environment variable, treating an MCPB placeholder as unset.

    The ``default`` entries in ``manifest.json`` are what keep the placeholders
    out of the environment in the first place, and a manifest test holds that
    line. This does not reach the bundles already installed with the broken
    manifest: a bundle carries this source and that manifest together, so
    whatever fixes one fixes the other. What it covers is a host that resolves
    ``user_config`` by some other rule or ignores ``default``, and a variable
    somebody set by hand after reading one out of an extension's own settings
    pane.

    Both failure modes are worth refusing. A literal in ``PROXY_SERVER`` aborts
    startup, which is loud. A literal in ``PROXY_USERNAME`` is not: the browser
    offers ``${user_config.proxy_username}`` to the proxy as a credential, and
    an authentication that fails that way surfaces as a timeout or an expired
    session, never as a configuration problem.

    Deliberately no ``strip()``: a proxy password may legitimately begin or end
    with a space, and silently trimming it would be a wrong password nobody can
    see.
    """
    value = os.environ.get(key)
    if not value or value == _MCPB_PLACEHOLDERS.get(key):
        return None
    return value


class SettingError(ValueError):
    """A value a setting cannot take, raised by the parsers below.

    Reported as ``Invalid <name>: '<raw>'. <reason>``, where the name is the
    environment variable or the option the value came through.
    """

    def describe(self, name: str, raw: str) -> str:
        return f"Invalid {name}: '{raw}'. {self}"


class SettingRemoved(SettingError):
    """A setting refused whatever its value, with an explanation of its own.

    The message is a template with a ``{setting}`` slot and never quotes the
    value.
    """

    def describe(self, name: str, raw: str) -> str:
        return str(self).format(setting=name)


LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
TRANSPORTS = ("stdio", "streamable-http")


def _text(raw: str) -> str:
    return raw


def _flag(raw: str) -> bool:
    """An on/off value.

    Refused rather than ignored, as LOG_LEVEL and TRANSPORT are: a value outside
    TRUTHY_VALUES and FALSY_VALUES is a typo, and ignoring it kept the default
    while reading as though the setting had applied.
    """
    value = _normalize_env(raw)
    if value in TRUTHY_VALUES:
        return True
    if value in FALSY_VALUES:
        return False
    raise SettingError(f"Must be one of {', '.join(TRUTHY_VALUES + FALSY_VALUES)}.")


def _log_level(raw: str) -> str:
    """A log level in any case.

    Refused rather than ignored, as TRANSPORT is: a value outside this set is a
    typo, and ignoring it let the server report the default level while reading
    as though the setting had applied.
    """
    value = raw.strip().upper()
    if value not in LOG_LEVELS:
        raise SettingError("Must be one of 'DEBUG', 'INFO', 'WARNING' or 'ERROR'.")
    return value


def _transport(raw: str) -> str:
    value = _normalize_env(raw)
    if value not in TRANSPORTS:
        raise SettingError("Must be 'stdio' or 'streamable-http'.")
    return value


TraceMode = Literal["off", "on_error", "always"]

#: Every spelling LINKEDIN_TRACE_MODE accepts. The on/off spellings keep the
#: meaning they had before an unknown value was refused: off, or the default.
TRACE_MODES: dict[str, TraceMode] = {
    "on_error": "on_error",
    "always": "always",
    "keep": "always",
    "persist": "always",
    **{value: "on_error" for value in TRUTHY_VALUES},
    **{value: "off" for value in FALSY_VALUES},
}


def _trace_mode(raw: str) -> TraceMode:
    """A trace retention mode.

    Refused rather than ignored, as the on/off settings are: an unknown value
    fell back to ``on_error`` while reading as though it had applied.
    """
    try:
        return TRACE_MODES[_normalize_env(raw)]
    except KeyError:
        raise SettingError("Must be 'on_error', 'always' or 'off'.")


def _integer(raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise SettingError("Must be an integer.")


def _finite(*, positive: bool, note: str = "") -> Callable[[str], float]:
    """A parser for finite numbers above zero, or from zero when not *positive*.

    *note* follows the bound in the refusal, to say what zero means.
    """
    bound = "positive" if positive else "non-negative"

    def parse(raw: str) -> float:
        try:
            value = float(raw)
        except ValueError:
            raise SettingError("Must be a number.")
        if not (math.isfinite(value) and (value > 0 if positive else value >= 0)):
            raise SettingError(f"Must be a {bound} finite number{note}.")
        return value

    return parse


def _viewport(raw: str) -> tuple[int, int]:
    try:
        width, height = raw.lower().split("x")
        return int(width), int(height)
    except ValueError:
        raise SettingError("Must be in format WxH (e.g., 1280x720).")


def _browser_to_import(raw: str) -> str:
    """A browser key, where a blank value means "auto"."""
    return _normalize_env(raw) or "auto"


def _http_token_file(raw: str) -> str:
    """The HTTP client token, read from the file the path *raw* names.

    A file rather than a value, so the token stays out of the process list. One
    trailing line break, as ``echo`` or an editor leaves it, is not part of the
    token. The token itself is judged in ``AppConfig.validate()``, once the
    variable and this file have settled which one applies.

    Opened first and judged on the open descriptor, so the file checked is the
    file read. Non-blocking, so a FIFO is refused rather than waited on. Only a
    regular file is read, or a link to one. On POSIX it has to be private to its
    owner, as an SSH key does: anyone else who can read it can use the session.
    Windows has no mode bits to judge, so the check is skipped there and the
    file's ACL is the operator's to set.
    """
    path = Path(raw).expanduser()
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise SettingError("It is not a regular file.")
        if os.name == "posix" and stat.S_IMODE(status.st_mode) & 0o077:
            raise SettingError(
                "Other accounts on this machine may access it. Make it private "
                "to you: chmod 600"
            )
        # Wrapped only once judged: fdopen itself refuses a directory, with an
        # error that would not say what is wrong.
        with os.fdopen(descriptor, "rb") as file:
            descriptor = -1
            content = file.read().decode("utf-8", errors="replace")
    # Every step, not only the open: Option.given() turns a SettingError into
    # a refusal that names the option, and anything else into a traceback.
    except OSError as error:
        raise SettingError(f"It cannot be read: {error.strerror or error}.")
    finally:
        if descriptor != -1:
            # A failed close must not replace the refusal already on its way.
            with suppress(OSError):
                os.close(descriptor)
    return content.removesuffix("\n").removesuffix("\r")


def _user_agent(raw: str) -> NoReturn:
    raise SettingRemoved(_USER_AGENT_REMOVED)


def _browser[T](name: str) -> Callable[[AppConfig, T], None]:
    """Store a setting's value in the browser field *name*."""
    return lambda config, value: setattr(config.browser, name, value)


def _server[T](name: str) -> Callable[[AppConfig, T], None]:
    """Store a setting's value in the server field *name*."""
    return lambda config, value: setattr(config.server, name, value)


def _set_transport(config: AppConfig, transport: str) -> None:
    _server("transport")(config, transport)
    config.server.transport_explicitly_set = True


def _set_viewport(config: AppConfig, size: tuple[int, int]) -> None:
    config.browser.viewport_width, config.browser.viewport_height = size


def _never_applied(config: AppConfig, value: Never) -> None:
    """For a setting whose parser refuses every value."""


def _read_where_used(config: AppConfig, value: object) -> None:
    """For a setting checked at startup and read by the module that uses it."""


def _dest(flag: str) -> str:
    return flag.removeprefix("--").replace("-", "_")


@dataclass(frozen=True)
class Env[T]:
    """How a setting is read from the environment variable *key*.

    The raw read is ``_env()``, so an empty value or an MCPB placeholder is
    unset before *parse* sees it. *blank* says what a value of only whitespace
    means: ``unset``, or ``parse``, which hands it to *parse* like any other.
    """

    key: str
    parse: Callable[[str], T]
    blank: Literal["unset", "parse"] = "parse"


@dataclass(frozen=True, kw_only=True)
class Option[T]:
    """A value option, ``--flag VALUE``.

    *parse* runs in the phase *phase* names. As the argparse ``type``, a bad
    value exits with status 2 before anything is loaded; at ``load``, it raises
    ConfigurationError the way a bad environment value does. *cli_empty* says
    what an explicit ``--flag ""`` means: ``ignore`` leaves the value from the
    environment alone, ``auto`` stands for *const*, and ``parse`` hands it to
    *parse*, which refuses it. A *const* also makes the value optional, so the
    bare flag means *const*.
    """

    flag: str
    parse: Callable[[str], T]
    phase: Literal["argparse", "load"] = "argparse"
    cli_empty: Literal["ignore", "auto", "parse"]
    choices: tuple[str, ...] | None = None
    const: str | None = None
    metavar: str | None = None
    help: str

    def add_to(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            self.flag,
            dest=_dest(self.flag),
            type=self.parse if self.phase == "argparse" else str,
            choices=self.choices,
            nargs=None if self.const is None else "?",
            const=self.const,
            default=None,
            metavar=self.metavar,
            help=self.help,
        )

    def given(self, args: argparse.Namespace) -> T | None:
        raw = getattr(args, _dest(self.flag))
        if raw is None or (raw == "" and self.cli_empty == "ignore"):
            return None
        if self.phase == "argparse":
            # Already converted: argparse ran parse as the option's type.
            return raw
        if raw == "" and self.cli_empty == "auto" and self.const is not None:
            raw = self.const
        try:
            return self.parse(raw)
        except SettingError as error:
            raise ConfigurationError(error.describe(self.flag, raw))


@dataclass(frozen=True, kw_only=True)
class Switch[T]:
    """A flag without a value, which sets *value* when given."""

    flag: str
    value: T
    help: str

    def add_to(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            self.flag, dest=_dest(self.flag), action="store_true", help=self.help
        )

    def given(self, args: argparse.Namespace) -> T | None:
        return self.value if getattr(args, _dest(self.flag)) else None


@dataclass(frozen=True, kw_only=True)
class SwitchPair:
    """``--on`` and ``--off``, mutually exclusive; given neither, nothing changes."""

    on: str
    off: str
    dest: str
    help_on: str
    help_off: str

    def add_to(self, parser: argparse.ArgumentParser) -> None:
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            self.on,
            dest=self.dest,
            action="store_true",
            default=None,
            help=self.help_on,
        )
        group.add_argument(
            self.off,
            dest=self.dest,
            action="store_false",
            default=None,
            help=self.help_off,
        )

    def given(self, args: argparse.Namespace) -> bool | None:
        return getattr(args, self.dest)


@dataclass(frozen=True, kw_only=True)
class Setting[T]:
    """One setting: every way it can be given, and where its value lands.

    Both loaders iterate ``SETTINGS``, and ``build_parser()`` generates the
    command line from it, so a setting is declared once. The environment phase
    converts only; range checks stay in ``AppConfig.validate()``, which runs
    after both, so ``TIMEOUT=0`` can still be repaired by ``--timeout 10``.
    """

    apply: Callable[[AppConfig, T], None]
    env: Env[T] | None = None
    cli: Option[T] | Switch[T] | SwitchPair | None = None


# In the order ``--help`` lists them.
SETTINGS: tuple[Setting, ...] = (
    Setting(
        env=Env(EnvironmentKeys.HEADLESS, _flag, blank="unset"),
        # Only the negative direction exists on the command line.
        cli=Switch(
            flag="--no-headless",
            value=False,
            help="Run browser with a visible window (useful for login and debugging)",
        ),
        apply=_browser("headless"),
    ),
    Setting(
        env=Env(EnvironmentKeys.LOG_LEVEL, _log_level),
        cli=Option(
            flag="--log-level",
            parse=str,
            cli_empty="parse",
            choices=LOG_LEVELS,
            help="Set logging level (default: WARNING)",
        ),
        apply=_server("log_level"),
    ),
    Setting(
        env=Env(EnvironmentKeys.TRANSPORT, _transport),
        cli=Option(
            flag="--transport",
            parse=str,
            cli_empty="parse",
            choices=TRANSPORTS,
            help="Specify the transport mode (stdio or streamable-http)",
        ),
        apply=_set_transport,
    ),
    Setting(
        env=Env(EnvironmentKeys.HOST, _text),
        cli=Option(
            flag="--host",
            parse=str,
            cli_empty="ignore",
            help="HTTP server host (default: 127.0.0.1)",
        ),
        apply=_server("host"),
    ),
    Setting(
        # Range checked in AppConfig.validate(), so --port 0 is refused there.
        env=Env(EnvironmentKeys.PORT, _integer),
        cli=Option(
            flag="--port",
            parse=int,
            cli_empty="parse",
            help="HTTP server port (default: 8000)",
        ),
        apply=_server("port"),
    ),
    Setting(
        env=Env(EnvironmentKeys.HTTP_PATH, _text),
        cli=Option(
            flag="--path",
            parse=str,
            cli_empty="ignore",
            help="HTTP server path (default: /mcp)",
        ),
        apply=_server("path"),
    ),
    Setting(
        # Judged in AppConfig.validate(), after the file has had its chance
        # to replace the variable, and only for HTTP, which is what uses it.
        # Kept out of the config's repr. Only the file is an option, so the
        # token stays out of the process list. The file wins over the
        # variable, as every option does.
        env=Env(EnvironmentKeys.LINKEDIN_MCP_HTTP_TOKEN, _text),
        cli=Option(
            flag="--http-token-file",
            parse=_http_token_file,
            phase="load",
            cli_empty="ignore",
            metavar="PATH",
            help=(
                "Require 'Authorization: Bearer <token>' from HTTP clients, with "
                "the token read from this file (one trailing newline is ignored; "
                "at least 32 characters). On macOS and Linux a file with group or "
                "other access is refused (chmod 600); Windows does not check. "
                "Overrides LINKEDIN_MCP_HTTP_TOKEN. Needed for a --host other "
                "than loopback"
            ),
        ),
        apply=_server("http_token"),
    ),
    Setting(
        # Environment only: serving beyond loopback without a token is a
        # deliberate, unsafe choice, checked in AppConfig.validate().
        env=Env(
            EnvironmentKeys.LINKEDIN_MCP_ALLOW_UNAUTHENTICATED_HTTP,
            _flag,
            blank="unset",
        ),
        apply=_server("allow_unauthenticated_http"),
    ),
    Setting(
        env=Env(EnvironmentKeys.SLOW_MO, _integer),
        cli=Option(
            flag="--slow-mo",
            parse=int,
            cli_empty="parse",
            metavar="MS",
            help="Slow down browser actions by N milliseconds (debugging)",
        ),
        apply=_browser("slow_mo"),
    ),
    Setting(
        env=Env(EnvironmentKeys.USER_AGENT, _user_agent),
        # Still accepted by the parser so using it produces the explanation
        # above rather than argparse's bare "unrecognized arguments".
        cli=Option(
            flag="--user-agent",
            parse=_user_agent,
            phase="load",
            cli_empty="ignore",
            help=argparse.SUPPRESS,
        ),
        apply=_never_applied,
    ),
    Setting(
        env=Env(EnvironmentKeys.VIEWPORT, _viewport),
        cli=Option(
            flag="--viewport",
            parse=_viewport,
            phase="load",
            cli_empty="ignore",
            metavar="WxH",
            help=(
                "Browser viewport size (default: 1280x720). Applies to the normal "
                "windowless mode only; a headed launch uses the real window size."
            ),
        ),
        apply=_set_viewport,
    ),
    Setting(
        env=Env(EnvironmentKeys.TIMEOUT, _integer),
        cli=Option(
            flag="--timeout",
            parse=positive_int,
            cli_empty="parse",
            metavar="MS",
            help="Browser timeout for page operations in milliseconds (default: 5000)",
        ),
        apply=_browser("default_timeout"),
    ),
    Setting(
        env=Env(EnvironmentKeys.TOOL_TIMEOUT, _finite(positive=True)),
        cli=Option(
            flag="--tool-timeout",
            parse=positive_float,
            cli_empty="parse",
            metavar="SECONDS",
            help="Per-tool MCP execution timeout in seconds (default: 180.0)",
        ),
        apply=_server("tool_timeout_seconds"),
    ),
    Setting(
        env=Env(
            EnvironmentKeys.LOGIN_TIMEOUT,
            _finite(positive=False, note=" (0 = no limit)"),
        ),
        cli=Option(
            flag="--login-timeout",
            parse=non_negative_float,
            cli_empty="parse",
            metavar="SECONDS",
            help="Manual login wait timeout in seconds (default: 1800; 0 = no limit)",
        ),
        apply=_browser("login_timeout_seconds"),
    ),
    Setting(
        # Clamped in BrowserConfig.validate(), as the three below are.
        env=Env(
            EnvironmentKeys.LOGIN_INLINE_WAIT,
            _finite(positive=False, note=" (0 = no inline wait)"),
        ),
        cli=Option(
            flag="--login-inline-wait",
            parse=non_negative_float,
            cli_empty="parse",
            metavar="SECONDS",
            help=(
                "Bounded inline wait for a tool call to resume after login completes, "
                "in seconds (default: 25, max 45; 0 = return immediately)"
            ),
        ),
        apply=_browser("login_inline_wait_seconds"),
    ),
    Setting(
        env=Env(EnvironmentKeys.BROWSER_WAIT, _finite(positive=False)),
        cli=Option(
            flag="--browser-wait",
            parse=non_negative_float,
            cli_empty="parse",
            metavar="SECONDS",
            help=(
                "How long to wait for another server process to hand over the shared "
                "browser, in seconds (default: 25, max 45; 0 = report busy at once)"
            ),
        ),
        apply=_browser("browser_wait_seconds"),
    ),
    Setting(
        env=Env(EnvironmentKeys.BROWSER_MIN_HOLD, _finite(positive=False)),
        cli=Option(
            flag="--browser-min-hold",
            parse=non_negative_float,
            cli_empty="parse",
            metavar="SECONDS",
            help=(
                "Shortest time this process keeps the shared browser before honouring "
                "a handoff request, in seconds (default: 20, clamped below "
                "--browser-wait; 0 = hand over after every tool call)"
            ),
        ),
        apply=_browser("browser_min_hold_seconds"),
    ),
    Setting(
        env=Env(EnvironmentKeys.BROWSER_IDLE_TIMEOUT, _finite(positive=False)),
        cli=Option(
            flag="--browser-idle-timeout",
            parse=non_negative_float,
            cli_empty="parse",
            metavar="SECONDS",
            help=(
                "Close an idle browser and release the shared profile after this many "
                "seconds without a tool call (default: 600; 0 = keep it open)"
            ),
        ),
        apply=_browser("browser_idle_timeout_seconds"),
    ),
    Setting(
        env=Env(EnvironmentKeys.CHROME_PATH, _text),
        cli=Option(
            flag="--chrome-path",
            parse=str,
            cli_empty="ignore",
            metavar="PATH",
            help="Path to Chrome/Chromium executable (for custom browser installations)",
        ),
        apply=_browser("chrome_path"),
    ),
    Setting(
        # Validated and split in BrowserConfig.validate(). Unlike the CLI flag,
        # PROXY_SERVER may carry the credentials a provider hands out: the
        # environment is not world-readable the way a process argument list is.
        env=Env(EnvironmentKeys.PROXY_SERVER, _text),
        cli=Option(
            flag="--proxy-server",
            parse=credential_free_url,
            cli_empty="ignore",
            metavar="URL",
            help=(
                "Route the browser through a proxy, as scheme://host:port "
                "(http, https, socks4 or socks5). Chromium cannot authenticate to "
                "a SOCKS proxy, so credentials require an http(s) endpoint"
            ),
        ),
        apply=_browser("proxy_server"),
    ),
    Setting(
        env=Env(EnvironmentKeys.PROXY_USERNAME, _text),
        cli=Option(
            flag="--proxy-username",
            parse=str,
            cli_empty="ignore",
            metavar="USER",
            help=(
                "Username for the proxy. Visible in the process list like any "
                "argument, which is acceptable because it grants nothing without "
                "the password; that one has no flag on purpose, set PROXY_PASSWORD"
            ),
        ),
        apply=_browser("proxy_username"),
    ),
    Setting(
        env=Env(EnvironmentKeys.PROXY_BYPASS, _text),
        cli=Option(
            flag="--proxy-bypass",
            parse=str,
            cli_empty="ignore",
            metavar="HOSTS",
            help="Comma-separated hosts to reach directly instead of via the proxy",
        ),
        apply=_browser("proxy_bypass"),
    ),
    # Environment only, never stripped: credential_free_url() says why there is
    # no flag, and _env() why a space at either end stays.
    Setting(
        env=Env(EnvironmentKeys.PROXY_PASSWORD, _text),
        apply=_browser("proxy_password"),
    ),
    Setting(
        cli=Switch(
            flag="--login",
            value=True,
            help="Login interactively via browser and save persistent profile",
        ),
        apply=_server("login"),
    ),
    Setting(
        cli=Switch(
            flag="--login-viewer",
            value=True,
            help=(
                "Expose the --login browser at an authenticated noVNC URL in Docker "
                "(requires --login and -p 127.0.0.1:6080:6080)"
            ),
        ),
        apply=_server("login_viewer"),
    ),
    Setting(
        cli=Switch(
            flag="--status",
            value=True,
            help="Check if current session is valid and exit",
        ),
        apply=_server("status"),
    ),
    Setting(
        cli=Switch(
            flag="--logout",
            value=True,
            help="Clear stored LinkedIn browser profile",
        ),
        apply=_server("logout"),
    ),
    Setting(
        env=Env(EnvironmentKeys.USER_DATA_DIR, _text),
        cli=Option(
            flag="--user-data-dir",
            parse=str,
            cli_empty="ignore",
            metavar="PATH",
            help="Path to persistent browser profile directory (default: ~/.linkedin-mcp/profile)",
        ),
        apply=_browser("user_data_dir"),
    ),
    Setting(
        env=Env(EnvironmentKeys.INSTALLER_TEMP_DIR, _text),
        cli=Option(
            flag="--installer-temp-dir",
            parse=str,
            cli_empty="ignore",
            metavar="PATH",
            help="Path to temporary parent directory for browser installation bootstrap",
        ),
        apply=_browser("installer_temp_dir"),
    ),
    Setting(
        cli=Switch(
            flag="--claim-profile-root",
            value=True,
            help=(
                "Take over a non-default profile directory that this server will "
                "not claim on its own: one whose parent already holds other files, "
                "or one carrying an ownership marker written for a different path. "
                "Needed once; this server moves and deletes that whole parent when "
                "it rotates or clears a session"
            ),
        ),
        apply=_server("claim_profile_root"),
    ),
    Setting(
        # Validated in ServerConfig.validate(). Blank means "auto" in both
        # places it can be blank: whitespace in the variable (an empty variable
        # is unset) and an explicit --import-from-browser="".
        env=Env(EnvironmentKeys.IMPORT_FROM_BROWSER, _browser_to_import),
        cli=Option(
            flag="--import-from-browser",
            parse=_browser_to_import,
            phase="load",
            cli_empty="auto",
            const="auto",
            metavar="BROWSER",
            help=(
                "Import a LinkedIn session from a locally logged-in Chromium browser "
                "(chrome, chromium, brave, edge, arc, vivaldi, helium, yandex, whale, "
                "coccoc, opera, opera_gx, or auto). Bare flag = auto (most recently "
                "used live session). On macOS the OS keychain may prompt for access "
                "to the browser's Safe Storage."
            ),
        ),
        apply=_server("import_from_browser"),
    ),
    Setting(
        # Auto-import a session from a logged-in browser on first no-session
        # tool call. Unset = on by default (interactive and non-interactive
        # desktop); false disables it. No effect under Docker or a non-loopback
        # HTTP bind.
        env=Env(EnvironmentKeys.AUTO_IMPORT_FROM_BROWSER, _flag, blank="unset"),
        cli=SwitchPair(
            on="--auto-import",
            off="--no-auto-import",
            dest="auto_import",
            help_on=(
                "Auto-import a session from a locally logged-in browser on first "
                "use (the default). Provided for explicitness; it cannot override "
                "the Docker or non-loopback-HTTP gates."
            ),
            help_off=(
                "Disable auto-import of a session from a browser on first use; "
                "require --login or --import-from-browser instead."
            ),
        ),
        apply=_browser("auto_import_from_browser"),
    ),
    Setting(
        # Share one browser-owning process across stdio clients.
        env=Env(EnvironmentKeys.DAEMON_ENABLED, _flag, blank="unset"),
        cli=SwitchPair(
            on="--daemon",
            off="--no-daemon",
            dest="daemon_enabled",
            help_on=(
                "Serve every stdio client from one browser-owning process instead "
                "of giving each its own (experimental)."
            ),
            help_off="Give every stdio client its own browser (default; overrides DAEMON_ENABLED=true).",
        ),
        apply=_server("daemon_enabled"),
    ),
    Setting(
        # debug_trace reads it from the environment for each run.
        env=Env(EnvironmentKeys.LINKEDIN_TRACE_MODE, _trace_mode, blank="unset"),
        apply=_read_where_used,
    ),
    Setting(
        # update_check reads it from the environment before asking PyPI.
        env=Env(EnvironmentKeys.LINKEDIN_MCP_CHECK_FOR_UPDATES, _flag, blank="unset"),
        apply=_read_where_used,
    ),
)

# Accepted and ignored. They once chose when full Chromium was installed;
# there is one browser now, and the README documented them, so an existing
# command line keeps working. Hidden from help so nobody adopts them.
_RETIRED_OPTIONS = ("--eager-full-chromium", "--no-eager-full-chromium")

#: The parser's options that belong to no setting.
IGNORED_OPTIONS = ("--help", *_RETIRED_OPTIONS)


def is_interactive_environment() -> bool:
    """
    Detect if running in an interactive environment (TTY).

    Returns:
        True if both stdin and stdout are TTY devices
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, OSError):
        return False


def _warn_about_placeholders() -> None:
    """Name the variables ``_env()`` drops as unsubstituted placeholders.

    That is a fail-open path and this says so: a host that fails to substitute
    a field the user *did* fill in skips the proxy, and the browser then goes
    out on the real address. So the variables are named in the log once. They
    hold placeholders, so naming them leaks nothing.
    """
    if unsubstituted := [
        key
        for key, literal in _MCPB_PLACEHOLDERS.items()
        if os.environ.get(key) == literal
    ]:
        logger.warning(
            "Ignoring %s: the value is an unsubstituted MCPB placeholder, not "
            "a setting, so nothing is configured from these. Update the "
            "extension bundle, and clear the variable from any environment "
            "override set by hand.",
            ", ".join(unsubstituted),
        )


def load_from_env(config: AppConfig) -> AppConfig:
    """Load configuration from environment variables."""
    _warn_about_placeholders()
    for setting in SETTINGS:
        if setting.env is None or (raw := _env(setting.env.key)) is None:
            continue
        if setting.env.blank == "unset" and not raw.strip():
            continue
        try:
            value = setting.env.parse(raw)
        except SettingError as error:
            raise ConfigurationError(error.describe(setting.env.key, raw))
        setting.apply(config, value)
    return config


def build_parser() -> argparse.ArgumentParser:
    """The command-line parser, generated from ``SETTINGS``.

    Every long option added here also belongs in
    ``remote_catalog.LONG_OPTIONS``, which resolves abbreviations before this
    parser is imported; a test compares the two.
    """
    parser = argparse.ArgumentParser(
        description="LinkedIn MCP Server - A Model Context Protocol server for LinkedIn integration"
    )
    for setting in SETTINGS:
        if setting.cli is not None:
            setting.cli.add_to(parser)
    for retired in _RETIRED_OPTIONS:
        parser.add_argument(
            retired, action="store_true", dest="_retired", help=argparse.SUPPRESS
        )
    return parser


def load_from_args(config: AppConfig, argv: Sequence[str]) -> AppConfig:
    """Load configuration from command line arguments.

    An option applies whenever it was given, including as 0 or as a ``--no-``
    switch. What an explicit empty value means is the option's ``cli_empty``.
    """
    args = build_parser().parse_args(argv)
    for setting in SETTINGS:
        if setting.cli is not None and (value := setting.cli.given(args)) is not None:
            setting.apply(config, value)
    return config


def load_config(argv: Sequence[str] | None = None) -> AppConfig:
    """
    Load configuration with clear precedence order.

    Explicit command line arguments have highest priority, followed by
    environment variables and defaults. Without *argv*, only environment
    variables and defaults are loaded.

    Returns:
        Fully configured application settings
    """
    # Start with default configuration
    config = AppConfig()

    # Set interactive mode
    config.is_interactive = is_interactive_environment()
    logger.debug(f"Interactive mode: {config.is_interactive}")

    # Override with environment variables
    config = load_from_env(config)

    # Override with explicit command line arguments (highest priority)
    if argv is not None:
        config = load_from_args(config, argv)

    # Validate final configuration
    config.validate()

    return config
