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
    return _config  # type: ignore[return-value]


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
]
