# src/linkedin_mcp_server/config/loaders.py
import os
import argparse
import logging
from typing import Optional
from .schema import AppConfig
from .providers import get_chromedriver_paths

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

    # ChromeDriver configuration
    if chromedriver := os.environ.get("CHROMEDRIVER"):
        config.chrome.chromedriver_path = chromedriver

    # Debug mode
    if os.environ.get("DEBUG") in ("1", "true", "True", "yes", "Yes"):
        config.server.debug = True

    # Headless mode
    if os.environ.get("HEADLESS") in ("0", "false", "False", "no", "No"):
        config.chrome.headless = False

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
        choices=["stdio", "sse"],
        default=None,
        help="Specify the transport mode (stdio or sse)",
    )

    parser.add_argument(
        "--chromedriver",
        type=str,
        help="Specify the path to the ChromeDriver executable",
    )

    args = parser.parse_args()

    # Update configuration with parsed arguments
    if args.no_headless:
        config.chrome.headless = False

    if args.debug:
        config.server.debug = True

    if args.no_setup:
        config.server.setup = False

    if args.no_lazy_init:
        config.server.lazy_init = False

    if args.transport:
        config.server.transport = args.transport

    if args.chromedriver:
        config.chrome.chromedriver_path = args.chromedriver

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
