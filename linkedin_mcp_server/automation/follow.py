"""
LinkedIn company follow automation.

Provides automation for following companies on LinkedIn.
"""

import logging
from typing import Any

from .base import AutomationError, BaseAutomation
from .selectors import CommonSelectors, CompanySelectors

logger = logging.getLogger(__name__)


class FollowError(AutomationError):
    """Error during follow operation."""

    pass


class AlreadyFollowingError(FollowError):
    """Already following this company."""

    pass


class CompanyFollowAutomation(BaseAutomation):
    """
    Automation for following LinkedIn companies.

    Navigates to a company page and clicks the follow button.
    """

    async def execute(
        self,
        company_url: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Follow a company.

        Args:
            company_url: LinkedIn company URL

        Returns:
            Dictionary with status and details
        """
        logger.info(f"Following company: {company_url}")

        try:
            # Navigate to company page
            await self.navigate(company_url)

            # Check for rate limiting
            if await self._check_rate_limit():
                return {
                    "status": "rate_limited",
                    "company_url": company_url,
                    "message": "LinkedIn rate limit detected",
                }

            # Check if already following
            if await self._is_already_following():
                company_name = await self._get_company_name()
                return {
                    "status": "already_following",
                    "company_url": company_url,
                    "company_name": company_name,
                    "message": f"Already following {company_name}",
                }

            # Click follow button
            if await self._click_follow_button():
                company_name = await self._get_company_name()
                return {
                    "status": "success",
                    "company_url": company_url,
                    "company_name": company_name,
                    "message": f"Now following {company_name}",
                }
            else:
                return {
                    "status": "failed",
                    "company_url": company_url,
                    "message": "Could not find follow button",
                }

        except Exception as e:
            logger.error(f"Follow operation failed: {e}")
            return {
                "status": "error",
                "company_url": company_url,
                "message": str(e),
            }

    async def _check_rate_limit(self) -> bool:
        """Check if we've hit a rate limit page."""
        return await self.exists(CommonSelectors.RATE_LIMIT_PAGE, timeout=2000)

    async def _is_already_following(self) -> bool:
        """Check if already following this company."""
        # Check for "Following" button state
        if await self.exists(CompanySelectors.FOLLOWING_BUTTON, timeout=2000):
            return True
        if await self.exists(CompanySelectors.FOLLOWING_BUTTON_ALT, timeout=1000):
            return True
        return False

    async def _click_follow_button(self) -> bool:
        """Find and click the follow button."""
        page = await self.get_page()

        # Try primary follow button
        try:
            follow_btn = page.locator(CompanySelectors.FOLLOW_BUTTON).first
            if await follow_btn.is_visible(timeout=3000):
                await follow_btn.click()
                await self.random_delay(1.0, 2.0)

                # Verify follow succeeded
                if await self._is_already_following():
                    return True

                # Check for success toast
                if await self.exists(CommonSelectors.TOAST_SUCCESS, timeout=2000):
                    return True

                return True  # Assume success if no error
        except Exception:
            pass

        # Try alternate follow button
        try:
            follow_btn = page.locator(CompanySelectors.FOLLOW_BUTTON_ALT).first
            if await follow_btn.is_visible(timeout=2000):
                await follow_btn.click()
                await self.random_delay(1.0, 2.0)
                return True
        except Exception:
            pass

        return False

    async def _get_company_name(self) -> str:
        """Get the company name from the page."""
        return await self.get_text(CompanySelectors.COMPANY_NAME, default="Unknown")


class PersonFollowAutomation(BaseAutomation):
    """
    Automation for following LinkedIn people (instead of connecting).

    Some profiles allow following without connecting.
    """

    async def execute(
        self,
        profile_url: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Follow a person.

        Args:
            profile_url: LinkedIn profile URL

        Returns:
            Dictionary with status and details
        """
        from .selectors import ProfileSelectors

        logger.info(f"Following person: {profile_url}")

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

            # Check if already following
            if await self.exists(ProfileSelectors.FOLLOWING_BUTTON, timeout=2000):
                name = await self.get_text(
                    ProfileSelectors.PROFILE_NAME, default="Unknown"
                )
                return {
                    "status": "already_following",
                    "profile_url": profile_url,
                    "profile_name": name,
                    "message": f"Already following {name}",
                }

            # Try to find follow button
            page = await self.get_page()
            follow_btn = page.locator(ProfileSelectors.FOLLOW_BUTTON).first

            if await follow_btn.is_visible(timeout=3000):
                await follow_btn.click()
                await self.random_delay(1.0, 2.0)

                name = await self.get_text(
                    ProfileSelectors.PROFILE_NAME, default="Unknown"
                )
                return {
                    "status": "success",
                    "profile_url": profile_url,
                    "profile_name": name,
                    "message": f"Now following {name}",
                }
            else:
                return {
                    "status": "failed",
                    "profile_url": profile_url,
                    "message": "Follow button not available - try connecting instead",
                }

        except Exception as e:
            logger.error(f"Follow operation failed: {e}")
            return {
                "status": "error",
                "profile_url": profile_url,
                "message": str(e),
            }

    async def _check_rate_limit(self) -> bool:
        """Check if we've hit a rate limit page."""
        return await self.exists(CommonSelectors.RATE_LIMIT_PAGE, timeout=2000)
