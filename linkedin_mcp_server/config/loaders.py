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
    USER_DATA_DIR = "USER_DATA_DIR"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    LOGIN_TIMEOUT = "LOGIN_TIMEOUT"
    LOGIN_INLINE_WAIT = "LOGIN_INLINE_WAIT"
    IMPORT_FROM_BROWSER = "IMPORT_FROM_BROWSER"
    AUTO_IMPORT_FROM_BROWSER = "AUTO_IMPORT_FROM_BROWSER"
    EAGER_FULL_CHROMIUM = "EAGER_FULL_CHROMIUM"


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

    # Custom user agent
    if user_agent_env := os.environ.get(EnvironmentKeys.USER_AGENT):
        config.browser.user_agent = user_agent_env

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

    parser.add_argument(
        "--user-agent",
        type=str,
        default=None,
        help="Custom browser user agent",
    )

    parser.add_argument(
        "--viewport",
        type=str,
        default=None,
        metavar="WxH",
        help="Browser viewport size (default: 1280x720)",
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
        "--chrome-path",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to Chrome/Chromium executable (for custom browser installations)",
    )

    # Session management
    parser.add_argument(
        "--login",
        action="store_true",
        help="Login interactively via browser and save persistent profile",
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

    eager_full_group = parser.add_mutually_exclusive_group()
    eager_full_group.add_argument(
        "--eager-full-chromium",
        dest="eager_full_chromium",
        action="store_true",
        default=None,
        help=(
            "Install full Chrome for Testing up front during browser setup "
            "instead of lazily on the first headed login (pre-warms the headed "
            "login fallback at the cost of a larger initial download)"
        ),
    )
    eager_full_group.add_argument(
        "--no-eager-full-chromium",
        dest="eager_full_chromium",
        action="store_false",
        default=None,
        help=(
            "Install full Chrome for Testing lazily on the first headed login "
            "(default; overrides EAGER_FULL_CHROMIUM=true)."
        ),
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
        config.browser.user_agent = args.user_agent

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

    if args.chrome_path:
        config.browser.chrome_path = args.chrome_path

    # Session management
    if args.login:
        config.server.login = True

    if args.status:
        config.server.status = True

    if args.logout:
        config.server.logout = True

    if args.user_data_dir:
        config.browser.user_data_dir = args.user_data_dir

    if args.import_from_browser is not None:
        value = args.import_from_browser.strip().lower()
        config.server.import_from_browser = value or "auto"

    if args.auto_import is not None:
        config.browser.auto_import_from_browser = args.auto_import

    if args.eager_full_chromium is not None:
        config.browser.eager_full_chromium = args.eager_full_chromium

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
