"""Custom exceptions for LinkedIn scraping operations."""


class LinkedInScraperException(Exception):
    """Base exception for LinkedIn scraper."""

    pass


class AuthenticationError(LinkedInScraperException):
    """Raised when authentication fails."""

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


class ProxyConnectionError(NetworkError):
    """Raised when the configured proxy cannot carry the request.

    A subclass of :class:`NetworkError` on purpose. A proxy outage arrives as a
    failed navigation, which the auth checks would otherwise read as an invalid
    session and answer with "run --login" -- advice that cannot help and that
    retires a perfectly good profile. Keeping it a network error also means any
    handler that has not learned about proxies yet still degrades sensibly.
    """

    pass


class ScrapingError(LinkedInScraperException):
    """Raised when scraping fails for various reasons."""

    pass
