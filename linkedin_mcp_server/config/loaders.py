# src/linkedin_mcp_server/config/loaders.py
import argparse
import logging
import os
from typing import Optional

from .providers import get_chromedriver_paths
from .schema import AppConfig

logger = logging.getLogger(__name__)


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


def load_from_env(config: AppConfig) -> AppConfig:
    """Load configuration from environment variables."""
    # LinkedIn credentials
    if email := os.environ.get("LINKEDIN_EMAIL"):
        config.linkedin.email = email

    if password := os.environ.get("LINKEDIN_PASSWORD"):
        config.linkedin.password = password

    if cookie := os.environ.get("LINKEDIN_COOKIE"):
        config.linkedin.cookie = cookie

    # ChromeDriver configuration
    if chromedriver := os.environ.get("CHROMEDRIVER"):
        config.chrome.chromedriver_path = chromedriver

    # Debug mode
    if os.environ.get("DEBUG") in ("1", "true", "True", "yes", "Yes"):
        config.server.debug = True

    # Headless mode
    if os.environ.get("HEADLESS") in ("0", "false", "False", "no", "No"):
        config.chrome.headless = False
    elif os.environ.get("HEADLESS") in ("1", "true", "True", "yes", "Yes"):
        config.chrome.headless = True

    # Non-interactive mode
    if os.environ.get("NON_INTERACTIVE") in ("1", "true", "True", "yes", "Yes"):
        config.chrome.non_interactive = True

    # Lazy initialization
    if os.environ.get("LAZY_INIT") in ("1", "true", "True", "yes", "Yes"):
        config.server.lazy_init = True
    elif os.environ.get("LAZY_INIT") in ("0", "false", "False", "no", "No"):
        config.server.lazy_init = False

    # Transport mode
    if transport_env := os.environ.get("TRANSPORT"):
        if transport_env == "stdio":
            config.server.transport = "stdio"
            config.server.transport_explicitly_set = True
        elif transport_env == "streamable-http":
            config.server.transport = "streamable-http"
            config.server.transport_explicitly_set = True

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
        "--debug",
        action="store_true",
        help="Enable debug mode with additional logging",
    )

    parser.add_argument(
        "--no-setup",
        action="store_true",
        help="Skip printing configuration information and interactive setup",
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

    args = parser.parse_args()

    # Update configuration with parsed arguments
    if args.no_headless:
        config.chrome.headless = False

    if args.debug:
        config.server.debug = True

    if args.no_setup:
        config.server.setup = False
        config.chrome.non_interactive = (
            True  # Automatically set when --no-setup is used
        )

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

    if hasattr(args, "get_cookie") and args.get_cookie:
        config.server.get_cookie = True
    if hasattr(args, "clear_keychain") and args.clear_keychain:
        config.server.clear_keychain = True
    if args.cookie:
        config.linkedin.cookie = args.cookie

    return config


def load_config() -> AppConfig:
    """
    Load configuration from all sources with defined precedence:
    1. Command line arguments (highest priority)
    2. Environment variables
    3. Default values and auto-detection (lowest priority)
    """
    # Start with default configuration
    config = AppConfig()

    # Auto-detect ChromeDriver path
    if chromedriver_path := find_chromedriver():
        config.chrome.chromedriver_path = chromedriver_path
        logger.debug(f"Auto-detected ChromeDriver at: {chromedriver_path}")

    # Override with environment variables
    config = load_from_env(config)

    # Override with command line arguments (highest priority)
    config = load_from_args(config)

    return config
