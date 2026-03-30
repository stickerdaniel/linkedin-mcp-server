"""Re-exports from linkedin_mcp_server.exceptions for backward compatibility."""

from linkedin_mcp_server.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
    NetworkError,
    ProfileNotFoundError,
    RateLimitError,
    ScrapingError,
)

__all__ = [
    "AuthenticationError",
    "LinkedInScraperException",
    "NetworkError",
    "ProfileNotFoundError",
    "RateLimitError",
    "ScrapingError",
]
