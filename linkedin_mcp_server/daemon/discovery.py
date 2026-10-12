"""Decide whether this process attaches to a daemon, owns one, or goes it alone.

The discovery half: is there an owner to talk to, and may it be trusted with a
token. Electing and spawning one is :mod:`linkedin_mcp_server.daemon.election`;
forwarding to it is :mod:`linkedin_mcp_server.daemon.proxy`.

A file says nothing about whether the process that wrote it still exists. Only
:meth:`DaemonLock.try_acquire` settles who owns the browser, and only connecting
settles whether anything listens. An owner publishes only once it listens, so a
client that just lost the lock race reads its predecessor's leftovers, which is
why finding an owner takes a wait rather than one read. The entry point returns
an :class:`OwnerLookup`, never a bare attachment, so no caller acts on "there is
a daemon" without the state that says what that is worth.
"""

from __future__ import annotations

import enum
import logging
import math
import queue
import threading
import time
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path

from linkedin_mcp_server import storage_class
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon import descriptor as daemon_descriptor
from linkedin_mcp_server.daemon.descriptor import DaemonDescriptor, DescriptorError
from linkedin_mcp_server.session_state import auth_root_dir, canonical, get_runtime_id
from linkedin_mcp_server.storage_class import StorageClass

logger = logging.getLogger(__name__)

#: Re-read this often while waiting. Short enough that the common case (an owner
#: that is nearly ready) does not feel like a stall. How *long* to wait belongs
#: to the caller that lost the lock race, not here.
_ATTACH_POLL_SECONDS = 0.1
_DESCRIPTOR_READ_SECONDS = 1.0


class _DescriptorReadTimeout(TimeoutError):
    pass


class OwnerState(enum.Enum):
    """What the published descriptor says, in the terms a caller must act on.

    Strictly a reading of one file: no state knows whether its writer still
    exists, so none decides who owns the browser.
    """

    #: Nothing published. The ordinary first start.
    ABSENT = "absent"

    #: A descriptor this client may use, with a token to match. Says the file
    #: is *compatible*, never that anyone is listening: an owner that crashes
    #: after publishing leaves exactly this behind. Whether the endpoint
    #: answers is settled by connecting, and who owns the browser by the lock.
    ATTACHABLE = "attachable"

    #: A descriptor we may not use: it names a different profile, runtime, or
    #: configuration. Says nothing about whether that daemon is still running.
    #: A crashed owner leaves a perfectly valid descriptor behind, so this is
    #: "not for us", never "in use".
    INCOMPATIBLE = "incompatible"

    #: A descriptor exists that cannot be trusted. Distinct from absence
    #: because it must not be *deleted* or explained away: beside a held lock
    #: it is a live daemon this client cannot talk to. Whether the position is
    #: actually free is still the lock's answer, not this one.
    UNTRUSTED = "untrusted"


class Mismatch(enum.Enum):
    """Which check an ``INCOMPATIBLE`` descriptor failed, as a fact to act on.

    Typed, because the election acts on some of these and the reason is prose
    for a log line.
    """

    RUNTIME = "runtime"
    PROFILE = "profile"
    #: Same file format, another tool protocol. The owner may be asked to stand
    #: down and may never be given a tool call.
    PROTOCOL = "protocol"
    #: Same protocol, another configuration fingerprint.
    CONFIGURATION = "configuration"


class DirectFallback(enum.Enum):
    """Why an election ended at once in this client's own browser.

    Silence is no proof the owner is alive, so it is reported apart from an
    answer.
    """

    #: The owner answered its probe.
    LIVE_RIVAL = "a live owner of this version uses a different configuration"
    #: The owner held its port and did not answer within the probe's budget.
    SILENT_RIVAL = (
        "an owner of this version with a different configuration did not answer"
    )


@dataclass(frozen=True)
class Attachment:
    """An owner worth talking to, and the credential for doing so."""

    descriptor: DaemonDescriptor
    #: Kept out of the repr, as the proxy credentials are: DEBUG logs of whole
    #: objects end up in issue reports, and this is a bearer token.
    token: str = field(repr=False)
    #: A pair proved for control and nothing else: the endpoint, profile,
    #: runtime and token all passed, and the owner still may not run a tool for
    #: this client. It comes only with an ``INCOMPATIBLE`` lookup, so it can ask
    #: for turnover or be probed for liveness, and every consumer that would
    #: dispatch a call refuses it (``daemon.proxy``).
    control_only: bool = False


@dataclass(frozen=True)
class Unanswered:
    """An owner generation that did not answer a control request in time.

    A control request or probe addressed to *instance_id* did not complete
    within its positive budget, and nothing about whether a connection opened.
    Two producers only: a heartbeat or preflight that ended in a read timeout
    (``daemon.proxy``), and an election probe that ran out its budget
    (``daemon.election``). Never a tool request, which can be slow while
    heartbeats answer.

    An observation for the text a caller reads, never a decision: nothing
    buries, stops or repairs an owner on it, because a paused or busy owner
    looks exactly like this and may answer a moment later.
    """

    instance_id: str
    pid: int

    @classmethod
    def of(cls, attachment: Attachment) -> Unanswered:
        """The generation *attachment* names, as found silent."""
        descriptor = attachment.descriptor
        return cls(descriptor.instance_id, descriptor.pid)


@dataclass(frozen=True)
class OwnerLookup:
    """The verdict, and the attachment when there is one."""

    state: OwnerState
    attachment: Attachment | None = None
    #: Why, for a log line. Never a token, and never a value read from the
    #: configuration. It can name a path or host from the descriptor when that
    #: is the diagnosis, so it is DEBUG-grade detail and logged as such.
    reason: str = ""
    #: Which check an ``INCOMPATIBLE`` reading failed, where one did.
    mismatch: Mismatch | None = None
    #: Set only by the election, and only after its one bounded probe of an
    #: owner of this same version with another configuration: this client leaves
    #: that owner alone and drives its own browser now, rather than waiting on a
    #: lock the owner may hold. Which value says what the probe established.
    fallback: DirectFallback | None = None

    @property
    def worth_connecting(self) -> bool:
        """Whether there is a compatible endpoint to try talking to.

        Not "an owner is running": a caller whose connection is refused is
        looking at leftovers and must fall through to the lock as if nothing
        had been published.
        """
        return self.state is OwnerState.ATTACHABLE


def _inspect(
    auth_root: Path, profile: Path, config: AppConfig, *, for_retirement: bool = False
) -> OwnerLookup:
    """Read the descriptor once and say what it means.

    Cheap local checks come first, so a descriptor that disqualifies itself is
    rejected before the token is read. *for_retirement* skips the configuration
    comparison: an owner of this profile with other settings still owns the
    browser a profile command is about to change.
    """
    descriptor = daemon_descriptor.read(auth_root)
    if descriptor is None:
        return OwnerLookup(state=OwnerState.ABSENT, reason="no daemon is published")

    # Not enforced by read(), deliberately: it is the caller who knows which
    # runtime it belongs to. A host attaching to a container's daemon would be
    # handed a browser in a different filesystem namespace.
    if descriptor.runtime_id != get_runtime_id():
        return OwnerLookup(
            state=OwnerState.INCOMPATIBLE,
            reason="the published daemon belongs to another runtime",
            mismatch=Mismatch.RUNTIME,
        )

    # The lock is per auth root, but a profile is what a browser opens. Two
    # profiles side by side elect one owner between them, so a published owner
    # can be one this client may not use.
    if not descriptor.serves(profile):
        return OwnerLookup(
            state=OwnerState.INCOMPATIBLE,
            reason="the published daemon serves a different profile",
            mismatch=Mismatch.PROFILE,
        )

    # No endpoint check here: `from_mapping` already refuses a non-local host,
    # so `read` above cannot return one.
    token = daemon_descriptor.read_token(auth_root, descriptor)

    # After every identity check and the token, so a control-only pair is
    # exactly as proved as an attachable one. `read` has already refused a
    # schema this build cannot parse and a protocol below the control floor.
    if descriptor.protocol_version != daemon_descriptor.PROTOCOL_VERSION:
        return OwnerLookup(
            state=OwnerState.INCOMPATIBLE,
            attachment=Attachment(
                descriptor=descriptor, token=token, control_only=True
            ),
            reason=(
                f"the published daemon speaks protocol {descriptor.protocol_version} "
                f"and this build speaks {daemon_descriptor.PROTOCOL_VERSION}"
            ),
            mismatch=Mismatch.PROTOCOL,
        )

    # Keyed with the token, so this can only run once the token is in hand.
    if not for_retirement and descriptor.config_fingerprint != (
        daemon_descriptor.config_fingerprint(config, key=token)
    ):
        return OwnerLookup(
            state=OwnerState.INCOMPATIBLE,
            # Control only, so the election can ask whether this owner is
            # alive before leaving it alone; a descriptor outlives its writer.
            attachment=Attachment(
                descriptor=descriptor, token=token, control_only=True
            ),
            # Names no values: the shared fields include a proxy password and
            # the path to someone's profile.
            reason="the published daemon uses a different configuration",
            mismatch=Mismatch.CONFIGURATION,
        )

    return OwnerLookup(
        state=OwnerState.ATTACHABLE,
        attachment=Attachment(descriptor=descriptor, token=token),
        # Says what the file is, not what any process is doing: nothing has
        # attached yet, and the writer may be dead.
        reason="the published daemon is compatible with this client",
    )


class _DescriptorInspector:
    """Reuse one native descriptor inspection until it has actually finished."""

    def __init__(
        self,
        auth_root: Path,
        profile: Path,
        config: AppConfig,
        *,
        for_retirement: bool = False,
    ) -> None:
        self._auth_root = auth_root
        self._profile = profile
        self._config = config
        self._for_retirement = for_retirement
        self._generation = 0
        self._required_generation = 0
        self._pending: tuple[int, queue.Queue[OwnerLookup | BaseException]] | None = (
            None
        )

    def require_fresh_inspection(self) -> None:
        """Reject any inspection that began before this point."""
        self._required_generation = self._generation + 1

    def inspect_until(self, *, timeout: float) -> OwnerLookup:
        """Wait within one budget without abandoning a blocked native reader."""
        deadline = time.monotonic() + max(timeout, 0.0)
        generation, pending = self._begin()
        while True:
            try:
                value = pending.get(timeout=max(deadline - time.monotonic(), 0.0))
            except queue.Empty:
                raise _DescriptorReadTimeout(
                    "Daemon descriptor state could not be read in time"
                ) from None
            self._pending = None
            if generation >= self._required_generation:
                if isinstance(value, BaseException):
                    raise value
                return value
            generation, pending = self._begin()

    def _begin(self) -> tuple[int, queue.Queue[OwnerLookup | BaseException]]:
        pending = self._pending
        if pending is not None:
            return pending
        self._generation += 1
        generation = self._generation
        result: queue.Queue[OwnerLookup | BaseException] = queue.Queue(maxsize=1)
        pending = (generation, result)
        self._pending = pending

        def inspect() -> None:
            try:
                value: OwnerLookup | BaseException
                # The election's reading is made exactly as it always was; only
                # a retirement lookup passes the flag.
                if self._for_retirement:
                    value = _inspect(
                        self._auth_root,
                        self._profile,
                        self._config,
                        for_retirement=True,
                    )
                else:
                    value = _inspect(self._auth_root, self._profile, self._config)
            except BaseException as exc:  # noqa: BLE001 - re-raised by the caller
                value = exc
            result.put(value)

        threading.Thread(
            target=inspect,
            name="daemon-descriptor-read",
            daemon=True,
        ).start()
        return pending


def _names_an_ignored_instance(lookup: OwnerLookup, ignored: AbstractSet[str]) -> bool:
    """Whether this reading is of a generation the caller has already written off."""
    if not ignored:
        return False
    attachment = lookup.attachment
    return attachment is not None and attachment.descriptor.instance_id in ignored


def look_up_owner(
    auth_root: Path,
    profile: Path,
    config: AppConfig,
    *,
    wait_seconds: float = 0.0,
    ignore_instances: AbstractSet[str] = frozenset(),
    for_retirement: bool = False,
    _inspector: _DescriptorInspector | None = None,
) -> OwnerLookup:
    """Read the descriptor until it is compatible or the budget runs out.

    Every state but ``ATTACHABLE`` is waited out, not only ``ABSENT``: a
    descriptor outlives its writer, so an owner mid-startup can be read
    through a dead predecessor's ``INCOMPATIBLE`` file. ``ATTACHABLE`` only
    says a compatible file is on disk; a caller whose connection then fails
    must go to the lock. With the default of no wait this is a single read.

    *ignore_instances* names generations the caller already judged unusable
    (by connecting, in :mod:`linkedin_mcp_server.daemon.election`). They are
    waited out like any unusable state, so a new generation published over
    them is still seen within one poll and the caller's retry loop does not
    spin at read speed. When the budget ends, the reading is returned as is.

    *for_retirement* skips the configuration comparison (``_inspect``).

    Raises:
        ValueError: *wait_seconds* is not finite.
    """
    # A non-finite budget would never expire, so the stdio process would never
    # finish starting. Refused rather than clamped, as the configuration
    # parsers refuse non-finite timeouts. A negative wait is merely no wait.
    if not math.isfinite(wait_seconds):
        raise ValueError(f"wait_seconds must be a finite number, got {wait_seconds}")

    wait_budget = max(wait_seconds, 0.0)
    deadline = time.monotonic() + wait_budget
    inspector = _inspector or _DescriptorInspector(
        auth_root, profile, config, for_retirement=for_retirement
    )
    last_lookup: OwnerLookup | None = None
    while True:
        remaining = deadline - time.monotonic()
        read_timeout = (
            _DESCRIPTOR_READ_SECONDS
            if wait_budget == 0.0
            else min(_DESCRIPTOR_READ_SECONDS, max(remaining, 0.0))
        )
        try:
            lookup = inspector.inspect_until(timeout=read_timeout)
        except _DescriptorReadTimeout as exc:
            return last_lookup or OwnerLookup(
                state=OwnerState.UNTRUSTED, reason=str(exc)
            )
        except DescriptorError as exc:
            # Distinct from absence so the file is preserved rather than
            # cleaned up: beside a held lock it belongs to a live daemon. It
            # says nothing about whether the position is free, which is why
            # this state still allows an election attempt.
            lookup = OwnerLookup(state=OwnerState.UNTRUSTED, reason=str(exc))

        last_lookup = lookup
        if lookup.state is OwnerState.ATTACHABLE and not _names_an_ignored_instance(
            lookup, ignore_instances
        ):
            return lookup

        # Never sleep past the deadline, or a tiny budget overruns by a whole
        # poll interval.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # The state at INFO, the detail at DEBUG. A reason can quote a path
            # or host out of the descriptor, and "nothing usable was published"
            # is the part that belongs in a log a user pastes into a report.
            # An ignored generation is named as such rather than by its state,
            # which would otherwise print the "attachable" this wait exists to
            # disbelieve.
            if _names_an_ignored_instance(lookup, ignore_instances):
                logger.info("No daemon to attach to (the published one is unusable)")
            else:
                logger.info("No daemon to attach to (%s)", lookup.state.value)
            logger.debug("Daemon lookup detail: %s", lookup.reason)
            return lookup
        time.sleep(min(_ATTACH_POLL_SECONDS, remaining))


def daemon_would_be_used(config: AppConfig) -> bool:
    """Whether this process is even a candidate for sharing a browser.

    Never raises: every refusal or failure to decide means today's Direct
    server, settled before any daemon state is prepared, locked, read or
    spawned. Recovery does not ask again, because ``DaemonProxyBackend`` keeps
    the configuration admitted here.
    """
    try:
        return _daemon_would_be_used(config)
    except Exception:
        logger.warning(
            "Could not decide whether to share a browser; this server drives "
            "its own browser"
        )
        logger.debug("Daemon eligibility failure", exc_info=True)
        return False


def _daemon_would_be_used(config: AppConfig) -> bool:
    if not config.server.daemon_enabled:
        return False
    # An explicit HTTP bind is already one server for many clients, so there is
    # nothing for a daemon to deduplicate. Settle that before container policy:
    # DAEMON_ENABLED on HTTP is inert everywhere, and warning that Docker
    # ignored it because of the display would describe a refusal that never
    # happened.
    if config.server.transport != "stdio":
        return False
    if get_runtime_id().endswith("-container"):
        # Xvfb belongs to PID 1's process group, while the owner starts its own
        # session to outlive a stdio frontend, so in the image it would outlive
        # the display it needs. The container keeps one browser per frontend.
        logger.warning(
            "DAEMON_ENABLED is ignored in a container; the shared-browser "
            "daemon cannot outlive the virtual display owned by this server"
        )
        return False
    if config.browser.chrome_path:
        # Only the bundled browser is shared. A custom executable keeps the
        # Direct server this configuration had before the daemon existed.
        logger.info(
            "CHROME_PATH is set, so this server drives its own browser instead "
            "of sharing one"
        )
        return False
    refusal = _storage_refusal(config)
    if refusal is not None:
        logger.warning(
            "%s; this server drives its own browser instead of sharing one",
            refusal,
        )
        return False
    return True


def _storage_refusal(config: AppConfig) -> str | None:
    """Why the daemon's roots are not on local storage, or None if both are.

    Checks the profile, its auth root and the state root. Only paths are
    computed: creating state for a daemon that will not run is what this
    refusal prevents.
    """
    # The auth root the election would use: ``_obtain_shared_owner`` passes
    # ``auth_root_dir(get_profile_dir())``, and ``get_profile_dir`` is this
    # same field of the configuration ``main`` installed before asking.
    try:
        profile = canonical(Path(config.browser.user_data_dir))
        # The profile too, not only the directory above it: a profile that is
        # itself a mount point can sit on other storage than its parent.
        roots = [
            ("profile directory", profile),
            ("directory holding the profile", auth_root_dir(profile)),
        ]
    except Exception as exc:
        return f"The profile directory could not be resolved ({type(exc).__name__})"
    try:
        # Computes a path and creates nothing; ``prepare_daemon_state`` is
        # what creates it, and only an admitted process reaches that.
        roots.append(("daemon state directory", daemon_descriptor.daemon_state_root()))
    except Exception as exc:
        return f"The daemon state directory could not be located ({type(exc).__name__})"

    for label, root in roots:
        verdict = storage_class.classify(root)
        if verdict.storage_class is not StorageClass.LOCAL:
            return (
                f"The {label} is on {verdict.storage_class.value} storage "
                f"({verdict.reason})"
            )
    return None
