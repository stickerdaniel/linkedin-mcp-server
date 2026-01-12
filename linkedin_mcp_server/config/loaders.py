"""
Configuration loading and argument parsing for LinkedIn MCP Server.

Loads settings from CLI arguments and environment variables.
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from .schema import AppConfig

# Load .env file if present
load_dotenv()

logger = logging.getLogger(__name__)

# Boolean value mappings for environment variable parsing
TRUTHY_VALUES = ("1", "true", "True", "yes", "Yes")
FALSY_VALUES = ("0", "false", "False", "no", "No")


class EnvironmentKeys:
    """Environment variable names used by the application."""

    HEADLESS = "HEADLESS"
    LOG_LEVEL = "LOG_LEVEL"
    TRANSPORT = "TRANSPORT"
    LINKEDIN_COOKIE = "LINKEDIN_COOKIE"
    DEFAULT_TIMEOUT = "DEFAULT_TIMEOUT"


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
        log_level_upper = log_level_env.upper()
        if log_level_upper in ("DEBUG", "INFO", "WARNING", "ERROR"):
            config.server.log_level = log_level_upper

    # Headless mode
    if os.environ.get(EnvironmentKeys.HEADLESS) in FALSY_VALUES:
        config.browser.headless = False
    elif os.environ.get(EnvironmentKeys.HEADLESS) in TRUTHY_VALUES:
        config.browser.headless = True

    # Transport mode
    if transport_env := os.environ.get(EnvironmentKeys.TRANSPORT):
        config.server.transport_explicitly_set = True
        if transport_env == "stdio":
            config.server.transport = "stdio"
        elif transport_env == "streamable-http":
            config.server.transport = "streamable-http"

    # LinkedIn cookie for headless auth
    if cookie := os.environ.get(EnvironmentKeys.LINKEDIN_COOKIE):
        config.server.linkedin_cookie = cookie

    # Default timeout for page operations
    if timeout_env := os.environ.get(EnvironmentKeys.DEFAULT_TIMEOUT):
        try:
            timeout_ms = int(timeout_env)
            if timeout_ms > 0:
                config.browser.default_timeout = timeout_ms
            else:
                logger.warning(f"Invalid timeout: {timeout_env}, must be positive")
        except ValueError:
            logger.warning(f"Invalid timeout value: {timeout_env}, using default")

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
        default="1280x720",
        metavar="WxH",
        help="Browser viewport size (default: 1280x720)",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        metavar="MS",
        help="Browser timeout for page operations in milliseconds (default: 5000)",
    )

    # Session management
    parser.add_argument(
        "--get-session",
        nargs="?",
        const="~/.linkedin-mcp/session.json",
        default=None,
        metavar="PATH",
        help="Login interactively and save session (default: ~/.linkedin-mcp/session.json)",
    )

    parser.add_argument(
        "--session-info",
        action="store_true",
        help="Check if current session is valid and exit",
    )

    parser.add_argument(
        "--clear-session",
        action="store_true",
        help="Clear stored LinkedIn session file",
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

    if args.viewport:
        try:
            width, height = args.viewport.lower().split("x")
            config.browser.viewport_width = int(width)
            config.browser.viewport_height = int(height)
        except ValueError:
            logger.warning(f"Invalid viewport format: {args.viewport}, using default")

    if args.timeout is not None:
        if args.timeout > 0:
            config.browser.default_timeout = args.timeout
        else:
            logger.warning(f"Invalid timeout: {args.timeout}, must be positive")

    # Session management
    if args.get_session is not None:
        config.server.get_session = True
        config.server.session_output_path = args.get_session

    if args.session_info:
        config.server.session_info = True

    if args.clear_session:
        config.server.clear_session = True

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

    return config
