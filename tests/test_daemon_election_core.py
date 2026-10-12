"""The election's decisions, driven by events alone.

Nothing here reads a descriptor, starts a process or opens a socket: every test
feeds ``daemon.core.lookup``, ``daemon.core.election`` or ``daemon.core.spawn``
and reads what it decided. The schedule tests explore every completion of every
wait to the end and check rules written from the default-on contract rather than
read off the reducers. Frontends electing against one lock, composed with the
owner and call cores, are in ``test_daemon_composed_core.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace

import pytest

from linkedin_mcp_server.daemon.core import election as core
from linkedin_mcp_server.daemon.core import lookup
from linkedin_mcp_server.daemon.core import spawn
from linkedin_mcp_server.daemon.core.lookup import Finding, Reach, Reading
from linkedin_mcp_server.daemon.core.spawn import Attempt, Started

A, B = "v1." + "a" * 32, "v1." + "b" * 32

TIMINGS = lookup.Timings(read=1.0, poll=0.1, probe=5.0)
PACING = core.Pacing(retry=0.2, burst=3, first_delay=0.5, max_delay=8.0)
BUDGETS = spawn.Budgets(failure_verdict=0.2, prepared_read=1.0)

ATTACHABLE = Reading(instance=A, attachable=True)
OLDER = Reading(instance=A, attachable=True, older=True)
#: An older owner speaking another protocol, and one that is not older.
OLD_PROTOCOL = Reading(instance=A, control_only=True, other_protocol=True, older=True)
NEW_PROTOCOL = Reading(instance=A, control_only=True, other_protocol=True)
#: Another configuration: the same version, an older one, and one newer or
#: not comparable (which reads the same).
RIVAL = Reading(instance=A, control_only=True, other_config=True, same_version=True)
OLD_RIVAL = Reading(instance=A, control_only=True, other_config=True, older=True)
NEW_RIVAL = Reading(instance=A, control_only=True, other_config=True)
NOTHING = Reading()


# --- One look ----------------------------------------------------------------------


def look(
    *,
    now: float = 0.0,
    deadline: float = 10.0,
    wait: float = 0.0,
    may_ask: bool = True,
    buried: frozenset[str] = frozenset(),
) -> tuple[lookup.Lookup, list[lookup.Effect]]:
    return lookup.look(
        now=now,
        deadline=deadline,
        wait=wait,
        may_ask=may_ask,
        buried=buried,
        timings=TIMINGS,
    )


def read_then(
    reading: Reading, reach: Reach | None = None, *, may_ask: bool = True
) -> list[lookup.Effect]:
    """Everything a pass decides after reading *reading* once, probed with *reach*."""
    state, effects = look(may_ask=may_ask)
    [first] = effects
    assert isinstance(first, lookup.ReadDescriptor)
    state, effects = lookup.on_event(state, lookup.Read(first.op, 0.0, reading))
    decided = list(effects)
    probes = [e for e in effects if isinstance(e, lookup.Probe)]
    if probes:
        assert reach is not None, "a probe was asked for and nobody answered it"
        state, effects = lookup.on_event(state, lookup.Probed(probes[0].op, 0.0, reach))
        decided += effects
    return decided


def passing(
    decided: tuple[lookup.Lookup, list[lookup.Effect]],
) -> tuple[lookup.Lookup, lookup.ReadDescriptor | lookup.Pause | lookup.Probe]:
    """The pass after a decision, and the one thing it now waits on."""
    state, [effect] = decided
    assert isinstance(effect, lookup.WAITS), effect
    return state, effect


def concluded(effects: list[lookup.Effect]) -> lookup.Conclude:
    [conclusion] = [e for e in effects if isinstance(e, lookup.Conclude)]
    return conclusion


class TestOneLook:
    def test_an_owner_that_answers_is_attached_to(self):
        effects = read_then(ATTACHABLE, Reach.ANSWERED)

        assert concluded(effects) == lookup.Conclude(Finding.AS_READ)
        assert not any(isinstance(e, lookup.Bury) for e in effects)

    def test_silence_is_not_death(self):
        effects = read_then(ATTACHABLE, Reach.SILENT)

        assert concluded(effects).finding is Finding.NOT_ANSWERED_YET
        assert not any(isinstance(e, lookup.Bury) for e in effects)

    def test_a_refusal_buries_the_generation(self):
        effects = read_then(ATTACHABLE, Reach.REFUSED)

        assert lookup.Bury(A) in effects
        assert concluded(effects).finding is Finding.LEFTOVERS

    @pytest.mark.parametrize("may_ask", [True, False], ids=["active", "settling"])
    @pytest.mark.parametrize("reading", [OLDER, OLD_RIVAL], ids=["same", "other"])
    def test_an_older_version_is_asked_to_stand_down_and_written_off(
        self, reading: Reading, may_ask: bool
    ):
        # Whatever its configuration: an older owner proved for control only,
        # because its fingerprint differs, once ended the pass unasked.
        effects = read_then(reading, may_ask=may_ask)

        asked = lookup.AskToStandDown(lookup.Turnover.OLDER_VERSION) in effects
        assert asked is may_ask
        assert lookup.Bury(A) in effects
        assert concluded(effects) == lookup.Conclude(
            Finding.OLDER_VERSION, asked=may_ask
        )
        assert not any(isinstance(e, lookup.Probe) for e in effects)

    def test_an_older_protocol_is_asked_to_stand_down(self):
        effects = read_then(OLD_PROTOCOL)

        assert effects[:2] == [
            lookup.AskToStandDown(lookup.Turnover.OLDER_PROTOCOL),
            lookup.Bury(A),
        ]
        assert concluded(effects) == lookup.Conclude(Finding.WRITTEN_OFF, asked=True)

    def test_a_protocol_that_is_not_older_is_left_running(self):
        effects = read_then(NEW_PROTOCOL)

        assert lookup.LeaveAlone() in effects
        assert not any(isinstance(e, lookup.AskToStandDown) for e in effects)
        assert concluded(effects).finding is Finding.WRITTEN_OFF

    @pytest.mark.parametrize(
        ("reach", "finding"),
        [(Reach.ANSWERED, Finding.LIVE_RIVAL), (Reach.SILENT, Finding.SILENT_RIVAL)],
    )
    def test_a_same_version_rival_is_left_alone_after_one_probe(
        self, reach: Reach, finding: Finding
    ):
        effects = read_then(RIVAL, reach)

        assert sum(isinstance(e, lookup.Probe) for e in effects) == 1
        assert not any(
            isinstance(e, (lookup.AskToStandDown, lookup.Bury)) for e in effects
        )
        assert concluded(effects) == lookup.Conclude(finding)

    def test_a_same_version_rival_that_refuses_is_leftovers(self):
        effects = read_then(RIVAL, Reach.REFUSED)

        assert lookup.Bury(A) in effects
        assert concluded(effects).finding is Finding.WRITTEN_OFF

    def test_a_newer_rival_is_only_waited_out(self):
        effects = read_then(NEW_RIVAL)

        assert effects == [lookup.Conclude(Finding.AS_READ)]

    def test_a_buried_generation_is_watched_and_never_probed(self):
        state, read = passing(look(wait=0.3, buried=frozenset({A})))
        effects: list[lookup.Effect] = []
        now = 0.0
        while not any(isinstance(e, lookup.Conclude) for e in effects):
            assert isinstance(read, lookup.ReadDescriptor)
            state, effects = lookup.on_event(
                state, lookup.Read(read.op, now, ATTACHABLE)
            )
            for effect in effects:
                assert not isinstance(effect, lookup.Probe)
                if isinstance(effect, lookup.Pause):
                    now += effect.seconds
                    state, (read,) = lookup.on_event(
                        state, lookup.Paused(effect.op, now)
                    )

        assert concluded(effects).finding is Finding.ALREADY_UNUSABLE
        assert now == pytest.approx(0.3)

    def test_a_paced_pass_picks_up_a_new_generation_inside_its_wait(self):
        state, read = passing(look(wait=4.0, buried=frozenset({A})))
        state, pause = passing(
            lookup.on_event(state, lookup.Read(read.op, 0.0, ATTACHABLE))
        )
        assert isinstance(pause, lookup.Pause) and pause.seconds == TIMINGS.poll
        state, read = passing(lookup.on_event(state, lookup.Paused(pause.op, 0.1)))
        fresh = replace(ATTACHABLE, instance=B)
        state, probe = passing(lookup.on_event(state, lookup.Read(read.op, 0.1, fresh)))

        assert isinstance(probe, lookup.Probe)

    @pytest.mark.parametrize(
        ("event", "finding"),
        [
            (lambda op: lookup.ReadTimedOut(op, 1.0), Finding.READ_TIMED_OUT),
        ],
    )
    def test_a_read_that_did_not_finish_is_no_observation(self, event, finding):
        state, read = passing(look())
        _, effects = lookup.on_event(state, event(read.op))

        assert effects == [lookup.Conclude(finding, observed=False)]

    def test_no_read_starts_after_the_deadline(self):
        assert look(now=10.0)[1] == [lookup.Conclude(Finding.NOT_READ, observed=False)]

    def test_no_probe_starts_after_the_deadline(self):
        state, read = passing(look(deadline=1.0))
        _, effects = lookup.on_event(state, lookup.Read(read.op, 1.0, ATTACHABLE))

        assert effects == [lookup.Conclude(Finding.NOT_PROBED)]

    def test_a_probe_is_bounded_by_the_time_left(self):
        state, read = passing(look(deadline=2.0))
        _, probe = passing(
            lookup.on_event(state, lookup.Read(read.op, 0.5, ATTACHABLE))
        )

        assert probe == lookup.Probe(2, 1.5)

    def test_a_stale_completion_changes_nothing(self):
        state, read = passing(look(wait=1.0))
        state, pause = passing(
            lookup.on_event(state, lookup.Read(read.op, 0.0, NOTHING))
        )
        after, effects = lookup.on_event(state, lookup.Read(read.op, 0.0, ATTACHABLE))

        assert (after, effects) == (state, [])


# --- The election loop -------------------------------------------------------------


def elect(
    *, now: float = 0.0, deadline: float = 10.0, settlement: float = 2.0
) -> tuple[core.Election, list[core.Effect]]:
    return core.elect(
        now=now,
        deadline_seconds=deadline,
        settlement_seconds=settlement,
        pacing=PACING,
        nothing_seen="nothing",
    )


def looked(
    op: int,
    now: float,
    seen: object = "absent",
    *,
    conclusive: bool = False,
    asked: bool = False,
    observed: bool = True,
) -> core.Looked:
    return core.Looked(op, now, seen, conclusive, asked, observed)


def waiting(
    decided: tuple[core.Election, list[core.Effect]],
) -> tuple[core.Election, core.Look | core.StartOwner]:
    """The election after a decision, and the one thing it now waits on."""
    state, [effect] = decided
    assert isinstance(effect, (core.Look, core.StartOwner)), effect
    return state, effect


class TestTheElectionLoop:
    def test_an_owner_found_at_once_starts_nothing(self):
        state, first = waiting(elect())
        _, effects = core.on_event(
            state, looked(first.op, 0.1, "owner", conclusive=True)
        )

        assert effects == [core.Decide("owner", started=False)]

    def test_nobody_found_starts_one_child_with_the_time_left(self):
        state, first = waiting(elect())
        _, effects = core.on_event(state, looked(first.op, 0.5))

        assert effects == [core.StartOwner(2, 9.5)]

    def test_a_started_owner_is_reported_as_started(self):
        state, first = waiting(elect())
        state, start = waiting(core.on_event(state, looked(first.op, 0.0)))
        state, watch = waiting(
            core.on_event(state, core.StartEnded(start.op, 1.0, Attempt.STARTED))
        )
        assert isinstance(watch, core.Look) and watch.wait == PACING.retry
        _, effects = core.on_event(
            state, looked(watch.op, 1.1, "owner", conclusive=True)
        )

        assert effects == [core.Decide("owner", started=True)]

    def test_a_start_is_not_repeated_before_its_delay(self):
        state, first = waiting(elect())
        state, start = waiting(core.on_event(state, looked(first.op, 0.0)))
        state, watch = waiting(
            core.on_event(state, core.StartEnded(start.op, 0.1, Attempt.FAILED))
        )
        state, again = waiting(core.on_event(state, looked(watch.op, 0.3)))
        state, effects = core.on_event(state, looked(again.op, 0.3))

        # Not due until 0.5: a paced pass, not a second child.
        assert [type(e) for e in effects] == [core.Look]

    def test_starts_back_off_after_the_initial_burst(self):
        delay = PACING.first_delay
        delays = []
        for starts in range(1, 7):
            delay = core.start_delay_after(starts, delay, PACING)
            delays.append(delay)

        assert delays == [0.5, 0.5, 1.0, 2.0, 4.0, 8.0]

    def test_an_unusable_lock_ends_the_election_with_what_was_seen(self):
        state, first = waiting(elect())
        state, start = waiting(core.on_event(state, looked(first.op, 0.0, "seen")))
        _, effects = core.on_event(
            state, core.StartRefused(start.op, 0.1, lock_unusable=True)
        )

        assert effects == [core.Decide("seen", started=False)]

    @pytest.mark.parametrize(
        "ending",
        [
            lambda op: core.StartRefused(op, 1.0, lock_unusable=False),
            lambda op: core.StartEnded(op, 1.0, Attempt.ABORTED),
        ],
        ids=["no child", "aborted"],
    )
    def test_a_start_that_cannot_succeed_gets_one_settlement_and_no_retry(
        self, ending: Callable[[int], core.Event]
    ):
        state, first = waiting(elect(settlement=2.0))
        state, start = waiting(core.on_event(state, looked(first.op, 0.0)))
        state, effects = core.on_event(state, ending(start.op))

        now = 1.0
        while True:
            [effect] = effects
            assert not isinstance(effect, core.StartOwner)
            if isinstance(effect, core.Decide):
                break
            assert isinstance(effect, core.Look) and not effect.may_ask
            assert effect.deadline == 3.0
            now += effect.wait
            state, effects = core.on_event(state, looked(effect.op, now))

        assert now == pytest.approx(3.0)

    @pytest.mark.parametrize("stage", ["entry", "lookup", "start", "watch"])
    def test_every_active_deadline_exit_settles_to_the_global_deadline(
        self, stage: str
    ):
        active_until = 0.0 if stage == "entry" else 10.0
        state, effects = elect(deadline=active_until, settlement=2.0)
        if stage != "entry":
            state, first = waiting((state, effects))
            if stage == "lookup":
                state, effects = core.on_event(state, looked(first.op, 10.0))
            else:
                state, start = waiting(core.on_event(state, looked(first.op, 0.0)))
                if stage == "start":
                    state, effects = core.on_event(
                        state, core.StartEnded(start.op, 10.0, Attempt.CONTENDED)
                    )
                else:
                    state, watch = waiting(
                        core.on_event(
                            state, core.StartEnded(start.op, 0.1, Attempt.CONTENDED)
                        )
                    )
                    state, effects = core.on_event(state, looked(watch.op, 10.0))
        now = active_until

        while True:
            [effect] = effects
            if isinstance(effect, core.Decide):
                break
            # The quiet tail: no request, no start, and one budget long.
            assert isinstance(effect, core.Look), effect
            assert not effect.may_ask and effect.deadline == active_until + 2.0
            now += effect.wait
            state, effects = core.on_event(state, looked(effect.op, now))

        assert now == pytest.approx(active_until + 2.0)

    def test_settlement_still_attaches_to_a_winner(self):
        state, first = waiting(elect(deadline=0.0))
        assert isinstance(first, core.Look) and not first.may_ask
        _, effects = core.on_event(state, looked(first.op, 0.1, "won", conclusive=True))

        assert effects == [core.Decide("won", started=False)]

    def test_an_owner_is_asked_to_stand_down_once_per_election(self):
        state, first = waiting(elect())
        state, start = waiting(core.on_event(state, looked(first.op, 0.0, asked=True)))
        _, watch = waiting(
            core.on_event(state, core.StartEnded(start.op, 0.1, Attempt.CONTENDED))
        )

        assert isinstance(watch, core.Look) and not watch.may_ask

    def test_a_read_that_did_not_finish_keeps_the_last_observation(self):
        state, first = waiting(elect(deadline=0.5, settlement=0.0))
        state, start = waiting(core.on_event(state, looked(first.op, 0.0, "seen")))
        state, watch = waiting(
            core.on_event(state, core.StartEnded(start.op, 0.1, Attempt.CONTENDED))
        )
        _, effects = core.on_event(
            state, looked(watch.op, 0.6, "timeout", observed=False)
        )

        assert effects == [core.Decide("seen", started=False)]

    def test_a_stale_completion_changes_nothing(self):
        state, first = waiting(elect())
        state, start = waiting(core.on_event(state, looked(first.op, 0.0)))

        assert core.on_event(state, looked(first.op, 0.1, conclusive=True)) == (
            state,
            [],
        )


# --- One child start ---------------------------------------------------------------


def started(*, contained: bool = False) -> tuple[spawn.Spawn, list[spawn.Effect]]:
    state = spawn.spawned(contained=contained, budgets=BUDGETS)
    return spawn.on_event(state, spawn.Connected(0.0, 10.0))


def drive(
    replies: list[Callable[[int], spawn.Event]], *, contained: bool = False
) -> tuple[spawn.Spawn, list[spawn.Effect]]:
    """Answer each wait in turn and return everything decided, wind-down included."""
    state, effects = started(contained=contained)
    decided = list(effects)
    for reply in replies:
        [waiting] = [e for e in effects if isinstance(e, spawn.WAITS)]
        state, effects = spawn.on_event(state, reply(waiting.op))
        decided += effects
    return state, decided + spawn.wind_down(state)


def ok(kind: type) -> Callable[[int], spawn.Event]:
    return lambda op: kind(op, 1.0)


def greeted(refusal: Started | None = None) -> Callable[[int], spawn.Event]:
    return lambda op: spawn.Greeted(op, 1.0, refusal)


def prepared(answer: str | Started = A) -> Callable[[int], spawn.Event]:
    return lambda op: spawn.Prepared(op, 1.0, answer)


def verdict(answer: Started) -> Callable[[int], spawn.Event]:
    return lambda op: spawn.Verdict(op, 1.0, answer)


TO_COMMIT = [greeted(), ok(spawn.HandedOver), prepared(), ok(spawn.Validated)]


def finish(effects: list[spawn.Effect]) -> Started:
    [end] = [e for e in effects if isinstance(e, spawn.Finish)]
    return end.started


class TestOneStart:
    def test_only_the_generation_the_child_prepared_is_committed(self):
        _, effects = drive([*TO_COMMIT, ok(spawn.CommitSent), verdict(Started.YES)])

        [commit] = [e for e in effects if isinstance(e, spawn.SendCommit)]
        assert commit.instance == A
        assert finish(effects) is Started.YES
        assert not any(
            isinstance(e, (spawn.StopChild, spawn.DiscardPrepared)) for e in effects
        )

    @pytest.mark.parametrize(
        ("replies", "outcome", "reported"),
        [
            ([greeted(Started.NO)], Started.NO, True),
            ([greeted(Started.ABORTED)], Started.ABORTED, False),
            ([greeted(), ok(spawn.HandOverFailed), prepared()], Started.NO, True),
            (
                [greeted(), ok(spawn.HandOverFailed), prepared(Started.RETRY)],
                Started.RETRY,
                True,
            ),
            (
                [greeted(), ok(spawn.HandedOver), prepared(Started.STILL_TRYING)],
                Started.NO,
                True,
            ),
            ([greeted(), ok(spawn.HandedOver), prepared(Started.NO)], Started.NO, True),
            (
                [greeted(), ok(spawn.HandedOver), prepared(Started.ABORTED)],
                Started.ABORTED,
                True,
            ),
            (
                [greeted(), ok(spawn.HandedOver), prepared(Started.RETRY)],
                Started.RETRY,
                False,
            ),
            ([*TO_COMMIT[:3], ok(spawn.ValidationTimedOut)], Started.NO, False),
            (
                [*TO_COMMIT[:3], ok(spawn.ValidationFailed), verdict(Started.RETRY)],
                Started.RETRY,
                False,
            ),
            (
                [*TO_COMMIT[:3], ok(spawn.ValidationFailed), verdict(Started.NO)],
                Started.ABORTED,
                False,
            ),
        ],
    )
    def test_every_failure_before_commit_stops_the_child(
        self,
        replies: list[Callable[[int], spawn.Event]],
        outcome: Started,
        reported: bool,
    ):
        state, effects = drive(replies)

        assert finish(effects) is outcome
        assert (
            effects.count(spawn.StopChild(silent=False))
            + effects.count(spawn.StopChild(silent=True))
            == 1
        )
        assert (spawn.ReportFailure() in effects) is reported
        # Whatever the child prepared is removed, since no COMMIT reached it.
        if state.prepared is not None:
            assert spawn.DiscardPrepared(state.prepared) in effects

    def test_a_child_that_prepared_nothing_is_reported_as_silent(self):
        _, effects = drive(
            [greeted(), ok(spawn.HandedOver), prepared(Started.STILL_TRYING)]
        )

        assert spawn.StopChild(silent=True) in effects

    @pytest.mark.parametrize("answer", [Started.NO, Started.STILL_TRYING])
    def test_a_missing_verdict_after_commit_is_settled_never_killed(
        self, answer: Started
    ):
        _, effects = drive(
            [
                *TO_COMMIT,
                ok(spawn.CommitSent),
                verdict(answer),
                lambda op: spawn.Settled(op, 1.0, Started.UNCERTAIN),
            ]
        )

        assert any(isinstance(e, spawn.SettleCommit) for e in effects)
        assert finish(effects) is Started.UNCERTAIN
        assert not any(isinstance(e, spawn.StopChild) for e in effects)

    def test_an_undelivered_commit_is_settled_never_killed(self):
        _, effects = drive(
            [
                *TO_COMMIT,
                ok(spawn.CommitUndelivered),
                lambda op: spawn.Settled(op, 1.0, Started.YES),
            ],
            contained=True,
        )

        assert finish(effects) is Started.YES
        assert not any(isinstance(e, spawn.StopChild) for e in effects)
        # The Job is let go rather than closed on an owner that may have adopted.
        assert spawn.ReleaseJob() in effects

    @pytest.mark.parametrize("answer", [Started.ABORTED, Started.RETRY])
    def test_the_childs_own_word_that_it_did_not_publish_permits_a_stop(
        self, answer: Started
    ):
        _, effects = drive([*TO_COMMIT, ok(spawn.CommitSent), verdict(answer)])

        assert spawn.StopChild() in effects
        assert finish(effects) is answer

    def test_an_uncertain_verdict_keeps_the_lock_holder_alive(self):
        _, effects = drive(
            [*TO_COMMIT, ok(spawn.CommitSent), verdict(Started.UNCERTAIN)],
            contained=True,
        )

        assert finish(effects) is Started.UNCERTAIN
        assert not any(isinstance(e, spawn.StopChild) for e in effects)

    @pytest.mark.parametrize(
        ("replies", "stops"),
        [
            ([], True),
            ([greeted()], True),
            ([greeted(), ok(spawn.HandOverFailed)], False),
            ([greeted(), ok(spawn.HandedOver)], True),
            (TO_COMMIT[:3], True),
            ([*TO_COMMIT[:3], ok(spawn.ValidationFailed)], True),
            (TO_COMMIT, False),
            ([*TO_COMMIT, ok(spawn.CommitSent)], False),
            ([*TO_COMMIT, ok(spawn.CommitUndelivered)], False),
        ],
        ids=[
            "greeting",
            "handing over",
            "after a failed hand-over",
            "preparing",
            "validating",
            "after unusable state",
            "committing",
            "awaiting the verdict",
            "settling",
        ],
    )
    def test_an_interruption_stops_the_child_only_before_commit(
        self, replies: list[Callable[[int], spawn.Event]], stops: bool
    ):
        state, effects = drive(replies)
        state, interrupted = spawn.on_event(state, spawn.Interrupted(state.pending))

        assert (spawn.StopChild() in interrupted) is stops
        assert spawn.StopChild() not in spawn.wind_down(state)

    def test_an_interruption_while_connecting_stops_the_child(self):
        state = spawn.spawned(contained=False, budgets=BUDGETS)

        assert spawn.on_event(state, spawn.Interrupted(0))[1] == [spawn.StopChild()]

    @pytest.mark.parametrize("contained", [False, True])
    def test_the_wind_down_settles_a_job_nobody_stopped(self, contained: bool):
        state, _ = drive([greeted()], contained=contained)

        ending = spawn.wind_down(state)

        assert (spawn.StopChild() in ending) is contained
        assert ending[-1] == spawn.HangUp()

    def test_each_wait_is_bounded_by_what_is_left_of_the_budget(self):
        state, [greet] = started()
        assert greet == spawn.Greet(1, 10.0)
        state, [hand_over] = spawn.on_event(state, spawn.Greeted(1, 3.0, None))
        assert hand_over == spawn.HandOver(2, 7.0)
        state, [await_prepared] = spawn.on_event(state, spawn.HandedOver(2, 4.0))
        assert await_prepared == spawn.AwaitPrepared(3, 6.0)
        _, [validate] = spawn.on_event(state, spawn.Prepared(3, 9.5, A))

        # The prepared read gets its own bound, never past the budget.
        assert validate == spawn.Validate(4, A, 10.0)

    @pytest.mark.parametrize(
        ("started_", "attempt"),
        [
            (Started.YES, Attempt.STARTED),
            (Started.NO, Attempt.FAILED),
            (Started.ABORTED, Attempt.ABORTED),
            (Started.RETRY, Attempt.CONTENDED),
            (Started.STILL_TRYING, Attempt.CONTENDED),
            (Started.UNCERTAIN, Attempt.CONTENDED),
        ],
    )
    def test_what_a_start_means_to_the_election(
        self, started_: Started, attempt: Attempt
    ):
        assert spawn.attempt_after(started_) is attempt


# --- Schedules: one start ----------------------------------------------------------
#
# Every completion of every wait, an interruption included, to the end.

_SPAWN_REPLIES: dict[type, list[Callable[[int], spawn.Event]]] = {
    spawn.Greet: [greeted(), greeted(Started.NO), greeted(Started.ABORTED)],
    spawn.HandOver: [ok(spawn.HandedOver), ok(spawn.HandOverFailed)],
    spawn.AwaitPrepared: [prepared(A)]
    + [prepared(s) for s in (Started.STILL_TRYING, Started.NO, Started.ABORTED)]
    + [prepared(Started.RETRY)],
    spawn.Validate: [
        ok(spawn.Validated),
        ok(spawn.ValidationTimedOut),
        ok(spawn.ValidationFailed),
    ],
    spawn.SendCommit: [ok(spawn.CommitSent), ok(spawn.CommitUndelivered)],
    spawn.AwaitVerdict: [verdict(s) for s in Started],
    spawn.SettleCommit: [
        lambda op: spawn.Settled(op, 1.0, Started.YES),
        lambda op: spawn.Settled(op, 1.0, Started.UNCERTAIN),
    ],
}


_SpawnWait = (
    spawn.Greet
    | spawn.HandOver
    | spawn.AwaitPrepared
    | spawn.Validate
    | spawn.SendCommit
    | spawn.AwaitVerdict
    | spawn.SettleCommit
)


@dataclass(frozen=True)
class _Turn:
    asked: spawn.Effect | None
    reply: spawn.Event
    then: tuple[spawn.Effect, ...]


def _spawn_schedules(contained: bool) -> Iterator[tuple[list[_Turn], spawn.Spawn]]:
    def explore(state: spawn.Spawn, waiting: _SpawnWait | None, path: list[_Turn]):
        assert len(path) < 12, "a start that does not end"
        op = 0 if waiting is None else waiting.op
        replies = [*_SPAWN_REPLIES.get(type(waiting), []), spawn.Interrupted]
        if waiting is None:
            replies = [lambda _op: spawn.Connected(0.0, 10.0), spawn.Interrupted]
        for reply in replies:
            event = reply(op)
            after, effects = spawn.on_event(state, event)
            turn = _Turn(waiting, event, tuple(effects))
            nxt = [e for e in effects if isinstance(e, spawn.WAITS)]
            if nxt and not isinstance(event, spawn.Interrupted):
                yield from explore(after, nxt[0], [*path, turn])
            else:
                yield [*path, turn], after

    yield from explore(spawn.spawned(contained=contained, budgets=BUDGETS), None, [])


@pytest.mark.parametrize("contained", [False, True], ids=["posix", "job"])
def test_every_start_keeps_the_termination_authority(contained: bool):
    schedules = list(_spawn_schedules(contained))
    # Every reply to every wait, to the end: a sanity count, not a contract.
    assert len(schedules) == 40

    for path, state in schedules:
        where = [type(t.reply).__name__ for t in path]
        ending = list(spawn.wind_down(state))
        decided = [e for t in path for e in t.then] + ending
        committed_at = next(
            (i for i, t in enumerate(path) if isinstance(t.asked, spawn.SendCommit)),
            None,
        )

        # No COMMIT before the prepared generation was read back, and only for
        # the generation this child prepared.
        commits = [e for e in decided if isinstance(e, spawn.SendCommit)]
        assert len(commits) <= 1, where
        if commits:
            validated = [t for t in path if isinstance(t.reply, spawn.Validated)]
            assert validated, where
            names = [
                t.reply.answer for t in path if isinstance(t.reply, spawn.Prepared)
            ]
            assert commits[0].instance in names, where

        # After a COMMIT may have reached the child, it is stopped only on its
        # own authenticated word that it did not publish.
        if committed_at is not None:
            for t in path[committed_at:]:
                if any(isinstance(e, spawn.StopChild) for e in t.then):
                    assert isinstance(t.reply, spawn.Verdict), where
                    assert t.reply.verdict in (Started.ABORTED, Started.RETRY), where
                    assert isinstance(t.asked, spawn.AwaitVerdict), where
            assert not any(isinstance(e, spawn.StopChild) for e in ending), where
            # Nor is anything it may have published removed from under it.
            assert not any(isinstance(e, spawn.DiscardPrepared) for e in decided), where

        # A start that ended before COMMIT leaves no child holding the lock: it
        # was stopped, or its input closes on it, and its pending state goes.
        finishes = [e for e in decided if isinstance(e, spawn.Finish)]
        interrupted = isinstance(path[-1].reply, spawn.Interrupted)
        assert len(finishes) == (0 if interrupted else 1), where
        if committed_at is None:
            # A Job stops it in every case. Without one, a child interrupted
            # while it may be preparing is left to its end of file.
            assert any(isinstance(e, spawn.StopChild) for e in decided) or (
                not contained
                and interrupted
                and isinstance(path[-1].asked, spawn.AwaitPrepared)
            ), where
            assert ending[-2:][-1] == spawn.HangUp() or isinstance(
                ending[-1], spawn.DiscardPrepared
            ), where
            prepared_names = [
                t.reply.answer
                for t in path
                if isinstance(t.reply, spawn.Prepared)
                and isinstance(t.reply.answer, str)
            ]
            for name in prepared_names:
                assert spawn.DiscardPrepared(name) in ending, where
        if contained:
            # Every Job is settled exactly once: stopped, or let go after COMMIT.
            settles = sum(
                isinstance(e, (spawn.StopChild, spawn.ReleaseJob)) for e in decided
            )
            assert settles == 1, where

        # Published means proved: by the child's verdict or the canonical read.
        if finishes and finishes[0].started is Started.YES:
            proof = path[-1].reply
            assert isinstance(proof, (spawn.Verdict, spawn.Settled)), where
            assert proof.verdict is Started.YES, where


# --- Schedules: one look -----------------------------------------------------------


_READINGS = [
    NOTHING,
    ATTACHABLE,
    OLDER,
    OLD_PROTOCOL,
    NEW_PROTOCOL,
    RIVAL,
    OLD_RIVAL,
    NEW_RIVAL,
    replace(ATTACHABLE, instance=B),
]


def _lookup_schedules(
    may_ask: bool, buried: frozenset[str]
) -> Iterator[list[tuple[lookup.Effect, lookup.Event, list[lookup.Effect]]]]:
    deadline = 1.0

    def explore(state: lookup.Lookup, effects: list[lookup.Effect], path, now: float):
        assert len(path) < 12, "a pass that does not end"
        waits = [e for e in effects if isinstance(e, lookup.WAITS)]
        if not waits:
            yield path
            return
        [waiting] = waits
        later = (now + 0.25, deadline)
        events: list[lookup.Event] = []
        if isinstance(waiting, lookup.ReadDescriptor):
            events = [lookup.Read(waiting.op, t, r) for t in later for r in _READINGS]
            events.append(lookup.ReadTimedOut(waiting.op, deadline))
        elif isinstance(waiting, lookup.Pause):
            # A sleep may overshoot, as far as the deadline.
            events = [
                lookup.Paused(waiting.op, t)
                for t in sorted({now + waiting.seconds, deadline})
            ]
        elif isinstance(waiting, lookup.Probe):
            events = [lookup.Probed(waiting.op, now, reach) for reach in Reach]
        for event in events:
            after, decided = lookup.on_event(state, event)
            yield from explore(
                after, decided, [*path, (waiting, event, decided)], event.now
            )

    state, effects = lookup.look(
        now=0.0,
        deadline=deadline,
        wait=0.5,
        may_ask=may_ask,
        buried=buried,
        timings=TIMINGS,
    )
    yield from explore(state, effects, [(None, None, effects)], 0.0)


@pytest.mark.parametrize("may_ask", [True, False], ids=["active", "settling"])
@pytest.mark.parametrize("buried", [frozenset(), frozenset({A})], ids=["", "buried"])
def test_every_look_keeps_the_contract(may_ask: bool, buried: frozenset[str]):
    schedules = list(_lookup_schedules(may_ask, buried))
    assert len(schedules) > 50

    for path in schedules:
        decided = [e for _, _, effects in path for e in effects]
        where = [type(event).__name__ for _, event, _ in path]
        [conclusion] = [e for e in decided if isinstance(e, lookup.Conclude)]
        assert isinstance(decided[-1], lookup.Conclude), where
        reads = [ev.reading for _, ev, _ in path if isinstance(ev, lookup.Read)]
        reaches = [ev.reach for _, ev, _ in path if isinstance(ev, lookup.Probed)]
        acted_on = reads[-1] if reads else None

        # Nothing is issued once the deadline has passed, and nothing waits past it.
        for asked, event, effects in path:
            for effect in effects:
                if isinstance(effect, lookup.Pause):
                    bound = effect.seconds
                elif isinstance(effect, (lookup.ReadDescriptor, lookup.Probe)):
                    bound = effect.timeout
                else:
                    continue
                start = 0.0 if event is None else event.now
                assert start < 1.0 and start + bound <= 1.0 + 1e-9, where

        # A written-off generation is never contacted, and nobody twice.
        probes = [e for e in decided if isinstance(e, lookup.Probe)]
        assert len(probes) <= 1, where
        if probes:
            assert acted_on is not None and acted_on.instance not in buried, where

        # Silence and an answer prove nothing against an owner: no burial.
        burials = [e for e in decided if isinstance(e, lookup.Bury)]
        if reaches and reaches[0] is not Reach.REFUSED:
            assert not burials, where
        for burial in burials:
            assert acted_on is not None and burial.instance == acted_on.instance, where
        # A refusal does: nothing usable is at that address.
        if reaches == [Reach.REFUSED]:
            assert acted_on is not None, where
            assert burials == [lookup.Bury(str(acted_on.instance))], where

        # Only a completed read is an observation settlement may keep.
        assert conclusion.observed == isinstance(
            path[-1][1], (lookup.Read, lookup.Probed)
        ), where

        # An owner of a protocol that is not older is written off and left alone.
        if acted_on == NEW_PROTOCOL and acted_on.instance not in buried:
            assert conclusion.finding is Finding.WRITTEN_OFF, where
            assert lookup.LeaveAlone() in decided, where
        if lookup.LeaveAlone() in decided:
            assert acted_on == NEW_PROTOCOL, where

        # Only an older owner is asked to stand down, only by a pass that may,
        # and never one already written off.
        asks = [e for e in decided if isinstance(e, lookup.AskToStandDown)]
        assert len(asks) <= 1, where
        if asks:
            assert may_ask and acted_on is not None and acted_on.older, where
            assert acted_on.instance not in buried, where
        assert conclusion.asked == bool(asks), where
        # And every such owner is, whatever its configuration: one left running
        # keeps the browser from every newer frontend.
        if acted_on is not None and acted_on.older and acted_on.instance not in buried:
            assert asks or not may_ask, where
            assert lookup.Bury(str(acted_on.instance)) in burials, where

        # Attaching needs an answer to a probe.
        if conclusion.finding is Finding.AS_READ and acted_on and acted_on.attachable:
            assert reaches == [Reach.ANSWERED], where

        # A same-version owner of another configuration ends in Direct at once
        # whenever it answered or stayed silent, and is left alone.
        if acted_on == RIVAL and acted_on.instance not in buried and reaches:
            if reaches[0] is not Reach.REFUSED:
                assert conclusion.finding in (
                    Finding.LIVE_RIVAL,
                    Finding.SILENT_RIVAL,
                ), where
                assert not asks and not burials, where
        if conclusion.finding in (Finding.LIVE_RIVAL, Finding.SILENT_RIVAL):
            assert acted_on == RIVAL and reaches and reaches[0] is not Reach.REFUSED

        # A newer or unknown version of another configuration is only waited
        # out: never probed, asked or written off.
        if acted_on == NEW_RIVAL:
            assert not probes and not asks and not burials, where


# --- Schedules: one election -------------------------------------------------------


def _election_replies(effect: core.Effect, now: float) -> list[core.Event]:
    steps = (0.1, 6.0, 20.0)
    if isinstance(effect, core.Look):
        return [
            looked(
                effect.op,
                now + dt,
                f"seen@{now + dt}",
                conclusive=c,
                asked=a,
                observed=o,
            )
            for dt in steps
            for c in (False, True)
            for a in ((False, True) if effect.may_ask else (False,))
            for o in (True, False)
            if not (c and not o)
        ]
    assert isinstance(effect, core.StartOwner)
    return [core.StartEnded(effect.op, now + 0.1, attempt) for attempt in Attempt] + [
        core.StartRefused(effect.op, now + 0.1, lock_unusable=lock)
        for lock in (True, False)
    ]


def _election_schedules() -> Iterator[list[tuple[core.Effect, core.Event | None]]]:
    def explore(state: core.Election, effects: list[core.Effect], path, now: float):
        [effect] = effects
        if isinstance(effect, core.Decide) or len(path) >= 6:
            yield [*path, (effect, None)]
            return
        for event in _election_replies(effect, now):
            after, decided = core.on_event(state, event)
            yield from explore(after, decided, [*path, (effect, event)], event.now)

    state, effects = core.elect(
        now=0.0,
        deadline_seconds=10.0,
        settlement_seconds=5.0,
        pacing=PACING,
        nothing_seen="nothing",
    )
    yield from explore(state, effects, [], 0.0)


def test_every_election_keeps_the_contract():
    schedules = list(_election_schedules())
    assert len(schedules) > 10_000

    for path in schedules:
        where = [
            (type(effect).__name__, type(event).__name__ if event else None)
            for effect, event in path
        ]
        effects = [effect for effect, _ in path]
        events = [event for _, event in path if event is not None]
        decided = isinstance(effects[-1], core.Decide)

        # One decision, and nothing after it.
        assert sum(isinstance(e, core.Decide) for e in effects) <= 1, where

        refused_at: float | None = None
        asked = False
        last_start: float | None = None
        last_seen: object = "nothing"
        for i, (effect, event) in enumerate(path):
            previous = path[i - 1][1] if i else None
            issued_at = 0.0 if previous is None else previous.now
            if isinstance(effect, core.StartOwner):
                # Never after the active deadline, never after a refusal, and
                # never two children inside the first delay.
                assert issued_at < 10.0, where
                assert refused_at is None, where
                if last_start is not None:
                    assert issued_at - last_start >= PACING.first_delay - 1e-9, where
                last_start = issued_at
                # The child gets what is left of the active budget, no more.
                assert effect.timeout == pytest.approx(10.0 - issued_at), where
            if isinstance(effect, core.Look):
                # Settlement observes: no request, and never past its deadline.
                if issued_at >= 10.0 or refused_at is not None:
                    assert not effect.may_ask, where
                if asked:
                    assert not effect.may_ask, where
                assert effect.deadline <= 15.0, where
                assert issued_at < effect.deadline, where
            if isinstance(effect, core.Decide):
                previous = path[i - 1][1] if i else None
                if isinstance(previous, core.Looked) and previous.conclusive:
                    # Attach, or Direct, at once with what was found.
                    assert effect.lookup == previous.lookup, where
                elif isinstance(previous, core.StartRefused) and previous.lock_unusable:
                    # An unusable lock: Direct now, with the last thing seen.
                    assert effect.lookup == last_seen, where
                else:
                    # Otherwise the election used its budget first.
                    assert effect.lookup == last_seen, where
                    tail = 15.0 if refused_at is None else min(15.0, refused_at + 5.0)
                    assert previous is not None and previous.now >= tail - 1e-9, where
                assert effect.started == any(
                    isinstance(e, core.StartEnded) and e.attempt is Attempt.STARTED
                    for e in events
                ), where
            if isinstance(event, core.Looked):
                asked = asked or event.asked
                if event.observed:
                    last_seen = event.lookup
                if event.conclusive:
                    # A conclusive pass is always the last.
                    after = path[i + 1][0]
                    assert isinstance(after, core.Decide), where
                    assert after.lookup == event.lookup, where
            if isinstance(event, core.StartRefused) or (
                isinstance(event, core.StartEnded) and event.attempt is Attempt.ABORTED
            ):
                refused_at = event.now
                if isinstance(event, core.StartRefused) and event.lock_unusable:
                    assert isinstance(path[i + 1][0], core.Decide), where
        if not decided:
            assert len(path) >= 6
