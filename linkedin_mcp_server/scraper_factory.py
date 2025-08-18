# linkedin_mcp_server/scraper_factory.py
"""
Factory pattern for LinkedIn scraper backends.

Provides clean separation between Chrome WebDriver (linkedin-scraper) and
Playwright (fast-linkedin-scraper) initialization flows. Ensures that each
backend only initializes its required dependencies.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict

from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.authentication import ensure_authentication

logger = logging.getLogger(__name__)


class ScraperBackend(ABC):
    """Abstract base class for scraper backends."""

    @abstractmethod
    def initialize(self) -> bool:
        """Initialize the scraper backend."""
        pass

    @abstractmethod
    def cleanup(self) -> None:
        """Clean up backend resources."""
        pass

    @abstractmethod
    def get_backend_info(self) -> Dict[str, Any]:
        """Get information about this backend."""
        pass


class LinkedInScraperBackend(ScraperBackend):
    """Chrome WebDriver backend for legacy linkedin-scraper."""

    def initialize(self) -> bool:
        """Initialize Chrome WebDriver for linkedin-scraper."""
        logger.info("Initializing Chrome WebDriver backend for linkedin-scraper")

        try:
            # Only import Chrome driver components when actually using legacy scraper
            from linkedin_mcp_server.drivers.chrome import get_or_create_driver

            # Get authentication and initialize driver
            authentication = ensure_authentication()
            get_or_create_driver(authentication)

            logger.info("Chrome WebDriver backend initialized successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to initialize Chrome WebDriver backend: {e}")
            return False

    def cleanup(self) -> None:
        """Clean up Chrome WebDriver resources."""
        try:
            from linkedin_mcp_server.drivers.chrome import close_all_drivers

            logger.info("Cleaning up Chrome WebDriver backend")
            close_all_drivers()
        except Exception as e:
            logger.warning(f"Error cleaning up Chrome WebDriver backend: {e}")

    def get_backend_info(self) -> Dict[str, Any]:
        """Get Chrome WebDriver backend information."""
        return {
            "backend": "linkedin-scraper",
            "type": "Chrome WebDriver (Selenium)",
            "supports_headless": True,
            "supports_no_lazy_init": True,
            "requires_chromedriver": True,
        }


class FastLinkedInScraperBackend(ScraperBackend):
    """Playwright backend for fast-linkedin-scraper."""

    def initialize(self) -> bool:
        """Initialize Playwright for fast-linkedin-scraper."""
        logger.info("Initializing Playwright backend for fast-linkedin-scraper")

        # fast-linkedin-scraper doesn't need persistent initialization
        # It creates sessions on-demand, so we just verify authentication is available
        try:
            ensure_authentication()
            logger.info("Playwright backend initialized successfully (on-demand mode)")
            return True

        except Exception as e:
            logger.error(f"Failed to initialize Playwright backend: {e}")
            return False

    def cleanup(self) -> None:
        """Clean up Playwright resources."""
        # fast-linkedin-scraper manages its own Playwright sessions
        # No persistent resources to clean up in on-demand mode
        logger.info(
            "Cleaning up Playwright backend (on-demand mode - no persistent resources)"
        )

    def get_backend_info(self) -> Dict[str, Any]:
        """Get Playwright backend information."""
        return {
            "backend": "fast-linkedin-scraper",
            "type": "Playwright (on-demand sessions)",
            "supports_headless": False,  # Playwright manages its own headless mode internally
            "supports_no_lazy_init": False,  # Creates sessions on-demand, no persistent initialization
            "requires_chromedriver": False,
            "initialization_mode": "on-demand",
        }


def get_scraper_backend() -> ScraperBackend:
    """
    Get the appropriate scraper backend based on configuration.

    Returns:
        ScraperBackend: The configured scraper backend instance
    """
    config = get_config()
    scraper_type = config.linkedin.scraper_type

    logger.info(f"Creating scraper backend for: {scraper_type}")

    if scraper_type == "linkedin-scraper":
        return LinkedInScraperBackend()
    elif scraper_type == "fast-linkedin-scraper":
        return FastLinkedInScraperBackend()
    else:
        raise ValueError(f"Unknown scraper type: {scraper_type}")


# Track initialization state
_backend_initialized = False


def initialize_scraper_backend() -> bool:
    """
    Initialize the configured scraper backend.

    Returns:
        bool: True if initialization was successful, False otherwise
    """
    global _backend_initialized

    try:
        backend = get_scraper_backend()
        result = backend.initialize()
        _backend_initialized = result
        return result
    except Exception as e:
        logger.error(f"Failed to initialize scraper backend: {e}")
        _backend_initialized = False
        return False


def cleanup_scraper_backend() -> None:
    """Clean up the configured scraper backend."""
    global _backend_initialized

    try:
        backend = get_scraper_backend()
        backend.cleanup()
        _backend_initialized = False
    except Exception as e:
        logger.warning(f"Error cleaning up scraper backend: {e}")
        _backend_initialized = False


def is_scraper_backend_initialized() -> bool:
    """Check if the scraper backend is initialized."""
    return _backend_initialized


def get_scraper_factory():
    """Get the scraper factory (for compatibility with existing code)."""
    return get_scraper_backend()


def get_backend_capabilities() -> Dict[str, Any]:
    """
    Get capabilities of the current scraper backend.

    Returns:
        Dict[str, Any]: Backend information and capabilities
    """
    try:
        backend = get_scraper_backend()
        return backend.get_backend_info()
    except Exception as e:
        logger.error(f"Error getting backend capabilities: {e}")
        return {"backend": "unknown", "error": str(e)}
