"""The parent's side of one owner start, decided without touching the child.

A frontend that wins nothing yet starts a child, greets it, hands over the
configuration, waits for the generation it prepared, checks that generation on
disk and asks the child to commit it. The exchange is
:mod:`linkedin_mcp_server.daemon.startup`; which step comes next, when the child
is stopped and what the attempt reports are decided here, from events alone.
``daemon.election._Startup`` runs each effect and feeds back what came of it.

**Publication happens only inside the lock-holding child.** The parent writes
nothing canonical. Its one write is :class:`DiscardPrepared`, which names the
pending state of the generation its own child prepared, and only while no COMMIT
can have reached that child. Ignoring a late completion does not cancel a rename,
so no timeout here leaves a writer behind that could publish later: the only
writer is the child, and the child is either stopped or owns its own lifetime.

**The termination authority ends where COMMIT may begin.** Before the COMMIT
frame is attempted, every failure, interruption and silence stops the child.
From then on the child settles its own lifetime: it commits and serves, or reads
end of file and discards its state. After that point the parent stops it only on
the child's authenticated verdict that it did not publish (``ABORTED`` or
``RETRY``), and never for a timeout, an unreadable answer or an interruption.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, replace


class Started(enum.Enum):
    """What the startup protocol established at the frontend boundary."""

    #: The endpoint answered and the descriptor is published.
    YES = "yes"

    #: The child could not serve, said so, died trying, or was stopped for
    #: having said nothing at all.
    NO = "no"

    #: The child proved a permanent error or could not publish safely.
    ABORTED = "aborted"

    #: This generation ended safely after releasing its position. Try again while
    #: the frontend's election budget remains.
    RETRY = "retry"

    #: Neither, within the budget, before a prepared generation exists.
    STILL_TRYING = "still_trying"

    #: The COMMIT frame may have reached the lock holder, which settles its own
    #: lifetime and must not be killed by the parent.
    UNCERTAIN = "uncertain"


class Attempt(enum.Enum):
    """What came of trying to start an owner, as the election acts on it.

    A boolean cannot carry this, and collapsing it was measured to cost a
    minute: a child that reported it could not serve read as "somebody is
    starting", so the caller waited out its whole deadline for a descriptor
    nothing was going to write.
    """

    #: An owner is up and has published. Ready to attach.
    STARTED = "started"

    #: Somebody is starting: another process holds the lock, or the child this
    #: process started has not answered yet and is still holding one. Wait.
    CONTENDED = "contended"

    #: The child this process started did not become an owner, and has been
    #: stopped and waited for. If the kernel could not finish that stop within
    #: the bounded wait, the shell logged that the profile may remain locked.
    FAILED = "failed"

    #: The child proved a permanent startup or publication failure. Retrying the
    #: same state would only spend the remaining election budget.
    ABORTED = "aborted"


def attempt_after(started: Started) -> Attempt:
    """What one start means to the election.

    A retry, an unfinished start and an uncertain commit all say the same thing
    to it: somebody may hold the lock now, so wait and look again.
    """
    match started:
        case Started.YES:
            return Attempt.STARTED
        case Started.NO:
            return Attempt.FAILED
        case Started.ABORTED:
            return Attempt.ABORTED
    return Attempt.CONTENDED


@dataclass(frozen=True)
class Budgets:
    """Fixed bounds of the exchange, in seconds, as the shell configures them."""

    #: How long a failed step still listens for the child's own verdict.
    failure_verdict: float
    #: The longest the parent spends reading the prepared generation.
    prepared_read: float


class Step(enum.Enum):
    """Which operation the attempt is waiting on."""

    #: The child exists; the Job, the gate and the channel are being set up.
    CONNECTING = "connecting"
    GREETING = "greeting"
    HANDING_OVER = "handing_over"
    #: The configuration did not cross; listening briefly for a verdict.
    SALVAGING_PREPARED = "salvaging_prepared"
    PREPARING = "preparing"
    VALIDATING = "validating"
    #: The prepared state is unusable; listening briefly for a verdict.
    SALVAGING_VERDICT = "salvaging_verdict"
    COMMITTING = "committing"
    AWAITING_VERDICT = "awaiting_verdict"
    #: No verdict came after COMMIT; the canonical descriptor is read instead.
    SETTLING = "settling"
    DONE = "done"


#: Where an interruption stops the child: every step before COMMIT but one.
#: After a failed hand-over the brief wait is left to the wind-down, as it
#: always was: a Job stops the child there, and elsewhere the channel's end of
#: file aborts a child that never received COMMIT.
_INTERRUPTION_STOPS = frozenset(
    {
        Step.CONNECTING,
        Step.GREETING,
        Step.HANDING_OVER,
        Step.PREPARING,
        Step.VALIDATING,
        Step.SALVAGING_VERDICT,
    }
)


@dataclass(frozen=True)
class Spawn:
    """One start's state between events."""

    #: A Windows Job holds the child, so the wind-down must settle it.
    contained: bool
    budgets: Budgets
    step: Step = Step.CONNECTING
    #: The operation the attempt is waiting on. Any other completion is stale.
    pending: int = 0
    #: On the shell's monotonic clock, when the greeting began.
    began: float = 0.0
    #: The budget for the whole exchange, counted from *began*.
    timeout: float = 0.0
    #: The generation the child prepared, once it named one.
    prepared: str | None = None
    #: COMMIT may have reached the child, which owns its own lifetime from here.
    commit_attempted: bool = False
    #: A stop was ordered. The Job, if any, went with it.
    stopped: bool = False


# --- Events: each completes the operation it names --------------------------------


@dataclass(frozen=True)
class Connected:
    """The channel is up at *now*; the exchange may take *timeout* from here."""

    now: float
    timeout: float


@dataclass(frozen=True)
class Greeted:
    """The greeting ended. *refusal* is ``None`` for an ACK from this build."""

    op: int
    now: float
    refusal: Started | None


@dataclass(frozen=True)
class HandedOver:
    op: int
    now: float


@dataclass(frozen=True)
class HandOverFailed:
    op: int
    now: float


@dataclass(frozen=True)
class Prepared:
    """The child's first answer after the configuration: a generation, or a verdict."""

    op: int
    now: float
    answer: str | Started


@dataclass(frozen=True)
class Validated:
    op: int
    now: float


@dataclass(frozen=True)
class ValidationTimedOut:
    op: int
    now: float


@dataclass(frozen=True)
class ValidationFailed:
    op: int
    now: float


@dataclass(frozen=True)
class CommitSent:
    op: int
    now: float


@dataclass(frozen=True)
class CommitUndelivered:
    op: int
    now: float


@dataclass(frozen=True)
class Verdict:
    """The child's answer to COMMIT, or to a failed step.

    ``ABORTED`` and ``RETRY`` are only ever read off an authenticated VERDICT
    frame; an unreadable frame is ``NO`` and silence ``STILL_TRYING``.
    """

    op: int
    now: float
    verdict: Started


@dataclass(frozen=True)
class Settled:
    """What the canonical descriptor said after a COMMIT without a verdict."""

    op: int
    now: float
    verdict: Started


@dataclass(frozen=True)
class Interrupted:
    """The operation *op* raised something none of the events above describes."""

    op: int


Event = (
    Connected
    | Greeted
    | HandedOver
    | HandOverFailed
    | Prepared
    | Validated
    | ValidationTimedOut
    | ValidationFailed
    | CommitSent
    | CommitUndelivered
    | Verdict
    | Settled
    | Interrupted
)


# --- Effects: work for the shell ---------------------------------------------------


@dataclass(frozen=True)
class Greet:
    op: int
    timeout: float


@dataclass(frozen=True)
class HandOver:
    op: int
    timeout: float


@dataclass(frozen=True)
class AwaitPrepared:
    op: int
    timeout: float


@dataclass(frozen=True)
class Validate:
    """Read the prepared generation *instance* back, until *until* at the latest."""

    op: int
    instance: str
    until: float


@dataclass(frozen=True)
class SendCommit:
    op: int
    instance: str
    timeout: float


@dataclass(frozen=True)
class AwaitVerdict:
    op: int
    timeout: float


@dataclass(frozen=True)
class SettleCommit:
    """Read which generation is canonical. Never a reason to stop the child."""

    op: int
    instance: str
    timeout: float


@dataclass(frozen=True)
class StopChild:
    """Kill the child's tree and wait for it, within the shell's own bound.

    *silent* is a child that prepared nothing within the budget, which the
    shell says before stopping it.
    """

    silent: bool = False


@dataclass(frozen=True)
class ReportFailure:
    """Log the most actionable diagnosis the child left on its bootstrap pipe."""


@dataclass(frozen=True)
class Finish:
    started: Started


@dataclass(frozen=True)
class ReleaseJob:
    """Close this process's handle on the Job without terminating it."""


@dataclass(frozen=True)
class HangUp:
    """Close this side of the startup channel and collect a child that exited."""


@dataclass(frozen=True)
class DiscardPrepared:
    """Remove the pending state of *instance*, which no COMMIT reached."""

    instance: str


Effect = (
    Greet
    | HandOver
    | AwaitPrepared
    | Validate
    | SendCommit
    | AwaitVerdict
    | SettleCommit
    | StopChild
    | ReportFailure
    | Finish
    | ReleaseJob
    | HangUp
    | DiscardPrepared
)

#: Effects that wait for the event completing them. At most one per decision.
WAITS = (
    Greet,
    HandOver,
    AwaitPrepared,
    Validate,
    SendCommit,
    AwaitVerdict,
    SettleCommit,
)

_EXPECTED: dict[Step, tuple[type, ...]] = {
    Step.CONNECTING: (Connected,),
    Step.GREETING: (Greeted,),
    Step.HANDING_OVER: (HandedOver, HandOverFailed),
    Step.SALVAGING_PREPARED: (Prepared,),
    Step.PREPARING: (Prepared,),
    Step.VALIDATING: (Validated, ValidationTimedOut, ValidationFailed),
    Step.SALVAGING_VERDICT: (Verdict,),
    Step.COMMITTING: (CommitSent, CommitUndelivered),
    Step.AWAITING_VERDICT: (Verdict,),
    Step.SETTLING: (Settled,),
}


def spawned(*, contained: bool, budgets: Budgets) -> Spawn:
    """A child that exists and holds nothing this process gave it yet."""
    return Spawn(contained=contained, budgets=budgets)


def on_event(spawn: Spawn, event: Event) -> tuple[Spawn, list[Effect]]:
    """The attempt after *event*, and what the shell must do next."""
    if spawn.step is Step.DONE:
        return spawn, []
    if isinstance(event, Interrupted):
        return _interrupted(spawn, event)
    if not isinstance(event, Connected) and event.op != spawn.pending:
        return spawn, []
    if not isinstance(event, _EXPECTED[spawn.step]):
        raise ValueError(
            f"{type(event).__name__} does not complete a start that is "
            f"{spawn.step.value}"
        )
    match event:
        case Connected():
            spawn = replace(spawn, began=event.now, timeout=event.timeout)
            return _issue(spawn, Step.GREETING, lambda op: Greet(op, event.timeout))
        case Greeted():
            return _greeted(spawn, event)
        case HandedOver():
            remaining = _remaining(spawn, event.now)
            return _issue(
                spawn, Step.PREPARING, lambda op: AwaitPrepared(op, remaining)
            )
        case HandOverFailed():
            # Listening briefly anyway: a child that prepared before the write
            # failed names a generation the wind-down must discard.
            budget = spawn.budgets.failure_verdict
            return _issue(
                spawn, Step.SALVAGING_PREPARED, lambda op: AwaitPrepared(op, budget)
            )
        case Prepared():
            return _prepared(spawn, event)
        case Validated():
            # From here the child may own itself. Everything after this line
            # follows the termination authority in the module docstring.
            spawn = replace(spawn, commit_attempted=True)
            instance = _prepared_instance(spawn)
            remaining = _remaining(spawn, event.now)
            return _issue(
                spawn, Step.COMMITTING, lambda op: SendCommit(op, instance, remaining)
            )
        case ValidationTimedOut():
            # No COMMIT was sent, so this child cannot become canonical. Waiting
            # for a verdict would leave it holding the lock until its own
            # authorization timeout expires.
            return _end(spawn, Started.NO, stop=True)
        case ValidationFailed():
            budget = spawn.budgets.failure_verdict
            return _issue(
                spawn, Step.SALVAGING_VERDICT, lambda op: AwaitVerdict(op, budget)
            )
        case CommitSent():
            remaining = _remaining(spawn, event.now)
            return _issue(
                spawn, Step.AWAITING_VERDICT, lambda op: AwaitVerdict(op, remaining)
            )
        case CommitUndelivered():
            return _settle(spawn, event.now)
        case Verdict():
            return _verdict(spawn, event)
        case Settled():
            return _end(spawn, event.verdict)


def wind_down(spawn: Spawn) -> list[Effect]:
    """What every attempt ends with, however it ended, in order."""
    effects: list[Effect] = []
    if spawn.contained and not spawn.stopped:
        # The COMMIT frame precedes the owner adopting its own Job. Closing this
        # copy cannot revoke an adopted owner, and kills one that never adopted.
        effects.append(ReleaseJob() if spawn.commit_attempted else StopChild())
    # End of file aborts a prepared child that never received COMMIT. After that
    # frame it is only a lease: the lock holder settles publication.
    effects.append(HangUp())
    if spawn.prepared is not None and not spawn.commit_attempted:
        effects.append(DiscardPrepared(spawn.prepared))
    return effects


def may_stop(spawn: Spawn) -> bool:
    """Whether this process may still kill the child for its own reasons."""
    return not spawn.commit_attempted


def _greeted(spawn: Spawn, event: Greeted) -> tuple[Spawn, list[Effect]]:
    if event.refusal is None:
        remaining = _remaining(spawn, event.now)
        return _issue(spawn, Step.HANDING_OVER, lambda op: HandOver(op, remaining))
    # Nothing usable crossed: the configuration is written only after a matching
    # ACK, so this child holds no settings and no lock it could have taken with
    # them.
    return _end(spawn, event.refusal, stop=True, report=event.refusal is Started.NO)


def _prepared(spawn: Spawn, event: Prepared) -> tuple[Spawn, list[Effect]]:
    answer = event.answer
    if spawn.step is Step.SALVAGING_PREPARED:
        if isinstance(answer, str):
            spawn = replace(spawn, prepared=answer)
        outcome = Started.RETRY if answer is Started.RETRY else Started.NO
        return _end(spawn, outcome, stop=True, report=True)
    if isinstance(answer, str):
        spawn = replace(spawn, prepared=answer)
        until = event.now + min(
            spawn.budgets.prepared_read, max(_remaining(spawn, event.now), 0.0)
        )
        return _issue(spawn, Step.VALIDATING, lambda op: Validate(op, answer, until))
    match answer:
        case Started.STILL_TRYING:
            return _end(spawn, Started.NO, stop=True, report=True, silent=True)
        case Started.NO | Started.ABORTED:
            return _end(spawn, answer, stop=True, report=True)
        case Started.RETRY:
            return _end(spawn, Started.RETRY, stop=True)
    raise ValueError(f"{answer} does not answer a configuration")


def _verdict(spawn: Spawn, event: Verdict) -> tuple[Spawn, list[Effect]]:
    verdict = event.verdict
    if spawn.step is Step.SALVAGING_VERDICT:
        # No COMMIT was sent, so the child is stopped however its verdict reads.
        outcome = Started.RETRY if verdict is Started.RETRY else Started.ABORTED
        return _end(spawn, outcome, stop=True)
    match verdict:
        case Started.YES | Started.UNCERTAIN:
            return _end(spawn, verdict)
        case Started.ABORTED:
            # The child's own authenticated word that it did not publish, and
            # the only kind of answer that licenses a stop after COMMIT.
            return _end(spawn, Started.ABORTED, stop=True, report=True)
        case Started.RETRY:
            return _end(spawn, Started.RETRY, stop=True)
    return _settle(spawn, event.now)


def _settle(spawn: Spawn, now: float) -> tuple[Spawn, list[Effect]]:
    # After the COMMIT frame may have arrived, a dead child is not proof of an
    # uncommitted generation, and silence is not proof of anything. Only the
    # canonical descriptor can say YES; everything else is UNCERTAIN.
    instance = _prepared_instance(spawn)
    remaining = _remaining(spawn, now)
    return _issue(
        spawn, Step.SETTLING, lambda op: SettleCommit(op, instance, remaining)
    )


def _interrupted(spawn: Spawn, event: Interrupted) -> tuple[Spawn, list[Effect]]:
    if event.op != spawn.pending:
        return spawn, []
    stop = spawn.step in _INTERRUPTION_STOPS and may_stop(spawn)
    spawn = replace(spawn, step=Step.DONE, stopped=spawn.stopped or stop)
    return spawn, [StopChild()] if stop else []


def _remaining(spawn: Spawn, now: float) -> float:
    return spawn.timeout - (now - spawn.began)


def _prepared_instance(spawn: Spawn) -> str:
    if spawn.prepared is None:
        raise ValueError("no generation was prepared")
    return spawn.prepared


def _issue(
    spawn: Spawn, step: Step, effect: Callable[[int], Effect]
) -> tuple[Spawn, list[Effect]]:
    op = spawn.pending + 1
    return replace(spawn, step=step, pending=op), [effect(op)]


def _end(
    spawn: Spawn,
    started: Started,
    *,
    stop: bool = False,
    report: bool = False,
    silent: bool = False,
) -> tuple[Spawn, list[Effect]]:
    effects: list[Effect] = []
    if stop:
        if not may_stop(spawn) and started not in (Started.ABORTED, Started.RETRY):
            raise AssertionError(f"a stop after COMMIT on {started}")
        effects.append(StopChild(silent=silent))
    if report:
        effects.append(ReportFailure())
    effects.append(Finish(started))
    return replace(spawn, step=Step.DONE, stopped=spawn.stopped or stop), effects
