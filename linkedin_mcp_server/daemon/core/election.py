"""Whether to look again, start an owner or stop, decided without doing any of it.

An election alternates a pass over the published descriptor
(:mod:`linkedin_mcp_server.daemon.core.lookup`) and a start of one child that
competes for the lock (:mod:`linkedin_mcp_server.daemon.core.spawn`).
``daemon.election.obtain_owner`` runs each effect and feeds back what came of it.

- Active election and quiescent settlement have separate budgets, both fixed
  from the election's start. An early terminal start failure gets at most one
  settlement budget from that failure, capped by the global deadline.
  Settlement only observes and authenticates; it never starts or turns over an
  owner.
- Direct on every refusal: an owner to attach to or a same-version
  ``OwnerLookup.fallback`` ends the election at once, an unusable lock ends it
  with the last thing seen, and a start that produced no child or a permanent
  failure ends it after settlement. Nothing here retries a refusal.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace

from linkedin_mcp_server.daemon.core.spawn import Attempt


@dataclass(frozen=True)
class Pacing:
    """The election's clocks, in seconds, as the shell configures them."""

    #: How long the pass after a start may watch the descriptor before the
    #: loop tries the lock again.
    retry: float
    #: How many starts go out at the first delay before it backs off.
    burst: int
    #: The delay between starts through the initial race.
    first_delay: float
    #: Where the backoff stops growing.
    max_delay: float


def start_delay_after(starts: int, current: float, pacing: Pacing) -> float:
    """The delay before the next start, after *starts* of them.

    Quick through the initial race, then backing off so a later lock release
    remains discoverable without creating one short-lived child per polling
    pass.
    """
    if starts < pacing.burst:
        return current
    return min(max(current * 2, pacing.first_delay), pacing.max_delay)


class Stage(enum.Enum):
    """Which operation the election is waiting on."""

    #: The pass at the top of each round, which decides whether to start.
    LOOKING = "looking"
    STARTING = "starting"
    #: The paced pass after a start, or after a start that was not due.
    WATCHING = "watching"
    #: A pass of the quiescent tail.
    SETTLING = "settling"
    DECIDED = "decided"


@dataclass(frozen=True)
class Election:
    """One election's state between events."""

    #: Absolute, on the shell's monotonic clock.
    active_until: float
    #: The settlement budget, in seconds.
    settlement: float
    #: The global settlement deadline: the active one plus the budget.
    settle_until: float
    pacing: Pacing
    #: The last concrete observation, opaque here. What the election reports
    #: when it ends without anything better.
    last: object
    #: The delay before the next start, as backed off so far.
    delay: float
    stage: Stage = Stage.LOOKING
    #: The operation the election is waiting on. Any other completion is stale.
    pending: int = 0
    #: Where this settlement ends, once settling.
    tail_until: float | None = None
    #: Whether this process started the owner. Diagnostics only.
    started: bool = False
    starts: int = 0
    next_start: float = 0.0
    #: An older owner was asked to stand down. Asked once per election.
    asked: bool = False


# --- Events: each completes the operation it names --------------------------------


@dataclass(frozen=True)
class Looked:
    """A pass ended with *lookup*, opaque here.

    *conclusive* is an owner to attach to or a Direct fallback; *asked* whether
    the pass sent a turnover request; *observed* whether *lookup* comes from a
    completed read.
    """

    op: int
    now: float
    lookup: object
    conclusive: bool
    asked: bool
    observed: bool


@dataclass(frozen=True)
class StartEnded:
    op: int
    now: float
    attempt: Attempt


@dataclass(frozen=True)
class StartRefused:
    """No child was started: the lock is unusable, or the start failed before one."""

    op: int
    now: float
    lock_unusable: bool


Event = Looked | StartEnded | StartRefused


# --- Effects: work for the shell ---------------------------------------------------


@dataclass(frozen=True)
class Look:
    """One pass, which may watch the descriptor for *wait* seconds."""

    op: int
    may_ask: bool
    wait: float
    deadline: float


@dataclass(frozen=True)
class StartOwner:
    """Start one child that competes for the lock, within *timeout*."""

    op: int
    timeout: float


@dataclass(frozen=True)
class Decide:
    """The election is over. Report *lookup*."""

    lookup: object
    started: bool


Effect = Look | StartOwner | Decide

_EXPECTED: dict[Stage, tuple[type, ...]] = {
    Stage.LOOKING: (Looked,),
    Stage.STARTING: (StartEnded, StartRefused),
    Stage.WATCHING: (Looked,),
    Stage.SETTLING: (Looked,),
}


def elect(
    *,
    now: float,
    deadline_seconds: float,
    settlement_seconds: float,
    pacing: Pacing,
    nothing_seen: object,
) -> tuple[Election, list[Effect]]:
    """An election entered at *now*. *nothing_seen* is reported if nothing is."""
    active_until = now + max(deadline_seconds, 0.0)
    settlement = max(settlement_seconds, 0.0)
    election = Election(
        active_until=active_until,
        settlement=settlement,
        settle_until=active_until + settlement,
        pacing=pacing,
        last=nothing_seen,
        delay=pacing.first_delay,
    )
    return _round(election, now)


def on_event(election: Election, event: Event) -> tuple[Election, list[Effect]]:
    """The election after *event*, and what the shell must do next."""
    if election.stage is Stage.DECIDED or event.op != election.pending:
        return election, []
    if not isinstance(event, _EXPECTED[election.stage]):
        raise ValueError(
            f"{type(event).__name__} does not complete an election that is "
            f"{election.stage.value}"
        )
    match event:
        case Looked():
            return _looked(election, event)
        case StartEnded():
            return _start_ended(election, event)
        case StartRefused():
            if event.lock_unusable:
                # A filesystem or an account, which no amount of waiting
                # changes, and no child exists to settle.
                return _decide(election, election.last)
            return _settle(election, event.now, until=event.now + election.settlement)


def _round(election: Election, now: float) -> tuple[Election, list[Effect]]:
    if now >= election.active_until:
        return _settle(election, now)
    return _look(election, Stage.LOOKING, wait=0.0, deadline=election.active_until)


def _looked(election: Election, event: Looked) -> tuple[Election, list[Effect]]:
    if event.observed:
        election = replace(election, last=event.lookup)
    if election.stage is Stage.SETTLING:
        if event.conclusive:
            return _decide(election, event.lookup)
        return _settle_pass(election, event.now)
    election = replace(election, asked=election.asked or event.asked)
    if event.conclusive:
        return _decide(election, event.lookup)
    if event.now >= election.active_until:
        return _settle(election, event.now)
    if election.stage is Stage.WATCHING:
        return _round(election, event.now)
    if event.now < election.next_start:
        return _after_start(election, Attempt.CONTENDED, event.now)
    starts = election.starts + 1
    delay = start_delay_after(starts, election.delay, election.pacing)
    op = election.pending + 1
    election = replace(
        election,
        stage=Stage.STARTING,
        pending=op,
        starts=starts,
        delay=delay,
        next_start=event.now + delay,
    )
    return election, [StartOwner(op, election.active_until - event.now)]


def _start_ended(
    election: Election, event: StartEnded
) -> tuple[Election, list[Effect]]:
    if event.attempt is Attempt.ABORTED:
        # Retrying the same state would only spend the remaining budget.
        return _settle(election, event.now, until=event.now + election.settlement)
    return _after_start(election, event.attempt, event.now)


def _after_start(
    election: Election, attempt: Attempt, now: float
) -> tuple[Election, list[Effect]]:
    """Watch for the result of a start, or of somebody else's.

    A failed child is paced like a contended one, not retried at once: it may
    have freed a lock a sibling frontend now holds.
    """
    if attempt is Attempt.STARTED:
        election = replace(election, started=True)
    remaining = election.active_until - now
    if remaining <= 0:
        return _settle(election, now)
    return _look(
        election,
        Stage.WATCHING,
        wait=min(election.pacing.retry, remaining),
        deadline=election.active_until,
    )


def _settle(
    election: Election, now: float, *, until: float | None = None
) -> tuple[Election, list[Effect]]:
    tail = election.settle_until if until is None else min(election.settle_until, until)
    return _settle_pass(replace(election, tail_until=tail), now)


def _settle_pass(election: Election, now: float) -> tuple[Election, list[Effect]]:
    assert election.tail_until is not None
    remaining = election.tail_until - now
    if remaining <= 0:
        return _decide(election, election.last)
    return _look(
        election,
        Stage.SETTLING,
        wait=min(election.pacing.retry, remaining),
        deadline=election.tail_until,
    )


def _look(
    election: Election, stage: Stage, *, wait: float, deadline: float
) -> tuple[Election, list[Effect]]:
    op = election.pending + 1
    # Settlement never asks: it observes, and a request is an owner-side effect.
    may_ask = stage is not Stage.SETTLING and not election.asked
    return replace(election, stage=stage, pending=op), [
        Look(op, may_ask=may_ask, wait=wait, deadline=deadline)
    ]


def _decide(election: Election, lookup: object) -> tuple[Election, list[Effect]]:
    return replace(election, stage=Stage.DECIDED), [Decide(lookup, election.started)]
