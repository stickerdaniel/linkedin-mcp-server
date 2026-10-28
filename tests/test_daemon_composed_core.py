"""Every decision core, composed through its effects, over every bounded schedule.

The reducers in ``daemon/core`` are driven the way their shells drive them and
wired to each other the way the processes are. An election's ``Look`` runs a
lookup pass whose ``Conclude`` returns as ``Looked``; its ``StartOwner`` runs one
spawn whose ``Finish`` returns through ``attempt_after``; a pass's turnover
request reaches the owner it read as ``TurnOver``; a call's ``Attempt`` is bound
to one owner incarnation and admitted by that owner's reducer; its ``Recover``
joins its frontend's one election, and only that election answers it. Every
decision is a real reducer's. What the shells keep for themselves stays in the
world, never in reducer state: process, lock, descriptor and incarnation
identity, a pass's last read (which it probes, asks to stand down and reports,
as the election shell does), and the ending an owner's loop carried out.
Bounded, decision-level evidence only.

**Bounds.** One profile and two frontends, F of build 1 and G of build 2, so at
most two elections run at once. In the *turnover* world a resident owner R of
build 1 holds two calls of F: c1, which could change something and whose body
has begun, and c2, read-only and still on its way. G elects from the start and
asks R to turn over; F elects when a call needs a recovery. In the *fresh*
worlds both frontends elect from the start on an empty profile, once with G of
build 2 and once with both of build 1. Each child prepares a generation named
after itself, and whichever publishes is the successor; G may ask a successor
F started to turn over, and nothing asks one G started. A call makes at most
two attempts and one recovery, the call core's own bound. A recovery takes
its flight's answer; the second flight ``recover`` allows after late burial news
is not explored. No sign-in or other protocol.

**Configurations.** Each frontend and owner process carries a configuration: R
serves F's, and a child its own frontend's. Each world runs once with G of F's
configuration and once with another. A pass then reads an owner of the other
configuration as control only. G asks an older one to turn over all the same,
after which F's recovery finds no successor it may attach to. A frontend that
finds one of its own version (*fresh-rival*, both of build 1) leaves it alone
and ends in Direct.

**Clock and schedules.** One monotone simulated clock stamps every event.
Completions are instantaneous and interleave in every order within an instant;
the clock ticks one unit only when none is left, so ordering decides each race
and a timed wait (a pause, a drain cutoff, a deadline) decides when time moves.
It stops at a horizon no election or call can outlast, so a world still working
there has failed to end. A tool body alone may run across ticks, which is how a
drain reaches its cutoff with work running. A completion is: a descriptor read
or pause; a probe (a live owner answers, a gone one refuses); COMMIT (it arrives
and is reported sent, which makes the descriptor canonical); the published
child's shell telling its owner the endpoint is live, a separate move so a call
can reach a canonical owner that has not yet heard it; a verdict; a call
reaching its owner; a body beginning or ending; an answer reaching its frontend; an owner's loop turn, which runs only
when it can decide (after a turnover request, and when the drain is empty or
past its cutoff); an owner's exit, once its loop chose ``Stop``; a call's
expiry. A greeting, the hand-over, the prepared answer, the validation, the
canonical read after COMMIT and the tool's classification are answered inside
the move that asked, since nothing else can change them meanwhile. Each schedule may spend one fault: a live owner
silent to a probe, COMMIT arriving unreported or not arriving, or a lost
verdict. Decisions are atomic, so no yield separates an admission's reservation
from its task.

**Environment, assumed rather than proved.** The daemon lock has one holder and
is freed when its holder exits. The profile lease is perfect, so a frontend that
falls back to Direct adds no browser; nothing here proves the lease. Heartbeats
always arrive, so nothing expires and no owner stalls. A stand-down request
arrives. Direct is a declared symbolic comparator and none of its code runs:
one browser per profile under the lease, no signal to another process's child,
no removal of a published generation, no repeated call.

**Projections, written from the contract, and what they cannot establish.**

1. No extra concurrent browser: one lock holder and one live published owner;
   COMMIT only to the holder, admission only by it; attaching only to the owner
   serving at that moment, and only to one of the frontend's configuration; a
   live owner the frontend could attach to is never written off. Cannot see
   browsers or the lease.
2. No wrong-target signal: a published child is never stopped; ``Cancel`` names
   a running task of that owner incarnation; turnover is asked only of an older
   owner. Cannot see PIDs, process groups, guardians or Jobs.
3. No mutation Direct refuses: a published generation is never discarded.
   Cannot see profile claims, files or Direct's own mutations.
4. No silent session loss: no admission before the owner heard it was
   published, nor after retirement; a call that may have
   acted is admitted again only if it is read-only or its owner said nothing
   ran; an owner says nothing ran only of a body that never began; a call whose
   body began is told "not sent" only after a read-only classification and
   before its deadline; every call and election ends, and every owner asked
   to go exits. Cannot see cookies, persisted sessions or real delivery.

Ticks only move the clock forward, so a move back to a world still on the
search path is work that never ends, and fails as such: deduplication alone
would read it as a world already judged.

Every rule but these has a broken reducer below that must produce its
counterexample, in a world of either configuration: one holder and one
published owner, COMMIT and admission only by the holder, attaching only to the
frontend's configuration, the ``Cancel`` target, and every call and election
ending. Those were seen to fail only against faults planted in the world; the
configuration rule against a pass shell that attaches on an ``AS_READ`` finding
whether or not its read was worth connecting to. Routing a recovery to its own
frontend's election is fidelity rather than a rule: answered from G's election
instead, no rule here breaks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Protocol

import pytest

from linkedin_mcp_server.daemon.core import call as calls
from linkedin_mcp_server.daemon.core import election
from linkedin_mcp_server.daemon.core import lookup
from linkedin_mcp_server.daemon.core import owner as lifecycle
from linkedin_mcp_server.daemon.core import spawn
from linkedin_mcp_server.daemon.core.lookup import Finding, Reach, Reading
from linkedin_mcp_server.daemon.core.spawn import Started

R, C1, C2 = "r", "c1", "c2"
#: Whether running the call again could change something.
MUTATING = {C1: True, C2: False}
RETIRING, UNREACHABLE = "retiring", "unreachable"

PACING = election.Pacing(retry=1.0, burst=1, first_delay=4.0, max_delay=4.0)
TIMINGS = lookup.Timings(read=1.0, poll=1.0, probe=1.0)
BUDGETS = spawn.Budgets(failure_verdict=1.0, prepared_read=1.0)
DEADLINE, SETTLEMENT, CALL_BUDGET, DRAIN = 6.0, 2.0, 8.0, 2.0
#: Past this the search is incomplete, which fails the test rather than passes.
MAX_STATES = 200_000


class _Broken(Exception):
    """A projection that does not hold, named by its rule."""

    def __init__(self, rule: str) -> None:
        super().__init__(rule)
        self.rule = rule


def _expect(holds: bool, rule: str) -> None:
    if not holds:
        raise _Broken(rule)


class _Calls(dict[int, lifecycle.Call]):
    """An owner's admitted calls, hashable so equal worlds are visited once."""

    def __hash__(self) -> int:
        return hash(frozenset(self.items()))


def _owner_on(
    owner: lifecycle.Owner, event: lifecycle.Event
) -> tuple[lifecycle.Owner, list[lifecycle.Effect]]:
    after, effects = lifecycle.on_event(owner, event)
    return replace(after, calls=_Calls(after.calls)), effects


@dataclass(frozen=True)
class _Proc:
    """An owner process: R, or the child of one start."""

    build: int
    #: The configuration it serves: R's is F's, a child's its frontend's.
    config: int = 1
    alive: bool = True
    holds_lock: bool = False
    #: Its descriptor is canonical.
    published: bool = False
    #: Its shell told the owner reducer so, which is what admission waits for.
    live: bool = False
    #: The owner reducer, once the process serves.
    owner: lifecycle.Owner | None = None
    #: A turnover request reached it.
    asked_to_go: bool = False
    #: The ending its loop carried out, the shell's record rather than the core's.
    ended_by: lifecycle.Stop | lifecycle.EndpointGone | None = None


@dataclass(frozen=True)
class _Task:
    """One admission, as the owner's shell started it."""

    owner: str
    ticket: int
    call: str
    op: int
    began: bool = False
    done: bool = False


@dataclass(frozen=True)
class _Answer:
    """An owner's answer to one attempt that its frontend has not read yet."""

    owner: str
    call: str
    op: int
    #: ``done``, or how a cut call was stopped (``lifecycle.Stopped``).
    says: str


@dataclass(frozen=True)
class _CallRun:
    call: calls.Call
    wait: calls.Effect | None
    #: The owner incarnation the pending attempt is bound to, and its ticket.
    bound: str | None = None
    ticket: int | None = None
    told: calls.Effect | None = None
    #: The truth, outside the reducer: a body of this call began somewhere; an
    #: admission of it may have acted; it was classified read-only.
    began: bool = False
    exposed: bool = False
    read_only: bool = False


@dataclass(frozen=True)
class _Front:
    build: int
    config: int = 1
    electing: election.Election | None = None
    pass_: lookup.Lookup | None = None
    #: The pass's last descriptor read, which the shell probes, asks and reports.
    read: Reading = Reading()
    #: What the pass or the start waits on, and where a pause ends.
    look_wait: lookup.Effect | None = None
    until: float = 0.0
    start: spawn.Spawn | None = None
    start_wait: spawn.Effect | None = None
    child: str | None = None
    children: int = 0
    #: The running election's burial set, and each election's decision.
    buried: frozenset[str] = frozenset()
    decided: tuple[str | None, ...] = ()
    #: The proxy backend: its owner, and the owners it wrote off.
    attached: str | None = None
    written_off: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _World:
    now: float
    fronts: tuple[tuple[str, _Front], ...]
    #: F's calls.
    calls: tuple[tuple[str, _CallRun], ...] = ()
    procs: tuple[tuple[str, _Proc], ...] = ()
    lock: str | None = None
    canonical: str | None = None
    tasks: tuple[_Task, ...] = ()
    outbox: tuple[_Answer, ...] = ()
    faults: int = 1

    def front(self, name: str) -> _Front:
        return dict(self.fronts)[name]

    def proc(self, name: str) -> _Proc:
        return dict(self.procs)[name]

    def run(self, name: str) -> _CallRun:
        return dict(self.calls)[name]

    def with_front(self, name: str, front: _Front) -> _World:
        return replace(self, fronts=_put(self.fronts, name, front))

    def with_proc(self, name: str, proc: _Proc) -> _World:
        return replace(self, procs=_put(self.procs, name, proc))

    def with_run(self, name: str, run: _CallRun) -> _World:
        return replace(self, calls=_put(self.calls, name, run))

    def serving(self) -> str | None:
        """The owner a client can reach: published, and still running."""
        if self.canonical is None or not self.proc(self.canonical).alive:
            return None
        return self.canonical

    def end(self, name: str) -> _World:
        """Process *name* exits, and the kernel frees its lock."""
        proc = self.proc(name)
        world = self.with_proc(name, replace(proc, alive=False, holds_lock=False))
        return replace(world, lock=None) if proc.holds_lock else world


def _put[T](
    pairs: tuple[tuple[str, T], ...], name: str, value: T
) -> tuple[tuple[str, T], ...]:
    return tuple({**dict(pairs), name: value}.items())


# --- Elections, passes and starts -----------------------------------------------------


def _start_election(world: _World, name: str) -> _World:
    front = world.front(name)
    state, effects = election.elect(
        now=world.now,
        deadline_seconds=DEADLINE,
        settlement_seconds=SETTLEMENT,
        pacing=PACING,
        nothing_seen=None,
    )
    front = replace(front, electing=state, buried=front.written_off)
    return _election_effects(world.with_front(name, front), name, effects)


def _elected(world: _World, name: str, event: election.Event) -> _World:
    front = world.front(name)
    assert front.electing is not None
    state, effects = election.on_event(front.electing, event)
    world = world.with_front(name, replace(front, electing=state))
    return _election_effects(world, name, effects)


def _election_effects(
    world: _World, name: str, effects: list[election.Effect]
) -> _World:
    front = world.front(name)
    [effect] = effects
    match effect:
        case election.Look(may_ask=may_ask, wait=wait, deadline=deadline):
            state, out = lookup.look(
                now=world.now,
                deadline=deadline,
                wait=wait,
                may_ask=may_ask,
                buried=front.buried,
                timings=TIMINGS,
            )
            world = world.with_front(name, replace(front, pass_=state, read=Reading()))
            return _pass_effects(world, name, out)
        case election.StartOwner(timeout=timeout):
            child = f"{name}{front.children + 1}"
            state, out = spawn.on_event(
                spawn.spawned(contained=False, budgets=BUDGETS),
                spawn.Connected(world.now, timeout),
            )
            front = replace(front, child=child, children=front.children + 1)
            proc = _Proc(front.build, front.config)
            world = world.with_proc(child, proc).with_front(name, front)
            return _start_effects(world, name, state, out)
        case election.Decide(lookup=target):
            assert target is None or isinstance(target, str)
            return _decided(world, name, target)


def _decided(world: _World, name: str, target: str | None) -> _World:
    _expect(target is None or target == world.serving(), "attached to a non-owner")
    front = world.front(name)
    _expect(
        target is None or world.proc(target).config == front.config,
        "attached to another configuration",
    )
    found = target if target not in front.written_off else None
    front = replace(
        front,
        electing=None,
        decided=(*front.decided, target),
        attached=found or front.attached,
    )
    world = world.with_front(name, front)
    if name != "f":
        # Every call is F's, and only F's own election answers its recovery.
        return world
    for call, run in world.calls:
        if isinstance(run.wait, calls.Recover):
            event = calls.Recovered(run.wait.op, world.now, found is not None)
            world = _call_event(world, call, event)
    return world


def _passed(world: _World, name: str, event: lookup.Event) -> _World:
    front = world.front(name)
    assert front.pass_ is not None
    read = event.reading if isinstance(event, lookup.Read) else front.read
    state, effects = lookup.on_event(front.pass_, event)
    front = replace(front, pass_=state, look_wait=None, read=read)
    return _pass_effects(world.with_front(name, front), name, effects)


def _pass_effects(world: _World, name: str, effects: list[lookup.Effect]) -> _World:
    build = world.front(name).build
    for effect in effects:
        front = world.front(name)
        match effect:
            case lookup.ReadDescriptor() | lookup.Probe():
                world = world.with_front(name, replace(front, look_wait=effect))
            case lookup.Pause(seconds=seconds):
                until = world.now + seconds
                world = world.with_front(
                    name, replace(front, look_wait=effect, until=until)
                )
            case lookup.AskToStandDown():
                instance = front.read.instance
                assert instance is not None
                proc = world.proc(instance)
                _expect(proc.build < build, "turnover asked of an owner not older")
                if proc.alive and proc.owner is not None:
                    owner, _ = _owner_on(proc.owner, lifecycle.TurnOver())
                    proc = replace(proc, owner=owner, asked_to_go=True)
                    world = world.with_proc(instance, proc)
            case lookup.Bury(instance=instance):
                proc = world.proc(instance)
                _expect(
                    not proc.alive or proc.build < build, "a live owner written off"
                )
                buried = front.buried | {instance}
                world = world.with_front(name, replace(front, buried=buried))
            case lookup.Conclude(finding=finding, asked=asked, observed=observed):
                assert front.electing is not None
                # As the shell reports it: the last read, and an owner only if
                # that read was worth connecting to. A rival left alone ends
                # the election in Direct at once.
                read = front.read
                as_read = finding is Finding.AS_READ and read.attachable
                target = read.instance if as_read else None
                rival = finding in (Finding.LIVE_RIVAL, Finding.SILENT_RIVAL)
                world = world.with_front(name, replace(front, pass_=None))
                looked = election.Looked(
                    front.electing.pending,
                    world.now,
                    target,
                    target is not None or rival,
                    asked,
                    observed,
                )
                return _elected(world, name, looked)
    return world


def _start_effects(
    world: _World, name: str, state: spawn.Spawn, effects: list[spawn.Effect]
) -> _World:
    front = replace(world.front(name), start=state, start_wait=None)
    world = world.with_front(name, front)
    child = front.child
    assert child is not None
    for effect in effects:
        match effect:
            case spawn.SendCommit() | spawn.AwaitVerdict():
                world = world.with_front(name, replace(front, start_wait=effect))
            case _ if isinstance(effect, spawn.WAITS):
                return _start_event(*_at_once(world, child, effect), name)
            case spawn.StopChild():
                world = _stop_child(world, child)
            case spawn.Finish(started=started):
                for ending in spawn.wind_down(state):
                    match ending:
                        case spawn.StopChild():
                            world = _stop_child(world, child)
                        case spawn.HangUp() if not world.proc(child).published:
                            # End of file aborts a child that never got COMMIT.
                            world = world.end(child)
                        case spawn.DiscardPrepared(instance=instance):
                            _expect(
                                not world.proc(instance).published,
                                "a published generation discarded",
                            )
                front = replace(world.front(name), start=None, child=None)
                world = world.with_front(name, front)
                assert front.electing is not None
                event = election.StartEnded(
                    front.electing.pending, world.now, spawn.attempt_after(started)
                )
                return _elected(world, name, event)
    return world


def _at_once(
    world: _World, child: str, wait: spawn.Effect
) -> tuple[_World, spawn.Event]:
    """The answer to a step nothing outside this start can change meanwhile.

    The child takes the lock at the hand-over if it is free, and only a holder
    publishes, so whether it prepares and what the canonical read says are
    already settled when they are asked.
    """
    proc, now = world.proc(child), world.now
    match wait:
        case spawn.Greet(op=op):
            return world, spawn.Greeted(op, now, None)
        case spawn.HandOver(op=op):
            if world.lock is None:
                world = replace(world, lock=child)
                world = world.with_proc(child, replace(proc, holds_lock=True))
            return world, spawn.HandedOver(op, now)
        case spawn.AwaitPrepared(op=op) if proc.holds_lock:
            return world, spawn.Prepared(op, now, child)
        case spawn.AwaitPrepared(op=op):
            # Lost the lock: the child releases its position and exits.
            return world.end(child), spawn.Prepared(op, now, Started.RETRY)
        case spawn.Validate(op=op):
            return world, spawn.Validated(op, now)
        case spawn.SettleCommit(op=op, instance=instance):
            seen = Started.YES if world.canonical == instance else Started.UNCERTAIN
            return world, spawn.Settled(op, now, seen)
    raise AssertionError(wait)


def _start_event(world: _World, event: spawn.Event, name: str) -> _World:
    front = world.front(name)
    assert front.start is not None
    state, effects = spawn.on_event(front.start, event)
    return _start_effects(world, name, state, effects)


def _stop_child(world: _World, child: str) -> _World:
    _expect(not world.proc(child).published, "a published owner stopped")
    return world.end(child)


def _commit(world: _World, name: str, event: spawn.Event, arrives: bool) -> _World:
    """COMMIT goes to the start's child, and publishes it if it arrives."""
    front = world.front(name)
    child = front.child
    assert isinstance(front.start_wait, spawn.SendCommit) and child is not None
    to_holder = world.proc(child).holds_lock and front.start_wait.instance == child
    _expect(to_holder, "COMMIT to a non-holder")
    return _start_event(_publish(world, child) if arrives else world, event, name)


def _publish(world: _World, child: str) -> _World:
    """The descriptor becomes canonical; the owner hears it on a later move."""
    owner, _ = _owner_on(lifecycle.Owner(), lifecycle.Named(child))
    proc = replace(world.proc(child), published=True, owner=owner)
    return replace(world.with_proc(child, proc), canonical=child)


def _go_live(world: _World, name: str) -> _World:
    proc = world.proc(name)
    assert proc.owner is not None
    owner, _ = _owner_on(proc.owner, lifecycle.Published(world.now))
    return world.with_proc(name, replace(proc, owner=owner, live=True))


# --- Calls and owners -----------------------------------------------------------------


def _call_event(world: _World, name: str, event: calls.Event) -> _World:
    run = world.run(name)
    state, effects = calls.on_event(run.call, event)
    world = world.with_run(name, replace(run, call=state, wait=None))
    for effect in effects:
        world = _call_effect(world, name, effect)
    return world


def _call_effect(world: _World, name: str, effect: calls.Effect) -> _World:
    f, run = world.front("f"), world.run(name)
    match effect:
        case calls.Bury(failure=(str() as owner, str() as why)):
            if why == RETIRING:
                f = replace(f, written_off=f.written_off | {owner})
            return world.with_front("f", f)
        case calls.Attempt():
            run = replace(run, wait=effect, bound=f.attached, ticket=None)
            return _bind(world.with_run(name, run), name)
        case calls.Recover(op=op, owner=owner):
            if f.attached != owner and f.attached not in f.written_off:
                # Somebody already replaced it: the backend's identity check.
                return _call_event(world, name, calls.Recovered(op, world.now, True))
            world = world.with_run(name, replace(run, wait=effect))
            return world if f.electing else _start_election(world, "f")
        case calls.Classify(op=op):
            could_change = MUTATING[name]
            run = replace(run, read_only=run.read_only or not could_change)
            event = calls.Classified(op, world.now, could_change)
            return _call_event(world.with_run(name, run), name, event)
        case calls.ReportLost(expired=expired) if run.began:
            _expect(run.read_only and not expired, "told not sent after it began")
    assert isinstance(effect, calls.TERMINAL), effect
    return world.with_run(name, replace(run, told=effect))


def _bind(world: _World, name: str) -> _World:
    """Refuse the pending attempt if its owner is written off, as every hop asks."""
    run = world.run(name)
    assert isinstance(run.wait, calls.Attempt) and run.bound is not None
    written_off = dict.fromkeys(world.front("f").written_off, RETIRING)
    why = calls.binding_refusal(run.bound, written_off=written_off, sent=False)
    if why is None:
        return world
    lost = calls.OwnerLost(run.wait.op, world.now, run.bound, True, (run.bound, why))
    return _call_event(world, name, lost)


def _deliver(world: _World, name: str) -> _World:
    """The pending attempt reaches the owner it is bound to."""
    before = world.run(name)
    world = _bind(world, name)
    run = world.run(name)
    if run.wait != before.wait:
        return world
    assert isinstance(run.wait, calls.Attempt) and run.bound is not None
    op, owner = run.wait.op, run.bound
    proc = world.proc(owner)
    if not proc.alive or proc.owner is None:
        lost = calls.OwnerLost(op, world.now, owner, True, (owner, UNREACHABLE))
        return _call_event(world, name, lost)
    state, [effect] = _owner_on(proc.owner, lifecycle.Admit(name, world.now))
    world = world.with_proc(owner, replace(proc, owner=state))
    if isinstance(effect, lifecycle.Refused):
        lost = calls.OwnerLost(op, world.now, owner, True, (owner, RETIRING))
        return _call_event(world, name, lost)
    assert isinstance(effect, lifecycle.StartTask)
    _expect(proc.live, "admitted before publication")
    _expect(proc.holds_lock, "admitted without the lock")
    _expect(not proc.asked_to_go, "admitted after retirement")
    _expect(not (MUTATING[name] and run.exposed), "a mutation run again")
    task = _Task(owner, effect.ticket, name, op)
    world = replace(world, tasks=(*world.tasks, task))
    return world.with_run(name, replace(run, ticket=effect.ticket, exposed=True))


def _task_event(world: _World, task: _Task, event: lifecycle.Event) -> _World:
    proc = world.proc(task.owner)
    assert proc.owner is not None
    owner, _ = _owner_on(proc.owner, event)
    return world.with_proc(task.owner, replace(proc, owner=owner))


def _with_task(world: _World, task: _Task, after: _Task) -> _World:
    return replace(world, tasks=tuple(after if t == task else t for t in world.tasks))


def _begin(world: _World, task: _Task) -> _World:
    owner = world.proc(task.owner).owner
    assert owner is not None
    # The serializing middleware's last check before the body.
    if lifecycle.abandoned(owner, task.call, world.now):
        return world
    world = _task_event(world, task, lifecycle.BodyBegan(task.call))
    world = _with_task(world, task, replace(task, began=True))
    return world.with_run(task.call, replace(world.run(task.call), began=True))


def _finish(world: _World, task: _Task, says: str) -> _World:
    world = _task_event(world, task, lifecycle.Finished(task.ticket, world.now))
    world = _with_task(world, task, replace(task, done=True))
    answer = _Answer(task.owner, task.call, task.op, says)
    return replace(world, outbox=(*world.outbox, answer))


def _turn(world: _World, name: str) -> _World:
    """One turn of *name*'s lifecycle loop, which leaves it at an ending.

    The loop ends at either ending. Only ``Stop`` stops the endpoint, and here
    the endpoint never stops on its own, so after ``EndpointGone`` it waits for
    good, as the shell would.
    """
    proc = world.proc(name)
    assert proc.owner is not None
    owner, effects = _owner_on(proc.owner, lifecycle.Tick(world.now))
    world = world.with_proc(name, replace(proc, owner=owner))
    for effect in effects:
        if isinstance(effect, lifecycle.TERMINAL):
            world = world.with_proc(name, replace(world.proc(name), ended_by=effect))
        elif isinstance(effect, lifecycle.Cancel):
            running = [
                t
                for t in world.tasks
                if (t.owner, t.ticket) == (name, effect.ticket) and not t.done
            ]
            _expect(len(running) == 1, "cancelled a task that is not running")
            # Asked as the task unwinds, before its ticket is released.
            stopped = lifecycle.stopped(owner, effect.ticket)
            assert stopped is not None
            if stopped is lifecycle.Stopped.NOT_RUN:
                _expect(not running[0].began, "said nothing ran after it began")
            world = _finish(world, running[0], stopped.value)
    return world


def _exit(world: _World, name: str) -> _World:
    """The owner exits; every call still on it loses its connection."""
    world = world.end(name)
    for call, run in world.calls:
        if isinstance(run.wait, calls.Attempt) and run.bound == name and run.ticket:
            failure = (name, UNREACHABLE)
            lost = calls.OwnerLost(run.wait.op, world.now, name, False, failure)
            world = _call_event(world, call, lost)
    return world


def _hear(world: _World, answer: _Answer) -> _World:
    world = replace(world, outbox=tuple(a for a in world.outbox if a != answer))
    if answer.says != lifecycle.Stopped.NOT_RUN.value:
        result = f"{answer.says} by {answer.owner}"
        return _call_event(
            world, answer.call, calls.Answered(answer.op, world.now, result)
        )
    # The owner's signed word that nothing ran: that admission did not act.
    world = world.with_run(answer.call, replace(world.run(answer.call), exposed=False))
    failure = (answer.owner, RETIRING)
    lost = calls.OwnerLost(answer.op, world.now, answer.owner, True, failure)
    return _call_event(world, answer.call, lost)


# --- The world's moves ----------------------------------------------------------------

_Moves = Iterator[tuple[str, Callable[[], _World]]]


def _pass_answers(world: _World, name: str) -> _Moves:
    front = world.front(name)
    match front.look_wait:
        case lookup.ReadDescriptor(op=op):
            reading = Reading()
            if world.canonical is not None:
                proc = world.proc(world.canonical)
                # Another configuration is proved for control only.
                other = proc.config != front.config
                reading = Reading(
                    instance=world.canonical,
                    attachable=not other,
                    control_only=other,
                    other_config=other,
                    older=proc.build < front.build,
                    same_version=proc.build == front.build,
                )
            event = lookup.Read(op, world.now, reading)
            yield (
                f"reads {reading.instance}",
                partial(_passed, world, name, event),
            )
        case lookup.Pause(op=op) if world.now >= front.until:
            event = lookup.Paused(op, world.now)
            yield "pauses", partial(_passed, world, name, event)
        case lookup.Probe(op=op):
            target = front.read.instance
            assert target is not None
            alive = world.proc(target).alive
            reach = Reach.ANSWERED if alive else Reach.REFUSED
            event = lookup.Probed(op, world.now, reach)
            yield f"probes {target}", partial(_passed, world, name, event)
            if alive and world.faults:
                silent = lookup.Probed(op, world.now, Reach.SILENT)
                yield (
                    f"probes {target}, silent",
                    partial(_passed, _spend(world), name, silent),
                )


def _child_answers(world: _World, name: str) -> _Moves:
    front = world.front(name)
    child = front.child
    assert child is not None
    proc, now = world.proc(child), world.now
    match front.start_wait:
        case spawn.SendCommit(op=op):
            sent, undelivered = (
                spawn.CommitSent(op, now),
                spawn.CommitUndelivered(op, now),
            )
            yield f"{child} committed", partial(_commit, world, name, sent, True)
            if world.faults:
                yield (
                    f"{child} committed, unreported",
                    partial(_commit, _spend(world), name, undelivered, True),
                )
                yield (
                    f"{child} never got COMMIT",
                    partial(_commit, _spend(world), name, undelivered, False),
                )
        case spawn.AwaitVerdict(op=op):
            lost = spawn.Verdict(op, now, Started.STILL_TRYING)
            if not proc.alive:
                yield f"{child} gone", partial(_start_event, world, lost, name)
                return
            # COMMIT reached this child, and only COMMIT publishes.
            yes = spawn.Verdict(op, now, Started.YES)
            yield f"{child} says yes", partial(_start_event, world, yes, name)
            if world.faults:
                yield (
                    f"{child} verdict lost",
                    partial(_start_event, _spend(world), lost, name),
                )


def _spend(world: _World) -> _World:
    return replace(world, faults=world.faults - 1)


def _urgent(world: _World) -> _Moves:
    for name, front in world.fronts:
        if front.look_wait is not None:
            for label, move in _pass_answers(world, name):
                yield f"{name} {label}", move
        if front.start_wait is not None:
            for label, move in _child_answers(world, name):
                yield f"{name} {label}", move
    for call, run in world.calls:
        if not isinstance(run.wait, calls.Attempt | calls.Recover):
            continue
        if world.now >= run.call.deadline:
            left = run.ticket is not None
            event = calls.Expired(run.wait.op, world.now, request_left=left)
            yield f"{call} expires", partial(_call_event, world, call, event)
        if isinstance(run.wait, calls.Attempt) and run.ticket is None:
            yield f"{call} reaches {run.bound}", partial(_deliver, world, call)
    for name, proc in world.procs:
        owner = proc.owner
        if not proc.alive or owner is None:
            continue
        if isinstance(proc.ended_by, lifecycle.Stop):
            yield f"{name} exits", partial(_exit, world, name)
        if proc.ended_by is not None:
            continue
        if proc.published and not proc.live:
            yield f"{name} goes live", partial(_go_live, world, name)
        if (owner.turnover and owner.phase is lifecycle.Phase.SERVING) or (
            owner.phase is lifecycle.Phase.DRAINING
            and (not owner.calls or world.now >= (owner.drain_until or 0.0))
        ):
            yield f"{name} turns", partial(_turn, world, name)
    for task in world.tasks:
        queued = not task.began and not task.done
        if queued and world.proc(task.owner).alive:
            # One body at a time per owner.
            if not any(
                t.began and not t.done for t in world.tasks if t.owner == task.owner
            ):
                yield (
                    f"{task.call} begins on {task.owner}",
                    partial(_begin, world, task),
                )
    for answer in world.outbox:
        yield f"{answer.call} hears {answer.says}", partial(_hear, world, answer)


def _moves(world: _World) -> list[tuple[str, Callable[[], _World]]]:
    """Every move the world can make next, the clock's tick included."""
    urgent = list(_urgent(world))
    lazy: list[tuple[str, Callable[[], _World]]] = [
        (f"{task.call} ends on {task.owner}", partial(_finish, world, task, "done"))
        for task in world.tasks
        if task.began and not task.done and world.proc(task.owner).alive
    ]
    working = (
        any(f.electing for _, f in world.fronts)
        or any(run.told is None for _, run in world.calls)
        or any(p.alive and p.asked_to_go for _, p in world.procs)
    )
    # The last election can begin at a call's deadline and must end inside its
    # own budgets, so a world still working past that never will.
    horizon = CALL_BUDGET + DEADLINE + SETTLEMENT
    if working and not urgent and world.now < horizon:
        lazy.append(("tick", partial(_later, world)))
    return urgent + lazy


def _later(world: _World) -> _World:
    return replace(world, now=world.now + 1.0)


def _tidy(world: _World) -> _World:
    """Drop answers nobody can read any more, so equal worlds compare equal."""
    readable = tuple(
        a
        for a in world.outbox
        if world.proc(a.owner).alive
        and isinstance(wait := world.run(a.call).wait, calls.Attempt)
        and wait.op == a.op
    )
    return world if readable == world.outbox else replace(world, outbox=readable)


def _initial(resident: bool, g_build: int, g_config: int) -> _World:
    g = _Front(build=g_build, config=g_config)
    world = _World(0.0, (("f", _Front(build=1)), ("g", g)))
    if not resident:
        return _start_election(_start_election(world, "f"), "g")
    owner = lifecycle.Owner()
    for event in (
        lifecycle.Named(R),
        lifecycle.Published(0.0),
        lifecycle.Admit(C1, 0.0),
        lifecycle.BodyBegan(C1),
    ):
        owner, _ = _owner_on(owner, event)
    runs = []
    for name in (C1, C2):
        state, [attempt] = calls.arrive(now=0.0, budget=CALL_BUDGET, sign_in_share=0.5)
        run = _CallRun(state, attempt, bound=R)
        if name == C1:
            run = replace(run, ticket=1, began=True, exposed=True)
        runs.append((name, run))
    world = replace(
        world.with_front("f", _Front(build=1, attached=R)),
        calls=tuple(runs),
        procs=((R, _Proc(1, holds_lock=True, published=True, live=True, owner=owner)),),
        lock=R,
        canonical=R,
        tasks=(_Task(R, 1, C1, op=1, began=True),),
    )
    return _start_election(world, "g")


@dataclass
class _Search:
    states: int = 0
    ended: list[_World] = field(default_factory=list)
    #: The first counterexample found for each broken rule, as its moves.
    broken: dict[str, list[str]] = field(default_factory=dict)


def _explore(
    resident: bool,
    *,
    g_build: int = 2,
    g_config: int = 1,
    stop_at: str | None = None,
) -> _Search:
    """Visit every reachable world once, depth first, judging every move.

    Time only moves forward, so a move back to a world still on the path is
    work that never ends without the clock ever ticking.
    """
    start = _initial(resident, g_build, g_config)
    came_from: dict[_World, tuple[_World, str] | None] = {start: None}
    search = _Search()

    def trace(world: _World, last: str) -> list[str]:
        moves, step = [last], came_from[world]
        while step is not None:
            world, label = step
            moves.insert(0, label)
            step = came_from[world]
        return moves

    def enter(world: _World) -> Iterator[tuple[str, Callable[[], _World]]]:
        moves = _moves(world)
        if not moves:
            search.ended.append(world)
            for name, front in world.fronts:
                if front.electing is not None:
                    search.broken.setdefault(
                        "an election never ended", trace(world, name)
                    )
            for call, run in world.calls:
                if run.told is None:
                    search.broken.setdefault("a call left waiting", trace(world, call))
            for name, proc in world.procs:
                if proc.alive and proc.asked_to_go:
                    search.broken.setdefault(
                        "an owner asked to go never exited", trace(world, name)
                    )
        return iter(moves)

    stack = [(start, enter(start))]
    on_path = {start}
    while stack and stop_at not in search.broken:
        world, moves = stack[-1]
        step = next(moves, None)
        if step is None:
            stack.pop()
            on_path.discard(world)
            continue
        label, move = step
        try:
            after = _tidy(move())
            # Environment: one lock holder, and one live published owner.
            _expect(sum(p.holds_lock for _, p in after.procs) <= 1, "two lock holders")
            published = sum(p.published and p.alive for _, p in after.procs)
            _expect(published <= 1, "two published owners")
            _expect(after not in on_path, "work that never ends")
        except _Broken as broken:
            search.broken.setdefault(broken.rule, trace(world, label))
            continue
        if after not in came_from:
            if len(came_from) >= MAX_STATES:
                pytest.fail(f"incomplete: more than {MAX_STATES} states")
            came_from[after] = (world, label)
            on_path.add(after)
            stack.append((after, enter(after)))
    search.states = len(came_from)
    return search


@pytest.fixture
def owner_times(monkeypatch: pytest.MonkeyPatch) -> None:
    # Heartbeats always arrive: nothing expires and the owner never stalls.
    monkeypatch.setattr(lifecycle, "EXPIRY_SECONDS", 1e9)
    monkeypatch.setattr(lifecycle, "STALL_SECONDS", 1e9)
    monkeypatch.setattr(lifecycle, "TURNOVER_DRAIN_SECONDS", DRAIN)


def _shared_successor(world: _World) -> bool:
    """Both frontends ended on one successor, which answered c2 if there is one."""
    successor = world.serving()
    if successor in (None, R) or world.front("f").attached != successor:
        return False
    if world.front("g").decided[-1:] != (successor,):
        return False
    told = dict(world.calls)[C2].told if world.calls else None
    return not world.calls or told == calls.Return(
        f"done by {successor}", calls.Because.ANSWERED
    )


def _upgraded_apart(world: _World) -> bool:
    """G ended on its own successor after an older owner of F's configuration
    was asked to go, and F is not on that successor."""
    g = world.front("g")
    successor = world.serving()
    if successor is None or g.decided[-1:] != (successor,):
        return False
    turned_over = any(p.asked_to_go and p.config != g.config for _, p in world.procs)
    return turned_over and world.front("f").attached != successor


def _left_alone(world: _World) -> bool:
    """One frontend ended on its successor, and the other, of another
    configuration, ended without it."""
    successor = world.serving()
    if successor is None:
        return False
    on, off = ("f", "g") if successor.startswith("f") else ("g", "f")
    return (
        world.front(on).decided[-1:] == (successor,)
        and world.front(off).decided[-1:] == (None,)
        and world.front(off).config != world.proc(successor).config
    )


@pytest.mark.usefixtures("owner_times")
@pytest.mark.parametrize(
    ("resident", "g_build", "g_config", "floor", "reached"),
    [
        (True, 2, 1, 20_000, _shared_successor),
        (True, 2, 2, 20_000, _upgraded_apart),
        (False, 2, 1, 2_000, _shared_successor),
        (False, 2, 2, 2_000, _upgraded_apart),
        (False, 1, 1, 300, _shared_successor),
        (False, 1, 2, 300, _left_alone),
    ],
    ids=[
        "turnover",
        "turnover-other-config",
        "fresh",
        "fresh-other-config",
        "fresh-same-version",
        "fresh-rival",
    ],
)
def test_every_composed_schedule_keeps_the_contract(
    resident: bool,
    g_build: int,
    g_config: int,
    floor: int,
    reached: Callable[[_World], bool],
):
    search = _explore(resident, g_build=g_build, g_config=g_config)

    assert search.broken == {}
    # Sanity, not contract: the bounds reach the case they exist for.
    assert search.states > floor
    assert any(reached(world) for world in search.ended)


# --- Broken reducers ------------------------------------------------------------------
#
# Each breaks one decision the way a plausible regression would, and the search
# must find the schedule that shows it. Patched on the module, so every caller
# inside the core sees the break too.


class _Break(Protocol):
    __name__: str

    def __call__(self, monkeypatch: pytest.MonkeyPatch, /) -> None: ...


def _stop_after_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence after COMMIT read as a failure, and the child stopped for it."""

    def settle(
        state: spawn.Spawn, now: float
    ) -> tuple[spawn.Spawn, list[spawn.Effect]]:
        done = replace(state, step=spawn.Step.DONE, stopped=True)
        return done, [spawn.StopChild(), spawn.Finish(Started.NO)]

    monkeypatch.setattr(spawn, "_settle", settle)


def _discard_after_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wind-down discards the prepared state whether or not COMMIT went."""
    real = spawn.wind_down

    def wind_down(state: spawn.Spawn) -> list[spawn.Effect]:
        effects = real(state)
        if state.prepared is not None and state.commit_attempted:
            effects.append(spawn.DiscardPrepared(state.prepared))
        return effects

    monkeypatch.setattr(spawn, "wind_down", wind_down)


def _bury_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe that went unanswered treated as refused."""
    real = lookup._probed

    def probed(
        state: lookup.Lookup, reach: Reach
    ) -> tuple[lookup.Lookup, list[lookup.Effect]]:
        return real(state, Reach.REFUSED if reach is Reach.SILENT else reach)

    monkeypatch.setattr(lookup, "_probed", probed)


def _attach_unprobed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A published descriptor attached to without asking its endpoint."""

    def probe(
        state: lookup.Lookup, reading: Reading, now: float
    ) -> tuple[lookup.Lookup, list[lookup.Effect]]:
        return lookup._conclude(replace(state, probing=reading), Finding.AS_READ)

    monkeypatch.setattr(lookup, "_probe", probe)


def _everyone_older(monkeypatch: pytest.MonkeyPatch) -> None:
    """The version comparison reads every owner as older."""
    real = lookup._decide

    def decide(
        state: lookup.Lookup, reading: Reading, now: float
    ) -> tuple[lookup.Lookup, list[lookup.Effect]]:
        return real(state, replace(reading, older=True), now)

    monkeypatch.setattr(lookup, "_decide", decide)


def _read_buried_as_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A written-off descriptor reported as read when the pass's wait runs out."""
    real = lookup._conclude

    def conclude(
        state: lookup.Lookup,
        finding: Finding,
        *,
        asked: bool = False,
        observed: bool = True,
        first: list[lookup.Effect] | None = None,
    ) -> tuple[lookup.Lookup, list[lookup.Effect]]:
        if finding is Finding.ALREADY_UNUSABLE:
            finding = Finding.AS_READ
        return real(state, finding, asked=asked, observed=observed, first=first)

    monkeypatch.setattr(lookup, "_conclude", conclude)


def _admit_while_retiring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Admission that no longer looks at retirement."""
    real = lifecycle._admit

    def admit(
        owner: lifecycle.Owner, event: lifecycle.Admit
    ) -> tuple[lifecycle.Owner, list[lifecycle.Effect]]:
        after, effects = real(replace(owner, retirement=None), event)
        return replace(after, retirement=owner.retirement), effects

    monkeypatch.setattr(lifecycle, "_admit", admit)


def _admit_before_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """Admission that no longer waits for the endpoint to be published."""
    real = lifecycle._admit

    def admit(
        owner: lifecycle.Owner, event: lifecycle.Admit
    ) -> tuple[lifecycle.Owner, list[lifecycle.Effect]]:
        after, effects = real(replace(owner, published=True), event)
        return replace(after, published=owner.published), effects

    monkeypatch.setattr(lifecycle, "_admit", admit)


def _drain_without_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """A drain that waits for its calls however long they take."""
    real = lifecycle._drain

    def drain(
        owner: lifecycle.Owner, tick: lifecycle.Tick
    ) -> tuple[lifecycle.Owner, list[lifecycle.Effect]]:
        if owner.calls and tick.wedged is None:
            return lifecycle._scan(owner, tick.now)
        return real(owner, tick)

    monkeypatch.setattr(lifecycle, "_drain", drain)


def _stop_as_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The drain's end reported as an endpoint that stopped on its own."""
    real = lifecycle._stop

    def stop(
        owner: lifecycle.Owner, effects: list[lifecycle.Effect], ending: lifecycle.Stop
    ) -> tuple[lifecycle.Owner, list[lifecycle.Effect]]:
        after, out = real(owner, effects, ending)
        return after, [*out[:-1], lifecycle.EndpointGone()]

    monkeypatch.setattr(lifecycle, "_stop", stop)


def _cut_as_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A call cut by the drain answered as never run, wherever it was."""
    real = lifecycle.stopped

    def stopped(owner: lifecycle.Owner, ticket: int | None) -> lifecycle.Stopped | None:
        said = real(owner, ticket)
        return lifecycle.Stopped.NOT_RUN if said is lifecycle.Stopped.UNKNOWN else said

    monkeypatch.setattr(lifecycle, "stopped", stopped)


def _replay_a_possible_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry after a possible send that forgets what the tool could change."""
    real = calls._after_first

    def after_first(
        call: calls.Call, now: float
    ) -> tuple[calls.Call, list[calls.Effect]]:
        return real(replace(call, could_change=False), now)

    monkeypatch.setattr(calls, "_after_first", after_first)


def _expire_as_unsent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An expiry that forgets the request had already left."""
    real = calls._expire

    def expire(
        call: calls.Call, *, request_left: bool = False
    ) -> tuple[calls.Call, list[calls.Effect]]:
        return real(call)

    monkeypatch.setattr(calls, "_expire", expire)


#: Each broken reducer, and the rule its counterexample breaks.
_BREAKS: list[tuple[_Break, str]] = [
    (_stop_after_commit, "a published owner stopped"),
    (_discard_after_commit, "a published generation discarded"),
    (_bury_silence, "a live owner written off"),
    (_attach_unprobed, "attached to a non-owner"),
    (_everyone_older, "turnover asked of an owner not older"),
    (_read_buried_as_usable, "attached to a non-owner"),
    (_admit_while_retiring, "admitted after retirement"),
    (_admit_before_published, "admitted before publication"),
    (_drain_without_cutoff, "work that never ends"),
    (_stop_as_gone, "an owner asked to go never exited"),
    (_cut_as_not_run, "said nothing ran after it began"),
    (_replay_a_possible_send, "a mutation run again"),
    (_expire_as_unsent, "told not sent after it began"),
]


@pytest.mark.usefixtures("owner_times")
@pytest.mark.parametrize("g_config", [1, 2], ids=["same-config", "other-config"])
@pytest.mark.parametrize(
    ("break_it", "rule"), _BREAKS, ids=[b.__name__.lstrip("_") for b, _ in _BREAKS]
)
def test_a_broken_reducer_yields_its_counterexample(
    monkeypatch: pytest.MonkeyPatch, break_it: _Break, rule: str, g_config: int
):
    break_it(monkeypatch)

    search = _explore(resident=True, g_config=g_config, stop_at=rule)

    assert rule in search.broken, sorted(search.broken)
