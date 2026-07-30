"""What job a server process does for the shared LinkedIn profile.

Its own module, and deliberately free of imports from the rest of the package.
The role decides how far down the stack behaviour differs — an owner must not
open a login window, a proxy must not take the profile lease — so the modules
that answer those questions need to read it. ``dependencies`` is one of them,
and it cannot import from ``server``: ``server`` imports the tool modules, and
those import ``dependencies``, so the edge would close the cycle.
"""

import enum


class ServerRole(enum.Enum):
    """Which job a server process does for the shared LinkedIn profile.

    One process per MCP client is the transport's doing, not a choice: a stdio
    server is spawned per client instance. That makes "who drives Chromium" a
    property of the process rather than of the code, and every difference below
    follows from it.
    """

    #: Drives its own browser and talks to its own client. The historical
    #: behaviour, and still what an explicit HTTP bind or an embedder gets.
    DIRECT = "direct"

    #: Drives the browser on behalf of other processes over loopback HTTP.
    #: Never speaks to an end client, so nothing user-facing belongs here.
    OWNER = "owner"

    #: Talks to its own client but drives no browser: every tool call is
    #: forwarded to the owner over loopback HTTP. Registers none of the local
    #: browser-backed tools, and serves the owner's instead.
    PROXY = "proxy"

    @property
    def drives_browser(self) -> bool:
        """Whether this role launches Chromium against the shared profile."""
        return self in (ServerRole.DIRECT, ServerRole.OWNER)

    @property
    def faces_a_client(self) -> bool:
        """Whether an end user reads this server's tool results.

        A proxy counts. It is the process the MCP client spawned, so its results
        are the ones a user reads, however little of the work happens here.
        """
        return self in (ServerRole.DIRECT, ServerRole.PROXY)
