# src/linkedin_mcp_server/config/loaders.py
"""
Configuration loading and argument parsing for LinkedIn MCP Server.

This module implements the layered configuration system that loads settings from
multiple sources in priority order: CLI arguments → environment variables → keyring
→ defaults. It provides the main configuration loading logic and argument parsing
for the MCP server.

Key Functions:
- Command-line argument parsing with comprehensive options
- Environment variable parsing with type conversion
- Integration with keyring providers for secure credential loading
- Chrome driver path auto-detection and validation
- Layered configuration with proper priority handling
"""

import argparse
import logging
import os
import sys
from typing import Any, Dict, Optional

from .providers import (
    get_chromedriver_paths,
    get_cookie_from_keyring,
    get_credentials_from_keyring,
)
from .schema import AppConfig

logger = logging.getLogger(__name__)

# Boolean value mappings for environment variable parsing
TRUTHY_VALUES = ("1", "true", "True", "yes", "Yes")
FALSY_VALUES = ("0", "false", "False", "no", "No")


class EnvironmentKeys:
    """Environment variable names used by the application."""

    # LinkedIn configuration
    LINKEDIN_EMAIL = "LINKEDIN_EMAIL"
    LINKEDIN_PASSWORD = "LINKEDIN_PASSWORD"
    LINKEDIN_COOKIE = "LINKEDIN_COOKIE"

    # Chrome configuration
    CHROMEDRIVER = "CHROMEDRIVER"
    HEADLESS = "HEADLESS"
    USER_AGENT = "USER_AGENT"

    # Server configuration
    LOG_LEVEL = "LOG_LEVEL"
    LAZY_INIT = "LAZY_INIT"
    TRANSPORT = "TRANSPORT"


def find_chromedriver() -> Optional[str]:
    """Find the ChromeDriver executable in common locations."""
    # First check environment variable
    if path := os.getenv("CHROMEDRIVER"):
        if os.path.exists(path):
            return path

    # Check common locations
    for path in get_chromedriver_paths():
        if os.path.exists(path) and (os.access(path, os.X_OK) or path.endswith(".exe")):
            return path

    return None


def is_interactive_environment() -> bool:
    """
    Detect if running in an interactive environment (TTY).

    Returns:
        bool: True if both stdin and stdout are TTY devices
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, OSError):
        # Handle cases where stdin/stdout might not have isatty() or fail
        # This can happen in some containers, test environments, or non-standard setups
        return False


def load_from_keyring(config: AppConfig) -> AppConfig:
    """Load configuration from system keyring."""
    # Load LinkedIn cookie first (higher priority)
    if cookie := get_cookie_from_keyring():
        config.linkedin.cookie = cookie
        logger.debug("LinkedIn cookie loaded from keyring")

    # Load LinkedIn credentials if cookie not available
    if not config.linkedin.cookie:
        credentials = get_credentials_from_keyring()
        if credentials["email"]:
            config.linkedin.email = credentials["email"]
            logger.debug("LinkedIn email loaded from keyring")
        if credentials["password"]:
            config.linkedin.password = credentials["password"]
            logger.debug("LinkedIn password loaded from keyring")

    return config


def load_from_env(config: AppConfig) -> AppConfig:
    """Load configuration from environment variables."""

    # LinkedIn credentials
    if email := os.environ.get(EnvironmentKeys.LINKEDIN_EMAIL):
        config.linkedin.email = email

    if password := os.environ.get(EnvironmentKeys.LINKEDIN_PASSWORD):
        config.linkedin.password = password

    if cookie := os.environ.get(EnvironmentKeys.LINKEDIN_COOKIE):
        config.linkedin.cookie = cookie

    # ChromeDriver configuration
    if chromedriver := os.environ.get(EnvironmentKeys.CHROMEDRIVER):
        config.chrome.chromedriver_path = chromedriver

    if user_agent := os.environ.get(EnvironmentKeys.USER_AGENT):
        config.chrome.user_agent = user_agent

    # Log level
    if log_level_env := os.environ.get(EnvironmentKeys.LOG_LEVEL):
        log_level_upper = log_level_env.upper()
        if log_level_upper in ("DEBUG", "INFO", "WARNING", "ERROR"):
            config.server.log_level = log_level_upper

    # Headless mode
    if os.environ.get(EnvironmentKeys.HEADLESS) in FALSY_VALUES:
        config.chrome.headless = False
    elif os.environ.get(EnvironmentKeys.HEADLESS) in TRUTHY_VALUES:
        config.chrome.headless = True

    # Lazy initialization
    if os.environ.get(EnvironmentKeys.LAZY_INIT) in TRUTHY_VALUES:
        config.server.lazy_init = True
    elif os.environ.get(EnvironmentKeys.LAZY_INIT) in FALSY_VALUES:
        config.server.lazy_init = False

    # Transport mode
    if transport_env := os.environ.get(EnvironmentKeys.TRANSPORT):
        config.server.transport_explicitly_set = True
        if transport_env == "stdio":
            config.server.transport = "stdio"
        elif transport_env == "streamable-http":
            config.server.transport = "streamable-http"

    return config


def load_from_args(config: AppConfig) -> AppConfig:
    """Load configuration from command line arguments."""
    parser = argparse.ArgumentParser(
        description="LinkedIn MCP Server - A Model Context Protocol server for LinkedIn integration"
    )

    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run Chrome with a visible browser window (useful for debugging)",
    )

    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Set logging level (default: WARNING)",
    )

    parser.add_argument(
        "--no-lazy-init",
        action="store_true",
        help="Initialize Chrome driver and login immediately",
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

    parser.add_argument(
        "--chromedriver",
        type=str,
        help="Specify the path to the ChromeDriver executable",
    )

    parser.add_argument(
        "--get-cookie",
        action="store_true",
        help="Login with credentials and display cookie for Docker setup",
    )

    parser.add_argument(
        "--clear-keychain",
        action="store_true",
        help="Clear all stored LinkedIn credentials and cookies from system keychain",
    )

    parser.add_argument(
        "--cookie",
        type=str,
        help="Specify LinkedIn cookie directly",
    )

    parser.add_argument(
        "--user-agent",
        type=str,
        help="Specify custom user agent string to prevent anti-scraping detection",
    )

    args = parser.parse_args()

    # Update configuration with parsed arguments
    if args.no_headless:
        config.chrome.headless = False

    # Handle log level argument
    if args.log_level:
        config.server.log_level = args.log_level

    if args.no_lazy_init:
        config.server.lazy_init = False

    if args.transport:
        config.server.transport = args.transport
        config.server.transport_explicitly_set = True

    if args.host:
        config.server.host = args.host

    if args.port:
        config.server.port = args.port

    if args.path:
        config.server.path = args.path

    if args.chromedriver:
        config.chrome.chromedriver_path = args.chromedriver

    if args.get_cookie:
        config.server.get_cookie = True
    if args.clear_keychain:
        config.server.clear_keychain = True
    if args.cookie:
        config.linkedin.cookie = args.cookie

    if args.user_agent:
        config.chrome.user_agent = args.user_agent

    return config


def detect_environment() -> Dict[str, Any]:
    """
    Detect environment settings without side effects.

    Returns:
        Dict containing detected environment settings
    """
    return {
        "chromedriver_path": find_chromedriver(),
        "is_interactive": is_interactive_environment(),
    }


def load_config() -> AppConfig:
    """
    Load configuration with clear precedence order.

    Configuration is loaded in the following priority order:
    1. Command line arguments (highest priority)
    2. Environment variables
    3. System keyring
    4. Auto-detection (ChromeDriver, interactive mode)
    5. Defaults (lowest priority)

    Returns:
        AppConfig: Fully configured application settings

    Raises:
        ConfigurationError: If configuration validation fails
    """
    # Start with default configuration
    config = AppConfig()

    # Apply environment detection
    env_settings = detect_environment()

    # Set detected values if not already configured
    if env_settings["chromedriver_path"] and not config.chrome.chromedriver_path:
        config.chrome.chromedriver_path = env_settings["chromedriver_path"]
        logger.debug(
            f"Auto-detected ChromeDriver found at: {env_settings['chromedriver_path']}"
        )

    config.is_interactive = env_settings["is_interactive"]
    logger.debug(f"Auto-detected interactive mode: {config.is_interactive}")

    # Load from keyring (lowest override priority)
    config = load_from_keyring(config)

    # Override with environment variables
    config = load_from_env(config)

    # Override with command line arguments (highest priority)
    config = load_from_args(config)

    return config
