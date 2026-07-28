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

    # A forwarding role belongs here too, but only once something actually
    # forwards. Adding it now would mean a server that registers the local
    # browser-backed tools and then declines to serialize them, which is a
    # worse starting point than not having the role at all.

    @property
    def drives_browser(self) -> bool:
        """Whether this role launches Chromium against the shared profile."""
        return self in (ServerRole.DIRECT, ServerRole.OWNER)

    @property
    def faces_a_client(self) -> bool:
        """Whether an end user reads this server's tool results."""
        return self is ServerRole.DIRECT
