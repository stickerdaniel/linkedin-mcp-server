"""When the owner takes a call and when it may go, decided without doing either.

The owner admits calls, hears its frontends' heartbeats, expires calls nobody
waits for, and goes away when idle, wedged, or asked for the browser by a newer
build or a profile command. ``daemon.liveness`` and ``daemon.owner`` run each
effect and feed back the event that completes it.

- Admission and retirement are one decision on one state: every event that
  reads one and writes the other is a single :func:`on_event` call, so either
  the call is admitted and retirement sees it, or retirement wins and the call
  is refused before anything runs.
- Nothing is admitted before :class:`Published`. A frontend reaches the
  endpoint only through the canonical descriptor, and an owner whose commit
  was ambiguous retires instead of publishing, so a call that arrives early
  is refused like one after retirement.
- Admission is reserved here, before the shell creates the task; a failed
  start comes back as :class:`StartFailed`. A completion naming a ticket that
  is no longer admitted is stale and changes nothing.
- A gap between the owner's own scans longer than :data:`STALL_SECONDS` means
  this process was not running, and writes nobody off.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

#: How long a call may go unheard before the owner stops it. Five of the
#: frontend's heartbeat cadences: the margin is the point, so a live call is
#: cancelled only after several consecutive lost beats or an event-loop stall
#: far beyond a heartbeat's own latency. Measured: docs/daemon-measurements.md.
EXPIRY_SECONDS = 10.0

#: A gap between two of the owner's own scans that means the owner was not
#: running rather than that a frontend went quiet. The serving loop polls ten
#: times a second, so anything approaching this is the process itself having
#: missed its turn: a laptop that slept, a machine under heavy load, or a long
#: synchronous stretch on the event loop.
STALL_SECONDS = 1.0

#: How long a call already admitted may keep running after the owner is asked
#: to stand down. Admission is closed for the whole of it. A call still running
#: at the end is cut off. The number is the policy the default-on contract
#: states, not a measurement.
TURNOVER_DRAIN_SECONDS = 30.0

#: How long an owner holds an unconsumed setup failure open for the retry the
#: in-progress message asks for "in a minute or two". Bounded to match that
#: guidance: an abandoned failure must not pin an owner forever.
SETUP_FAILURE_RETRY_GRACE_SECONDS = 120.0


class Retirement(enum.StrEnum):
    """Why admission closed. Only the first reason is kept."""

    #: A newer build asked for the browser.
    TURNOVER = "turnover"
    #: A profile command the user confirmed asked, and only because nothing
    #: was in flight or queued.
    PROFILE_COMMAND = "retire"
    IDLE = "idle"
    #: The browser cannot be driven from this process any more.
    WEDGED = "wedged"
    #: The owner exits while its own publication is in doubt.
    UNCERTAIN_PUBLICATION = "uncertain publication"


class Fate(enum.Enum):
    """Who is still waiting for an admitted call."""

    #: Its frontend is still saying so.
    WATCHED = "watched"
    #: Nobody asked for it within the expiry, and it was cancelled.
    ABANDONED = "abandoned"
    #: This owner cancelled it to stand down; its client is still waiting.
    CUT = "cut"


class Stopped(enum.Enum):
    """What a call this owner cancelled is answered with."""

    #: Cut while still queued: nothing ran, and it may be repeated.
    NOT_RUN = "not_run"
    #: Cut after its tool body began: it may have acted.
    UNKNOWN = "unknown"
    #: Expired because nobody was waiting for it.
    ABANDONED = "abandoned"


class Phase(enum.Enum):
    """Where the owner's lifecycle is."""

    SERVING = "serving"
    #: Admission is closed and admitted calls get until the cutoff.
    DRAINING = "draining"


@dataclass(frozen=True)
class Call:
    """One admitted call, until it finishes."""

    call_id: str
    #: On the shell's monotonic clock.
    last_heard: float
    fate: Fate = Fate.WATCHED
    #: Past its last abandonment check and into its tool body. The line between
    #: a call that was only ever queued and one that may have acted.
    began: bool = False


@dataclass(frozen=True)
class Owner:
    """The owner's admission and lifecycle state between events."""

    #: Named in every refusal, so a frontend can tell this owner's answer from
    #: arbitrary tool output.
    instance: str | None = None
    #: Set once and never cleared: a retiring owner admits nothing more.
    retirement: Retirement | None = None
    #: The endpoint's descriptor is proved canonical. Admission waits for it.
    published: bool = False
    #: A stand-down was asked for and the drain is owed.
    turnover: bool = False
    phase: Phase = Phase.SERVING
    #: The cutoff, once draining.
    drain_until: float | None = None
    #: The stop the shell was told to carry out. Nothing more is decided after
    #: it, and every later tick repeats it.
    ending: Stop | None = None
    next_ticket: int = 1
    #: Every admitted call that has not finished, by ticket, whether it is
    #: running or still queued behind another one. Counted from admission, so
    #: a queued call is as busy as a running one.
    calls: Mapping[int, Call] = field(default_factory=dict)
    #: When this owner last did anything for anybody, or ``None`` while a call
    #: is admitted and while the endpoint has not been published. An owner that
    #: counted its own startup as idle time would exit during it. Not the
    #: publication flag: it is ``None`` during admitted work too.
    quiet_since: float | None = None
    #: When the owner last asked who was still waiting.
    last_scan: float | None = None


# --- Events ------------------------------------------------------------------------


@dataclass(frozen=True)
class Named:
    """The instance whose refusals this owner signs."""

    instance: str


@dataclass(frozen=True)
class Admit:
    """A marked call asks to run. Its first heartbeat preceded it."""

    call_id: str
    now: float


@dataclass(frozen=True)
class StartFailed:
    """The shell could not start the task for the admission *ticket*."""

    ticket: int
    now: float


@dataclass(frozen=True)
class Finished:
    """The admitted call *ticket* ended, however it ended."""

    ticket: int
    now: float


@dataclass(frozen=True)
class Heard:
    """A frontend is still waiting for *call_id*."""

    call_id: str
    now: float


@dataclass(frozen=True)
class BodyBegan:
    """The call *call_id* is past the point where it could still be refused."""

    call_id: str


@dataclass(frozen=True)
class Published:
    """The endpoint is live: calls may be admitted, and idleness means something."""

    now: float


@dataclass(frozen=True)
class BackgroundFinished:
    """Detached work, such as a browser setup, that kept the owner busy ended."""

    now: float


@dataclass(frozen=True)
class Retire:
    """Close admission whatever is in flight, for a reason decided elsewhere."""

    reason: Retirement


@dataclass(frozen=True)
class RetireIfIdle:
    """Close admission only if nothing is in flight, queued or in the background.

    An owner already retiring stays retiring, whatever it is still draining.
    """

    reason: Retirement
    background: bool = False


@dataclass(frozen=True)
class TurnOver:
    """Stand down: close admission now and drain on the next tick."""


@dataclass(frozen=True)
class Tick:
    """One turn of the owner's lifecycle loop, with what the shell observed.

    The defaults observe nothing: no idle exit, no wedge, the endpoint serving.
    """

    now: float
    #: Seconds of quiet before the owner exits, 0 for never.
    idle_timeout: float = 0.0
    #: Why the browser cannot be driven, if it cannot.
    wedged: str | None = None
    #: A browser setup is still running in the background.
    setup_running: bool = False
    #: A failed setup waits for the next call to report it.
    setup_failed: bool = False
    #: The endpoint stopped serving on its own.
    endpoint_stopped: bool = False


Event = (
    Named
    | Admit
    | StartFailed
    | Finished
    | Heard
    | BodyBegan
    | Published
    | BackgroundFinished
    | Retire
    | RetireIfIdle
    | TurnOver
    | Tick
)


# --- Effects -----------------------------------------------------------------------


@dataclass(frozen=True)
class StartTask:
    """Start the admitted call's task. Answered by :class:`StartFailed` if it fails."""

    ticket: int


@dataclass(frozen=True)
class Refused:
    """The call is not admitted: this owner is unpublished or retiring.

    Nothing was counted, and the call never reached a tool.
    """


@dataclass(frozen=True)
class Cancel:
    """Cancel the task of *ticket*, *because* nobody waits or the owner is going."""

    ticket: int
    call_id: str
    because: Fate


@dataclass(frozen=True)
class Forgave:
    """The owner was not scheduled for *seconds*; every clock restarted instead."""

    seconds: float


@dataclass(frozen=True)
class Drain:
    """Admission is closed and the drain began, at *requested_by*'s request."""

    requested_by: Retirement


@dataclass(frozen=True)
class CutOff:
    """The drain ran out with calls admitted; *count* were cut here."""

    count: int


@dataclass(frozen=True)
class Stop:
    """Stop the endpoint, within the shell's own bound, and exit.

    *quiet* is how long an idle owner was quiet, *wedged* why the browser can
    no longer be driven; each only for its own reason.
    """

    because: Retirement
    quiet: float | None = None
    wedged: str | None = None


@dataclass(frozen=True)
class EndpointGone:
    """The endpoint stopped on its own, outside a drain. Wait for it and leave.

    Not a decision to stop, so nothing is recorded: the owner stops because its
    endpoint did, and the lifecycle has nothing to add.
    """


Effect = StartTask | Refused | Cancel | Forgave | Drain | CutOff | Stop | EndpointGone

#: Effects that end the serving loop. At most one per event, and always last.
TERMINAL = (Stop, EndpointGone)


# --- Queries -----------------------------------------------------------------------


def busy(owner: Owner, *, background: bool = False) -> bool:
    """Whether retiring now would cut off somebody's work.

    *background* is detached work the shell knows about and this state does
    not, such as a browser setup still running.
    """
    return bool(owner.calls) or background


def admitting(owner: Owner) -> bool:
    """Whether a new call would be admitted now.

    The heartbeat preflight answers from this, so a preflight that passes is
    never followed by a refused call from the same owner state.
    """
    return owner.published and owner.retirement is None


def quiet_for(owner: Owner, now: float) -> float | None:
    """Seconds since this owner last had anything to do, if it is free now.

    ``None`` while a call is admitted, and while the endpoint has not been
    published. A hint only: whether the owner may go is decided on a
    :class:`Tick`, at the moment it acts.
    """
    if owner.calls or owner.quiet_since is None:
        return None
    return now - owner.quiet_since


def watching(owner: Owner, call_id: str) -> bool:
    """Whether a frontend is still known to be waiting for *call_id*."""
    return _watched(owner, call_id) is not None


def abandoned(owner: Owner, call_id: str, now: float) -> bool:
    """Whether *call_id* may no longer start browser work.

    An unknown call counts as abandoned on purpose: an unwatched call was
    written off or never admitted, and nobody vouched for it. A call older
    than the expiry is abandoned before the next scan writes it off, unless
    this owner itself stalled, the scan's own rule.
    """
    call = _watched(owner, call_id)
    if call is None:
        return True
    if owner.last_scan is not None and now - owner.last_scan > STALL_SECONDS:
        return False
    return now - call.last_heard > EXPIRY_SECONDS


def stopped(owner: Owner, ticket: int | None) -> Stopped | None:
    """What a cancelled call *ticket* is answered with, if this owner cancelled it.

    ``None`` for a call still watched, whose cancellation came from elsewhere.
    A call this owner no longer tracks reads as abandoned, as it does for
    :func:`abandoned`.
    """
    call = owner.calls.get(ticket) if ticket is not None else None
    if call is None:
        return Stopped.ABANDONED
    match call.fate:
        case Fate.WATCHED:
            return None
        case Fate.ABANDONED:
            return Stopped.ABANDONED
        case Fate.CUT:
            return Stopped.UNKNOWN if call.began else Stopped.NOT_RUN


# --- The reducer -------------------------------------------------------------------


def on_event(owner: Owner, event: Event) -> tuple[Owner, list[Effect]]:
    """The owner after *event*, and what the shell must do next."""
    match event:
        case Named():
            return replace(owner, instance=event.instance), []
        case Admit():
            return _admit(owner, event)
        case StartFailed() | Finished():
            return _release(owner, event.ticket, event.now), []
        case Heard():
            return _heard(owner, event), []
        case BodyBegan():
            return _body_began(owner, event.call_id), []
        case Published():
            quiet_since = owner.quiet_since
            if quiet_since is None and not owner.calls:
                quiet_since = event.now
            return replace(owner, published=True, quiet_since=quiet_since), []
        case BackgroundFinished():
            # A fresh quiet period, rather than one counted from before the
            # detached work kept the owner active.
            if not owner.calls:
                return replace(owner, quiet_since=event.now), []
            return owner, []
        case Retire():
            return _retire(owner, event.reason), []
        case RetireIfIdle():
            return _retire_if_idle(owner, event.reason, background=event.background), []
        case TurnOver():
            return replace(_retire(owner, Retirement.TURNOVER), turnover=True), []
        case Tick():
            return _tick(owner, event)


def _admit(owner: Owner, event: Admit) -> tuple[Owner, list[Effect]]:
    if not admitting(owner):
        return owner, [Refused()]
    ticket = owner.next_ticket
    # Watched from the moment it is admitted, and already heard: the first
    # heartbeat is the preflight sent before the call was dispatched.
    call = Call(call_id=event.call_id, last_heard=event.now)
    return (
        replace(
            owner,
            next_ticket=ticket + 1,
            calls={**owner.calls, ticket: call},
            quiet_since=None,
        ),
        [StartTask(ticket)],
    )


def _release(owner: Owner, ticket: int, now: float) -> Owner:
    if ticket not in owner.calls:
        return owner
    calls = {key: call for key, call in owner.calls.items() if key != ticket}
    return replace(owner, calls=calls, quiet_since=owner.quiet_since if calls else now)


def _watched(owner: Owner, call_id: str) -> Call | None:
    found = None
    for call in owner.calls.values():
        if call.call_id == call_id and call.fate is Fate.WATCHED:
            found = call
    return found


def _heard(owner: Owner, event: Heard) -> Owner:
    # Only a watched call: a beat that arrives after its call was written off
    # or cut must not resurrect it.
    if _watched(owner, event.call_id) is None:
        return owner
    return replace(
        owner,
        calls={
            ticket: (
                replace(call, last_heard=event.now)
                if call.call_id == event.call_id and call.fate is Fate.WATCHED
                else call
            )
            for ticket, call in owner.calls.items()
        },
    )


def _body_began(owner: Owner, call_id: str) -> Owner:
    if not any(call.call_id == call_id for call in owner.calls.values()):
        return owner
    return replace(
        owner,
        calls={
            ticket: replace(call, began=True) if call.call_id == call_id else call
            for ticket, call in owner.calls.items()
        },
    )


def _retire(owner: Owner, reason: Retirement) -> Owner:
    if owner.retirement is not None:
        return owner
    return replace(owner, retirement=reason)


def _retire_if_idle(owner: Owner, reason: Retirement, *, background: bool) -> Owner:
    if owner.retirement is not None or busy(owner, background=background):
        return owner
    return _retire(owner, reason)


def _scan(owner: Owner, now: float) -> tuple[Owner, list[Effect]]:
    since = now - owner.last_scan if owner.last_scan is not None else 0.0
    owner = replace(owner, last_scan=now)
    if since > STALL_SECONDS:
        # The owner itself was not running, so nobody had a chance to be
        # heard. Everyone's clock starts again rather than moving by the length
        # of the stall, which for a stall longer than the expiry would push
        # deadlines into the future. A call that really was abandoned gets one
        # more cycle, which is the cheaper of the two mistakes.
        calls = {
            ticket: replace(call, last_heard=now) if call.fate is Fate.WATCHED else call
            for ticket, call in owner.calls.items()
        }
        return replace(owner, calls=calls), [Forgave(since)]
    deadline = now - EXPIRY_SECONDS
    expired = [
        ticket
        for ticket, call in owner.calls.items()
        if call.fate is Fate.WATCHED and call.last_heard < deadline
    ]
    return _write_off(owner, expired, Fate.ABANDONED)


def _write_off(
    owner: Owner, tickets: list[int], fate: Fate
) -> tuple[Owner, list[Effect]]:
    if not tickets:
        return owner, []
    calls = dict(owner.calls)
    for ticket in tickets:
        calls[ticket] = replace(calls[ticket], fate=fate)
    return replace(owner, calls=calls), [
        Cancel(ticket, calls[ticket].call_id, fate) for ticket in tickets
    ]


def _stop(
    owner: Owner, effects: list[Effect], ending: Stop
) -> tuple[Owner, list[Effect]]:
    return replace(owner, ending=ending), [*effects, ending]


def _tick(owner: Owner, tick: Tick) -> tuple[Owner, list[Effect]]:
    if owner.ending is not None:
        return owner, [owner.ending]
    if owner.phase is Phase.DRAINING:
        return _drain(owner, tick)
    if tick.endpoint_stopped:
        return owner, [EndpointGone()]
    if tick.wedged is not None:
        # Not recoverable in place, which is what separates this from every
        # other error an owner reports and keeps serving after. The profile is
        # held until this process exits, so exiting *is* the recovery: the
        # kernel frees the daemon lock and the next call elects an owner that
        # can open the profile. Admission closes first, so a frontend's
        # preflight hears 409 and looks elsewhere instead of dispatching to a
        # browser nobody can drive.
        owner = _retire(owner, Retirement.WEDGED)
        return _stop(owner, [], Stop(Retirement.WEDGED, wedged=tick.wedged))
    if owner.turnover:
        # Admission was closed by the request itself, a profile command's
        # included; closed again here so the drain does not depend on how the
        # request got in. The first reason is the one that asked.
        requested_by = owner.retirement or Retirement.TURNOVER
        owner = replace(
            _retire(owner, Retirement.TURNOVER),
            phase=Phase.DRAINING,
            drain_until=tick.now + TURNOVER_DRAIN_SECONDS,
        )
        owner, effects = _drain(owner, tick)
        return owner, [Drain(requested_by), *effects]
    owner, effects = _scan(owner, tick.now)
    # The third reason to go, after wedge and turnover. Otherwise an owner holds
    # the daemon lock for the machine's uptime, having closed the browser hours
    # ago. A failed setup is only reported to the next tool call, so it holds
    # the owner open for that retry, for a bounded grace. A setup still running
    # is work for this owner even though no call remains, and exiting would
    # cancel every install that lasts longer than the idle timeout.
    quiet = quiet_for(owner, tick.now)
    required = tick.idle_timeout
    if tick.setup_failed:
        required = max(required, SETUP_FAILURE_RETRY_GRACE_SECONDS)
    if tick.idle_timeout > 0 and quiet is not None and quiet >= required:
        owner = _retire_if_idle(owner, Retirement.IDLE, background=tick.setup_running)
        if owner.retirement is not None:
            return _stop(owner, effects, Stop(Retirement.IDLE, quiet=quiet))
    return owner, effects


def _drain(owner: Owner, tick: Tick) -> tuple[Owner, list[Effect]]:
    """One turn of the drain: wait for the admitted calls, or cut them off.

    Admission is closed, so the count can only fall. A call whose client leaves
    meanwhile is still expired, and a browser that stops being drivable ends
    the wait early, since nothing admitted could finish on it anyway. A stopped
    endpoint does not: the drain owes its calls the same bound either way.
    """
    assert owner.drain_until is not None
    if owner.calls and tick.now < owner.drain_until and tick.wedged is None:
        return _scan(owner, tick.now)
    effects: list[Effect] = []
    if owner.calls:
        # Cut here rather than left for the endpoint to drop with its
        # connection, which would lose the answer along with the call. Each one
        # is answered: as not run if still queued, as an unknown outcome if its
        # body began.
        cut = [
            ticket for ticket, call in owner.calls.items() if call.fate is Fate.WATCHED
        ]
        owner, effects = _write_off(owner, cut, Fate.CUT)
        effects.append(CutOff(len(cut)))
    return _stop(owner, effects, Stop(owner.retirement or Retirement.TURNOVER))
