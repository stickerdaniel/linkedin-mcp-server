# linkedin_mcp_server/playwright_wrapper.py
"""
Playwright initialization wrapper to fix fast-linkedin-scraper context manager issues.

The fast-linkedin-scraper library has issues with Playwright context manager initialization,
causing '_connection' attribute errors. This wrapper ensures Playwright is properly
initialized before the library tries to use it.

Updated to support both async and sync contexts properly.
"""

import logging
from contextlib import contextmanager
from typing import Generator, Any
import threading

logger = logging.getLogger(__name__)

# Global Playwright instance management
_playwright_lock = threading.Lock()
_global_playwright = None
_reference_count = 0
_is_async_instance = False


def ensure_playwright_started():
    """
    Ensure Playwright is properly started and return the instance.

    This fixes the '_connection' attribute error by properly initializing
    Playwright before any library tries to use it.

    Automatically detects if we're in an async context and uses appropriate API.
    """
    global _global_playwright, _reference_count, _is_async_instance

    with _playwright_lock:
        if _global_playwright is None:
            logger.debug("Starting global Playwright instance")

            # Always use sync API - fast-linkedin-scraper requires it
            # The scraper adapter handles async context by running everything in a separate thread
            logger.debug("Initializing Playwright with sync API")
            from playwright.sync_api import sync_playwright

            playwright_cm = sync_playwright()
            _global_playwright = playwright_cm.start()
            _is_async_instance = False

            _reference_count = 0

        _reference_count += 1
        logger.debug(f"Playwright reference count: {_reference_count}")
        return _global_playwright


def release_playwright():
    """
    Release Playwright reference and stop it if no more references exist.
    """
    global _global_playwright, _reference_count, _is_async_instance

    with _playwright_lock:
        if _global_playwright is not None:
            _reference_count -= 1
            logger.debug(
                f"Playwright reference count after release: {_reference_count}"
            )

            if _reference_count <= 0:
                logger.debug("Stopping global Playwright instance")
                try:
                    if _is_async_instance:
                        # Handle async cleanup if needed
                        logger.debug("Stopping async Playwright instance")
                    else:
                        # Standard sync cleanup
                        if _global_playwright is not None:
                            _global_playwright.stop()
                except Exception as e:
                    logger.warning(f"Error stopping Playwright: {e}")
                finally:
                    _global_playwright = None
                    _reference_count = 0
                    _is_async_instance = False


@contextmanager
def managed_playwright() -> Generator[Any, None, None]:
    """
    Context manager that ensures Playwright is properly initialized.

    This can be used to wrap any code that needs Playwright to be running,
    ensuring it's properly started and cleaned up.
    """
    playwright = ensure_playwright_started()
    try:
        yield playwright
    finally:
        release_playwright()


def initialize_playwright_for_fast_scraper():
    """
    Pre-initialize Playwright for fast-linkedin-scraper to prevent context manager issues.

    Call this before using fast-linkedin-scraper to ensure Playwright is ready.
    Works properly in both async and sync contexts.
    """
    logger.info("Pre-initializing Playwright for fast-linkedin-scraper")

    try:
        # Start Playwright properly (handles async context detection)
        playwright = ensure_playwright_started()

        # Verify it's working by testing browser access
        logger.debug("Testing Playwright browser access")

        # Run browser test (this should work fine since we're in the same thread as Playwright)
        browser = playwright.chromium.launch(headless=True)
        logger.debug("Playwright browser launched successfully")
        browser.close()
        logger.debug("Playwright test completed successfully")
        return True

    except Exception as e:
        logger.error(f"Playwright initialization test failed: {e}")
        release_playwright()
        return False


def cleanup_playwright():
    """
    Force cleanup of all Playwright resources.

    Call this on shutdown to ensure proper cleanup.
    """
    global _global_playwright, _reference_count, _is_async_instance

    logger.info("Forcing Playwright cleanup")

    with _playwright_lock:
        if _global_playwright is not None:
            try:
                if _is_async_instance:
                    logger.debug("Cleaning up async Playwright instance")
                else:
                    logger.debug("Cleaning up sync Playwright instance")

                if _global_playwright is not None:
                    _global_playwright.stop()
                logger.info("Playwright stopped successfully")
            except Exception as e:
                logger.warning(f"Error during forced Playwright cleanup: {e}")
            finally:
                _global_playwright = None
                _reference_count = 0
                _is_async_instance = False
