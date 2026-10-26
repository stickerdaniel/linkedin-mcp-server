"""
Configuration system for LinkedIn MCP Server.

Provides a singleton pattern for configuration management with
loading from CLI arguments and environment variables.
"""

import logging

from .loaders import load_config
from .schema import AppConfig, BrowserConfig, ServerConfig

logger = logging.getLogger(__name__)

# Singleton pattern for configuration
_config: AppConfig | None = None


def get_config() -> AppConfig:
    """Get the application configuration, initializing it if needed."""
    global _config
    if _config is None:
        _config = load_config()
        logger.debug("Configuration loaded")
    return _config


def set_config(config: AppConfig) -> None:
    """Install *config* as the configuration this process runs on.

    For the detached daemon owner, which is handed its settings rather than
    parsing them. Half of what decides which browser opens arrives on the
    command line of the process the MCP client spawned, and that is the
    frontend: an owner that called ``load_config`` would see only the
    environment, come up with a different browser, and be refused by the very
    client that started it, because ``config_fingerprint`` compares exactly
    those fields.

    Called before anything reads the configuration. Nothing here unwinds
    decisions already made from an earlier value.
    """
    global _config
    _config = config
    logger.debug("Configuration installed")


def reset_config() -> None:
    """Reset the configuration to force reloading."""
    global _config
    _config = None
    logger.debug("Configuration reset")


__all__ = [
    "AppConfig",
    "BrowserConfig",
    "ServerConfig",
    "get_config",
    "reset_config",
    "set_config",
]
