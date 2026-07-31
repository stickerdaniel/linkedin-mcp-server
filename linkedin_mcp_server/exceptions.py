# src/linkedin_mcp_server/exceptions.py
"""
Custom exceptions for LinkedIn MCP Server with specific error categorization.

Defines hierarchical exception types for different error scenarios including
authentication failures and MCP client reporting.
"""


class LinkedInMCPError(Exception):
    """Base exception for LinkedIn MCP Server."""

    pass


class CredentialsNotFoundError(LinkedInMCPError):
    """No credentials available in non-interactive mode."""

    pass


class SessionExpiredError(LinkedInMCPError):
    """Session has expired and needs to be refreshed."""

    def __init__(self, message: str | None = None):
        default_msg = (
            "LinkedIn session has expired.\n\n"
            "To fix this:\n"
            "  Run with --login to create a new session"
        )
        super().__init__(message or default_msg)


class BrowserSetupInProgressError(LinkedInMCPError):
    """Patchright Chromium browser setup is still running."""


class BrowserSetupFailedError(LinkedInMCPError):
    """Patchright Chromium browser setup failed."""


class AuthenticationStartedError(LinkedInMCPError):
    """Interactive LinkedIn login has been started."""


class AuthenticationInProgressError(LinkedInMCPError):
    """Interactive LinkedIn login is already running."""


class AuthenticationBootstrapFailedError(LinkedInMCPError):
    """Interactive LinkedIn login could not be completed."""


class OwnerCannotAuthenticateError(LinkedInMCPError):
    """The shared browser owner found bad auth and cannot fix it itself.

    A detached owner has nobody in front of it. It can open a browser, and does
    when configured to run headed, but an interactive login is different in kind:
    it waits for someone to type credentials and answer a challenge, and no client
    is attached to this process to do that. The same goes for a keychain prompt.
    It also must not move the shared profile aside: the process that will log in
    needs to find the session state as the owner saw it, and rotating from here
    would race that login for the same files.

    So the owner reports instead of acting, and the frontend, which is the process
    an MCP client actually spawned, does the work. These two carry which of the
    two situations it is, because the frontend's response differs:

    * missing means no usable session on disk, so a plain login is enough;
    * stale means a session that exists and no longer works, which has to be
      retired before a new one can replace it.

    Subclasses rather than one class with a field: they are raised from different
    places and the distinction has to survive being caught by type.

    ``nothing_ran_yet`` is carried separately from which subclass this is, because
    the two are independent and conflating them costs correctness. Whether the
    session is missing or stale says what has to be repaired; whether any work has
    happened says whether the call may be run again afterwards. Both combinations
    occur: a stale session is usually found by the readiness check before a tool
    has done anything, and only the owner can tell, because by the time a failure
    reaches a client every origin looks the same.

    It defaults to False so a path that has not thought about it is not replayed.
    """

    def __init__(
        self,
        message: str,
        *,
        nothing_ran_yet: bool = False,
        generation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.nothing_ran_yet = nothing_ran_yet
        #: The login generation this owner found broken, as observed at the
        #: moment it decided. Carried rather than re-read later: the profile
        #: lease is released once the browser closes, so a second read can see a
        #: generation somebody else has just written, and a frontend acting on
        #: that would rotate away the very session it names. ``None`` is a value
        #: (a rotated profile has no generation), not an absence.
        self.generation = generation


class AuthMissingOnOwnerError(OwnerCannotAuthenticateError):
    """No usable LinkedIn session exists, and the owner cannot create one."""


class AuthStaleOnOwnerError(OwnerCannotAuthenticateError):
    """The LinkedIn session stopped working, and the owner cannot replace it."""


class DockerHostLoginRequiredError(LinkedInMCPError):
    """Docker runtime requires host-side login creation."""


class LinuxBrowserDependencyError(LinkedInMCPError):
    """Linux host dependencies required for Chromium are missing."""


class BrowserBinaryMissingError(LinkedInMCPError):
    """Patchright Chromium binary is absent or stale on disk."""


class CookieDecryptionError(LinkedInMCPError):
    """A browser cookie could not be decrypted."""


class KeystoreUnavailableError(CookieDecryptionError):
    """The OS keystore holding the browser's Safe Storage key is unavailable."""


class V20EncryptedError(CookieDecryptionError):
    """Cookie uses Chrome 127+ app-bound encryption (v20); needs OS elevation."""


class NoLinkedInSessionFoundError(LinkedInMCPError):
    """No discoverable local browser profile has a decryptable LinkedIn (li_at) session."""


class BrowserShutdownUnconfirmedError(LinkedInMCPError):
    """A browser's teardown did not complete, so it may still hold the profile.

    Distinct from an ordinary failure because the recovery differs: the profile
    must be left exactly as it is. Resetting it, restoring over it, or trying
    the next candidate would all write underneath a Chromium that may still be
    running.

    Raising it also asks a shared owner to stand down, because for that process
    the message below is not something anyone can act on: it outlives the client
    that started it, so restarting the client re-attaches to the same held
    profile. Measured live against a real detached owner, over a teardown that
    timed out after 10 seconds: three consecutive fresh clients each attached to
    the same wedged owner and got the same refusal.

    The request lives here rather than at the raise sites because that is what
    makes it complete. It sat at one site once, in ``handle_auth_error``, and the
    path this was measured on never goes through it: the failure came from
    ``_authenticate_existing_profile``, deep in the driver.

    Not sufficient on its own, though, and the reason is an ordering. This runs
    while the raise is still in flight, so a site that marks the profile held
    *after* constructing the error is invisible here and has to ask for itself;
    ``_create_browser`` is exactly that, and was measured wedged with this in
    place.
    """

    def __init__(self, message: str | None = None):
        super().__init__(
            message
            or (
                "A browser on this profile did not shut down cleanly and may "
                "still be running. Restart the server to recover."
            )
        )
        # Imported here rather than at module scope: this module is imported by
        # nearly everything, and `server_role` must stay free of cycles.
        from linkedin_mcp_server.server_role import (
            a_held_profile_means_this_owner_must_go,
        )

        a_held_profile_means_this_owner_must_go()


class BrowserBusyError(LinkedInMCPError):
    """Another server process holds the shared browser profile.

    Deliberately not an ``AuthenticationError``: that class is routed into
    ``invalidate_auth_and_trigger_relogin``, which force-retires the shared
    profile. Classifying contention as an auth failure would let a process that
    merely lost a race destroy every other process's session.
    """

    def __init__(self, message: str | None = None):
        super().__init__(
            message
            or (
                "Another LinkedIn MCP client is currently using the browser. "
                "This is not a failure and your saved session was not changed. "
                "Wait a moment and call this exact tool again."
            )
        )
