"""The owner's admission and lifecycle decisions, driven by events alone.

Nothing here starts a task, reads a clock or opens a socket: every test feeds
``daemon.core.owner.on_event`` and reads what it decided. The schedule tests at
the end enumerate interleavings of admission, completion, heartbeat expiry,
stand-down, cutoff and failed task start, and check rules written from the
default-on contract rather than from the reducer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

import pytest

from linkedin_mcp_server.daemon.core import owner as lifecycle
from linkedin_mcp_server.daemon.core.owner import (
    EXPIRY_SECONDS,
    SETUP_FAILURE_RETRY_GRACE_SECONDS,
    STALL_SECONDS,
    TURNOVER_DRAIN_SECONDS,
    Admit,
    BackgroundFinished,
    BodyBegan,
    Cancel,
    CutOff,
    Drain,
    Effect,
    EndpointGone,
    Event,
    Fate,
    Finished,
    Forgave,
    Heard,
    Named,
    Owner,
    Published,
    Refused,
    Retire,
    RetireIfIdle,
    Retirement,
    StartFailed,
    StartTask,
    Stop,
    Stopped,
    Tick,
    TurnOver,
)

A, B = "v1." + "a" * 32, "v1." + "b" * 32


def run(owner: Owner, *events: Event) -> tuple[Owner, list[Effect]]:
    """*owner* after every one of *events*, and all they decided, in order."""
    decided: list[Effect] = []
    for event in events:
        owner, effects = lifecycle.on_event(owner, event)
        decided.extend(effects)
    return owner, decided


def admitted(owner: Owner, call_id: str, now: float) -> tuple[Owner, int]:
    """*owner* with *call_id* admitted at *now*, and the ticket it was given."""
    owner, effects = lifecycle.on_event(owner, Admit(call_id, now))
    assert len(effects) == 1 and isinstance(effects[0], StartTask), effects
    return owner, effects[0].ticket


def live(now: float = 0.0) -> Owner:
    """An owner whose endpoint went live at *now*, with nothing admitted."""
    return run(Owner(), Published(now))[0]


class TestAdmission:
    def test_a_call_is_counted_before_its_task_exists(self):
        owner, effects = run(live(), Admit(A, 1.0))

        assert effects == [StartTask(1)]
        assert lifecycle.busy(owner)
        assert lifecycle.watching(owner, A)
        assert lifecycle.quiet_for(owner, 5.0) is None

    def test_a_retiring_owner_refuses_and_changes_nothing(self):
        owner = run(live(), Retire(Retirement.WEDGED))[0]

        after, effects = run(owner, Admit(A, 1.0))

        assert effects == [Refused()]
        assert after == owner

    def test_a_failed_start_releases_the_reservation(self):
        owner, ticket = admitted(live(), A, 1.0)

        owner, effects = run(owner, StartFailed(ticket, 2.0))

        assert effects == []
        assert not lifecycle.busy(owner)
        assert not lifecycle.watching(owner, A)
        assert lifecycle.quiet_for(owner, 3.0) == pytest.approx(1.0)

    def test_a_stale_completion_changes_nothing(self):
        owner, first = admitted(live(), A, 1.0)
        idle = run(owner, Finished(first, 2.0))[0]

        # The first call's completion again, and a start failure for it: both
        # answer an admission that is already over. Neither restarts the quiet
        # period, nor releases the call admitted since.
        stale = Finished(first, 4.0), StartFailed(first, 4.0)
        assert run(idle, *stale)[0] == idle
        assert lifecycle.quiet_for(idle, 5.0) == pytest.approx(3.0)

        busy, second = admitted(idle, B, 3.0)
        after = run(busy, *stale)[0]

        assert after == busy
        assert lifecycle.watching(after, B)
        assert second != first

    def test_retirement_keeps_its_first_reason(self):
        owner = run(
            live(),
            TurnOver(),
            Retire(Retirement.IDLE),
            RetireIfIdle(Retirement.PROFILE_COMMAND),
        )[0]

        assert owner.retirement is Retirement.TURNOVER

    @pytest.mark.parametrize("background", [False, True])
    def test_retiring_if_idle_refuses_while_anything_is_busy(self, background: bool):
        owner = live()
        if not background:
            owner = admitted(owner, A, 1.0)[0]

        after = run(owner, RetireIfIdle(Retirement.IDLE, background=background))[0]

        assert after.retirement is None
        assert run(after, Admit(B, 2.0))[1] == [StartTask(after.next_ticket)]


class TestPublication:
    """Nothing is admitted before the endpoint is published, nor after retiring."""

    def test_a_call_before_publication_is_refused_and_changes_nothing(self):
        owner = run(Owner(), Named("the-owner"))[0]

        after, effects = run(owner, Admit(A, 1.0))

        assert effects == [Refused()]
        assert after == owner

    def test_publication_opens_admission(self):
        owner, effects = run(Owner(), Admit(A, 0.5), Published(1.0), Admit(B, 2.0))

        assert effects == [Refused(), StartTask(1)]
        assert lifecycle.watching(owner, B)
        assert not lifecycle.watching(owner, A)

    def test_an_owner_that_retired_unpublished_never_admits(self):
        # An ambiguous commit retires before the endpoint is ever published.
        effects = run(
            Owner(),
            Retire(Retirement.UNCERTAIN_PUBLICATION),
            Published(1.0),
            Admit(A, 2.0),
        )[1]

        assert effects == [Refused()]

    @pytest.mark.parametrize(
        "events, admits",
        [
            ((), False),
            ((Published(0.0),), True),
            ((Retire(Retirement.UNCERTAIN_PUBLICATION),), False),
            ((Published(0.0), Retire(Retirement.WEDGED)), False),
            ((Published(0.0), TurnOver()), False),
            ((Published(0.0), RetireIfIdle(Retirement.PROFILE_COMMAND)), False),
        ],
        ids=[
            "unpublished",
            "published",
            "retired unpublished",
            "retired",
            "turning over",
            "retired for a profile command",
        ],
    )
    def test_the_preflight_answers_as_admission_would(
        self, events: tuple[Event, ...], admits: bool
    ):
        owner = run(Owner(), *events)[0]

        decided = run(owner, Admit(A, 1.0))[1]

        assert lifecycle.admitting(owner) is admits
        assert (decided == [StartTask(owner.next_ticket)]) is admits


class TestHeartbeats:
    def test_a_beat_keeps_a_call_and_silence_expires_it(self):
        owner, heard = admitted(live(), A, 0.0)
        owner, unheard = admitted(owner, B, 0.0)
        owner = run(owner, Tick(0.5))[0]

        # Ticks every half second, so the owner was running throughout.
        times = [0.5 * step for step in range(2, int(2 * EXPIRY_SECONDS) + 4)]
        effects: list[Effect] = []
        for now in times:
            owner, decided = run(owner, Heard(A, now), Tick(now))
            effects.extend(decided)

        assert effects == [Cancel(unheard, B, Fate.ABANDONED)]
        assert lifecycle.watching(owner, A)
        assert lifecycle.stopped(owner, unheard) is Stopped.ABANDONED
        assert lifecycle.stopped(owner, heard) is None

    def test_a_beat_for_a_call_written_off_does_not_revive_it(self):
        owner, ticket = admitted(live(), A, 0.0)
        owner = run(owner, Tick(EXPIRY_SECONDS))[0]
        owner = run(owner, Tick(EXPIRY_SECONDS + 0.5))[0]
        assert lifecycle.stopped(owner, ticket) is Stopped.ABANDONED

        owner = run(owner, Heard(A, EXPIRY_SECONDS + 0.6))[0]

        assert not lifecycle.watching(owner, A)
        assert lifecycle.abandoned(owner, A, EXPIRY_SECONDS + 0.6)

    def test_a_beat_for_an_unknown_call_adds_nothing(self):
        owner = live()
        assert run(owner, Heard(A, 1.0))[0] == owner

    def test_an_owner_that_was_not_running_writes_nobody_off(self):
        owner, _ticket = admitted(live(), A, 0.0)
        owner = run(owner, Tick(0.1))[0]
        late = 0.1 + EXPIRY_SECONDS + 5

        # The query agrees with the tick's scan before the tick has run.
        assert not lifecycle.abandoned(owner, A, late)
        owner, effects = run(owner, Tick(late))

        [forgave] = effects
        assert isinstance(forgave, Forgave)
        assert forgave.seconds == pytest.approx(late - 0.1)
        assert lifecycle.watching(owner, A)

    def test_the_stall_buys_one_expiry_not_immunity(self):
        owner, ticket = admitted(live(), A, 0.0)
        owner = run(owner, Tick(0.1), Tick(30.0))[0]

        now = 30.0
        effects: list[Effect] = []
        while now < 30.0 + EXPIRY_SECONDS + 1:
            now += STALL_SECONDS / 2
            owner, decided = run(owner, Tick(now))
            effects.extend(decided)

        assert effects == [Cancel(ticket, A, Fate.ABANDONED)]


class TestIdleExit:
    def test_an_owner_quiet_for_the_timeout_stops(self):
        owner, effects = run(live(0.0), Tick(10.0, idle_timeout=5.0))

        assert effects == [Stop(Retirement.IDLE, quiet=10.0)]
        assert owner.retirement is Retirement.IDLE
        assert run(owner, Admit(A, 11.0))[1] == [Refused()]

    @pytest.mark.parametrize(
        "owner, tick",
        [
            (Owner(), Tick(1000.0, idle_timeout=5.0)),
            (live(0.0), Tick(4.0, idle_timeout=5.0)),
            (live(0.0), Tick(1000.0, idle_timeout=0.0)),
            (admitted(live(0.0), A, 0.0)[0], Tick(1000.0, idle_timeout=5.0)),
            (live(0.0), Tick(1000.0, idle_timeout=5.0, setup_running=True)),
            (
                live(0.0),
                Tick(
                    SETUP_FAILURE_RETRY_GRACE_SECONDS - 1,
                    idle_timeout=5.0,
                    setup_failed=True,
                ),
            ),
        ],
        ids=[
            "not yet published",
            "not quiet long enough",
            "a zero timeout",
            "a call admitted",
            "a setup running",
            "a setup failure waiting for its retry",
        ],
    )
    def test_an_owner_with_a_reason_to_stay_stays(self, owner: Owner, tick: Tick):
        after, effects = run(owner, tick)

        assert not any(isinstance(effect, Stop) for effect in effects)
        assert after.retirement is None

    def test_a_setup_failure_holds_the_owner_only_for_the_grace(self):
        effects = run(
            live(0.0),
            Tick(
                SETUP_FAILURE_RETRY_GRACE_SECONDS, idle_timeout=5.0, setup_failed=True
            ),
        )[1]

        assert effects == [
            Stop(Retirement.IDLE, quiet=SETUP_FAILURE_RETRY_GRACE_SECONDS)
        ]

    def test_the_quiet_period_restarts_when_work_ends(self):
        owner, ticket = admitted(live(0.0), A, 1.0)
        owner = run(owner, Finished(ticket, 50.0))[0]
        assert run(owner, Tick(54.0, idle_timeout=5.0))[1] == []

        owner = run(owner, BackgroundFinished(80.0))[0]
        assert run(owner, Tick(84.0, idle_timeout=5.0))[1] == []


class TestOtherWaysOut:
    def test_a_wedged_browser_stops_the_owner_at_once(self):
        owner, _ticket = admitted(live(), A, 0.0)

        owner, effects = run(owner, Tick(1.0, wedged="the profile is held"))

        assert effects == [Stop(Retirement.WEDGED, wedged="the profile is held")]
        assert owner.retirement is Retirement.WEDGED

    def test_a_stopped_endpoint_ends_the_loop_and_decides_nothing(self):
        owner = live()

        after, effects = run(owner, Tick(1.0, endpoint_stopped=True))

        assert effects == [EndpointGone()]
        assert after == owner

    def test_after_a_stop_every_tick_repeats_it(self):
        owner, first = run(live(0.0), Tick(10.0, idle_timeout=5.0))

        again = run(owner, Tick(11.0, wedged="later"), Tick(12.0))[1]

        assert again == first + first


class TestTurnover:
    def test_admission_closes_with_the_request_not_the_tick(self):
        owner = run(live(), TurnOver())[0]

        assert run(owner, Admit(A, 1.0))[1] == [Refused()]

    def test_nothing_admitted_stops_on_the_first_tick(self):
        effects = run(live(), TurnOver(), Tick(1.0))[1]

        assert effects == [Drain(Retirement.TURNOVER), Stop(Retirement.TURNOVER)]

    def test_a_profile_command_is_named_as_the_one_that_asked(self):
        effects = run(
            live(), RetireIfIdle(Retirement.PROFILE_COMMAND), TurnOver(), Tick(1.0)
        )[1]

        assert effects == [
            Drain(Retirement.PROFILE_COMMAND),
            Stop(Retirement.PROFILE_COMMAND),
        ]

    def test_an_admitted_call_is_waited_for_until_it_finishes(self):
        owner, ticket = admitted(live(), A, 0.0)
        owner, effects = run(owner, TurnOver(), Tick(0.1))
        assert effects == [Drain(Retirement.TURNOVER)]

        owner, effects = run(
            owner, Heard(A, 0.2), Tick(0.2), Finished(ticket, 0.25), Tick(0.3)
        )

        assert effects == [Stop(Retirement.TURNOVER)]

    def test_a_stopped_endpoint_does_not_end_the_drain(self):
        owner, _ticket = admitted(live(), A, 0.0)
        owner = run(owner, TurnOver(), Tick(0.1))[0]

        owner, effects = run(owner, Tick(0.2, endpoint_stopped=True))

        assert effects == []
        assert lifecycle.watching(owner, A)

    @pytest.mark.parametrize("began", [False, True], ids=["queued", "running"])
    def test_the_cutoff_cuts_and_classifies(self, began: bool):
        owner, ticket = admitted(live(), A, 0.0)
        if began:
            owner = run(owner, BodyBegan(A))[0]
        owner = run(owner, TurnOver(), Tick(0.1))[0]

        # Its frontend keeps waiting, so only the cutoff can end the drain.
        now, effects = 0.1, []
        while now < 0.1 + TURNOVER_DRAIN_SECONDS + STALL_SECONDS:
            if any(isinstance(effect, Stop) for effect in effects):
                break
            now += STALL_SECONDS / 2
            owner, effects = run(owner, Heard(A, now), Tick(now))

        assert now >= 0.1 + TURNOVER_DRAIN_SECONDS
        assert effects == [
            Cancel(ticket, A, Fate.CUT),
            CutOff(1),
            Stop(Retirement.TURNOVER),
        ]
        assert lifecycle.stopped(owner, ticket) is (
            Stopped.UNKNOWN if began else Stopped.NOT_RUN
        )

    def test_a_wedge_during_the_drain_cuts_at_once(self):
        owner, ticket = admitted(live(), A, 0.0)
        owner = run(owner, TurnOver(), Tick(0.1))[0]

        effects = run(owner, Tick(0.2, wedged="gone"))[1]

        assert effects == [
            Cancel(ticket, A, Fate.CUT),
            CutOff(1),
            Stop(Retirement.TURNOVER),
        ]


# --- Schedules -----------------------------------------------------------------------

#: Small enough that a schedule stays short, in the proportions that matter:
#: the loop turns well inside the stall threshold, and the expiry and the drain
#: each take several turns.
_STEP = 0.25
_STALL = 0.6
_EXPIRY = 1.0
_DRAIN = 1.5


@dataclass
class _World:
    """One schedule's run of the reducer, with what the shell would know."""

    owner: Owner = field(default_factory=lambda: live(0.0))
    now: float = 0.0
    #: The ticket each call was started under; absent if refused.
    ticket: dict[str, int] = field(default_factory=dict)
    refused: set[str] = field(default_factory=set)
    quiet: set[str] = field(default_factory=set)
    began: set[str] = field(default_factory=set)
    cancelled: dict[int, Fate] = field(default_factory=dict)
    finished: set[int] = field(default_factory=set)
    #: Set once the shell has been told to stand down or to stop.
    closed: bool = False
    stopped: Stop | None = None
    drain_began: float | None = None
    began_before_cut: dict[int, bool] = field(default_factory=dict)
    #: What each cancelled call was answered with, asked as its task unwound.
    verdicts: dict[int, Stopped | None] = field(default_factory=dict)
    trace: list[str] = field(default_factory=list)

    def feed(self, event: Event) -> list[Effect]:
        before = self.owner
        self.owner, effects = lifecycle.on_event(before, event)
        self.trace.append(f"{event!r} -> {effects!r}")
        self._judge(before, event, effects)
        return effects

    def _judge(self, before: Owner, event: Event, effects: list[Effect]) -> None:
        for effect in effects:
            match effect:
                case Refused():
                    # A refusal leaves nothing behind.
                    assert self.owner == before, self.trace
                case StartTask():
                    # Nobody is admitted by an owner that decided to leave.
                    assert not self.closed, self.trace
                case Cancel():
                    # Only a started call that has not ended is signalled,
                    # and only once.
                    assert effect.ticket in self.ticket.values(), self.trace
                    assert effect.ticket not in self.finished, self.trace
                    assert effect.ticket not in self.cancelled, self.trace
                    self.cancelled[effect.ticket] = effect.because
                    if effect.because is Fate.ABANDONED:
                        # Only a call whose client stopped asking is written
                        # off as abandoned.
                        assert effect.call_id in self.quiet, self.trace
                    else:
                        self.began_before_cut[effect.ticket] = (
                            effect.call_id in self.began
                        )
                case Drain():
                    self.drain_began = self.now
                case Stop():
                    self._judge_stop(effect)
        if isinstance(event, TurnOver):
            self.closed = True

    def _judge_stop(self, stop: Stop) -> None:
        if self.stopped is not None:
            assert stop == self.stopped, self.trace
            return
        self.stopped = stop
        self.closed = True
        # Judged from the tasks the shell started, not from the owner's own
        # count, which is what is under test.
        running = {t for t in self.ticket.values() if t not in self.finished}
        if stop.because is Retirement.IDLE:
            # An owner that leaves for idleness had nobody to leave behind.
            assert not running, self.trace
        else:
            # A drain with a call still running ends only at its cutoff.
            assert self.drain_began is not None, self.trace
            if running:
                assert self.now >= self.drain_began + _DRAIN, self.trace
        # Every call the owner leaves behind was cancelled, and so answered.
        assert running <= set(self.cancelled), self.trace

    def turn(self, *, idle_timeout: float = 0.0) -> None:
        """Time passes by one loop turn: every client still waiting beats."""
        self.now += _STEP
        for call_id, ticket in self.ticket.items():
            if call_id not in self.quiet and ticket not in self.finished:
                self.feed(Heard(call_id, self.now))
        if self.stopped is None:
            self.feed(Tick(self.now, idle_timeout=idle_timeout))

    def admit(self, call_id: str, *, start_fails: bool = False) -> None:
        effects = self.feed(Admit(call_id, self.now))
        if effects == [Refused()]:
            self.refused.add(call_id)
            return
        [start] = effects
        assert isinstance(start, StartTask)
        self.ticket[call_id] = start.ticket
        if start_fails:
            # No await between the reservation and the start that failed.
            self.feed(StartFailed(start.ticket, self.now))
            self.finished.add(start.ticket)

    def begin(self, call_id: str) -> None:
        ticket = self.ticket.get(call_id)
        if ticket is None or ticket in self.finished or ticket in self.cancelled:
            return
        # The serializing middleware's last check before the body.
        if lifecycle.abandoned(self.owner, call_id, self.now):
            return
        self.feed(BodyBegan(call_id))
        self.began.add(call_id)

    def finish(self, call_id: str) -> None:
        ticket = self.ticket.get(call_id)
        if ticket is None or ticket in self.finished:
            return
        if ticket in self.cancelled:
            # The middleware asks before it releases the call.
            self.verdicts[ticket] = lifecycle.stopped(self.owner, ticket)
        self.feed(Finished(ticket, self.now))
        self.finished.add(ticket)


def _actions(world: _World, b_fails: bool) -> dict[str, list[Callable[[], object]]]:
    """Each actor's steps, in its own order.

    B is admitted and either fails to start, atomically, or runs until the
    schedule ends; A covers a completion in the middle.
    """

    def go_quiet() -> None:
        world.quiet.add(A)
        for _ in range(int((_EXPIRY + _STALL) / _STEP) + 1):
            world.turn()

    def pass_cutoff() -> None:
        for _ in range(int((_DRAIN + _STALL) / _STEP) + 1):
            world.turn()

    return {
        "a": [
            lambda: world.admit(A),
            lambda: world.begin(A),
            lambda: world.finish(A),
        ],
        "b": [lambda: world.admit(B, start_fails=b_fails)],
        "expiry": [go_quiet],
        "stand-down": [lambda: world.feed(TurnOver())],
        "cutoff": [pass_cutoff],
        "idle": [lambda: world.turn(idle_timeout=_STEP / 2)],
    }


def _interleavings(lengths: dict[str, int]) -> Iterator[tuple[str, ...]]:
    """Every order of the actors' steps that keeps each actor's own order."""
    if not any(lengths.values()):
        yield ()
        return
    for name, left in lengths.items():
        if left:
            rest = {**lengths, name: left - 1}
            for tail in _interleavings(rest):
                yield (name, *tail)


def _play(order: Iterable[str], b_fails: bool) -> _World:
    world = _World()
    actions = _actions(world, b_fails)
    for name in order:
        actions[name].pop(0)()
        world.turn()
        # A drain is bounded: past its cutoff the owner has gone, whoever is
        # still admitted and still waiting.
        if world.drain_began is not None and world.now > world.drain_began + _DRAIN:
            assert world.stopped is not None, world.trace
    # Every task ends eventually, cancelled or not, and the shell reports it.
    world.finish(A)
    world.finish(B)
    return world


@pytest.fixture
def small_times(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "STALL_SECONDS", _STALL)
    monkeypatch.setattr(lifecycle, "EXPIRY_SECONDS", _EXPIRY)
    monkeypatch.setattr(lifecycle, "TURNOVER_DRAIN_SECONDS", _DRAIN)


@pytest.mark.usefixtures("small_times")
@pytest.mark.parametrize("b_fails", [False, True], ids=["b runs", "b fails to start"])
def test_every_schedule_keeps_admission_and_retirement_apart(b_fails: bool):
    """Admission against retirement, across every interleaving.

    Each step is followed by one loop turn. The rules are checked as the
    schedule runs (:meth:`_World._judge`) and once it has settled.
    """
    lengths = {name: len(steps) for name, steps in _actions(_World(), b_fails).items()}
    schedules = 0
    for order in _interleavings(lengths):
        world = _play(order, b_fails)
        schedules += 1
        owner = world.owner
        # Every reservation was released, and releasing one again changes
        # nothing.
        assert owner.calls == {}, world.trace
        stale = [Finished(ticket, world.now + 1) for ticket in world.ticket.values()]
        assert run(owner, *stale)[0] == owner, world.trace
        # A cancelled call is answered according to where it was when this
        # owner cancelled it.
        for ticket, fate in world.cancelled.items():
            if fate is Fate.ABANDONED:
                expected = Stopped.ABANDONED
            elif world.began_before_cut[ticket]:
                expected = Stopped.UNKNOWN
            else:
                expected = Stopped.NOT_RUN
            assert world.verdicts[ticket] is expected, world.trace
        # A refused call left nothing behind to be cancelled or released.
        assert not world.refused & set(world.ticket), (
            world.trace
        )  # Eight steps, three of them A's in its own order: 8! / 3!.
    assert schedules == 6720
