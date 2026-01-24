"""
Base automation class for LinkedIn browser automation.

Provides common functionality for Playwright-based automation including
page navigation, element interaction, and error handling.
"""

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from typing import Any

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeout

from linkedin_mcp_server.drivers.browser import get_or_create_browser

logger = logging.getLogger(__name__)


class AutomationError(Exception):
    """Base exception for automation errors."""

    pass


class ElementNotFoundError(AutomationError):
    """Element could not be found on the page."""

    pass


class NavigationError(AutomationError):
    """Navigation to a page failed."""

    pass


class BaseAutomation(ABC):
    """
    Base class for LinkedIn automation tasks.

    Provides common utilities for page interaction, human-like delays,
    and error handling.
    """

    def __init__(self, page: Page | None = None):
        """
        Initialize the automation.

        Args:
            page: Optional Playwright page. If not provided, will be
                  obtained from the browser manager.
        """
        self._page = page
        self._min_typing_delay = 50
        self._max_typing_delay = 150

    async def get_page(self) -> Page:
        """Get the Playwright page, creating browser if needed."""
        if self._page is None:
            browser = await get_or_create_browser()
            self._page = browser.page
        return self._page

    async def navigate(self, url: str, wait_for: str = "networkidle") -> None:
        """
        Navigate to a URL with wait conditions.

        Args:
            url: The URL to navigate to
            wait_for: Wait condition (load, domcontentloaded, networkidle)

        Raises:
            NavigationError: If navigation fails
        """
        page = await self.get_page()
        try:
            logger.debug(f"Navigating to {url}")
            await page.goto(url, wait_until=wait_for)
            await self.random_delay(0.5, 1.5)
        except PlaywrightTimeout as e:
            raise NavigationError(f"Timeout navigating to {url}: {e}")
        except Exception as e:
            raise NavigationError(f"Failed to navigate to {url}: {e}")

    async def wait_for_selector(
        self,
        selector: str,
        timeout: int = 10000,
        state: str = "visible",
    ) -> Locator:
        """
        Wait for an element to appear.

        Args:
            selector: CSS selector
            timeout: Maximum wait time in milliseconds
            state: Expected state (attached, detached, visible, hidden)

        Returns:
            Locator for the found element

        Raises:
            ElementNotFoundError: If element not found within timeout
        """
        page = await self.get_page()
        try:
            locator = page.locator(selector)
            await locator.wait_for(timeout=timeout, state=state)
            return locator
        except PlaywrightTimeout:
            raise ElementNotFoundError(
                f"Element '{selector}' not found within {timeout}ms"
            )

    async def click(
        self,
        selector: str,
        timeout: int = 10000,
        force: bool = False,
    ) -> None:
        """
        Click an element with human-like behavior.

        Args:
            selector: CSS selector
            timeout: Maximum wait time in milliseconds
            force: Force click even if not visible

        Raises:
            ElementNotFoundError: If element not found
        """
        locator = await self.wait_for_selector(selector, timeout)
        await self.random_delay(0.1, 0.3)
        try:
            await locator.click(force=force)
        except Exception as e:
            raise AutomationError(f"Failed to click '{selector}': {e}")

    async def fill(
        self,
        selector: str,
        value: str,
        timeout: int = 10000,
        clear_first: bool = True,
    ) -> None:
        """
        Fill an input field with human-like typing.

        Args:
            selector: CSS selector
            value: Text to type
            timeout: Maximum wait time in milliseconds
            clear_first: Clear the field before typing

        Raises:
            ElementNotFoundError: If element not found
        """
        locator = await self.wait_for_selector(selector, timeout)
        await self.random_delay(0.1, 0.3)

        if clear_first:
            await locator.clear()
            await self.random_delay(0.1, 0.2)

        # Type with human-like delays
        for char in value:
            await locator.press(char)
            delay = random.uniform(
                self._min_typing_delay / 1000,
                self._max_typing_delay / 1000,
            )
            await asyncio.sleep(delay)

    async def get_text(
        self,
        selector: str,
        timeout: int = 10000,
        default: str = "",
    ) -> str:
        """
        Get text content of an element.

        Args:
            selector: CSS selector
            timeout: Maximum wait time in milliseconds
            default: Default value if element not found

        Returns:
            Text content of the element
        """
        try:
            locator = await self.wait_for_selector(selector, timeout)
            text = await locator.text_content()
            return text.strip() if text else default
        except ElementNotFoundError:
            return default

    async def get_attribute(
        self,
        selector: str,
        attribute: str,
        timeout: int = 10000,
        default: str | None = None,
    ) -> str | None:
        """
        Get an attribute from an element.

        Args:
            selector: CSS selector
            attribute: Attribute name
            timeout: Maximum wait time in milliseconds
            default: Default value if not found

        Returns:
            Attribute value or default
        """
        try:
            locator = await self.wait_for_selector(selector, timeout)
            return await locator.get_attribute(attribute) or default
        except ElementNotFoundError:
            return default

    async def exists(self, selector: str, timeout: int = 3000) -> bool:
        """
        Check if an element exists on the page.

        Args:
            selector: CSS selector
            timeout: Maximum wait time in milliseconds

        Returns:
            True if element exists
        """
        try:
            await self.wait_for_selector(selector, timeout)
            return True
        except ElementNotFoundError:
            return False

    async def random_delay(self, min_seconds: float, max_seconds: float) -> float:
        """
        Wait for a random duration.

        Args:
            min_seconds: Minimum wait time
            max_seconds: Maximum wait time

        Returns:
            Actual delay in seconds
        """
        delay = random.uniform(min_seconds, max_seconds)
        await asyncio.sleep(delay)
        return delay

    async def scroll_down(self, pixels: int = 300) -> None:
        """Scroll the page down by specified pixels."""
        page = await self.get_page()
        await page.mouse.wheel(0, pixels)
        await self.random_delay(0.3, 0.7)

    async def scroll_to_bottom(self, max_scrolls: int = 10) -> None:
        """Scroll to the bottom of the page gradually."""
        page = await self.get_page()
        for _ in range(max_scrolls):
            previous_height = await page.evaluate("document.body.scrollHeight")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await self.random_delay(0.5, 1.0)
            current_height = await page.evaluate("document.body.scrollHeight")
            if current_height == previous_height:
                break

    async def take_screenshot(self, path: str) -> None:
        """Take a screenshot for debugging."""
        page = await self.get_page()
        await page.screenshot(path=path)
        logger.debug(f"Screenshot saved to {path}")

    @abstractmethod
    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """
        Execute the automation task.

        Args:
            **kwargs: Task-specific arguments

        Returns:
            Dictionary with results
        """
        pass
