"""
Configuration loading and argument parsing for LinkedIn MCP Server.

Loads settings from CLI arguments and environment variables.
"""

import argparse
import logging
import math
import os
import sys
from typing import Literal, cast
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
# The second sentence about a running server is not padding. A shared owner
# started by an older version carries its user agent in the configuration
# fingerprint, and a client of this version can only compute ``None`` because
# the setting is refused here. ``daemon.py`` rejects a fingerprint mismatch
# before it ever compares package versions, so that owner can no longer be
# asked to stand down: it keeps running and keeps the profile. Removing the
# setting is therefore only half the fix, and the other half is invisible
# unless this message says it.
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
    EAGER_FULL_CHROMIUM = "EAGER_FULL_CHROMIUM"
    DAEMON_ENABLED = "DAEMON_ENABLED"
    # Company-cache TTLs, in days. Firmographics move on the order of years, so
    # a long default is safe; open roles are the volatile signal, so a short one.
    COMPANY_FIRMOGRAPHICS_TTL_DAYS = "COMPANY_FIRMOGRAPHICS_TTL_DAYS"
    COMPANY_JOBS_TTL_DAYS = "COMPANY_JOBS_TTL_DAYS"


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


def load_from_env(config: AppConfig) -> AppConfig:
    """Load configuration from environment variables."""

    # Log level
    if log_level_env := os.environ.get(EnvironmentKeys.LOG_LEVEL):
        log_level_upper = log_level_env.strip().upper()
        if log_level_upper in ("DEBUG", "INFO", "WARNING", "ERROR"):
            config.server.log_level = cast(
                Literal["DEBUG", "INFO", "WARNING", "ERROR"], log_level_upper
            )

    # Headless mode
    if headless_env := os.environ.get(EnvironmentKeys.HEADLESS):
        headless_value = _normalize_env(headless_env)
        if headless_value in FALSY_VALUES:
            config.browser.headless = False
        elif headless_value in TRUTHY_VALUES:
            config.browser.headless = True

    # Transport mode
    if transport_env := os.environ.get(EnvironmentKeys.TRANSPORT):
        config.server.transport_explicitly_set = True
        transport_value = _normalize_env(transport_env)
        if transport_value == "stdio":
            config.server.transport = "stdio"
        elif transport_value == "streamable-http":
            config.server.transport = "streamable-http"
        else:
            raise ConfigurationError(
                f"Invalid TRANSPORT: '{transport_env}'. Must be 'stdio' or 'streamable-http'."
            )

    # Persistent browser profile directory
    if user_data_dir := os.environ.get(EnvironmentKeys.USER_DATA_DIR):
        config.browser.user_data_dir = user_data_dir

    # Timeout for page operations (validated in BrowserConfig.validate())
    if timeout_env := os.environ.get(EnvironmentKeys.TIMEOUT):
        try:
            config.browser.default_timeout = int(timeout_env)
        except ValueError:
            raise ConfigurationError(
                f"Invalid TIMEOUT: '{timeout_env}'. Must be an integer."
            )

    # Per-tool MCP execution timeout in seconds (also validated in ServerConfig.validate())
    if tool_timeout_env := os.environ.get(EnvironmentKeys.TOOL_TIMEOUT):
        try:
            tool_timeout_value = float(tool_timeout_env)
        except ValueError:
            raise ConfigurationError(
                f"Invalid TOOL_TIMEOUT: '{tool_timeout_env}'. Must be a number."
            )
        if not (math.isfinite(tool_timeout_value) and tool_timeout_value > 0):
            raise ConfigurationError(
                f"Invalid TOOL_TIMEOUT: '{tool_timeout_env}'. Must be a positive finite number."
            )
        config.server.tool_timeout_seconds = tool_timeout_value

    # Manual-login wait timeout in seconds; 0 = no limit (validated in
    # BrowserConfig.validate())
    if login_timeout_env := os.environ.get(EnvironmentKeys.LOGIN_TIMEOUT):
        try:
            login_timeout_value = float(login_timeout_env)
        except ValueError:
            raise ConfigurationError(
                f"Invalid LOGIN_TIMEOUT: '{login_timeout_env}'. Must be a number."
            )
        if not (math.isfinite(login_timeout_value) and login_timeout_value >= 0):
            raise ConfigurationError(
                f"Invalid LOGIN_TIMEOUT: '{login_timeout_env}'. Must be a non-negative finite number (0 = no limit)."
            )
        config.browser.login_timeout_seconds = login_timeout_value

    # Bounded inline wait before the pending signal; 0 = immediate return
    # (validated and clamped in BrowserConfig.validate())
    if login_inline_wait_env := os.environ.get(EnvironmentKeys.LOGIN_INLINE_WAIT):
        try:
            login_inline_wait_value = float(login_inline_wait_env)
        except ValueError:
            raise ConfigurationError(
                f"Invalid LOGIN_INLINE_WAIT: '{login_inline_wait_env}'. Must be a number."
            )
        if not (
            math.isfinite(login_inline_wait_value) and login_inline_wait_value >= 0
        ):
            raise ConfigurationError(
                f"Invalid LOGIN_INLINE_WAIT: '{login_inline_wait_env}'. Must be a non-negative finite number (0 = no inline wait)."
            )
        config.browser.login_inline_wait_seconds = login_inline_wait_value

    # Shared-browser coordination between concurrent server processes
    # (validated and clamped in BrowserConfig.validate())
    for env_key, attribute in (
        (EnvironmentKeys.BROWSER_WAIT, "browser_wait_seconds"),
        (EnvironmentKeys.BROWSER_MIN_HOLD, "browser_min_hold_seconds"),
        (EnvironmentKeys.BROWSER_IDLE_TIMEOUT, "browser_idle_timeout_seconds"),
    ):
        raw = os.environ.get(env_key)
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            raise ConfigurationError(f"Invalid {env_key}: '{raw}'. Must be a number.")
        if not (math.isfinite(value) and value >= 0):
            raise ConfigurationError(
                f"Invalid {env_key}: '{raw}'. Must be a non-negative finite number."
            )
        setattr(config.browser, attribute, value)

    if os.environ.get(EnvironmentKeys.USER_AGENT):
        raise ConfigurationError(_USER_AGENT_REMOVED.format(setting="USER_AGENT"))

    # HTTP server host
    if host_env := os.environ.get(EnvironmentKeys.HOST):
        config.server.host = host_env

    # HTTP server port (validated in AppConfig.validate())
    if port_env := os.environ.get(EnvironmentKeys.PORT):
        try:
            config.server.port = int(port_env)
        except ValueError:
            raise ConfigurationError(f"Invalid PORT: '{port_env}'. Must be an integer.")

    # HTTP server path
    if path_env := os.environ.get(EnvironmentKeys.HTTP_PATH):
        config.server.path = path_env

    # Slow motion delay for debugging (validated in BrowserConfig.validate())
    if slow_mo_env := os.environ.get(EnvironmentKeys.SLOW_MO):
        try:
            config.browser.slow_mo = int(slow_mo_env)
        except ValueError:
            raise ConfigurationError(
                f"Invalid SLOW_MO: '{slow_mo_env}'. Must be an integer."
            )

    # Browser viewport (validated in BrowserConfig.validate())
    if viewport_env := os.environ.get(EnvironmentKeys.VIEWPORT):
        try:
            width, height = viewport_env.lower().split("x")
            config.browser.viewport_width = int(width)
            config.browser.viewport_height = int(height)
        except ValueError:
            raise ConfigurationError(
                f"Invalid VIEWPORT: '{viewport_env}'. Must be in format WxH (e.g., 1280x720)."
            )

    # Custom Chrome/Chromium executable path
    if chrome_path_env := os.environ.get(EnvironmentKeys.CHROME_PATH):
        config.browser.chrome_path = chrome_path_env

    # Browser proxy (validated and split in BrowserConfig.validate()). Unlike
    # the CLI flag, PROXY_SERVER may carry the credentials a provider hands out:
    # the environment is not world-readable the way a process argument list is.
    #
    # Read through _env(), which drops a placeholder the host left behind.
    # That is a fail-open path and it says so: a host that fails to substitute
    # a field the user *did* fill in skips the proxy, and the browser then goes
    # out on the real address. So the variables are named in the log once. They
    # hold placeholders, so naming them leaks nothing.
    if unsubstituted := [
        key
        for key, literal in _MCPB_PLACEHOLDERS.items()
        if os.environ.get(key) == literal
    ]:
        logger.warning(
            "Ignoring %s: the value is an unsubstituted MCPB placeholder, not "
            "a setting, so no proxy is configured from these. Update the "
            "extension bundle, and clear the variable from any environment "
            "override set by hand.",
            ", ".join(unsubstituted),
        )

    if proxy_server_env := _env(EnvironmentKeys.PROXY_SERVER):
        config.browser.proxy_server = proxy_server_env

    if proxy_username_env := _env(EnvironmentKeys.PROXY_USERNAME):
        config.browser.proxy_username = proxy_username_env

    if proxy_password_env := _env(EnvironmentKeys.PROXY_PASSWORD):
        config.browser.proxy_password = proxy_password_env

    if proxy_bypass_env := _env(EnvironmentKeys.PROXY_BYPASS):
        config.browser.proxy_bypass = proxy_bypass_env

    # Import a LinkedIn session from a locally logged-in browser (validated in
    # ServerConfig.validate())
    if import_browser_env := os.environ.get(EnvironmentKeys.IMPORT_FROM_BROWSER):
        config.server.import_from_browser = _normalize_env(import_browser_env) or "auto"

    # Auto-import a session from a logged-in browser on first no-session tool
    # call. Unset = on by default (interactive and non-interactive desktop);
    # false disables it. No effect under Docker or a non-loopback HTTP bind.
    if auto_import_env := os.environ.get(EnvironmentKeys.AUTO_IMPORT_FROM_BROWSER):
        auto_import_value = _normalize_env(auto_import_env)
        if auto_import_value in FALSY_VALUES:
            config.browser.auto_import_from_browser = False
        elif auto_import_value in TRUTHY_VALUES:
            config.browser.auto_import_from_browser = True

    # Install full chromium up front instead of lazily on the first headed login.
    if eager_full_env := os.environ.get(EnvironmentKeys.EAGER_FULL_CHROMIUM):
        eager_full_value = _normalize_env(eager_full_env)
        if eager_full_value in FALSY_VALUES:
            config.browser.eager_full_chromium = False
        elif eager_full_value in TRUTHY_VALUES:
            config.browser.eager_full_chromium = True

    # Share one browser-owning process across stdio clients.
    if daemon_env := os.environ.get(EnvironmentKeys.DAEMON_ENABLED):
        daemon_value = _normalize_env(daemon_env)
        if daemon_value in FALSY_VALUES:
            config.server.daemon_enabled = False
        elif daemon_value in TRUTHY_VALUES:
            config.server.daemon_enabled = True

    return config


def load_from_args(config: AppConfig) -> AppConfig:
    """Load configuration from command line arguments."""
    parser = argparse.ArgumentParser(
        description="LinkedIn MCP Server - A Model Context Protocol server for LinkedIn integration"
    )

    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser with a visible window (useful for login and debugging)",
    )

    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Set logging level (default: WARNING)",
    )

    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=None,
        help="Specify the transport mode (stdio or streamable-http)",
    )

    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="HTTP server host (default: 127.0.0.1)",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP server port (default: 8000)",
    )

    parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="HTTP server path (default: /mcp)",
    )

    # Browser configuration
    parser.add_argument(
        "--slow-mo",
        type=int,
        default=0,
        metavar="MS",
        help="Slow down browser actions by N milliseconds (debugging)",
    )

    # Still accepted by the parser so using it produces the explanation above
    # rather than argparse's bare "unrecognized arguments".
    parser.add_argument(
        "--user-agent",
        type=str,
        default=None,
        help=argparse.SUPPRESS,
    )

    parser.add_argument(
        "--viewport",
        type=str,
        default=None,
        metavar="WxH",
        help=(
            "Browser viewport size (default: 1280x720). Applies to the normal "
            "windowless mode only; a headed launch uses the real window size."
        ),
    )

    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=None,
        metavar="MS",
        help="Browser timeout for page operations in milliseconds (default: 5000)",
    )

    parser.add_argument(
        "--tool-timeout",
        type=positive_float,
        default=None,
        metavar="SECONDS",
        help="Per-tool MCP execution timeout in seconds (default: 180.0)",
    )

    parser.add_argument(
        "--login-timeout",
        type=non_negative_float,
        default=None,
        metavar="SECONDS",
        help="Manual login wait timeout in seconds (default: 1800; 0 = no limit)",
    )

    parser.add_argument(
        "--login-inline-wait",
        type=non_negative_float,
        default=None,
        metavar="SECONDS",
        help=(
            "Bounded inline wait for a tool call to resume after login completes, "
            "in seconds (default: 25, max 45; 0 = return immediately)"
        ),
    )
    parser.add_argument(
        "--browser-wait",
        type=non_negative_float,
        default=None,
        metavar="SECONDS",
        help=(
            "How long to wait for another server process to hand over the shared "
            "browser, in seconds (default: 25, max 45; 0 = report busy at once)"
        ),
    )
    parser.add_argument(
        "--browser-min-hold",
        type=non_negative_float,
        default=None,
        metavar="SECONDS",
        help=(
            "Shortest time this process keeps the shared browser before honouring "
            "a handoff request, in seconds (default: 20, clamped below "
            "--browser-wait; 0 = hand over after every tool call)"
        ),
    )
    parser.add_argument(
        "--browser-idle-timeout",
        type=non_negative_float,
        default=None,
        metavar="SECONDS",
        help=(
            "Close an idle browser and release the shared profile after this many "
            "seconds without a tool call (default: 600; 0 = keep it open)"
        ),
    )

    parser.add_argument(
        "--chrome-path",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to Chrome/Chromium executable (for custom browser installations)",
    )

    parser.add_argument(
        "--proxy-server",
        type=credential_free_url,
        default=None,
        metavar="URL",
        help=(
            "Route the browser through a proxy, as scheme://host:port "
            "(http, https, socks4 or socks5). Chromium cannot authenticate to "
            "a SOCKS proxy, so credentials require an http(s) endpoint"
        ),
    )

    parser.add_argument(
        "--proxy-username",
        type=str,
        default=None,
        metavar="USER",
        help=(
            "Username for the proxy. Visible in the process list like any "
            "argument, which is acceptable because it grants nothing without "
            "the password; that one has no flag on purpose, set PROXY_PASSWORD"
        ),
    )

    parser.add_argument(
        "--proxy-bypass",
        type=str,
        default=None,
        metavar="HOSTS",
        help="Comma-separated hosts to reach directly instead of via the proxy",
    )

    # Session management
    parser.add_argument(
        "--login",
        action="store_true",
        help="Login interactively via browser and save persistent profile",
    )

    parser.add_argument(
        "--login-viewer",
        action="store_true",
        help=(
            "Expose the --login browser at an authenticated noVNC URL in Docker "
            "(requires --login and -p 127.0.0.1:6080:6080)"
        ),
    )

    parser.add_argument(
        "--status",
        action="store_true",
        help="Check if current session is valid and exit",
    )

    parser.add_argument(
        "--logout",
        action="store_true",
        help="Clear stored LinkedIn browser profile",
    )

    parser.add_argument(
        "--user-data-dir",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to persistent browser profile directory (default: ~/.linkedin-mcp/profile)",
    )

    parser.add_argument(
        "--claim-profile-root",
        action="store_true",
        help=(
            "Take over a non-default profile directory that this server will "
            "not claim on its own: one whose parent already holds other files, "
            "or one carrying an ownership marker written for a different path. "
            "Needed once; this server moves and deletes that whole parent when "
            "it rotates or clears a session"
        ),
    )

    parser.add_argument(
        "--import-from-browser",
        nargs="?",
        const="auto",
        default=None,
        metavar="BROWSER",
        help=(
            "Import a LinkedIn session from a locally logged-in Chromium browser "
            "(chrome, chromium, brave, edge, arc, vivaldi, helium, yandex, whale, "
            "coccoc, opera, opera_gx, or auto). Bare flag = auto (most recently "
            "used live session). On macOS the OS keychain may prompt for access "
            "to the browser's Safe Storage."
        ),
    )

    auto_import_group = parser.add_mutually_exclusive_group()
    auto_import_group.add_argument(
        "--auto-import",
        dest="auto_import",
        action="store_true",
        default=None,
        help=(
            "Auto-import a session from a locally logged-in browser on first "
            "use (the default). Provided for explicitness; it cannot override "
            "the Docker or non-loopback-HTTP gates."
        ),
    )
    auto_import_group.add_argument(
        "--no-auto-import",
        dest="auto_import",
        action="store_false",
        default=None,
        help=(
            "Disable auto-import of a session from a browser on first use; "
            "require --login or --import-from-browser instead."
        ),
    )

    # Accepted and inert. There is one browser now, so "up front" and "lazily"
    # describe the same install. Kept rather than removed so an existing command
    # line or compose file does not stop working over a setting that no longer
    # decides anything; hidden from help so nobody adopts it.
    eager_full_group = parser.add_mutually_exclusive_group()
    eager_full_group.add_argument(
        "--eager-full-chromium",
        dest="eager_full_chromium",
        action="store_true",
        default=None,
        help=argparse.SUPPRESS,
    )
    eager_full_group.add_argument(
        "--no-eager-full-chromium",
        dest="eager_full_chromium",
        action="store_false",
        default=None,
        help=argparse.SUPPRESS,
    )

    daemon_group = parser.add_mutually_exclusive_group()
    daemon_group.add_argument(
        "--daemon",
        dest="daemon_enabled",
        action="store_true",
        default=None,
        help=(
            "Serve every stdio client from one browser-owning process instead "
            "of giving each its own (experimental)."
        ),
    )
    daemon_group.add_argument(
        "--no-daemon",
        dest="daemon_enabled",
        action="store_false",
        default=None,
        help="Give every stdio client its own browser (default; overrides DAEMON_ENABLED=true).",
    )

    args = parser.parse_args()

    # Update configuration with parsed arguments
    if args.no_headless:
        config.browser.headless = False

    if args.log_level:
        config.server.log_level = args.log_level

    if args.transport:
        config.server.transport = args.transport
        config.server.transport_explicitly_set = True

    if args.host:
        config.server.host = args.host

    if args.port:
        config.server.port = args.port

    if args.path:
        config.server.path = args.path

    # Browser configuration
    if args.slow_mo:
        config.browser.slow_mo = args.slow_mo

    if args.user_agent:
        raise ConfigurationError(_USER_AGENT_REMOVED.format(setting="--user-agent"))

    # Viewport (validated in BrowserConfig.validate())
    if args.viewport:
        try:
            width, height = args.viewport.lower().split("x")
            config.browser.viewport_width = int(width)
            config.browser.viewport_height = int(height)
        except ValueError:
            raise ConfigurationError(
                f"Invalid --viewport: '{args.viewport}'. Must be in format WxH (e.g., 1280x720)."
            )

    if args.timeout is not None:
        config.browser.default_timeout = args.timeout

    if args.tool_timeout is not None:
        config.server.tool_timeout_seconds = args.tool_timeout

    if args.login_timeout is not None:
        config.browser.login_timeout_seconds = args.login_timeout

    if args.login_inline_wait is not None:
        config.browser.login_inline_wait_seconds = args.login_inline_wait

    if args.browser_wait is not None:
        config.browser.browser_wait_seconds = args.browser_wait

    if args.browser_min_hold is not None:
        config.browser.browser_min_hold_seconds = args.browser_min_hold

    if args.browser_idle_timeout is not None:
        config.browser.browser_idle_timeout_seconds = args.browser_idle_timeout

    if args.chrome_path:
        config.browser.chrome_path = args.chrome_path

    # Proxy (validated and normalized in BrowserConfig.validate())
    if args.proxy_server:
        config.browser.proxy_server = args.proxy_server

    if args.proxy_username:
        config.browser.proxy_username = args.proxy_username

    if args.proxy_bypass:
        config.browser.proxy_bypass = args.proxy_bypass

    # Session management
    if args.login:
        config.server.login = True

    if args.login_viewer:
        config.server.login_viewer = True

    if args.status:
        config.server.status = True

    if args.logout:
        config.server.logout = True

    if args.user_data_dir:
        config.browser.user_data_dir = args.user_data_dir

    if args.claim_profile_root:
        config.server.claim_profile_root = True

    if args.import_from_browser is not None:
        value = args.import_from_browser.strip().lower()
        config.server.import_from_browser = value or "auto"

    if args.auto_import is not None:
        config.browser.auto_import_from_browser = args.auto_import

    if args.eager_full_chromium is not None:
        config.browser.eager_full_chromium = args.eager_full_chromium

    if args.daemon_enabled is not None:
        config.server.daemon_enabled = args.daemon_enabled

    return config


def load_config() -> AppConfig:
    """
    Load configuration with clear precedence order.

    Configuration is loaded in the following priority order:
    1. Command line arguments (highest priority)
    2. Environment variables
    3. Defaults (lowest priority)

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

    # Override with command line arguments (highest priority)
    config = load_from_args(config)

    # Validate final configuration
    config.validate()

    return config
