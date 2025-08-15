# src/linkedin_mcp_server/drivers/__init__.py
"""
Driver management package for LinkedIn scraping.

This package provides unified driver management for both legacy (Selenium)
and modern (Playwright) LinkedIn scraper backends. It handles scraper-specific
driver initialization, session management, and cleanup.

Key Components:
- Chrome WebDriver management for legacy linkedin-scraper
- Playwright session management awareness for fast-linkedin-scraper
- Scraper-aware driver initialization and cleanup
- Singleton pattern for legacy driver reuse
- Cross-platform Chrome driver detection and setup
"""

import logging
from typing import Any, Optional

from linkedin_mcp_server.config import get_config

logger = logging.getLogger(__name__)


def get_driver_for_scraper_type() -> Optional[Any]:
    """
    Get appropriate driver based on configured scraper type.

    For legacy scraper: Returns Selenium WebDriver
    For fast scraper: Returns None (manages its own Playwright sessions)

    Returns:
        Optional[Any]: Driver instance for legacy scraper, None for fast scraper
    """
    config = get_config()

    if config.linkedin.scraper_type == "linkedin-scraper":
        from linkedin_mcp_server.authentication import ensure_authentication
        from linkedin_mcp_server.drivers.chrome import get_or_create_driver

        logger.debug("Getting Chrome WebDriver for legacy scraper")
        authentication = ensure_authentication()
        return get_or_create_driver(authentication)
    else:
        logger.debug("Using fast-linkedin-scraper - no driver management needed")
        return None


def close_all_sessions() -> None:
    """
    Close all active sessions for the configured scraper type.

    This function is deprecated. Use scraper_factory.cleanup_scraper_backend() instead.
    """
    config = get_config()

    if config.linkedin.scraper_type == "linkedin-scraper":
        from linkedin_mcp_server.drivers.chrome import close_all_drivers

        logger.info("Closing Chrome WebDriver sessions")
        close_all_drivers()
    else:
        # For fast-linkedin-scraper, cleanup Playwright resources
        try:
            from linkedin_mcp_server.playwright_wrapper import cleanup_playwright

            logger.info("Cleaning up Playwright resources for fast-linkedin-scraper")
            cleanup_playwright()
        except ImportError:
            logger.debug("Playwright wrapper not available for cleanup")
        except Exception as e:
            logger.warning(f"Error cleaning up Playwright resources: {e}")


def is_driver_needed() -> bool:
    """
    Check if the current scraper type needs driver management.

    Returns:
        bool: True if driver management is needed, False otherwise
    """
    config = get_config()
    return config.linkedin.scraper_type == "linkedin-scraper"
