"""The decisions one forwarded tool call makes, driven by events alone.

``daemon.core.call`` decides; nothing here awaits or reads a clock. Each test
answers the effects the core asks for with completion events, the way the shell
in ``daemon.proxy`` does, and asserts what the client would be told and what
work was asked for on the way. The rules checked are the contract's: a call
that may have acted is never repeated unless it is read-only, an expired call
is never told "not sent" after its request may have left, and one deadline
bounds every wait.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

import pytest

from linkedin_mcp_server.daemon.core import call as core
from linkedin_mcp_server.daemon.core.call import AttemptKind, Because, Dispatch

START = 100.0
BUDGET = 10.0
SHARE = 5 / 6
DEADLINE = START + BUDGET

#: One reply to the effect the call is waiting on, given that effect's op.
Reply = Callable[[int], core.Event]


def answered(result: object = "answer", *, auth: core.AuthRequest | None = None):
    return lambda op: core.Answered(op, START + 1, result, auth)


def lost(*, sent: bool, owner: str = "A", at: float = START + 1) -> Reply:
    return lambda op: core.OwnerLost(
        op, at, owner, nothing_was_sent=not sent, failure=f"lost {owner}"
    )


def raised() -> Reply:
    return lambda op: core.Raised(op, START + 1, RuntimeError("the tool broke"))


def recovered(found: bool) -> Reply:
    return lambda op: core.Recovered(op, START + 2, found)


def classified(could_change: bool) -> Reply:
    return lambda op: core.Classified(op, START + 3, could_change)


def repaired(outcome: core.Repair) -> Reply:
    return lambda op: core.Repaired(op, START + 4, outcome)


def signed_in(outcome: core.SignIn) -> Reply:
    return lambda op: core.SignedIn(op, START + 5, outcome)


def expired(*, request_left: bool = False) -> Reply:
    return lambda op: core.Expired(op, DEADLINE, request_left)


SIGN_IN = core.AuthRequest(
    reason="missing", replayable=True, browser_open=False, generation=None
)
STARTED_WORK = core.AuthRequest(
    reason="stale", replayable=False, browser_open=False, generation="g"
)
PROFILE_HELD = core.AuthRequest(
    reason="missing", replayable=True, browser_open=True, generation=None
)


@dataclass
class Run:
    call: core.Call
    effects: list[core.Effect]

    @property
    def ending(self) -> core.Effect:
        return self.effects[-1]

    @property
    def attempts(self) -> list[AttemptKind]:
        return [e.kind for e in self.effects if isinstance(e, core.Attempt)]

    def count(self, kind: type) -> int:
        return sum(isinstance(e, kind) for e in self.effects)


#: The effects a call waits on, each answered by one completion.
_WAITS = (
    core.Attempt,
    core.Recover,
    core.Classify,
    core.RepairAuth,
    core.WaitForSignIn,
)


def _waiting_op(effects: Sequence[core.Effect]) -> int | None:
    ops = [e.op for e in effects if isinstance(e, _WAITS)]
    assert len(ops) <= 1, f"more than one wait at once: {effects}"
    return ops[0] if ops else None


def drive(*replies: Reply, share: float = SHARE) -> Run:
    """Answer each wait with the next reply, until the call has ended."""
    call, effects = core.arrive(now=START, budget=BUDGET, sign_in_share=share)
    seen = list(effects)
    pending = list(replies)
    while (op := _waiting_op(effects)) is not None:
        assert pending, f"the call is still waiting: {effects}"
        call, effects = core.on_event(call, pending.pop(0)(op))
        seen.extend(effects)
    assert not pending, f"the call ended with {len(pending)} replies unused"
    assert isinstance(seen[-1], core.TERMINAL), seen[-1]
    return Run(call, seen)


def _says(effect: core.Effect) -> tuple[object, ...]:
    """What the client is told, as a comparable tuple."""
    match effect:
        case core.Return(result=result, because=because):
            return ("return", result, because)
        case core.ReportUnknown(failure=failure, expired=was_expired):
            return ("unknown", failure, was_expired)
        case core.ReportLost(failure=failure, expired=was_expired):
            return ("lost", failure, was_expired)
        case core.Reraise():
            return ("reraise",)
        case core.ReportRestricted():
            return ("restricted",)
    raise AssertionError(effect)


F, R, P = AttemptKind.FIRST, AttemptKind.RETRY, AttemptKind.REPLAY

#: Dispatch state at the failure point x what happens there -> what the client
#: is told, and every attempt made. Grouped by the point the call was waiting
#: at; the id names the dispatch state the call was in when it happened.
_TABLE = [
    # The first attempt, nothing sent yet.
    ("unsent @ attempt: answered", [answered()], ("return", "answer", Because.ANSWERED), [F]),
    ("unsent @ attempt: raised", [raised()], ("reraise",), [F]),
    ("unsent @ attempt: expired before sending", [expired()], ("lost", None, True), [F]),
    ("unsent @ attempt: expired after sending", [expired(request_left=True)], ("unknown", None, True), [F]),
    ("unsent @ attempt: lost, replaced, retried", [lost(sent=False), recovered(True), answered()], ("return", "answer", Because.ANSWERED), [F, R]),
    ("unsent @ attempt: lost, nobody to replace it", [lost(sent=False), recovered(False)], ("lost", "lost A", False), [F]),
    ("possibly sent @ attempt: mutating", [lost(sent=True), recovered(True), classified(True)], ("unknown", "lost A", False), [F]),
    ("possibly sent @ attempt: read-only, retried", [lost(sent=True), recovered(True), classified(False), answered()], ("return", "answer", Because.ANSWERED), [F, R]),
    ("possibly sent @ attempt: read-only, no replacement", [lost(sent=True), recovered(False), classified(False)], ("lost", "lost A", False), [F]),
    ("possibly sent @ attempt: mutating, no replacement", [lost(sent=True), recovered(False), classified(True)], ("unknown", "lost A", False), [F]),
    # Waiting on the shared recovery.
    ("unsent @ recovery: expired", [lost(sent=False), expired()], ("lost", "lost A", True), [F]),
    ("possibly sent @ recovery: expired", [lost(sent=True), expired()], ("unknown", "lost A", True), [F]),
    # Waiting on the annotation lookup.
    ("possibly sent @ classification: expired", [lost(sent=True), recovered(True), expired()], ("unknown", "lost A", True), [F]),
    # The retry on a replacement.
    ("unsent @ retry: lost unsent", [lost(sent=False), recovered(True), lost(sent=False, owner="B")], ("lost", "lost B", False), [F, R]),
    ("unsent @ retry: lost after sending, mutating", [lost(sent=False), recovered(True), lost(sent=True, owner="B"), classified(True)], ("unknown", "lost B", False), [F, R]),
    ("unsent @ retry: lost after sending, read-only", [lost(sent=False), recovered(True), lost(sent=True, owner="B"), classified(False)], ("lost", "lost B", False), [F, R]),
    ("possibly sent @ retry: lost again, read-only already known", [lost(sent=True), recovered(True), classified(False), lost(sent=True, owner="B")], ("lost", "lost B", False), [F, R]),
    ("unsent @ retry: raised", [lost(sent=False), recovered(True), raised()], ("reraise",), [F, R]),
    ("unsent @ retry: expired before sending", [lost(sent=False), recovered(True), expired()], ("lost", "lost A", True), [F, R]),
    ("unsent @ retry: expired after sending", [lost(sent=False), recovered(True), expired(request_left=True)], ("unknown", "lost A", True), [F, R]),
    ("possibly sent @ retry: expired before sending", [lost(sent=True), recovered(True), classified(False), expired()], ("unknown", "lost A", True), [F, R]),
    ("possibly sent @ second classification: expired", [lost(sent=False), recovered(True), lost(sent=True, owner="B"), expired()], ("unknown", "lost B", True), [F, R]),
    # Answered, asking for a sign-in: the answer is held from here on.
    ("answered @ marker: profile still held", [answered("sign in", auth=PROFILE_HELD)], ("return", "sign in", Because.PROFILE_HELD), [F]),
    ("answered @ repair: ready, read-only, replayed", [answered("sign in", auth=SIGN_IN), repaired(core.Repair.READY), classified(False), answered()], ("return", "answer", Because.ANSWERED), [F, P]),
    ("answered @ repair: ready, work had started", [answered("sign in", auth=STARTED_WORK), repaired(core.Repair.READY)], ("return", "sign in", Because.NOT_REPLAYABLE), [F]),
    ("answered @ repair: ready, could change something", [answered("sign in", auth=SIGN_IN), repaired(core.Repair.READY), classified(True)], ("return", "sign in", Because.COULD_CHANGE), [F]),
    ("answered @ repair: failed", [answered("sign in", auth=SIGN_IN), repaired(core.Repair.FAILED)], ("return", "sign in", Because.REPAIR_FAILED), [F]),
    ("answered @ repair: restricted", [answered("sign in", auth=SIGN_IN), repaired(core.Repair.RESTRICTED)], ("restricted",), [F]),
    ("answered @ repair: expired", [answered("sign in", auth=SIGN_IN), expired()], ("return", "sign in", Because.EXPIRED), [F]),
    ("answered @ sign-in: finished, replayed", [answered("sign in", auth=SIGN_IN), repaired(core.Repair.STARTED), signed_in(core.SignIn.SIGNED_IN), classified(False), answered()], ("return", "answer", Because.ANSWERED), [F, P]),
    ("answered @ sign-in: not finished", [answered("sign in", auth=SIGN_IN), repaired(core.Repair.STARTED), signed_in(core.SignIn.NOT_YET)], ("return", "sign in", Because.SIGN_IN_UNFINISHED), [F]),
    ("answered @ sign-in: restricted", [answered("sign in", auth=SIGN_IN), repaired(core.Repair.STARTED), signed_in(core.SignIn.RESTRICTED)], ("restricted",), [F]),
    ("answered @ sign-in: expired", [answered("sign in", auth=SIGN_IN), repaired(core.Repair.STARTED), expired()], ("return", "sign in", Because.EXPIRED), [F]),
    ("answered @ classification before replay: expired", [answered("sign in", auth=SIGN_IN), repaired(core.Repair.READY), expired()], ("return", "sign in", Because.EXPIRED), [F]),
    ("answered @ replay: asks to sign in again", [answered("sign in", auth=SIGN_IN), repaired(core.Repair.READY), classified(False), answered("again", auth=SIGN_IN)], ("return", "again", Because.ANSWERED), [F, P]),
    ("answered @ replay: expired after sending", [answered("sign in", auth=SIGN_IN), repaired(core.Repair.READY), classified(False), expired(request_left=True)], ("return", "sign in", Because.REPLAY_EXPIRED), [F, P]),
    ("answered @ replay: lost, replaced, retried", [answered("sign in", auth=SIGN_IN), repaired(core.Repair.READY), classified(False), lost(sent=False), recovered(True), answered()], ("return", "answer", Because.ANSWERED), [F, P, R]),
    ("answered @ replay recovery: expired", [answered("sign in", auth=SIGN_IN), repaired(core.Repair.READY), classified(False), lost(sent=True), expired()], ("return", "sign in", Because.REPLAY_EXPIRED), [F, P]),
    ("answered @ replay: lost after sending, classified afresh", [answered("sign in", auth=SIGN_IN), repaired(core.Repair.READY), classified(False), lost(sent=True), recovered(True), classified(True)], ("unknown", "lost A", False), [F, P]),
    # A retry's answer can ask for a sign-in too.
    ("answered @ retry: asks to sign in", [lost(sent=False), recovered(True), answered("sign in", auth=SIGN_IN), repaired(core.Repair.FAILED)], ("return", "sign in", Because.REPAIR_FAILED), [F, R]),
]  # fmt: skip


@pytest.mark.parametrize(
    ("replies", "says", "attempts"),
    [row[1:] for row in _TABLE],
    ids=[row[0] for row in _TABLE],
)
def test_every_dispatch_state_at_every_failure_point(
    replies: list[Reply], says: tuple[object, ...], attempts: list[AttemptKind]
):
    run = drive(*replies)

    assert _says(run.ending) == says
    assert run.attempts == attempts
    # Each owner failure is recorded once, whatever follows it.
    assert run.count(core.Bury) == sum(
        isinstance(reply(0), core.OwnerLost) for reply in replies
    )
    # One recovery per invocation at most, never one for a second attempt.
    assert run.count(core.Recover) <= attempts.count(F) + attempts.count(P)


def test_an_expired_call_that_may_have_acted_ends_unknown():
    run = drive(expired(request_left=True))
    assert run.call.dispatch is Dispatch.UNKNOWN


def test_the_deadline_is_counted_from_arrival():
    """Every wait ends at arrival plus the budget, the sign-in's earlier."""
    call, _ = core.arrive(now=START, budget=12.0, sign_in_share=5 / 6)
    assert call.deadline == START + 12.0

    run = drive(
        answered("sign in", auth=SIGN_IN),
        repaired(core.Repair.STARTED),
        signed_in(core.SignIn.NOT_YET),
    )
    (wait,) = [e for e in run.effects if isinstance(e, core.WaitForSignIn)]
    assert wait.until == pytest.approx(START + BUDGET * SHARE)


def test_the_sign_in_never_waits_past_the_deadline():
    run = drive(
        answered("sign in", auth=SIGN_IN),
        repaired(core.Repair.STARTED),
        signed_in(core.SignIn.NOT_YET),
        share=2.0,
    )
    (wait,) = [e for e in run.effects if isinstance(e, core.WaitForSignIn)]
    assert wait.until == DEADLINE


@pytest.mark.parametrize(
    ("late", "says"),
    [
        (lost(sent=False, at=DEADLINE), ("lost", "lost A", True)),
        (lost(sent=True, at=DEADLINE + 1), ("unknown", "lost A", True)),
        (
            lambda op: core.Answered(op, DEADLINE, "sign in", SIGN_IN),
            ("return", "sign in", Because.EXPIRED),
        ),
        (
            lambda op: core.Answered(op, DEADLINE + 1, "answer"),
            ("return", "answer", Because.ANSWERED),
        ),
    ],
    ids=[
        "a failure before sending",
        "a failure after sending",
        "a request to sign in",
        "an answer",
    ],
)
def test_nothing_new_starts_once_the_deadline_has_passed(late: Reply, says):
    """A completion arriving at the deadline ends the call; an answer is used."""
    run = drive(late)

    assert _says(run.ending) == says
    assert not any(
        isinstance(e, (core.Recover, core.Classify, core.RepairAuth))
        for e in run.effects
    )


def test_a_completion_for_another_operation_changes_nothing():
    call, effects = core.arrive(now=START, budget=BUDGET, sign_in_share=SHARE)
    (attempt,) = effects
    assert isinstance(attempt, core.Attempt)

    stale = core.Answered(attempt.op - 1, START + 1, "an older answer")
    assert core.on_event(call, stale) == (call, [])

    ended, last = core.on_event(call, core.Answered(attempt.op, START + 1, "answer"))
    assert core.on_event(ended, core.Recovered(attempt.op, START + 2, True)) == (
        ended,
        [],
    )
    assert _says(last[-1]) == ("return", "answer", Because.ANSWERED)


def test_a_completion_of_the_wrong_kind_is_refused():
    call, (attempt,) = core.arrive(now=START, budget=BUDGET, sign_in_share=SHARE)
    assert isinstance(attempt, core.Attempt)
    with pytest.raises(ValueError, match="does not complete"):
        core.on_event(call, core.Recovered(attempt.op, START + 1, True))


# --- Schedules ---------------------------------------------------------------------
#
# Every way a call can be answered, explored to the end, and checked against
# rules written from the contract rather than read off the reducer.

_ATTEMPT_REPLIES: list[tuple[str, Reply]] = [
    ("answered", answered()),
    ("asks to sign in", answered("held", auth=SIGN_IN)),
    ("asks after starting work", answered("held", auth=STARTED_WORK)),
    ("asks with the profile held", answered("held", auth=PROFILE_HELD)),
    ("lost unsent", lost(sent=False)),
    ("lost sent", lost(sent=True)),
    ("lost unsent, late", lost(sent=False, at=DEADLINE)),
    ("lost sent, late", lost(sent=True, at=DEADLINE)),
    ("raised", raised()),
    ("expired unsent", expired()),
    ("expired sent", expired(request_left=True)),
]
_REPLIES: dict[type, list[tuple[str, Reply]]] = {
    core.Attempt: _ATTEMPT_REPLIES,
    core.Recover: [
        ("found", recovered(True)),
        ("none", recovered(False)),
        ("expired", expired()),
    ],
    core.Classify: [
        ("could change", classified(True)),
        ("read-only", classified(False)),
        ("expired", expired()),
    ],
    core.RepairAuth: [(outcome.value, repaired(outcome)) for outcome in core.Repair]
    + [("expired", expired())],
    core.WaitForSignIn: [(outcome.value, signed_in(outcome)) for outcome in core.SignIn]
    + [("expired", expired())],
}


@dataclass(frozen=True)
class Step:
    asked: core.Effect
    reply: core.Event
    then: tuple[core.Effect, ...]


def _schedules() -> Iterator[list[Step]]:
    """Every complete schedule of one call, depth first."""

    def explore(call: core.Call, waiting: core.Effect, path: list[Step]):
        assert len(path) < 16, "a call that does not end"
        for _name, reply in _REPLIES[type(waiting)]:
            op = _waiting_op([waiting])
            assert op is not None
            event = reply(op)
            after, effects = core.on_event(call, event)
            step = Step(waiting, event, tuple(effects))
            nxt = [e for e in effects if isinstance(e, _WAITS)]
            if nxt:
                yield from explore(after, nxt[0], [*path, step])
            else:
                yield [*path, step]

    call, (first,) = core.arrive(now=START, budget=BUDGET, sign_in_share=SHARE)
    yield from explore(call, first, [])


def _invocations(path: list[Step]) -> list[list[Step]]:
    """Split a schedule where the replay after a sign-in starts."""
    parts: list[list[Step]] = [[]]
    for step in path:
        if isinstance(step.asked, core.Attempt) and step.asked.kind is P:
            parts.append([])
        parts[-1].append(step)
    return parts


def _possibly_sent(step: Step) -> bool:
    return (
        isinstance(step.reply, core.OwnerLost) and not step.reply.nothing_was_sent
    ) or (isinstance(step.reply, core.Expired) and step.reply.request_left)


def _ran_out(step: Step) -> bool:
    return isinstance(step.reply, core.Expired) or step.reply.now >= DEADLINE


def test_every_schedule_of_one_call_keeps_the_contract():
    schedules = list(_schedules())
    # Every reply to every wait, to the end: a sanity count, not a contract.
    assert len(schedules) > 300

    for path in schedules:
        effects = [e for step in path for e in step.then]
        where = [type(s.reply).__name__ for s in path]
        ending = effects[-1]
        terminals = [e for e in effects if isinstance(e, core.TERMINAL)]
        assert terminals == [ending], where

        # Bounded: two attempts and one recovery per invocation, two invocations.
        attempts = [s for s in path if isinstance(s.asked, core.Attempt)]
        assert len(attempts) <= 4, where
        for part in _invocations(path):
            assert sum(isinstance(s.asked, core.Attempt) for s in part) <= 2, where
            assert sum(isinstance(s.asked, core.Recover) for s in part) <= 1, where

        # Every owner failure is recorded at once.
        for step in path:
            if isinstance(step.reply, core.OwnerLost):
                assert step.then[0] == core.Bury(step.reply.failure), where

        # Once the deadline has passed, the call only ends.
        for i, step in enumerate(path):
            if _ran_out(step) and not isinstance(step.reply, core.Answered):
                assert i == len(path) - 1, where
                assert not any(isinstance(e, _WAITS) for e in step.then), where

        held = next(
            (
                s.reply.result
                for s in path
                if isinstance(s.reply, core.Answered) and s.reply.auth is not None
            ),
            None,
        )
        last = path[-1]
        invocation = _invocations(path)[-1]
        may_have_acted = any(_possibly_sent(s) for s in invocation)
        said_read_only = any(
            isinstance(s.reply, core.Classified) and not s.reply.could_change
            for s in invocation
        )

        # A call that may have acted and could change something is never run
        # again: after a possible send, the only retry is a confirmed read.
        for part in _invocations(path):
            for i, step in enumerate(part):
                if not _possibly_sent(step):
                    continue
                later = part[i + 1 :]
                retried = any(isinstance(s.asked, core.Attempt) for s in later)
                if retried:
                    assert any(
                        isinstance(s.reply, core.Classified)
                        and not s.reply.could_change
                        for s in later
                    ), where

        # What an expired call is told depends only on how far it got.
        if _ran_out(last) and not isinstance(last.reply, (core.Answered, core.Raised)):
            if held is not None:
                assert isinstance(ending, core.Return), where
                assert ending.result == held, where
            elif may_have_acted:
                assert isinstance(ending, core.ReportUnknown), where
            else:
                assert isinstance(ending, core.ReportLost), where

        # "Not sent" after a possible send only for a confirmed read, and never
        # at the deadline.
        if isinstance(ending, core.ReportLost) and may_have_acted:
            assert said_read_only and not _ran_out(last), where
        # An unknown outcome before the deadline only for a call that may have
        # changed something.
        if isinstance(ending, core.ReportUnknown) and not ending.expired:
            assert may_have_acted and not said_read_only, where
