"""Exception hierarchy for LinkedIn MCP Server."""


class LinkedInMCPError(Exception):
    """Base exception for MCP-layer errors."""


class SessionExpiredError(LinkedInMCPError):
    """Session has expired and needs to be refreshed."""

    def __init__(self, message: str | None = None):
        default_msg = (
            "LinkedIn session has expired.\n\n"
            "To fix this:\n"
            "  Run with --login to create a new session"
        )
        super().__init__(message or default_msg)


class CredentialsNotFoundError(LinkedInMCPError):
    """No authentication credentials available."""


class AuthenticationError(LinkedInMCPError):
    """Authentication failed."""


class LinkedInScraperException(Exception):
    """Base exception for scraper-layer errors."""


class RateLimitError(LinkedInScraperException):
    """Rate limiting detected."""

    def __init__(self, message: str, suggested_wait_time: int = 300):
        super().__init__(message)
        self.suggested_wait_time = suggested_wait_time


class ProfileNotFoundError(LinkedInScraperException):
    """Profile/page returned 404."""


class NetworkError(LinkedInScraperException):
    """Network-related failure."""


class ScrapingError(LinkedInScraperException):
    """Scraping failed."""
