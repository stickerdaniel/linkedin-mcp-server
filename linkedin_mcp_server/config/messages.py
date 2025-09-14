# linkedin_mcp_server/config/messages.py
"""
Centralized message formatting for consistent user communication across contexts.

Provides structured error and informational messages with context-aware formatting
for interactive vs non-interactive modes and different authentication scenarios.
"""


class ErrorMessages:
    """Centralized error message formatting for consistent communication."""

    @staticmethod
    def no_cookie_found(is_interactive: bool) -> str:
        """
        Generate appropriate error message when no LinkedIn cookie is found.

        Args:
            is_interactive: Whether the application is running in interactive mode

        Returns:
            str: Formatted error message with appropriate instructions
        """
        if is_interactive:
            return "No LinkedIn authentication found. Please run setup to configure authentication."
        else:
            return (
                "No LinkedIn cookie found. You can:\n"
                "  1. Run with --get-cookie to extract a cookie using email/password\n"
                "  2. Set LINKEDIN_COOKIE environment variable with a valid LinkedIn session cookie"
            )

    @staticmethod
    def no_credentials_found() -> str:
        """Error message when credentials are required but not found."""
        return (
            "No LinkedIn credentials found. Please provide credentials via "
            "environment variables (LINKEDIN_EMAIL, LINKEDIN_PASSWORD) for setup."
        )

    @staticmethod
    def invalid_cookie_format(cookie_sample: str) -> str:
        """
        Error message for invalid cookie format.

        Args:
            cookie_sample: Sample of the invalid cookie (truncated for security)

        Returns:
            str: Formatted error message
        """
        # Only show first 20 characters for security
        safe_sample = (
            cookie_sample[:20] + "..." if len(cookie_sample) > 20 else cookie_sample
        )
        return (
            f"Invalid LinkedIn cookie format: '{safe_sample}'. "
            "Cookie should be a LinkedIn session token (li_at=...) or raw token value."
        )

    @staticmethod
    def authentication_setup_instructions() -> str:
        """Instructions for setting up authentication."""
        return (
            "To set up LinkedIn authentication:\n"
            "  1. Run with --get-cookie flag to extract a session cookie\n"
            "  2. Or set LINKEDIN_COOKIE environment variable\n"
            "  3. Or run interactively to enter credentials"
        )


class InfoMessages:
    """Centralized informational message formatting."""

    @staticmethod
    def credentials_stored_securely() -> str:
        """Message when credentials are successfully stored."""
        return "Credentials stored securely in system keyring"

    @staticmethod
    def cookie_stored_securely() -> str:
        """Message when cookie is successfully stored."""
        return "Cookie stored securely in system keyring"

    @staticmethod
    def keyring_storage_failed() -> str:
        """Warning when keyring storage fails."""
        return "Could not store credentials in system keyring"

    @staticmethod
    def using_cookie_from(source: str) -> str:
        """
        Message indicating cookie source.

        Args:
            source: Source of the cookie (e.g., "environment", "keyring", "configuration")

        Returns:
            str: Formatted message
        """
        return f"Using LinkedIn cookie from {source}"
