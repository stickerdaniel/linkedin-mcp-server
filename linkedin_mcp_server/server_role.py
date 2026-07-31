"""What job a server process does for the shared LinkedIn profile.

Its own module, and deliberately free of imports from the rest of the package.
The role decides how far down the stack behaviour differs — an owner must not
open a login window, a proxy must not take the profile lease — so the modules
that answer those questions need to read it. ``dependencies`` is one of them,
and it cannot import from ``server``: ``server`` imports the tool modules, and
those import ``dependencies``, so the edge would close the cycle.

Which is why the role also lives here as process state, not only as an argument.
``create_mcp_server`` is handed a role and that settles everything it decides,
but the auth gates sit in ``bootstrap``, reached from a tool body that has no
server to ask, and :func:`process_role` is what those ask instead.
"""

import enum
from typing import Any


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


#: What this process is, for the code that cannot be handed the role.
#:
#: Assembling a server takes the role as an argument, which is enough for
#: everything ``server`` decides. Authentication is not like that: whether a
#: login window may be opened is decided far down the stack, in ``bootstrap``,
#: which is reached from a tool body with no server in sight. A detached owner
#: has no terminal and no desktop session, so it must answer that question
#: differently, and the only thing it can consult is the process it is.
#:
#: Deliberately not in ``AppConfig``. That is settings a user chose, and it
#: travels to the owner over a pipe (``daemon_config``) carrying exactly the
#: fields both ends must agree on. The role is not one of them: an owner knows
#: what it is, and being told by the frontend would be the wrong direction.
#:
#: ``None`` means nothing has claimed this process yet, which is distinct from
#: ``DIRECT``. Using ``DIRECT`` for both would make the claim below unable to
#: tell "nobody said" from "somebody said single-process", and a real ``DIRECT``
#: server followed by an ``OWNER`` would then be accepted: the ``DIRECT``
#: server's own tool calls would afterwards read ``OWNER`` and refuse the logins
#: it is supposed to perform.
_role: ServerRole | None = None


class RoleAlreadyClaimedError(RuntimeError):
    """Two different roles were claimed in one process.

    Not resolvable by picking one. The role decides whether this process may open
    a login window, and a proxy and an owner cannot both be right about that, so
    the second claim is refused rather than applied or ignored.
    """


def set_process_role(role: ServerRole) -> None:
    """Claim this process for *role*, or refuse if another already has it.

    The check lives here rather than at the call sites so every entry point is
    covered by one rule. Re-stating the same role is allowed: the owner records
    it at its entry point and again when it assembles its server, and a test
    building several servers of one kind is doing nothing contradictory.

    Not locked. Every production caller builds exactly one server synchronously
    before anything else runs, so there is no window to protect. A threaded
    embedder claiming two roles at once would need one, and would also need to
    explain which of them was supposed to win.
    """
    global _role
    if _role is not None and _role is not role:
        raise RoleAlreadyClaimedError(
            f"This process already serves as {_role.value}, so it cannot also "
            f"serve as {role.value}"
        )
    _role = role


def process_role() -> ServerRole:
    """What this process is, defaulting to the historical single-process server.

    ``DIRECT`` when nothing has claimed it, so an embedder that never calls
    :func:`set_process_role`, and every test that does not care, get exactly the
    behaviour they had. The unclaimed state is deliberately not visible here:
    callers ask what this process *is*, and "not yet decided" is not an answer
    any of them could act on.
    """
    return ServerRole.DIRECT if _role is None else _role


#: Set when this process can no longer serve and has to be replaced.
#:
#: Only an owner uses it. A single-process server telling its own client to
#: restart is an instruction someone can follow; a detached owner saying the same
#: is not, because it outlives the client that started it and the next frontend
#: attaches straight back to it. So the process that cannot recover has to remove
#: itself, and the serve loop is the only place that can do it cleanly.
_must_stand_down: list[str] = []


def ask_this_process_to_stand_down(why: str) -> None:
    """Record that this process must exit so a replacement can take over.

    Recorded rather than acted on. This is called from inside a tool call, and an
    owner that stopped serving mid-call would leave the client unable to tell a
    completed handover from a refusal. The serve loop notices, finishes what is
    in flight, and exits; the kernel frees the daemon lock, and the next call
    elects a fresh owner.

    Safe to call more than once, and cheap enough that no caller has to check
    first.
    """
    _must_stand_down.append(why)


def stand_down_reason() -> str | None:
    """Why this process must exit, or ``None`` while it may keep serving."""
    return _must_stand_down[0] if _must_stand_down else None


def a_held_profile_means_this_owner_must_go(lease: Any | None = None) -> None:
    """Give way if this is an owner an unconfirmed teardown has stranded.

    One function rather than the same three lines at each site, because the sites
    kept being found one at a time. A browser whose shutdown could not be
    confirmed keeps the profile lease until its process exits, deliberately. For
    a single-process server the resulting "restart the server" is actionable: its
    client owns it. A detached owner outlives that client, so restarting elects
    nothing, every later call meets ``BrowserBusyError``, and only a manual kill
    recovers. Measured live: three consecutive fresh clients attached to the same
    stranded owner.

    Called from wherever the lease is *kept*, not from wherever the failure is
    reported, and the difference is what the third site turned on: the routine
    close path returns normally, so a handoff, the idle timeout and
    ``close_session`` strand an owner without raising anything at all.

    *lease* is the object the caller already holds, and passing it is what keeps
    this function off the path-based lookup. That lookup resolves a directory and
    consults a registry, neither of which the caller needs: it is holding the
    very lease being asked about. Callers with nothing in hand may still omit it
    and get the lookup, which is right for the ones reasoning about the profile
    rather than about an object they own.

    A read that fails asks anyway. Not being able to tell is not the same as
    being told the profile is free, and the two outcomes are not symmetric: an
    unnecessary stand-down costs one election, while a stranded owner costs every
    later call until somebody kills it by hand. Measured with the read raising
    ``PermissionError``: the owner kept serving with the question unanswered.

    Typed loosely on purpose. ``ProfileLease`` lives in a module this one must not
    import — the whole point of ``server_role`` is that it depends on nothing —
    and the only thing wanted here is ``browser_open``.

    Never raises. The caller is mid-teardown or mid-failure and has its own
    exception to deliver.
    """
    if process_role() is not ServerRole.OWNER:
        return

    try:
        if lease is None:
            from linkedin_mcp_server.profile_lease import get_profile_lease

            lease = get_profile_lease()
        held = lease.browser_open
    except Exception:  # noqa: BLE001 - unreadable is not the same as free
        held = True
    if not held:
        return
    ask_this_process_to_stand_down(
        "the browser did not shut down cleanly, so the profile is held"
    )


def reset_process_role_for_testing() -> None:
    """Return to the unclaimed state, for test isolation.

    Without this an ``OWNER`` claimed by one test would refuse logins in every
    test after it, in a suite where most never mention a role at all.
    """
    global _role
    _role = None
    _must_stand_down.clear()
