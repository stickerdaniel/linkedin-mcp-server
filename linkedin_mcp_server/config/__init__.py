"""Configuration system for LinkedIn MCP Server."""

import logging

from .loaders import AppConfig, BrowserConfig, ConfigurationError, ServerConfig, load_config

logger = logging.getLogger(__name__)

_config: AppConfig | None = None


def get_config() -> AppConfig:
    """Get the application configuration, initializing if needed."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """Reset configuration to force reloading."""
    global _config
    _config = None


__all__ = [
    "AppConfig",
    "BrowserConfig",
    "ConfigurationError",
    "ServerConfig",
    "get_config",
    "reset_config",
]
