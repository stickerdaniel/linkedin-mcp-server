# src/linkedin_mcp_server/config/__init__.py
from typing import Optional
import logging
from .schema import AppConfig, ChromeConfig, LinkedInConfig, ServerConfig
from .loaders import load_config
from .providers import (
    get_credentials_from_keyring,
    save_credentials_to_keyring,
    clear_credentials_from_keyring,
    get_keyring_name,
)

logger = logging.getLogger(__name__)

# Singleton pattern for configuration
_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    """Get the application configuration, initializing it if needed."""
    global _config
    if _config is None:
        _config = load_config()
        logger.debug("Configuration loaded")
    return _config


def reset_config() -> None:
    """Reset the configuration to force reloading."""
    global _config
    _config = None
    logger.debug("Configuration reset")


# Export schema classes for type annotations
__all__ = [
    "AppConfig",
    "ChromeConfig",
    "LinkedInConfig",
    "ServerConfig",
    "get_config",
    "reset_config",
    "get_credentials_from_keyring",
    "save_credentials_to_keyring",
    "clear_credentials_from_keyring",
    "get_keyring_name",
]
