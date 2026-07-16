"""Custom exceptions for LinkedIn scraping operations."""


class LinkedInScraperException(Exception):
    """Base exception for LinkedIn scraper."""

    pass


class AuthenticationError(LinkedInScraperException):
    """Raised when authentication fails."""

    pass


class ChallengeError(AuthenticationError):
    """Raised when LinkedIn shows a recoverable interactive barrier -- a
    security checkpoint, email verification, the saved-account chooser, or
    a profile page that rendered with no real content (a common signal of
    a silent soft-block). Distinct from BlockError: plausibly clearable by
    a retry or a fresh interactive login, not necessarily a dead session.
    """

    pass


class BlockError(AuthenticationError):
    """Raised when the session is simply not authenticated -- a hard login
    wall (e.g. redirected to /login or /authwall). Needs a full re-login
    (--login), not a retry."""

    pass


class RateLimitError(LinkedInScraperException):
    """Raised when rate limiting is detected."""

    def __init__(self, message: str, suggested_wait_time: int = 300):
        super().__init__(message)
        self.suggested_wait_time = suggested_wait_time


class ElementNotFoundError(LinkedInScraperException):
    """Raised when an expected element is not found."""

    pass


class ProfileNotFoundError(LinkedInScraperException):
    """Raised when a profile/page returns 404."""

    pass


class NetworkError(LinkedInScraperException):
    """Raised when network-related issues occur."""

    pass


class BrowserTeardownError(NetworkError):
    """Browser ownership is uncertain after bounded cleanup failed."""

    pass


class ScrapingError(LinkedInScraperException):
    """Raised when scraping fails for various reasons."""

    pass
