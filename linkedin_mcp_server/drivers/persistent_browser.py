"""
Persistent browser management using Playwright's launchPersistentContext.

This module provides a browser manager that uses Playwright's persistent context
feature to automatically maintain browser state (cookies, localStorage, etc.) in
a user data directory, eliminating the need for manual session save/load cycles.
"""

import logging
import os
from pathlib import Path
from typing import Any

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

logger = logging.getLogger(__name__)


class PersistentBrowserManager:
    """
    Async context manager for Playwright persistent browser context.

    Uses launchPersistentContext() to maintain browser state (cookies, localStorage)
    in a user data directory, eliminating need for manual session save/load.

    Compatible with linkedin_scraper's scraper classes which expect a `.page` property.
    """

    def __init__(
        self,
        user_data_dir: str | Path,
        headless: bool = True,
        slow_mo: int = 0,
        viewport: dict[str, int] | None = None,
        user_agent: str | None = None,
        **launch_options: Any,
    ):
        """
        Initialize persistent browser manager.

        Args:
            user_data_dir: Directory to store browser profile/session data
            headless: Run browser in headless mode
            slow_mo: Slow down operations by specified milliseconds
            viewport: Browser viewport size (default: 1280x720)
            user_agent: Custom user agent string
            **launch_options: Additional Playwright launch options (e.g., executable_path)
        """
        self.user_data_dir = Path(user_data_dir).expanduser()
        self.headless = headless
        self.slow_mo = slow_mo
        self.viewport = viewport or {"width": 1280, "height": 720}
        self.user_agent = user_agent
        self.launch_options = launch_options

        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def __aenter__(self) -> "PersistentBrowserManager":
        """Start browser and create context."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Close browser and cleanup."""
        await self.close()

    async def start(self) -> None:
        """Start Playwright and launch persistent context."""
        try:
            # Ensure user data directory parent exists
            self.user_data_dir.parent.mkdir(parents=True, exist_ok=True)

            # Check if directory is writable
            if self.user_data_dir.exists() and not os.access(
                self.user_data_dir, os.W_OK
            ):
                raise PermissionError(
                    f"Cannot write to {self.user_data_dir}. "
                    "For Docker: ensure volume has correct permissions (chown 1000:1000)"
                )

            self._playwright = await async_playwright().start()

            # Prepare launch options for persistent context
            context_options: dict[str, Any] = {
                "viewport": self.viewport,
                "headless": self.headless,
                "slow_mo": self.slow_mo,
                **self.launch_options,
            }

            if self.user_agent:
                context_options["user_agent"] = self.user_agent

            logger.info(
                "Launching persistent browser context (headless=%s, slow_mo=%sms, user_data_dir=%s)",
                self.headless,
                self.slow_mo,
                self.user_data_dir,
            )

            # Launch persistent context - this is the key feature
            # Cookies, localStorage, and other state persist automatically
            try:
                self._context = (
                    await self._playwright.chromium.launch_persistent_context(
                        str(self.user_data_dir), **context_options
                    )
                )
            except Exception as e:
                error_msg = str(e).lower()

                # Check for profile corruption
                if (
                    "failed to create browser context" in error_msg
                    or "corrupt" in error_msg
                ):
                    raise RuntimeError(
                        f"Browser profile at {self.user_data_dir} appears corrupted. "
                        f"Run with --clear-session to reset, then --get-session to re-authenticate."
                    ) from e

                # Check for concurrent access
                if "already in use" in error_msg or "lock" in error_msg:
                    raise RuntimeError(
                        f"Browser profile at {self.user_data_dir} is already in use by another process. "
                        "Only one instance can use a profile at a time."
                    ) from e

                raise

            # Get or create the main page
            # Persistent context usually creates a page automatically
            pages = self._context.pages
            if pages:
                self._page = pages[0]
            else:
                self._page = await self._context.new_page()

            logger.info("Persistent browser context created successfully")

        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        """Close browser and cleanup resources."""
        try:
            if self._page:
                await self._page.close()
                self._page = None

            if self._context:
                await self._context.close()
                self._context = None

            if self._playwright:
                await self._playwright.stop()
                self._playwright = None

            logger.info("Persistent browser context closed")

        except Exception as e:
            logger.error(f"Error closing persistent browser: {e}")

    @property
    def page(self) -> Page:
        """
        Get the main page.

        This property is required by linkedin_scraper's scraper classes
        (PersonScraper, CompanyScraper, JobScraper).

        Returns:
            Main Playwright page

        Raises:
            RuntimeError: If browser not started
        """
        if not self._page:
            raise RuntimeError(
                "Browser not started. Use async context manager or call start()."
            )
        return self._page

    @property
    def context(self) -> BrowserContext:
        """
        Get the browser context.

        Returns:
            Playwright browser context

        Raises:
            RuntimeError: If context not initialized
        """
        if not self._context:
            raise RuntimeError("Browser context not initialized.")
        return self._context
