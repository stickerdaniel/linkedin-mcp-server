"""What one look at the published owner establishes, decided without looking.

One pass reads the canonical descriptor, waits a little for a usable one, then
acts: an attachable owner is probed, an older one asked to stand down whatever
its configuration, one this frontend may only control asked, written off or
probed once, and leftovers buried. ``daemon.election._live_lookup`` carries out
the effects.

"Older" and "same version" compare package versions, never build identity: an
editable install or rebuild keeps its version.

- A descriptor that fails the probe is downgraded, never deleted: deleting it
  would strand a live but unreachable owner and its clients. The lock decides.
  A refusal buries the generation; a silence records nothing, so the next pass
  asks again.
- An owner of this version with another configuration is probed once.
  Answering or silent, it is left alone and the election ends in Direct
  (:attr:`Finding.LIVE_RIVAL`, :attr:`Finding.SILENT_RIVAL`) with no turnover,
  start or second probe. Refused, it is leftovers.
- An older owner with another configuration is turned over like one with this
  configuration (docs/decisions/2026-10-12-older-owner-turnover.md). A
  frontend attached to it with that other configuration gets no continuity:
  its recovery cannot attach to the successor. Concurrent older frontends are
  outside the contract's client scope.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace


class Reach(enum.Enum):
    """What a probe of a published endpoint established.

    "No answer" is two opposite situations: a gone process leaves a closed
    port that refuses at once, while a stalled one still holds its socket and
    the probe waits out its budget. Treating a stall as gone buries a healthy
    owner, so the two stay apart.
    """

    #: The endpoint answered an authenticated request. Attach to it.
    ANSWERED = "answered"

    #: Nothing is serving at that address, or something is and it is not this
    #: owner. Either way the descriptor is leftovers.
    REFUSED = "refused"

    #: Something holds the port and did not answer in time. No proof it is
    #: alive, and no proof it is dead.
    SILENT = "silent"


class Finding(enum.Enum):
    """What the pass reports, which the shell turns into an ``OwnerLookup``."""

    #: The reading itself: an owner that answered its probe, or an unusable
    #: reading the pass waited out.
    AS_READ = "as_read"
    #: The deadline passed before a read could start.
    NOT_READ = "not_read"
    #: The read did not finish within its budget.
    READ_TIMED_OUT = "read_timed_out"
    #: The reading names a generation this election already wrote off.
    ALREADY_UNUSABLE = "already_unusable"
    #: The owner's version is older, whatever its configuration, and it is asked
    #: to stand down if this pass may ask.
    OLDER_VERSION = "older_version"
    #: The deadline passed before the probe could start.
    NOT_PROBED = "not_probed"
    #: The probe was not answered in time. Nothing is recorded.
    NOT_ANSWERED_YET = "not_answered_yet"
    #: The probe was refused, so the descriptor is leftovers.
    LEFTOVERS = "leftovers"
    #: An owner this frontend may only control, written off so the lock decides.
    WRITTEN_OFF = "written_off"
    #: Same version, another configuration, answered: Direct, at once.
    LIVE_RIVAL = "live_rival"
    #: Same version, another configuration, silent: Direct, at once.
    SILENT_RIVAL = "silent_rival"


class Turnover(enum.Enum):
    """Why an owner is asked to stand down, which is all the shell logs about it."""

    OLDER_VERSION = "older_version"
    OLDER_PROTOCOL = "older_protocol"


@dataclass(frozen=True)
class Reading:
    """One completed read of the published descriptor, as the decision needs it.

    Every field but *attachable* describes the pair the read proved, and is
    empty when it proved none: nothing published, an untrusted descriptor, or
    one that failed a check before its token was read.
    """

    instance: str | None = None
    #: A compatible endpoint to try talking to (``OwnerLookup.worth_connecting``).
    attachable: bool = False
    #: Proved for its endpoint, profile, runtime and token, and still not one
    #: this client may give a tool call.
    control_only: bool = False
    #: The owner speaks another tool protocol.
    other_protocol: bool = False
    #: The owner speaks this protocol with another configuration fingerprint.
    other_config: bool = False
    #: The owner's package version is older than this frontend's.
    older: bool = False
    #: The owner's package version equals this frontend's. Not build identity.
    same_version: bool = False


@dataclass(frozen=True)
class Timings:
    """The pass's bounds, in seconds, as the shell configures them."""

    #: The longest one descriptor read may take.
    read: float
    #: The pause between two reads while the pass waits for a usable one.
    poll: float
    #: The longest the probe of a published endpoint may take.
    probe: float


class Step(enum.Enum):
    READING = "reading"
    PAUSING = "pausing"
    PROBING = "probing"
    DONE = "done"


@dataclass(frozen=True)
class Lookup:
    """One pass's state between events."""

    #: Absolute, on the shell's monotonic clock: the election's own deadline.
    deadline: float
    #: Where the pass stops waiting for a usable reading. Never past *deadline*.
    wait_until: float
    #: Whether this pass may ask an older owner to stand down.
    may_ask: bool
    #: Generations already written off, as the pass began.
    buried: frozenset[str]
    timings: Timings
    step: Step = Step.READING
    #: The operation the pass is waiting on. Any other completion is stale.
    pending: int = 0
    #: The reading being probed.
    probing: Reading | None = None


# --- Events: each completes the operation it names --------------------------------


@dataclass(frozen=True)
class Read:
    op: int
    now: float
    reading: Reading


@dataclass(frozen=True)
class ReadTimedOut:
    op: int
    now: float


@dataclass(frozen=True)
class Paused:
    op: int
    now: float


@dataclass(frozen=True)
class Probed:
    op: int
    now: float
    reach: Reach


Event = Read | ReadTimedOut | Paused | Probed


# --- Effects: work for the shell ---------------------------------------------------


@dataclass(frozen=True)
class ReadDescriptor:
    op: int
    timeout: float


@dataclass(frozen=True)
class Pause:
    op: int
    seconds: float


@dataclass(frozen=True)
class Probe:
    """Ask the published endpoint, with its token, within *timeout*."""

    op: int
    timeout: float


@dataclass(frozen=True)
class AskToStandDown:
    """Send the stable turnover request to the owner just read. Nothing completes it.

    Whether the owner complies is never assumed: the lock says when the browser
    is free.
    """

    because: Turnover


@dataclass(frozen=True)
class LeaveAlone:
    """An owner of another protocol that is not older is left running."""


@dataclass(frozen=True)
class Bury:
    """Write *instance* off for the rest of the election."""

    instance: str


@dataclass(frozen=True)
class Conclude:
    """The pass is over.

    *asked* is whether it sent a turnover request, and *observed* whether its
    finding comes from a completed read, which is what settlement keeps when a
    later read times out.
    """

    finding: Finding
    asked: bool = False
    observed: bool = True


Effect = ReadDescriptor | Pause | Probe | AskToStandDown | LeaveAlone | Bury | Conclude

#: Effects that wait for the event completing them. At most one per decision.
WAITS = (ReadDescriptor, Pause, Probe)

_EXPECTED: dict[Step, tuple[type, ...]] = {
    Step.READING: (Read, ReadTimedOut),
    Step.PAUSING: (Paused,),
    Step.PROBING: (Probed,),
}


def look(
    *,
    now: float,
    deadline: float,
    wait: float,
    may_ask: bool,
    buried: frozenset[str],
    timings: Timings,
) -> tuple[Lookup, list[Effect]]:
    """A pass starting at *now* that may wait *wait* seconds for a usable reading.

    The wait lets a paced pass watch the descriptor instead of sleeping through
    a replacement, and it ends at *deadline* at the latest.
    """
    lookup = Lookup(
        deadline=deadline,
        wait_until=min(deadline, now + max(wait, 0.0)),
        may_ask=may_ask,
        buried=buried,
        timings=timings,
    )
    return _read(lookup, now)


def on_event(lookup: Lookup, event: Event) -> tuple[Lookup, list[Effect]]:
    """The pass after *event*, and what the shell must do next."""
    if lookup.step is Step.DONE or event.op != lookup.pending:
        return lookup, []
    if not isinstance(event, _EXPECTED[lookup.step]):
        raise ValueError(
            f"{type(event).__name__} does not complete a pass that is "
            f"{lookup.step.value}"
        )
    match event:
        case Read():
            return _read_done(lookup, event.reading, event.now)
        case ReadTimedOut():
            return _conclude(lookup, Finding.READ_TIMED_OUT, observed=False)
        case Paused():
            return _read(lookup, event.now)
        case Probed():
            return _probed(lookup, event.reach)


def wants_a_decision(reading: Reading) -> bool:
    """Whether an unusable reading still needs the election to act on it.

    Three control-only kinds need action rather than waiting: a protocol
    mismatch (ask to stand down or write off), an older owner with another
    configuration (ask to stand down) and one of this version with another
    configuration (probe). A newer or unknown version with another
    configuration is paced like every other reading, so a caller does not spin
    at read speed on a file it cannot use.
    """
    if not reading.control_only:
        return False
    return reading.other_protocol or (
        reading.other_config and (reading.older or reading.same_version)
    )


def _read(lookup: Lookup, now: float) -> tuple[Lookup, list[Effect]]:
    remaining = lookup.deadline - now
    if remaining <= 0:
        return _conclude(lookup, Finding.NOT_READ, observed=False)
    op = lookup.pending + 1
    return replace(lookup, step=Step.READING, pending=op), [
        ReadDescriptor(op, min(lookup.timings.read, remaining))
    ]


def _read_done(
    lookup: Lookup, reading: Reading, now: float
) -> tuple[Lookup, list[Effect]]:
    ignored = reading.instance is not None and reading.instance in lookup.buried
    # Before the generic filter, not after it: a control-only reading is never
    # worth connecting to, and discarded here it would never reach the turnover
    # or the liveness probe below.
    if (reading.attachable or wants_a_decision(reading)) and not ignored:
        return _decide(lookup, reading, now)
    remaining = min(lookup.wait_until, lookup.deadline) - now
    if remaining <= 0:
        found = Finding.ALREADY_UNUSABLE if ignored else Finding.AS_READ
        return _conclude(lookup, found)
    op = lookup.pending + 1
    return replace(lookup, step=Step.PAUSING, pending=op), [
        Pause(op, min(lookup.timings.poll, remaining))
    ]


def _decide(
    lookup: Lookup, reading: Reading, now: float
) -> tuple[Lookup, list[Effect]]:
    instance = reading.instance
    if instance is None:
        raise ValueError("a reading worth deciding on names no instance")
    if reading.older and not reading.other_protocol:
        # Whatever its configuration: a control-only pair is as proved as an
        # attachable one, and an older owner left running would keep the
        # browser from every newer frontend.
        #
        # Classification and turnover are separate. Settlement never sends the
        # request, but it still writes this generation off so its paced reads
        # wait for a replacement instead of rediscovering the same stale owner
        # at speed.
        ask: list[Effect] = (
            [AskToStandDown(Turnover.OLDER_VERSION)] if lookup.may_ask else []
        )
        return _conclude(
            lookup,
            Finding.OLDER_VERSION,
            asked=lookup.may_ask,
            first=[*ask, Bury(instance)],
        )
    if reading.control_only:
        return _decide_control_only(lookup, reading, instance, now)
    return _probe(lookup, reading, now)


def _decide_control_only(
    lookup: Lookup, reading: Reading, instance: str, now: float
) -> tuple[Lookup, list[Effect]]:
    """Act on an owner this frontend may control but never give a tool call.

    Another protocol: an older owner is asked to stand down through the one
    route every protocol keeps; the same or a newer one is left running.
    Either way the generation is written off and the lock decides. Same version
    with another configuration: probed once, since a descriptor outlives its
    writer. An older owner of this protocol is turned over before this, in
    :func:`_decide`.
    """
    if not reading.other_protocol:
        return _probe(lookup, reading, now)
    asked = reading.older and lookup.may_ask
    first: list[Effect] = []
    if asked:
        first.append(AskToStandDown(Turnover.OLDER_PROTOCOL))
    elif not reading.older:
        first.append(LeaveAlone())
    first.append(Bury(instance))
    return _conclude(lookup, Finding.WRITTEN_OFF, asked=asked, first=first)


def _probe(lookup: Lookup, reading: Reading, now: float) -> tuple[Lookup, list[Effect]]:
    remaining = lookup.deadline - now
    if remaining <= 0:
        return _conclude(lookup, Finding.NOT_PROBED)
    op = lookup.pending + 1
    return replace(lookup, step=Step.PROBING, pending=op, probing=reading), [
        Probe(op, min(lookup.timings.probe, remaining))
    ]


def _probed(lookup: Lookup, reach: Reach) -> tuple[Lookup, list[Effect]]:
    reading = lookup.probing
    assert reading is not None and reading.instance is not None
    if reach is Reach.REFUSED:
        # Nothing usable at that address: leftovers, and the lock decides.
        # Deliberately not ABSENT. The file is still there, and calling it
        # absent invites a caller to clean it up.
        found = Finding.WRITTEN_OFF if reading.control_only else Finding.LEFTOVERS
        return _conclude(lookup, found, first=[Bury(reading.instance)])
    if reading.control_only:
        # Answered or silent, the election ends here. Silence proves nothing
        # about the owner's life and so authorizes no turnover, but it does not
        # make the owner usable either: waiting out a whole election for a
        # process that could never serve this configuration buys nothing, and
        # the profile lease is what keeps the two browsers apart meanwhile. The
        # two are reported apart, so a log never calls a silent owner alive.
        found = Finding.LIVE_RIVAL if reach is Reach.ANSWERED else Finding.SILENT_RIVAL
        return _conclude(lookup, found)
    if reach is Reach.SILENT:
        # Not buried, and that is the whole of the fix. Something holds the port
        # and did not get to us inside the budget, which is what a paused or
        # overloaded owner looks like, including one this very election started
        # and watched report ready. Burying it made every later pass short
        # circuit, so the owner never got a second chance inside the election it
        # belonged to.
        #
        # Left to the caller's own deadline rather than bounded by a retry
        # count. Each probe already costs the full budget, so a fixed number of
        # retries covers a fixed and rather short stall: measured, one retry
        # rescues a six second freeze and still gives up on a fifteen second
        # one.
        return _conclude(lookup, Finding.NOT_ANSWERED_YET)
    return _conclude(lookup, Finding.AS_READ)


def _conclude(
    lookup: Lookup,
    finding: Finding,
    *,
    asked: bool = False,
    observed: bool = True,
    first: list[Effect] | None = None,
) -> tuple[Lookup, list[Effect]]:
    return replace(lookup, step=Step.DONE), [
        *(first or []),
        Conclude(finding, asked=asked, observed=observed),
    ]
