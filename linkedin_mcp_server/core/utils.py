"""Utility functions for scraping operations."""

import asyncio
import logging

from patchright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .exceptions import RateLimitError

logger = logging.getLogger(__name__)


async def detect_rate_limit(page: Page) -> None:
    """Detect if LinkedIn has rate limited the session.

    Raises:
        RateLimitError: If rate limiting is detected
    """
    # Check URL for security challenges
    current_url = page.url
    if "linkedin.com/checkpoint" in current_url or "authwall" in current_url:
        raise RateLimitError(
            "LinkedIn security checkpoint detected. "
            "You may need to verify your identity or wait before continuing.",
            suggested_wait_time=3600,
        )

    # Check for CAPTCHA
    try:
        captcha = await page.locator(
            'iframe[title*="captcha" i], iframe[src*="captcha" i]'
        ).count()
        if captcha > 0:
            raise RateLimitError(
                "CAPTCHA challenge detected. Manual intervention required.",
                suggested_wait_time=3600,
            )
    except RateLimitError:
        raise
    except PlaywrightTimeoutError:
        pass
    except Exception as e:
        logger.debug("Error checking for CAPTCHA: %s", e)

    # Check for rate limit messages
    try:
        body_text = await page.locator("body").inner_text(timeout=1000)
        if body_text:
            body_lower = body_text.lower()
            if any(
                phrase in body_lower
                for phrase in [
                    "too many requests",
                    "rate limit",
                    "slow down",
                    "try again later",
                ]
            ):
                raise RateLimitError(
                    "Rate limit message detected on page.",
                    suggested_wait_time=1800,
                )
    except RateLimitError:
        raise
    except PlaywrightTimeoutError:
        pass


async def scroll_to_bottom(
    page: Page, pause_time: float = 1.0, max_scrolls: int = 10
) -> None:
    """Scroll to the bottom of the page to trigger lazy loading.

    Args:
        page: Patchright page object
        pause_time: Time to pause between scrolls (seconds)
        max_scrolls: Maximum number of scroll attempts
    """
    for i in range(max_scrolls):
        previous_height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(pause_time)

        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == previous_height:
            logger.debug("Reached bottom after %d scrolls", i + 1)
            break


async def handle_modal_close(page: Page) -> bool:
    """Close any popup modals that might be blocking content.

    Returns:
        True if a modal was closed, False otherwise
    """
    try:
        close_button = page.locator(
            'button[aria-label="Dismiss"], '
            'button[aria-label="Close"], '
            "button.artdeco-modal__dismiss"
        ).first

        if await close_button.is_visible(timeout=1000):
            await close_button.click()
            await asyncio.sleep(0.5)
            logger.debug("Closed modal")
            return True
    except PlaywrightTimeoutError:
        pass
    except Exception as e:
        logger.debug("Error closing modal: %s", e)

    return False
