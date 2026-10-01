"""Custom exceptions for LinkedIn operations."""


class LinkedInOperationError(Exception):
    """Base exception for LinkedIn operations."""

    pass


class InvalidReferenceError(LinkedInOperationError):
    """A caller-supplied profile, company, job or thread reference is unusable.

    Separate from the other operation errors because nothing is broken: the
    argument is wrong and the message says how to correct it. `raise_tool_error`
    keeps it free of issue-report diagnostics for that reason.
    """

    pass


class AuthenticationError(LinkedInOperationError):
    """Raised when authentication fails."""

    pass


class AccountRestrictedError(LinkedInOperationError):
    """LinkedIn restricted the account and wants identity verification.

    Deliberately not an ``AuthenticationError``: every tool routes that class
    into ``handle_auth_error``, which retires the session state and opens a
    login window. No login can lift a restriction, so that recovery would
    discard the session and then wait for a sign-in that cannot complete.
    """

    def __init__(self, message: str | None = None):
        super().__init__(
            message
            or (
                "LinkedIn has restricted access to this account and asks for "
                "identity verification. Resolve it on linkedin.com in your own "
                "browser. The server will not open a login window or retry; "
                "once LinkedIn lifts the restriction, run --login."
            )
        )


class RateLimitError(LinkedInOperationError):
    """Raised when rate limiting is detected."""

    def __init__(self, message: str, suggested_wait_time: int = 300):
        super().__init__(message)
        self.suggested_wait_time = suggested_wait_time


class ElementNotFoundError(LinkedInOperationError):
    """Raised when an expected element is not found."""

    pass


class ProfileNotFoundError(LinkedInOperationError):
    """Raised when a profile/page returns 404."""

    pass


class NetworkError(LinkedInOperationError):
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


class PageReadError(LinkedInOperationError):
    """Raised when reading a page fails for various reasons."""

    pass
