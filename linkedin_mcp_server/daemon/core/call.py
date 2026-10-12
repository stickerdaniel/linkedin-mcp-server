"""What one forwarded tool call does next, decided without doing any of it.

A call may wait on the heartbeat preflight and the forwarded request, a
recovery, the tool's annotations, a sign-in and the read-only replay after it.
This module decides which comes next, whether a failed call may run again, and
what the caller is told when time runs out; ``daemon.proxy`` runs each effect
and feeds back the event that completes it.

- One deadline per call, set by :func:`arrive` and never restarted; every wait
  ends there, the auth repair and its replay included.
- The deadline ends this call's wait, never shared work: an election or a
  sign-in serves other calls too and is never cancelled for this one.
- At most two attempts and one recovery per invocation. After an owner failure
  the call runs again only on a replacement, and only when nothing was sent or
  the tool is read-only (looked up once, after the recovery). After a sign-in,
  only when the owner said nothing ran and the tool is read-only.
- A call keeps its owner once a request may have left
  (:func:`binding_refusal`).
- An expired call with an answer gets that answer; one whose request may have
  reached an owner gets ``outcome_unknown``; only one that provably sent
  nothing is told it was not sent.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace


class Dispatch(enum.Enum):
    """How far the current invocation's tool request got, as far as is provable.

    An invocation is the call, or its replay after a sign-in. The state only
    moves forward within one: a later attempt that provably sent nothing does
    not make an earlier one that may have unsent.
    """

    #: No tool request of this invocation can have reached an owner.
    UNSENT = "unsent"
    #: A tool request may have reached an owner, and no answer came back.
    POSSIBLY_SENT = "possibly_sent"
    #: An owner answered.
    ANSWERED = "answered"
    #: Ended without knowing whether the call acted, and reported as such.
    UNKNOWN = "unknown"


class Phase(enum.Enum):
    """What the call is waiting on."""

    ATTEMPTING = "attempting"
    RECOVERING = "recovering"
    CLASSIFYING = "classifying"
    REPAIRING = "repairing"
    SIGNING_IN = "signing_in"
    DONE = "done"


class AttemptKind(enum.Enum):
    """Why an attempt is made, which is all the shell logs about it."""

    FIRST = "first"
    #: On a replacement owner, after a recovery.
    RETRY = "retry"
    #: After a sign-in the owner asked for.
    REPLAY = "replay"


class Repair(enum.Enum):
    """How starting a sign-in ended."""

    #: A usable session exists now, this process's or a peer's.
    READY = "ready"
    #: A login is running and may still finish.
    STARTED = "started"
    #: LinkedIn restricted the account; no login lifts that.
    RESTRICTED = "restricted"
    FAILED = "failed"


class SignIn(enum.Enum):
    """How waiting for a started login ended."""

    SIGNED_IN = "signed_in"
    NOT_YET = "not_yet"
    RESTRICTED = "restricted"


class Because(enum.Enum):
    """Why a call ended on an answer it already had."""

    ANSWERED = "answered"
    PROFILE_HELD = "profile_held"
    REPAIR_FAILED = "repair_failed"
    SIGN_IN_UNFINISHED = "sign_in_unfinished"
    NOT_REPLAYABLE = "not_replayable"
    COULD_CHANGE = "could_change"
    #: The deadline passed during the replay.
    REPLAY_EXPIRED = "replay_expired"
    #: The deadline passed before the replay could start.
    EXPIRED = "expired"


class _Then(enum.Enum):
    """What a classification of the tool is asked for."""

    AFTER_FIRST = "after_first"
    AFTER_SECOND = "after_second"
    BEFORE_REPLAY = "before_replay"


@dataclass(frozen=True)
class AuthRequest:
    """The owner's request to sign in, as the shell read it off an answer."""

    reason: str
    replayable: bool
    browser_open: bool
    generation: str | None


# --- Events: each completes the operation it names --------------------------------


@dataclass(frozen=True)
class Answered:
    """An owner answered the attempt. *result* is opaque here."""

    op: int
    now: float
    result: object
    auth: AuthRequest | None = None


@dataclass(frozen=True)
class OwnerLost:
    """The owner the attempt was bound to could not take it.

    *failure* is opaque here and handed back in the effects that need it.
    """

    op: int
    now: float
    owner: str
    nothing_was_sent: bool
    failure: object


@dataclass(frozen=True)
class Raised:
    """The attempt failed for a reason that is not the owner's."""

    op: int
    now: float
    error: BaseException


@dataclass(frozen=True)
class Recovered:
    op: int
    now: float
    found: bool


@dataclass(frozen=True)
class Classified:
    op: int
    now: float
    could_change: bool


@dataclass(frozen=True)
class Repaired:
    op: int
    now: float
    outcome: Repair
    error: BaseException | None = None


@dataclass(frozen=True)
class SignedIn:
    op: int
    now: float
    outcome: SignIn
    error: BaseException | None = None


@dataclass(frozen=True)
class Expired:
    """The deadline passed while the operation was outstanding.

    *request_left* is whether the attempt's tool request had been handed to its
    session by then; meaningless for anything but an attempt.
    """

    op: int
    now: float
    request_left: bool = False


Event = (
    Answered
    | OwnerLost
    | Raised
    | Recovered
    | Classified
    | Repaired
    | SignedIn
    | Expired
)


# --- Effects: work for the shell ---------------------------------------------------


@dataclass(frozen=True)
class Attempt:
    """Preflight, bind and send the call to the owner the backend holds now."""

    op: int
    kind: AttemptKind


@dataclass(frozen=True)
class Recover:
    """Join, or start, the one recovery for *owner*."""

    op: int
    owner: str
    failure: object


@dataclass(frozen=True)
class Classify:
    """Look up whether running the tool again could change something."""

    op: int


@dataclass(frozen=True)
class RepairAuth:
    op: int
    reason: str
    generation: str | None


@dataclass(frozen=True)
class WaitForSignIn:
    """Wait for the started login until *until*, which is never past the deadline."""

    op: int
    until: float


@dataclass(frozen=True)
class Bury:
    """Record *failure* against its owner at once. Nothing completes it."""

    failure: object


@dataclass(frozen=True)
class Return:
    result: object
    because: Because


@dataclass(frozen=True)
class ReportUnknown:
    """Nobody can say whether the call acted. *failure* is the last one seen."""

    failure: object | None
    expired: bool


@dataclass(frozen=True)
class ReportLost:
    """No owner took the call, and nothing of it can have run."""

    failure: object | None
    expired: bool


@dataclass(frozen=True)
class Reraise:
    error: BaseException


@dataclass(frozen=True)
class ReportRestricted:
    error: BaseException | None


Effect = (
    Attempt
    | Recover
    | Classify
    | RepairAuth
    | WaitForSignIn
    | Bury
    | Return
    | ReportUnknown
    | ReportLost
    | Reraise
    | ReportRestricted
)

#: Effects that end the call. Exactly one is the last effect of every call.
TERMINAL = (Return, ReportUnknown, ReportLost, Reraise, ReportRestricted)


@dataclass(frozen=True)
class Call:
    """One call's state between events."""

    #: Absolute, on the shell's monotonic clock: arrival plus the budget.
    deadline: float
    #: Where waiting for a sign-in ends, leaving the rest for the replay.
    sign_in_until: float
    phase: Phase = Phase.ATTEMPTING
    #: The operation the call is waiting on. Any other completion is stale.
    pending: int = 0
    replaying: bool = False
    #: Attempts made in this invocation.
    attempts: int = 0
    dispatch: Dispatch = Dispatch.UNSENT
    #: The owner this invocation's last failed attempt was bound to.
    owner: str | None = None
    #: That attempt's failure, opaque.
    lost: object | None = None
    found: bool = False
    #: Whether running the tool again could change something, once looked up in
    #: this invocation. Never carried into the replay: today's policy asks again.
    could_change: bool | None = None
    then: _Then | None = None
    #: The owner's answer asking for a sign-in, kept to fall back on.
    held: object | None = None
    auth: AuthRequest | None = None


_EXPECTED: dict[Phase, tuple[type, ...]] = {
    Phase.ATTEMPTING: (Answered, OwnerLost, Raised, Expired),
    Phase.RECOVERING: (Recovered, Expired),
    Phase.CLASSIFYING: (Classified, Expired),
    Phase.REPAIRING: (Repaired, Expired),
    Phase.SIGNING_IN: (SignedIn, Expired),
}


def binding_refusal[R](
    owner: str, *, written_off: Mapping[str, R], sent: bool
) -> R | None:
    """Why no new request may go to *owner* now, or ``None`` to let it go.

    The one rule for every point a call's owner is checked. A written-off
    owner gets nothing new, but once a request may have left (*sent*) the call
    keeps its owner, since moving its heartbeats would get it cancelled.
    *written_off* maps each abandoned owner to the reason a refusal names.
    """
    if sent:
        return None
    return written_off.get(owner)


def arrive(
    *, now: float, budget: float, sign_in_share: float
) -> tuple[Call, list[Effect]]:
    """A call arriving at the frontend at *now*, with *budget* seconds to answer.

    *sign_in_share* is the part of the budget a sign-in may use, counted from
    the same arrival, so the rest is left for the replay it was waited for.
    """
    call = Call(deadline=now + budget, sign_in_until=now + budget * sign_in_share)
    return _attempt(call, AttemptKind.FIRST, now)


def on_event(call: Call, event: Event) -> tuple[Call, list[Effect]]:
    """The call after *event*, and what the shell must do next.

    A completion for anything but the pending operation changes nothing. One
    arriving at or after the deadline ends the call as an expiry would, unless
    it is an answer (the owner's or the tool's own failure), so a completion
    racing the deadline is judged like the ``Expired`` it raced.
    """
    if call.phase is Phase.DONE or event.op != call.pending:
        return call, []
    if not isinstance(event, _EXPECTED[call.phase]):
        raise ValueError(
            f"{type(event).__name__} does not complete a call that is {call.phase.value}"
        )
    match event:
        case Expired():
            return _expire(call, request_left=event.request_left)
        case Answered():
            return _answered(call, event)
        case Raised():
            return _end(call, Reraise(event.error))
    if event.now >= call.deadline:
        if isinstance(event, OwnerLost):
            call = _note_loss(call, event)
            call, effects = _expire(call)
            return call, [Bury(event.failure), *effects]
        return _expire(call)
    match event:
        case OwnerLost():
            return _owner_lost(call, event)
        case Recovered():
            call = replace(call, found=event.found)
            if call.dispatch is Dispatch.POSSIBLY_SENT and call.could_change is None:
                return _classify(call, _Then.AFTER_FIRST, event.now)
            return _after_first(call, event.now)
        case Classified():
            return _classified(replace(call, could_change=event.could_change), event)
        case Repaired():
            return _repaired(call, event)
        case SignedIn():
            return _signed_in(call, event)


def _answered(call: Call, event: Answered) -> tuple[Call, list[Effect]]:
    call = replace(call, dispatch=Dispatch.ANSWERED)
    auth = event.auth
    # A replay's answer is final, a second request to sign in included: one
    # login per call.
    if call.replaying or auth is None:
        return _end(call, Return(event.result, Because.ANSWERED))
    call = replace(call, held=event.result, auth=auth)
    if auth.browser_open:
        return _end(call, Return(event.result, Because.PROFILE_HELD))
    return _issue(
        call,
        Phase.REPAIRING,
        event.now,
        lambda op: RepairAuth(op, auth.reason, auth.generation),
    )


def _note_loss(call: Call, event: OwnerLost) -> Call:
    return replace(
        call,
        owner=event.owner,
        lost=event.failure,
        dispatch=(call.dispatch if event.nothing_was_sent else Dispatch.POSSIBLY_SENT),
    )


def _owner_lost(call: Call, event: OwnerLost) -> tuple[Call, list[Effect]]:
    possibly_sent = not event.nothing_was_sent
    call = _note_loss(call, event)
    # Recorded whether or not anything follows, and before it: an owner found
    # retiring must not receive the next call, this one's retry included.
    bury: Effect = Bury(event.failure)
    if call.attempts == 1:
        call, effects = _issue(
            call,
            Phase.RECOVERING,
            event.now,
            lambda op: Recover(op, event.owner, event.failure),
        )
    elif possibly_sent and call.could_change is None:
        call, effects = _classify(call, _Then.AFTER_SECOND, event.now)
    else:
        # The second attempt's failure is never recovered from: a third attempt
        # would be a guess about an owner that has now failed twice.
        call, effects = _after_second(call, possibly_sent=possibly_sent)
    return call, [bury, *effects]


def _after_first(call: Call, now: float) -> tuple[Call, list[Effect]]:
    if call.dispatch is Dispatch.POSSIBLY_SENT and call.could_change:
        return _unknown(call, expired=False)
    if not call.found:
        return _end(call, ReportLost(call.lost, expired=False))
    return _attempt(call, AttemptKind.RETRY, now)


def _after_second(call: Call, *, possibly_sent: bool) -> tuple[Call, list[Effect]]:
    if possibly_sent and call.could_change:
        return _unknown(call, expired=False)
    return _end(call, ReportLost(call.lost, expired=False))


def _classified(call: Call, event: Classified) -> tuple[Call, list[Effect]]:
    match call.then:
        case _Then.AFTER_FIRST:
            return _after_first(call, event.now)
        case _Then.AFTER_SECOND:
            return _after_second(call, possibly_sent=True)
        case _Then.BEFORE_REPLAY:
            if event.could_change:
                return _end(call, Return(call.held, Because.COULD_CHANGE))
            return _attempt(
                replace(
                    call,
                    replaying=True,
                    attempts=0,
                    dispatch=Dispatch.UNSENT,
                    owner=None,
                    lost=None,
                    found=False,
                    could_change=None,
                ),
                AttemptKind.REPLAY,
                event.now,
            )
        case None:
            raise ValueError("a classification nobody asked for")


def _repaired(call: Call, event: Repaired) -> tuple[Call, list[Effect]]:
    match event.outcome:
        case Repair.READY:
            return _before_replay(call, event.now)
        case Repair.STARTED:
            until = min(call.sign_in_until, call.deadline)
            return _issue(
                call, Phase.SIGNING_IN, event.now, lambda op: WaitForSignIn(op, until)
            )
        case Repair.RESTRICTED:
            return _end(call, ReportRestricted(event.error))
        case Repair.FAILED:
            return _end(call, Return(call.held, Because.REPAIR_FAILED))


def _signed_in(call: Call, event: SignedIn) -> tuple[Call, list[Effect]]:
    match event.outcome:
        case SignIn.SIGNED_IN:
            return _before_replay(call, event.now)
        case SignIn.NOT_YET:
            return _end(call, Return(call.held, Because.SIGN_IN_UNFINISHED))
        case SignIn.RESTRICTED:
            return _end(call, ReportRestricted(event.error))


def _before_replay(call: Call, now: float) -> tuple[Call, list[Effect]]:
    assert call.auth is not None
    if not call.auth.replayable:
        return _end(call, Return(call.held, Because.NOT_REPLAYABLE))
    # Asked afresh rather than taken from an earlier lookup, as before.
    return _classify(replace(call, could_change=None), _Then.BEFORE_REPLAY, now)


def _classify(call: Call, then: _Then, now: float) -> tuple[Call, list[Effect]]:
    return _issue(replace(call, then=then), Phase.CLASSIFYING, now, Classify)


def _attempt(call: Call, kind: AttemptKind, now: float) -> tuple[Call, list[Effect]]:
    if now >= call.deadline:
        return _expire(call)
    op = call.pending + 1
    call = replace(call, phase=Phase.ATTEMPTING, pending=op, attempts=call.attempts + 1)
    return call, [Attempt(op, kind)]


def _issue(
    call: Call, phase: Phase, now: float, effect: Callable[[int], Effect]
) -> tuple[Call, list[Effect]]:
    """Wait on one more operation, unless the deadline has already passed."""
    if now >= call.deadline:
        return _expire(call)
    op = call.pending + 1
    return replace(call, phase=phase, pending=op), [effect(op)]


def _expire(call: Call, *, request_left: bool = False) -> tuple[Call, list[Effect]]:
    """End the call at its deadline with the best answer its state allows."""
    if request_left:
        call = replace(call, dispatch=Dispatch.POSSIBLY_SENT)
    if call.held is not None:
        because = Because.REPLAY_EXPIRED if call.replaying else Because.EXPIRED
        return _end(call, Return(call.held, because))
    if call.dispatch is Dispatch.POSSIBLY_SENT:
        return _unknown(call, expired=True)
    return _end(call, ReportLost(call.lost, expired=True))


def _unknown(call: Call, *, expired: bool) -> tuple[Call, list[Effect]]:
    return _end(
        replace(call, dispatch=Dispatch.UNKNOWN), ReportUnknown(call.lost, expired)
    )


def _end(call: Call, effect: Effect) -> tuple[Call, list[Effect]]:
    return replace(call, phase=Phase.DONE), [effect]
