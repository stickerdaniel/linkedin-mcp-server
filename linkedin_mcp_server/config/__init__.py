# src/linkedin_mcp_server/config/__init__.py
import logging
from typing import Optional

from .loaders import load_config
from .providers import (
    clear_credentials_from_keyring,
    clear_all_keychain_data,
    check_keychain_data_exists,
    get_credentials_from_keyring,
    get_keyring_name,
    save_credentials_to_keyring,
)
from .schema import AppConfig, ChromeConfig, LinkedInConfig, ServerConfig

logger = logging.getLogger(__name__)

# Singleton pattern for configuration
_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    """Get the application configuration, initializing it if needed."""
    global _config
    if _config is None:
        _config = load_config()
        logger.debug("Configuration loaded")
    # At this point _config is guaranteed to be AppConfig, not None
    return _config  # type: ignore[return-value]


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
    "clear_all_keychain_data",
    "check_keychain_data_exists",
    "get_keyring_name",
]
