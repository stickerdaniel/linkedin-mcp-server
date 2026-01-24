"""
LinkedIn connection request automation.

Provides automation for sending connection requests with optional
personalized messages.
"""

import logging
from typing import Any

from .base import AutomationError, BaseAutomation
from .selectors import CommonSelectors, ProfileSelectors

logger = logging.getLogger(__name__)


class ConnectionRequestError(AutomationError):
    """Error during connection request."""

    pass


class AlreadyConnectedError(ConnectionRequestError):
    """Already connected to this person."""

    pass


class PendingConnectionError(ConnectionRequestError):
    """Connection request already pending."""

    pass


class ConnectionRequestAutomation(BaseAutomation):
    """
    Automation for sending LinkedIn connection requests.

    Navigates to a profile and sends a connection request with an
    optional personalized message.
    """

    async def execute(
        self,
        profile_url: str,
        message: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Send a connection request.

        Args:
            profile_url: LinkedIn profile URL
            message: Optional personalized message (max 300 chars)

        Returns:
            Dictionary with status and details
        """
        logger.info(f"Sending connection request to {profile_url}")

        try:
            # Navigate to profile
            await self.navigate(profile_url)

            # Check for rate limiting
            if await self._check_rate_limit():
                return {
                    "status": "rate_limited",
                    "profile_url": profile_url,
                    "message": "LinkedIn rate limit detected",
                }

            # Check current connection status
            status = await self._check_connection_status()
            if status == "connected":
                return {
                    "status": "already_connected",
                    "profile_url": profile_url,
                    "message": "Already connected to this person",
                }
            elif status == "pending":
                return {
                    "status": "already_pending",
                    "profile_url": profile_url,
                    "message": "Connection request already pending",
                }

            # Find and click connect button
            if not await self._click_connect_button():
                return {
                    "status": "failed",
                    "profile_url": profile_url,
                    "message": "Could not find connect button",
                }

            # Add note if message provided
            if message:
                await self._add_connection_note(message)

            # Send the request
            if await self._send_request():
                # Get profile name for confirmation
                name = await self._get_profile_name()
                return {
                    "status": "success",
                    "profile_url": profile_url,
                    "profile_name": name,
                    "message_sent": message is not None,
                    "message": f"Connection request sent to {name}",
                }
            else:
                return {
                    "status": "failed",
                    "profile_url": profile_url,
                    "message": "Failed to send connection request",
                }

        except Exception as e:
            logger.error(f"Connection request failed: {e}")
            return {
                "status": "error",
                "profile_url": profile_url,
                "message": str(e),
            }

    async def _check_rate_limit(self) -> bool:
        """Check if we've hit a rate limit page."""
        return await self.exists(CommonSelectors.RATE_LIMIT_PAGE, timeout=2000)

    async def _check_connection_status(self) -> str:
        """Check current connection status with the profile."""
        # Check if already connected (has Message button as primary)
        if await self.exists(ProfileSelectors.MESSAGE_BUTTON, timeout=2000):
            # Need to check if Connect is in "More" menu
            if not await self.exists(ProfileSelectors.CONNECT_BUTTON, timeout=1000):
                return "connected"

        # Check if pending
        if await self.exists(ProfileSelectors.PENDING_BUTTON, timeout=1000):
            return "pending"

        return "not_connected"

    async def _click_connect_button(self) -> bool:
        """Find and click the connect button."""
        page = await self.get_page()

        # Try primary connect button (div/a with span containing "Connect")
        # This is the most common pattern on LinkedIn
        try:
            connect_btn = page.locator(ProfileSelectors.CONNECT_BUTTON).first
            if await connect_btn.count() > 0:
                await connect_btn.click()
                await self.random_delay(0.5, 1.0)
                return True
        except Exception:
            pass

        # Try button element with Connect text (fallback)
        try:
            connect_btn = page.locator(ProfileSelectors.CONNECT_BUTTON_ALT).first
            if await connect_btn.count() > 0 and await connect_btn.is_visible():
                await connect_btn.click()
                await self.random_delay(0.5, 1.0)
                return True
        except Exception:
            pass

        # Try role=button elements
        try:
            connect_btn = page.locator(ProfileSelectors.CONNECT_BUTTON_ROLE).first
            if await connect_btn.count() > 0:
                await connect_btn.click()
                await self.random_delay(0.5, 1.0)
                return True
        except Exception:
            pass

        # Try "More" dropdown as last resort
        try:
            more_btn = page.locator(ProfileSelectors.MORE_BUTTON).first
            if await more_btn.count() > 0 and await more_btn.is_visible():
                await more_btn.click()
                await self.random_delay(0.3, 0.6)

                # Find Connect in dropdown
                connect_item = page.locator(ProfileSelectors.CONNECT_IN_DROPDOWN).first
                if await connect_item.count() > 0 and await connect_item.is_visible():
                    await connect_item.click()
                    await self.random_delay(0.5, 1.0)
                    return True
        except Exception:
            pass

        return False

    async def _add_connection_note(self, message: str) -> bool:
        """Add a personalized note to the connection request."""
        page = await self.get_page()

        try:
            # Wait for modal to appear
            await self.random_delay(0.5, 1.0)

            # Click "Add a note" button
            add_note_btn = page.locator(ProfileSelectors.ADD_NOTE_BUTTON).first
            if await add_note_btn.count() > 0:
                await add_note_btn.click()
                await self.random_delay(0.3, 0.6)

                # Type the message (limited to 300 chars)
                message = message[:300]
                textarea = page.locator(ProfileSelectors.NOTE_TEXTAREA).first
                if await textarea.count() > 0:
                    await textarea.fill(message)
                    await self.random_delay(0.3, 0.5)
                    return True

        except Exception as e:
            logger.debug(f"Failed to add note: {e}")

        return False

    async def _send_request(self) -> bool:
        """Click send button to complete the connection request."""
        page = await self.get_page()

        try:
            # Try primary send button (combined selector for invitation/send now)
            send_btn = page.locator(ProfileSelectors.SEND_BUTTON).first
            if await send_btn.count() > 0:
                await send_btn.click()
                await self.random_delay(1.0, 2.0)

                # Check for success toast
                if await self.exists(CommonSelectors.TOAST_SUCCESS, timeout=3000):
                    return True

                # If no toast, check modal closed (also success)
                if not await self.exists(CommonSelectors.MODAL_CONTAINER, timeout=1000):
                    return True

        except Exception:
            pass

        # Try alternate send button (text-based)
        try:
            send_btn = page.locator(ProfileSelectors.SEND_BUTTON_ALT).first
            if await send_btn.count() > 0:
                await send_btn.click()
                await self.random_delay(1.0, 2.0)

                # Check for success
                if await self.exists(CommonSelectors.TOAST_SUCCESS, timeout=3000):
                    return True
                if not await self.exists(CommonSelectors.MODAL_CONTAINER, timeout=1000):
                    return True
        except Exception:
            pass

        return False

    async def _get_profile_name(self) -> str:
        """Get the name from the profile page."""
        return await self.get_text(ProfileSelectors.PROFILE_NAME, default="Unknown")

    async def _close_modal(self) -> None:
        """Close any open modal dialog."""
        try:
            page = await self.get_page()
            # Use a more specific selector for the connection modal dismiss button
            close_btn = page.locator("div[data-test-modal] button[aria-label='Dismiss']").first
            if await close_btn.count() > 0:
                await close_btn.click()
                return

            # Fallback to general dismiss button
            close_btn = page.locator(ProfileSelectors.CLOSE_MODAL).first
            if await close_btn.count() > 0:
                await close_btn.click()
        except Exception:
            pass
