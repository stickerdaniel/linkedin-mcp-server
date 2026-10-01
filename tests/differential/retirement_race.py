"""Calls racing the owner's retirement: its idle exit (H-R13) and a turnover.

Both read ``get_person_profile`` with ``sections="experience,education"``,
the calibrated read (``call_loss``), for a username of the row's own, and hold
its pages at the synthetic origin (``SyntheticOrigin.hold``), never longer
than ``HOLD_CAP_SECONDS`` each, below the gate's deadline.

**H-R13** declares an idle timeout of its own, ``IDLE_RACE_TIMEOUT_SECONDS``,
the same in K1 frozen, K3 and K0. It has to exceed the time from the owner's
publication to the first call it admits, or the owner retires before the row
began: then the row's owner was replaced before the race, which is invalid
evidence, never a finding. The margin is derived from the packet
(``idle_margins``): the descriptor's write time against the warm-up's send,
and the warm-up's end against the read's.

* **Admission wins** (``H-R13-admission``): the read is sent at once after
  the warm-up and its profile page is held from before the idle threshold
  until after it and the owner's connection grace (``OWNER_GRACE_SECONDS``),
  so an idle decision that ignored the call in flight would have cut it. The
  read must complete on the original owner, the browser's lifetime unchanged
  across the hold, with no refusal in between; only afterwards does the
  owner idle out, shown by its idle-exit line and its exit. K1: Direct's
  conditional idle close must not close the browser under the held call, and
  closes it only afterwards.
* **Retirement wins** (``H-R13-retirement``): the row waits for positive
  retirement, the owner's idle-exit line (and its exit, where seen), and
  only then calls through the still-live frontend. The call either recovers
  to a verified successor that this call started and that read the row's own
  pages, or fails explicitly; both are safe and kept apart as branches
  (``DELIVERED``, ``FAILED``). How the frontend classified the retired owner,
  ``retiring`` or unanswered, is recorded from its own output when it says.
  K1: the browser idled closed and the next call reopens it.

**Turnover** (daemon only): the row sends the owner the authenticated
stand-down with no body, the legacy unconditional form a newer build uses,
reading the token from the row's own auth root and never recording it. Its
30 s drain and unknown-outcome answer are the contract's policy, so K1 is
recorded not applicable (``K1_NOT_APPLICABLE``). These lanes turn over an
owner of this build and do not discharge W6, the upgrade of an
older-protocol owner (``W6_NOT_DISCHARGED``).

* **Drain** (``H-TURNOVER-drain``): admitted held work, released well inside
  the drain, completes normally; the owner then stands down, cutting nothing.
* **Refused** (``H-TURNOVER-refused``): with the drain still running, a new
  read is refused and recovered to a successor, or fails explicitly, and
  never runs on the retiring owner: none of its pages before that owner is
  seen gone.
* **Cut** (``H-TURNOVER-cut``): two held pages, each below the gate's
  deadline, carry the read past the drain. The owner cuts it, and the caller
  gets ``outcome_unknown`` with ``retry_safe`` false; nothing of the read goes
  on after the cut, and it is never replayed.
* **Queued** (``H-TURNOVER-queued``): as cut, with a second read sent before
  the stand-down and queued behind the held one. Cut before its body began,
  it gets the owner's signed not-run refusal and is recovered to a successor
  (or fails explicitly), told apart from the begun read's unknown outcome.

Every attempt the frontend reports in its own output becomes an ``attempt``
event; nothing is counted from browser navigations. A deadline at a gate, a
retirement never seen, a stand-down never answered, a cut never made or a
reading taken out of its window is invalid evidence (``INVALID``), apart
from a finding. K2 is recorded not applicable for every row here.

The scripts run on a ``harness.RowContext`` with its ``race`` seams; nothing
here reads a process, and the verdicts read the raw record alone, so each can
be replayed from the published packet.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from differential.call_loss import (
    CALIBRATION_IDLE_TIMEOUT_SECONDS,
    ENTRY_SECONDS,
    EXPECTED_SECTIONS,
    GATE_END_SECONDS,
    INVALID,
    PERSON_TOOL,
    _entered_or_ended,
    _phase,
    _read_of,
    _release_at,
    _sleep_until,
)
from differential.host_comparison import (
    _of_launch,
    host_problems,
    owner_launches,
    same_lifetime,
)
from differential.synthetic_origin import (
    DEADLINE,
    GATE_DEADLINE_SECONDS,
    PEER_GONE,
    RELEASED_BY_ROW,
    SERVED,
    Gate,
    person_path,
)

if TYPE_CHECKING:
    from differential.harness import RaceSeams, RowContext

ROW_ADMISSION = "H-R13-admission"
ROW_RETIREMENT = "H-R13-retirement"
ROW_DRAIN = "H-TURNOVER-drain"
ROW_REFUSED = "H-TURNOVER-refused"
ROW_CUT = "H-TURNOVER-cut"
ROW_QUEUED = "H-TURNOVER-queued"
IDLE_ROWS = (ROW_ADMISSION, ROW_RETIREMENT)

#: The row's first call, the harness's warm-up read (``harness.READ_TOOL``).
WARM_TOOL = "get_feed"

#: Each row's own username, so no request of one row stands for another's;
#: the second reads' are their own as well.
USERNAMES = {
    ROW_ADMISSION: "synthetic-admission",
    ROW_RETIREMENT: "synthetic-retirement",
    ROW_DRAIN: "synthetic-drain",
    ROW_REFUSED: "synthetic-refused",
    ROW_CUT: "synthetic-cut",
    ROW_QUEUED: "synthetic-queued",
}
SECOND_USERNAMES = {
    ROW_REFUSED: "synthetic-refused-new",
    ROW_QUEUED: "synthetic-queued-behind",
}

#: H-R13's idle timeout: a declared scenario setting, the same in K1, K3 and
#: K0. A candidate the plan names, to be confirmed on every runner by the
#: margins the packet records (``idle_margins``).
IDLE_RACE_TIMEOUT_SECONDS = 8.0
#: The turnover lanes keep the calibration's configuration.
TURNOVER_IDLE_TIMEOUT_SECONDS = CALIBRATION_IDLE_TIMEOUT_SECONDS
#: The owner's connection grace on shutdown, uvicorn's
#: ``timeout_graceful_shutdown`` as ``daemon_owner.create_owner_server`` sets
#: it. An idle exit that ignored a call in flight would cut it by then.
OWNER_GRACE_SECONDS = 5.0
#: How far past the idle threshold and the grace the admission hold lasts.
PAST_THRESHOLD_SECONDS = 2.0
#: The longest any one hold lasts, two seconds inside the gate's deadline.
HOLD_CAP_SECONDS = GATE_DEADLINE_SECONDS - 2.0
#: How long the row waits, beyond the idle timeout, for a retirement to show.
RETIREMENT_EVIDENCE_SECONDS = 60.0
#: How long an owner that logged its idle exit is given to be gone.
OWNER_EXIT_SECONDS = 60.0
#: How long a browser is given to be gone once its close was logged.
BROWSER_GONE_SECONDS = 60.0
#: How long a scripted read may take to end: an election (90 s) and a read.
CALL_END_SECONDS = 180.0
#: How far apart a process's create time and the harness's wall clock may be
#: read: psutil derives one on Linux from a boot time in whole seconds.
START_TOLERANCE_SECONDS = 1.0

#: The contract's drain: how long admitted calls may run after a turnover
#: (``daemon_owner._TURNOVER_DRAIN_SECONDS``).
TURNOVER_DRAIN_SECONDS = 30.0
#: How long the owner is given, from the stand-down's answer, to be gone:
#: the drain, the owner's bounded stop (30 s) and as much again.
TURNOVER_EXIT_SECONDS = 90.0
#: How long past the drain the second hold of a read that outlasts it lasts.
CUT_MARGIN_SECONDS = 3.0
#: How long a queued read is given, once sent, to reach the owner before the
#: stand-down. Nothing observes that it did until the owner's cut count.
QUEUE_SECONDS = 2.0
#: How long after the stand-down's answer the new work is sent.
AFTER_SECONDS = 1.0

#: How a second read meets the turnover: sent after the stand-down, or sent
#: before it and queued behind the held read.
AFTER = "after"
QUEUED = "queued"


@dataclass(frozen=True)
class TurnoverCase:
    """One turnover lane: when its first held page is let go after the
    stand-down, whether a second hold carries the read past the drain, and
    its second read, if any."""

    first_release: float
    past_drain: bool = False
    second: str | None = None

    @property
    def successor(self) -> bool:
        """Whether a replacement owner may serve: only a second read calls
        after the turnover."""
        return self.second is not None


TURNOVER_CASES: dict[str, TurnoverCase] = {
    ROW_DRAIN: TurnoverCase(first_release=8.0),
    ROW_REFUSED: TurnoverCase(first_release=8.0, second=AFTER),
    ROW_CUT: TurnoverCase(first_release=14.0, past_drain=True),
    ROW_QUEUED: TurnoverCase(first_release=14.0, past_drain=True, second=QUEUED),
}

K2_NOT_APPLICABLE = {
    "status": "not applicable",
    "reason": (
        "the plan names a historical-daemon regression witness only for R6, R7, "
        "R11 and R12, none for a call racing the owner's retirement, and the "
        "contract forbids inventing one"
    ),
}
K1_NOT_APPLICABLE = {
    "status": "not applicable",
    "reason": (
        "policy, not parity: the contract states the 30s drain and the unknown "
        "outcome after it as a policy to implement and test, and Direct has no "
        "turnover to compare it with"
    ),
}
W6_NOT_DISCHARGED = (
    "not discharged: these lanes turn over an owner of this build; the upgrade "
    "of an older-protocol owner stays W6"
)

#: What the owner logs as it idles out (``daemon_owner``), and what Direct
#: logs as it closes an idle browser (``drivers.browser``).
OWNER_IDLE_LINE = "Nothing has needed the browser in"
DIRECT_IDLE_LINE = "Closing idle browser after"
#: What the owner logs on a turnover, and as its drain runs out.
TURNOVER_LINE = "A newer build asked for the browser; standing down"
CUT_COUNT = re.compile(r"Requested stand-down cancellation of (\d+) pending call")
CUT_CALL_LINE = "Cutting off call"

#: Each attempt the frontend reports (``daemon_proxy``), by the start of its
#: line, and the attempt it is (``events.ATTEMPTS``).
_ATTEMPT_LINES = (
    ("The shared browser owner refused the call preflight", "preflight"),
    ("The shared browser owner did not answer the call preflight", "preflight"),
    ("The shared browser owner refused the call before running it", "dispatch"),
    ("Attached to a replacement shared browser owner", "election"),
    ("No shared browser owner could be established", "election"),
    ("Attached to a replacement owner; running the call again", "replay"),
)
_PREFLIGHT_CLASS = re.compile(r"\(HTTP \d+, ([a-z_]+)\)")
_DISPATCH_CLASS = re.compile(r"before running it \(([a-z_]+)\)")
#: A preflight the owner never answered: the frontend's output does not say
#: whether that was ``unreachable`` or ``owner_error``.
UNANSWERED = "unanswered"
#: The classifications a retiring or retired owner may get.
RETIRED_CLASSES = frozenset({"retiring", UNANSWERED})

#: How a call after a retirement ended: a correct read through whoever
#: serves, an explicit failure, or neither, which is a silent cut.
DELIVERED = "delivered"
FAILED = "failed"
SILENT = "silent"
#: K0's projection of both safe branches.
NO_SILENT_CUT = "no silent cut"
#: How the turnover's first read is classified.
COMPLETED = "completed"
UNKNOWN_OUTCOME = "outcome_unknown"


def _s(seconds: float) -> int:
    return int(seconds * 1e9)


# --- Readings the scripts take --------------------------------------------------------


def attempts_in(lines: Sequence[str]) -> list[dict[str, Any]]:
    """Each attempt the frontend reported in *lines*, in order, with the
    classification its line names, or None where it names none."""
    found = []
    for line in lines:
        for marker, attempt in _ATTEMPT_LINES:
            if marker not in line:
                continue
            classification: str | None = None
            if attempt == "preflight":
                match = _PREFLIGHT_CLASS.search(line)
                classification = match.group(1) if match else None
            elif attempt == "dispatch":
                match = _DISPATCH_CLASS.search(line)
                classification = match.group(1) if match else None
            elif attempt == "election":
                classification = "attached" if "Attached" in marker else "none"
            found.append({"attempt": attempt, "classification": classification})
            break
    return found


def call_classification(attempts: Any) -> str | None:
    """How the frontend classified the owner it met first, from its output:
    a refusal's class, ``UNANSWERED``, or None when it reported neither."""
    for item in _sequence(attempts):
        item = _mapping(item)
        if item.get("attempt") in ("preflight", "dispatch"):
            return item.get("classification") or UNANSWERED
    return None


def owner_log_reading(lines: Sequence[str]) -> dict[str, Any]:
    """What the owner's log shows of a turnover after the row's mark."""
    counts = [int(m.group(1)) for line in lines if (m := CUT_COUNT.search(line))]
    return {
        "turned_over": any(TURNOVER_LINE in line for line in lines),
        "cut": counts[0] if counts else 0,
        "cut_lines": sum(1 for line in lines if CUT_CALL_LINE in line),
    }


async def _line_within(
    source: Callable[[], list[str]], marker: str, mark: int, seconds: float
) -> int | None:
    """When *marker* is first seen in *source* past its first *mark* lines,
    within *seconds*, on the monotonic clock; None if it never is."""
    deadline = time.monotonic() + seconds
    while True:
        if any(marker in line for line in source()[mark:]):
            return time.monotonic_ns()
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.1)


async def _release_held(gate: Gate, at_ns: int) -> None:
    """Let *gate* go at *at_ns*, or sooner if it entered too early to be held
    that long; at *at_ns* anyway if it never entered."""
    while not gate.entered.is_set() and time.monotonic_ns() < at_ns:
        await asyncio.sleep(0.01)
    entered = gate.entered_monotonic_ns
    if entered is not None:
        at_ns = min(at_ns, entered + _s(HOLD_CAP_SECONDS))
    await _sleep_until(at_ns)
    gate.release(by=RELEASED_BY_ROW)


def _emit_attempts(ctx: RowContext, label: str, attempts: Sequence[Any]) -> None:
    for item in attempts:
        ctx.emit("frontend", "attempt", call=label, **dict(item))


async def _settle_tasks(tasks: Sequence[asyncio.Future[Any]]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# --- H-R13: the scripts ---------------------------------------------------------------

#: The page the admission race holds: the read's first, so the read is
#: admitted and at the origin before the idle threshold.
HELD_PAGE = "main_profile"


async def idle_script(ctx: RowContext) -> None:
    """H-R13's scripted phase, right after the warm-up read: the quiet period
    starts as the warm-up ends, and this is the row's anchor for it."""
    record = ctx.record
    username = USERNAMES[ctx.row]
    record.update(username=username, grace_seconds=OWNER_GRACE_SECONDS)
    race = ctx.race
    if race is None:
        record["observation_problems"].append(
            f"{INVALID}the row was given no way to observe its owner"
        )
        return
    anchor = time.monotonic_ns()
    owner = ctx.owner()
    record.update(
        anchor_ns=anchor,
        owner_identified=(
            [owner.pid, owner.create_time, owner.instance_id] if owner else None
        ),
        script_began=time.time(),
        published=race.published(),
        attempts_before=attempts_in(race.host_output()),
    )
    if ctx.row == ROW_ADMISSION:
        await _admission(ctx, race, username, anchor)
    else:
        await _retirement(ctx, race, username)


async def _admission(
    ctx: RowContext, race: RaceSeams, username: str, anchor: int
) -> None:
    record = ctx.record
    problems: list[str] = record["observation_problems"]
    idle = float(record["idle_timeout_seconds"])
    held = person_path(username, HELD_PAGE)
    record["held"] = {
        "path": held,
        "ordinal": 1,
        "deadline_seconds": GATE_DEADLINE_SECONDS,
    }
    threshold = anchor + _s(idle + OWNER_GRACE_SECONDS)
    gate = ctx.hold(held, ordinal=1)
    _phase(ctx, "armed")
    mark = len(race.host_output())
    read = asyncio.ensure_future(ctx.call(PERSON_TOOL, _read_of(username)))
    tasks: list[asyncio.Future[Any]] = [read]
    try:
        if not await _entered_or_ended(gate, read):
            problems.append(
                f"{INVALID}the held page was not requested within {ENTRY_SECONDS}s "
                f"of arming, or the read ended first: nothing was raced"
            )
            return
        entered = gate.entered_monotonic_ns
        assert entered is not None
        _phase(ctx, "entered", entered)
        release_at = min(
            threshold + _s(PAST_THRESHOLD_SECONDS), entered + _s(HOLD_CAP_SECONDS)
        )
        record["release"] = {"threshold_ns": threshold, "scheduled_ns": release_at}
        releasing = asyncio.ensure_future(_release_at(gate, release_at))
        tasks.append(releasing)
        record["roots"] = [await race.roots("hold entered")]
        await _sleep_until(threshold)
        _phase(ctx, "past the threshold")
        record["roots"].append(await race.roots("past the threshold"))
        await releasing
        _phase(ctx, "released", gate.release_requested_monotonic_ns)
        await asyncio.wait({read}, timeout=CALL_END_SECONDS)
        record["read_open"] = not read.done()
        _phase(ctx, "returned")
        window = race.host_output()[mark:]
        attempts = attempts_in(window)
        record["read_window"] = {
            "attempts": attempts,
            "idle_closes": sum(1 for line in window if DIRECT_IDLE_LINE in line),
        }
        _emit_attempts(ctx, "held read", attempts)
        if not await ctx.ended(gate, GATE_END_SECONDS):
            problems.append(
                f"{INVALID}the released hold recorded no end within {GATE_END_SECONDS}s"
            )
        if ctx.daemon:
            record["owner_after_read"] = await race.owner_reading("after the read")
        # Only now may the owner, or Direct's browser, idle out.
        logged, output = len(race.owner_log()), len(race.host_output())
        if ctx.daemon:
            seen_exit = await race.owner_exit(idle + RETIREMENT_EVIDENCE_SECONDS)
            record["after"] = {
                "idle_lines": sum(
                    1 for line in race.owner_log()[logged:] if OWNER_IDLE_LINE in line
                ),
                "exit": seen_exit,
            }
        else:
            seen = await _line_within(
                race.host_output,
                DIRECT_IDLE_LINE,
                output,
                idle + RETIREMENT_EVIDENCE_SECONDS,
            )
            record["after"] = {
                "idle_lines": 0 if seen is None else 1,
                "browser_gone": await race.browser_gone(BROWSER_GONE_SECONDS),
            }
        _phase(ctx, "retired")
    finally:
        await _settle_tasks(tasks)


async def _retirement(ctx: RowContext, race: RaceSeams, username: str) -> None:
    record = ctx.record
    problems: list[str] = record["observation_problems"]
    idle = float(record["idle_timeout_seconds"])
    if ctx.daemon:
        seen = await _line_within(
            race.owner_log,
            OWNER_IDLE_LINE,
            len(race.owner_log()),
            idle + RETIREMENT_EVIDENCE_SECONDS,
        )
    else:
        seen = await _line_within(
            race.host_output,
            DIRECT_IDLE_LINE,
            len(race.host_output()),
            idle + RETIREMENT_EVIDENCE_SECONDS,
        )
    retirement: dict[str, Any] = {"line_seen_ns": seen}
    record["retirement"] = retirement
    if seen is None:
        problems.append(
            f"{INVALID}no positive retirement evidence: the "
            f"{'owner' if ctx.daemon else 'Direct server'}'s idle line was never "
            f"seen, so no call was made after it"
        )
        return
    _phase(ctx, "retired", seen)
    if ctx.daemon:
        retirement["exit"] = await race.owner_exit(OWNER_EXIT_SECONDS)
    else:
        retirement["browser_gone"] = await race.browser_gone(BROWSER_GONE_SECONDS)
    record["roots"] = [await race.roots("retired")]
    mark = len(race.host_output())
    _phase(ctx, "called")
    read = asyncio.ensure_future(ctx.call(PERSON_TOOL, _read_of(username)))
    try:
        await asyncio.wait({read}, timeout=CALL_END_SECONDS)
        record["read_open"] = not read.done()
        _phase(ctx, "returned")
        attempts = attempts_in(race.host_output()[mark:])
        record["read_window"] = {"attempts": attempts}
        _emit_attempts(ctx, "after the retirement", attempts)
        record["roots"].append(await race.roots("after the call"))
        if ctx.daemon:
            record["owner_after_read"] = await race.owner_reading("after the call")
    finally:
        await _settle_tasks([read])


# --- Turnover: the script -------------------------------------------------------------


async def turnover_script(ctx: RowContext) -> None:
    """A turnover lane's scripted phase, after the warm-up read.

    Arm the holds and read; once the first held page entered (the read's
    body has begun), send the second read where it queues, ask the owner to
    stand down, and schedule every release from the stand-down's answer.
    Then the new work where the lane has it, every read's end, the owner's
    exit, watched from the answer on, the owner's log, and who serves now.
    """
    case = TURNOVER_CASES[ctx.row]
    record = ctx.record
    problems: list[str] = record["observation_problems"]
    username = USERNAMES[ctx.row]
    second = SECOND_USERNAMES.get(ctx.row)
    record.update(
        username=username,
        case={
            "first_release_seconds": case.first_release,
            "past_drain": case.past_drain,
            "second": case.second,
        },
        k1=dict(K1_NOT_APPLICABLE),
        w6=W6_NOT_DISCHARGED,
        drain_seconds=TURNOVER_DRAIN_SECONDS,
    )
    if second is not None:
        record["second_username"] = second
    race = ctx.race
    if not ctx.daemon or race is None or race.stand_down is None:
        problems.append(
            f"{INVALID}a turnover lane runs only through an owner the row may ask "
            f"to stand down"
        )
        return
    owner = ctx.owner()
    record["owner_identified"] = (
        [owner.pid, owner.create_time, owner.instance_id] if owner else None
    )
    record["script_began"] = time.time()
    if owner is None:
        problems.append(f"{INVALID}the owner was never identified")
        return
    gates = [ctx.hold(person_path(username, "main_profile"), ordinal=1)]
    if case.past_drain:
        gates.append(ctx.hold(person_path(username, "experience"), ordinal=1))
    record["held"] = [
        {"path": gate.path, "ordinal": 1, "deadline_seconds": GATE_DEADLINE_SECONDS}
        for gate in gates
    ]
    _phase(ctx, "armed")
    reads = [asyncio.ensure_future(ctx.call(PERSON_TOOL, _read_of(username)))]
    tasks: list[asyncio.Future[Any]] = []
    try:
        if not await _entered_or_ended(gates[0], reads[0]):
            problems.append(
                f"{INVALID}the first held page was not requested within "
                f"{ENTRY_SECONDS}s of arming, or the read ended first"
            )
            return
        entered = gates[0].entered_monotonic_ns
        assert entered is not None
        _phase(ctx, "entered", entered)
        second_mark: int | None = None
        if second is not None and case.second == QUEUED:
            second_mark = len(race.host_output())
            reads.append(asyncio.ensure_future(ctx.call(PERSON_TOOL, _read_of(second))))
            await asyncio.sleep(QUEUE_SECONDS)
        logged = len(race.owner_log())
        stood = await race.stand_down()
        record["stand_down"] = stood
        answered = stood.get("answered_ns")
        if (
            type(answered) is not int
            or stood.get("status") != 200
            or stood.get("standing_down") is not True
        ):
            problems.append(
                f"{INVALID}the owner was not shown asked to stand down: status "
                f"{stood.get('status')!r}, error {stood.get('error')!r}"
            )
            return
        _phase(ctx, "stood down", answered)
        exiting = asyncio.ensure_future(race.owner_exit(TURNOVER_EXIT_SECONDS))
        tasks.append(exiting)
        first_at = min(
            answered + _s(case.first_release), entered + _s(HOLD_CAP_SECONDS)
        )
        release: dict[str, Any] = {"first_ns": first_at}
        tasks.append(asyncio.ensure_future(_release_at(gates[0], first_at)))
        if case.past_drain:
            release["second_ns"] = answered + _s(
                TURNOVER_DRAIN_SECONDS + CUT_MARGIN_SECONDS
            )
            tasks.append(
                asyncio.ensure_future(_release_held(gates[1], release["second_ns"]))
            )
        record["release"] = release
        if second is not None and case.second == AFTER:
            await asyncio.sleep(AFTER_SECONDS)
            second_mark = len(race.host_output())
            reads.append(asyncio.ensure_future(ctx.call(PERSON_TOOL, _read_of(second))))
        _, still_open = await asyncio.wait(reads, timeout=CALL_END_SECONDS)
        record["calls_open"] = len(still_open)
        _phase(ctx, "returned")
        record["owner_exit"] = await exiting
        if record["owner_exit"].get("how") == "exited":
            _phase(ctx, "owner gone", record["owner_exit"].get("seen_ns"))
        await asyncio.wait(tasks[1:], timeout=HOLD_CAP_SECONDS + GATE_END_SECONDS)
        for gate in gates:
            if gate.entered.is_set() and not await ctx.ended(gate, GATE_END_SECONDS):
                problems.append(
                    f"{INVALID}the hold on {gate.path} recorded no end within "
                    f"{GATE_END_SECONDS}s"
                )
        record["owner_log"] = owner_log_reading(race.owner_log()[logged:])
        if second_mark is not None:
            attempts = attempts_in(race.host_output()[second_mark:])
            record["second_attempts"] = attempts
            _emit_attempts(ctx, "second read", attempts)
        if case.successor:
            record["owner_after"] = await race.owner_reading("after the second read")
    finally:
        await _settle_tasks([*tasks, *reads])


# --- Reading a record -----------------------------------------------------------------


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, str) else ()


def _ns(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _calls(record: Mapping[str, Any], tool: str) -> list[Mapping[str, Any]]:
    return [
        _mapping(call)
        for call in _sequence(record.get("calls"))
        if _mapping(call).get("tool") == tool
    ]


def _warm(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    calls = [_mapping(call) for call in _sequence(record.get("calls"))]
    first = calls[0] if calls else {}
    if first.get("tool") != WARM_TOOL or first.get("outcome") != "returned":
        return None
    if _ns(first.get("ended_monotonic_ns")) is None:
        return None
    return first


def _gates(record: Mapping[str, Any], path: str) -> list[Mapping[str, Any]]:
    return [
        _mapping(gate)
        for gate in _sequence(record.get("gates"))
        if _mapping(gate).get("path") == path
    ]


def _arrivals(record: Mapping[str, Any], path: str) -> list[int | None]:
    return [
        _ns(_mapping(request).get("monotonic_ns"))
        for request in _sequence(record.get("requests"))
        if _mapping(request).get("path") == path
    ]


def _pages(record: Mapping[str, Any], username: str) -> dict[str, list[int | None]]:
    return {
        section: _arrivals(record, person_path(username, section))
        for section in EXPECTED_SECTIONS
    }


def _read_ok(call: Mapping[str, Any]) -> bool:
    marked = set(_sequence(call.get("marked_sections")))
    return (
        call.get("outcome") == "returned"
        and call.get("is_error") is False
        and set(EXPECTED_SECTIONS) <= marked
        and not set(_sequence(call.get("section_errors"))) & set(EXPECTED_SECTIONS)
    )


def branch(call: Mapping[str, Any] | None) -> str:
    """How a call after a retirement ended (``DELIVERED``, ``FAILED`` or
    ``SILENT``). An error the caller sees, raised or returned, is explicit,
    and so is a result that names every section it could not read in its
    ``section_errors``; a result that looks like success without its data, a
    cancellation or no end at all is not."""
    call = _mapping(call)
    if _read_ok(call):
        return DELIVERED
    outcome = call.get("outcome")
    if outcome == "raised" or (outcome == "returned" and call.get("is_error") is True):
        return FAILED
    if outcome == "returned" and call.get("is_error") is False:
        marked = set(_sequence(call.get("marked_sections")))
        errors = set(_sequence(call.get("section_errors")))
        missing = set(EXPECTED_SECTIONS) - marked
        if missing and missing <= errors:
            return FAILED
    return SILENT


def _interval(call: Mapping[str, Any]) -> tuple[int, int] | None:
    began, ended = (
        _ns(call.get("began_monotonic_ns")),
        _ns(call.get("ended_monotonic_ns")),
    )
    if began is None or ended is None or ended < began:
        return None
    return began, ended


def _page_problems(
    record: Mapping[str, Any],
    username: str,
    call: Mapping[str, Any],
    *,
    after: int | None = None,
    after_label: str = "",
    held: str | None = None,
) -> list[str]:
    """The read's own pages: each once, inside its interval and, with
    *after*, after that time, all but the *held* one, which arrived before
    its own release. What lets a read be credited to this call and not to
    another one."""
    interval = _interval(call)
    if interval is None:
        return ["the read's times are missing or out of order"]
    found = []
    for section, arrivals in _pages(record, username).items():
        if len(arrivals) != 1:
            found.append(
                f"the {section} page was requested {len(arrivals)} times, not once"
            )
            continue
        at = arrivals[0]
        if at is None or not interval[0] <= at <= interval[1]:
            found.append(
                f"the {section} page was not requested inside the read's interval"
            )
        elif after is not None and section != held and at <= after:
            found.append(f"the {section} page was requested before {after_label}")
    return found


def _session_problems(record: Mapping[str, Any], usernames: Sequence[str]) -> list[str]:
    paths = {
        person_path(name, section)
        for name in usernames
        for section in EXPECTED_SECTIONS
    }
    unsigned = sorted(
        {
            str(_mapping(request).get("path"))
            for request in _sequence(record.get("requests"))
            if _mapping(request).get("path") in paths
            and _mapping(request).get("session_valid") is not True
        }
    )
    return (
        [f"these pages did not carry the staged session: {unsigned}"]
        if unsigned
        else []
    )


def _common(
    record: Mapping[str, Any], *, rows: Sequence[str], daemon: bool, idle: float
) -> list[str]:
    problems: list[str] = []
    mode = "daemon" if daemon else "direct"
    if record.get("mode") != mode:
        problems.append(f"the record is for mode {record.get('mode')!r}, not {mode}")
    if record.get("script_error"):
        problems.append(f"the row's script failed: {record['script_error']}")
    problems += [str(p) for p in _sequence(record.get("observation_problems"))]
    if record.get("idle_timeout_seconds") != idle:
        problems.append(
            f"the row ran with an idle timeout of "
            f"{record.get('idle_timeout_seconds')!r}, not the declared {idle}"
        )
    if record.get("k2") != K2_NOT_APPLICABLE:
        problems.append("the record does not say why K2 is not applicable")
    problems += host_problems(record.get("host"))
    row = record.get("row")
    if row in rows and record.get("username") != USERNAMES[str(row)]:
        problems.append(
            f"{INVALID}the record names {record.get('username')!r} as its username"
        )
    # Every hold, by when it let go rather than its label: a handler resumed
    # late still records ``served`` after the gate's deadline.
    for gate in _sequence(record.get("gates")):
        entered = _ns(_mapping(gate).get("entered_monotonic_ns"))
        let_go = _ns(_mapping(gate).get("released_monotonic_ns"))
        if entered is None:
            continue
        if let_go is None:
            problems.append(
                f"{INVALID}the hold on {_mapping(gate).get('path')} has no end time"
            )
        elif (let_go - entered) / 1e9 > GATE_DEADLINE_SECONDS:
            problems.append(
                f"{INVALID}the hold on {_mapping(gate).get('path')} let go "
                f"{(let_go - entered) / 1e9:.1f}s after its entry, past the "
                f"gate's {GATE_DEADLINE_SECONDS}s deadline"
            )
    return problems


def _identified(record: Mapping[str, Any]) -> Sequence[Any] | None:
    identified = _sequence(record.get("owner_identified"))
    return identified if len(identified) == 3 else None


def _other_launches(
    record: Mapping[str, Any], owner: Sequence[Any]
) -> list[list[Any]] | None:
    """The owner launches the row started besides *owner*'s, or None unread.

    Counted as ``call_loss`` counts them, from the lifetimes recorded after
    the watcher stopped (``owner_processes``, ``gate_processes``): on Windows
    a venv launcher and the interpreter it started with the same command are
    one launch, and *owner* may be either. A release gate beyond one for each
    owner launch is a start attempted that never ran an owner. Each entry
    ends with its start, by the wall clock.
    """
    owners = record.get("owner_processes")
    gates = record.get("gate_processes")
    if not isinstance(owners, list) or not isinstance(gates, list):
        return None
    windows = str(record.get("platform", "")).startswith("win")
    launches = owner_launches(owners, windows=windows)
    others = [launch for launch in launches if not _of_launch(owner, launch, owners)]
    if len(others) == len(launches):
        # The identified owner is not among the launches at all.
        others.append(["identified owner not launched by the row", *owner])
    gate_launches = owner_launches(gates, windows=windows)
    others += [["release gate", *launch] for launch in gate_launches[len(launches) :]]
    return others


def _lost_the_lock(
    entry: Sequence[Any], record: Mapping[str, Any], owner: Sequence[Any]
) -> bool:
    """Whether the launch *entry* names, an owner launch ``[pid, start]`` or
    ``["release gate", pid, start]``, is shown to be an election candidate
    that never held the lock: every process of the launch (on Windows the
    venv launcher and the interpreter it started) seen gone by a sample that
    began while the retiring *owner* was still seen alive, and so still held
    the lock. A launch with any process not seen gone, or gone only after the
    owner, may have served and is not shown to be one.
    """
    if not entry:
        return False
    if entry[0] == "release gate":
        lifetimes, launch = _sequence(record.get("gate_processes")), entry[1:]
    elif type(entry[0]) is int:
        lifetimes, launch = _sequence(record.get("owner_processes")), entry
    else:
        return False
    owners = [_sequence(p) for p in _sequence(record.get("owner_processes"))]
    holder = [p for p in owners if same_lifetime(p[:2], owner[:2])]
    # The retiring owner's last read: the latest moment it is known alive.
    alive_until = _number(holder[0][6]) if holder and len(holder[0]) > 6 else None
    members = [
        _sequence(p)
        for p in lifetimes
        if same_lifetime(_sequence(p)[:2], launch[:2])
        or _of_launch(_sequence(p)[:2], launch, lifetimes)
    ]
    if alive_until is None or not members:
        return False
    gone = [_number(member[4]) if len(member) > 4 else None for member in members]
    return all(at is not None and at <= alive_until for at in gone)


def _started(entry: Sequence[Any]) -> float | None:
    return _number(entry[-1]) if entry else None


def _replaced_before(record: Mapping[str, Any]) -> list[str]:
    """Daemon: whether the owner was already replaced when the race began,
    which is an idle timeout too small for the runner, never a finding."""
    identified = _identified(record)
    if identified is None:
        return [f"{INVALID}the owner was never identified"]
    others = _other_launches(record, identified[:2])
    began = _number(record.get("script_began"))
    if others is None or began is None:
        return [
            f"{INVALID}the row's owner launches or its script's start were not "
            f"recorded, so a replacement before the race cannot be excluded"
        ]
    early = [entry for entry in others if (_started(entry) or 0.0) < began]
    if early:
        return [
            f"{INVALID}the owner was replaced before the race (other launches: "
            f"{early}): it retired before its first admitted call, so the idle "
            f"timeout is too small for this runner"
        ]
    return []


def _owner_kept(record: Mapping[str, Any], label: str) -> bool:
    identified = _identified(record)
    seen = _mapping(record.get(label))
    if identified is None:
        return False
    return (
        seen.get("alive") is True
        and same_lifetime(seen.get("lifetime"), identified[:2])
        and seen.get("instance_id") == identified[2]
        and _other_launches(record, identified[:2]) == []
    )


def _successor_problems(
    record: Mapping[str, Any], label: str, call: Mapping[str, Any], username: str
) -> list[str]:
    """A successor this call started, which read *username*'s pages for it:
    published after it, launched before those pages were read, the one launch the
    row made besides the retiring owner's, and launched inside the call's own
    wall interval, so it cannot be one another call elected."""
    identified = _identified(record)
    seen = _mapping(record.get(label))
    lifetime = _sequence(seen.get("lifetime"))
    if identified is None:
        return ["the retiring owner was never identified"]
    if len(lifetime) != 2:
        return [f"no successor is published after the call: {seen.get('problem')!r}"]
    found = []
    if (
        same_lifetime(lifetime, identified[:2])
        or seen.get("instance_id") == identified[2]
    ):
        found.append("the call was answered by the retiring owner, not a successor")
    others = _other_launches(record, identified[:2])
    if others is None:
        return [*found, "the row's owner and release gate lifetimes were not recorded"]
    owners = _sequence(record.get("owner_processes"))
    mine = [
        entry
        for entry in others
        if type(entry[0]) is int and _of_launch(lifetime, entry, owners)
    ]
    if len(mine) != 1:
        found.append("the successor is not an owner the row was seen to launch")
    # While the retiring owner still holds the lock, the election starts
    # candidates on its backoff (``daemon_election._owner_start_delay_after``)
    # and each one that cannot take the lock exits. Those are the election
    # doing its job; any other launch besides the successor is a second owner
    # the row cannot account for.
    extra = [
        entry
        for entry in others
        if entry not in mine and not _lost_the_lock(entry, record, identified)
    ]
    if extra:
        found.append(f"the row launched other owners besides the successor: {extra}")
    began, ended = _number(call.get("began")), _number(call.get("ended"))
    start = _started(mine[0]) if len(mine) == 1 else None
    if began is None or ended is None or start is None:
        found.append("the successor's launch cannot be placed against the call")
    elif (
        not began - START_TOLERANCE_SECONDS <= start <= ended + START_TOLERANCE_SECONDS
    ):
        found.append(
            f"the successor was not started by this call: launched "
            f"{start - began:+.1f}s from its send, outside its interval"
        )
    # The pages the call read must come after the successor existed, or the
    # successor cannot be what read them.
    pages = [
        _number(_mapping(request).get("t"))
        for request in _sequence(record.get("requests"))
        if str(_mapping(request).get("path", "")).startswith(f"/in/{username}/")
        and began is not None
        and (_number(_mapping(request).get("t")) or 0) >= began
    ]
    if start is not None and pages:
        first = min(page for page in pages if page is not None)
        if first < start - START_TOLERANCE_SECONDS:
            found.append(
                f"the call's first page was read {start - first:.1f}s before "
                f"the successor was launched: the successor did not read it"
            )
    elif start is not None:
        found.append("the call read no page after it was sent")
    return found


def _roots(record: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    found = [
        _mapping(point)
        for point in _sequence(record.get("roots"))
        if _mapping(point).get("label") == label
    ]
    return found[0] if len(found) == 1 else {}


def _classification_problems(attempts: Any, *, required: bool) -> list[str]:
    classification = call_classification(attempts)
    if classification is None:
        return ["the frontend reported no refusal of the call"] if required else []
    if classification not in RETIRED_CLASSES:
        return [
            f"the frontend classified the retiring owner as {classification!r}, not "
            f"retiring or unanswered"
        ]
    return []


# --- H-R13: the verdicts --------------------------------------------------------------


def idle_margins(record: Mapping[str, Any]) -> dict[str, float | None]:
    """How much of the idle timeout each step left, from the packet.

    ``startup``: from the descriptor's write, which precedes publication, to
    the warm-up's send, so a lower bound on what the owner's first quiet
    period left; None in Direct, which publishes nothing. ``script``: from the
    warm-up's end, where the quiet period starts, to the read's send.
    """
    idle = _number(record.get("idle_timeout_seconds"))
    warm = _warm(record) or {}
    written = _number(_mapping(record.get("published")).get("written"))
    began_wall = _number(warm.get("began"))
    reads = _calls(record, PERSON_TOOL)
    read_sent = _ns(reads[0].get("began_monotonic_ns")) if reads else None
    warm_end = _ns(warm.get("ended_monotonic_ns"))
    return {
        "startup": None
        if idle is None or written is None or began_wall is None
        else round(idle - (began_wall - written), 3),
        "script": None
        if idle is None or read_sent is None or warm_end is None
        else round(idle - (read_sent - warm_end) / 1e9, 3),
    }


def _race_invalid(record: Mapping[str, Any], *, daemon: bool) -> list[str]:
    """Shared by both races: a warm-up to time the quiet period from, and an
    owner not already replaced before the race began."""
    found = []
    if _warm(record) is None:
        found.append(
            f"{INVALID}the warm-up read is not recorded as returned, so the quiet "
            f"period's start is unknown"
        )
    if daemon:
        found += _replaced_before(record)
    if _sequence(record.get("attempts_before")):
        found.append(
            f"{INVALID}the frontend met a refusal or an election before the race: "
            f"{list(_sequence(record.get('attempts_before')))}"
        )
    return found


def admission_problems(record: Mapping[str, Any], *, daemon: bool) -> list[str]:
    """Admission wins: the held read completes on the original owner and the
    browser it started on, and the owner or Direct's browser idles out only
    afterwards."""
    idle = IDLE_RACE_TIMEOUT_SECONDS
    problems = _common(record, rows=IDLE_ROWS, daemon=daemon, idle=idle)
    problems += _race_invalid(record, daemon=daemon)
    username = USERNAMES[ROW_ADMISSION]
    warm = _warm(record) or {}
    quiet = _ns(warm.get("ended_monotonic_ns"))
    reads = _calls(record, PERSON_TOOL)
    read = reads[0] if len(reads) == 1 else None
    if read is None:
        problems.append(f"{INVALID}the record holds {len(reads)} reads, not one")
        return problems
    held = _gates(record, person_path(username, HELD_PAGE))
    gate = held[0] if len(held) == 1 else {}
    entered = _ns(gate.get("entered_monotonic_ns"))
    requested = _ns(gate.get("release_requested_monotonic_ns"))
    sent = _ns(read.get("began_monotonic_ns"))
    if not gate:
        problems.append(f"{INVALID}the record holds no gate on the held page")
    elif entered is None:
        problems.append(f"{INVALID}the held page never entered the gate")
    else:
        terminal = gate.get("terminal")
        if terminal == DEADLINE:
            problems.append(
                f"{INVALID}the hold ran out its deadline before the release"
            )
        elif terminal == PEER_GONE:
            problems.append(
                "the held request's browser went away while it was held: an idle cut"
            )
        elif terminal != SERVED:
            problems.append(f"{INVALID}the hold recorded no end: {terminal!r}")
        if gate.get("released_by") != RELEASED_BY_ROW:
            problems.append(
                f"{INVALID}the hold was released by {gate.get('released_by')!r}"
            )
    if quiet is not None:
        if sent is None or (sent - quiet) / 1e9 >= idle:
            problems.append(
                f"{INVALID}the read was not sent inside the {idle}s quiet period "
                f"after the warm-up"
            )
        if entered is not None and (entered - quiet) / 1e9 >= idle:
            problems.append(f"{INVALID}the held page entered after the idle threshold")
        if requested is None or (requested - quiet) / 1e9 < idle + OWNER_GRACE_SECONDS:
            problems.append(
                f"{INVALID}the hold did not last past the idle threshold and the "
                f"owner's {OWNER_GRACE_SECONDS}s grace: nothing was raced"
            )
    problems += _hold_roots_problems(record, warm, quiet, requested)
    # Findings.
    if not _read_ok(read):
        problems.append(
            f"the held read did not complete normally: outcome "
            f"{read.get('outcome')!r}, error {read.get('is_error')!r}, sections "
            f"{sorted(_sequence(read.get('marked_sections')))}"
        )
    problems += _page_problems(
        record,
        username,
        read,
        after=requested,
        after_label="the release",
        held=HELD_PAGE,
    )
    window = _mapping(record.get("read_window"))
    if _sequence(window.get("attempts")):
        problems.append(
            f"the frontend met a refusal or an election during the held read: "
            f"{list(_sequence(window.get('attempts')))}"
        )
    if window.get("idle_closes"):
        problems.append("Direct closed its idle browser during the held read")
    after = _mapping(record.get("after"))
    if daemon:
        if not _owner_kept(record, "owner_after_read"):
            problems.append(
                "the read did not complete on the original owner: it is not shown "
                "alive, the same lifetime and the only owner started, after the read"
            )
        seen_exit = _mapping(after.get("exit"))
        if not after.get("idle_lines") or seen_exit.get("how") != "exited":
            problems.append(
                f"the owner is not shown to idle out after the read: idle lines "
                f"{after.get('idle_lines')!r}, exit {seen_exit.get('how')!r}"
            )
    elif (
        not after.get("idle_lines")
        or _sequence(_mapping(after.get("browser_gone")).get("remaining"))
        or "remaining" not in _mapping(after.get("browser_gone"))
    ):
        problems.append("Direct is not shown to close its idle browser after the read")
    problems += _session_problems(record, [username])
    return problems


def _hold_roots_problems(
    record: Mapping[str, Any],
    warm: Mapping[str, Any],
    quiet: int | None,
    requested: int | None,
) -> list[str]:
    """The browser across the hold: one root at its entry and past the idle
    threshold, the same lifetime both times, started by the warm-up."""
    found: list[str] = []
    points = [_roots(record, "hold entered"), _roots(record, "past the threshold")]
    if not all(points):
        return [f"{INVALID}the browser was not read across the hold"]
    for point in points:
        if point.get("roots") is None:
            found.append(f"{INVALID}the browser could not be read {point.get('label')}")
    past = _ns(points[1].get("seen_ns"))
    if quiet is not None and (
        past is None
        or (past - quiet) / 1e9
        < float(record.get("idle_timeout_seconds") or 0) + OWNER_GRACE_SECONDS
    ):
        found.append(f"{INVALID}the browser was read before an idle cut could land")
    # The reading lasts from its stamp to its census being done: all of it
    # must fall inside the hold, not only its start.
    done = _ns(points[1].get("done_ns"))
    if done is None:
        found.append(f"{INVALID}the browser reading past the threshold has no end")
    elif requested is not None and done > requested:
        found.append(f"{INVALID}the browser was read after the hold ended")
    if found:
        return found
    roots = [_sequence(point.get("roots")) for point in points]
    ended = _number(warm.get("ended"))
    if (
        any(len(found_roots) != 1 for found_roots in roots)
        or not same_lifetime(roots[0][0], roots[1][0])
        or ended is None
        or (_number(_sequence(roots[0][0])[1]) or 0) > ended + START_TOLERANCE_SECONDS
    ):
        found.append(
            f"the browser's lifetime changed across the hold: "
            f"{[list(r) for r in roots]}"
        )
    return found


def retirement_problems(record: Mapping[str, Any], *, daemon: bool) -> list[str]:
    """Retirement wins: after positive retirement, the call reaches a verified
    successor or a reopened browser and reads, or fails explicitly; never a
    silent cut."""
    idle = IDLE_RACE_TIMEOUT_SECONDS
    problems = _common(record, rows=IDLE_ROWS, daemon=daemon, idle=idle)
    problems += _race_invalid(record, daemon=daemon)
    username = USERNAMES[ROW_RETIREMENT]
    retirement = _mapping(record.get("retirement"))
    seen = _ns(retirement.get("line_seen_ns"))
    if seen is None:
        problems.append(
            f"{INVALID}no positive retirement evidence before the call: no idle "
            f"line was seen"
        )
        return problems
    reads = _calls(record, PERSON_TOOL)
    if len(reads) != 1:
        problems.append(f"{INVALID}the record holds {len(reads)} reads, not one")
        return problems
    read = reads[0]
    sent = _ns(read.get("began_monotonic_ns"))
    if sent is None or sent < seen:
        problems.append(f"{INVALID}the call was not sent after the retirement was seen")
    # The idle line is the evidence asked for; the owner's exit, where the
    # row saw it before calling, is recorded beside it and judged by the
    # owner's own exit wait after the row.
    if not daemon:
        gone = _mapping(retirement.get("browser_gone"))
        if (
            _sequence(gone.get("remaining"))
            or "remaining" not in gone
            or _sequence(_roots(record, "retired").get("roots"))
        ):
            problems.append("Direct's browser is not shown closed after its idle line")
    attempts = _mapping(record.get("read_window")).get("attempts")
    outcome = branch(read)
    if outcome == SILENT:
        problems.append(
            f"the call after the retirement was cut silently: outcome "
            f"{read.get('outcome')!r}, error {read.get('is_error')!r}"
        )
    elif outcome == DELIVERED:
        problems += _page_problems(
            record, username, read, after=seen, after_label="the retirement"
        )
        if daemon:
            problems += _successor_problems(record, "owner_after_read", read, username)
        else:
            problems += _reopened_problems(record)
    if daemon:
        problems += _classification_problems(attempts, required=False)
    problems += _session_problems(record, [username])
    return problems


def _reopened_problems(record: Mapping[str, Any]) -> list[str]:
    """Direct: the call read through a browser opened after the close."""
    retired, after = _roots(record, "retired"), _roots(record, "after the call")
    closed_at = _number(retired.get("seen"))
    roots = _sequence(after.get("roots"))
    if retired.get("roots") is None or after.get("roots") is None or closed_at is None:
        return [f"{INVALID}the browser was not read around the call"]
    if len(roots) != 1:
        return [f"the call's browser is not one root: {list(roots)}"]
    start = _number(_sequence(roots[0])[1])
    if start is None or start < closed_at - START_TOLERANCE_SECONDS:
        return ["the browser the call read with is not shown reopened after the close"]
    return []


def idle_problems(record: Mapping[str, Any] | None, *, daemon: bool) -> list[str]:
    """H-R13's verdict over its raw record, by its row: every problem, or
    nothing. Invalid evidence starts with ``INVALID``."""
    if not isinstance(record, Mapping):
        return ["the row kept no record"]
    row = record.get("row")
    if row == ROW_ADMISSION:
        return admission_problems(record, daemon=daemon)
    if row == ROW_RETIREMENT:
        return retirement_problems(record, daemon=daemon)
    return [f"the record is for row {row!r}, which races no idle retirement"]


# --- Turnover: the verdicts -----------------------------------------------------------


def _unknown_outcome(call: Mapping[str, Any]) -> bool:
    return (
        call.get("outcome") == "returned"
        and call.get("is_error") is True
        and call.get("status") == UNKNOWN_OUTCOME
        and call.get("retry_safe") is False
    )


def _first_category(call: Mapping[str, Any]) -> str:
    if _read_ok(call):
        return COMPLETED
    if _unknown_outcome(call):
        return UNKNOWN_OUTCOME
    return f"other: {call.get('outcome')!r}, status {call.get('status')!r}"


def _turnover_invalid(
    record: Mapping[str, Any], case: TurnoverCase, first: Mapping[str, Any]
) -> tuple[list[str], int | None]:
    """Why the record does not measure its turnover, and the stand-down's
    answer on the monotonic clock."""
    found: list[str] = []
    found += _replaced_before(record)
    stood = _mapping(record.get("stand_down"))
    answered = _ns(stood.get("answered_ns"))
    sent = _ns(stood.get("sent_ns"))
    if (
        answered is None
        or sent is None
        or stood.get("status") != 200
        or stood.get("standing_down") is not True
        or stood.get("addressed") is not True
    ):
        found.append(
            f"{INVALID}the stand-down was not answered as standing down by the "
            f"identified owner: status {stood.get('status')!r}"
        )
        answered = None
    username = USERNAMES[str(record.get("row"))]
    first_gate = _gates(record, person_path(username, "main_profile"))
    gate = first_gate[0] if len(first_gate) == 1 else {}
    entered = _ns(gate.get("entered_monotonic_ns"))
    if entered is None:
        found.append(f"{INVALID}the first held page never entered its gate")
    elif sent is not None and sent < entered:
        found.append(f"{INVALID}the stand-down was sent before the read's body began")
    if gate and (
        gate.get("terminal") != SERVED or gate.get("released_by") != RELEASED_BY_ROW
    ):
        found.append(
            f"{INVALID}the first hold did not end as the row released it: "
            f"{gate.get('terminal')!r} by {gate.get('released_by')!r}"
        )
    if case.past_drain:
        for second_gate in _gates(record, person_path(username, "experience")):
            if second_gate.get("terminal") == DEADLINE:
                found.append(f"{INVALID}the second hold ran out its deadline")
    ended = _ns(first.get("ended_monotonic_ns"))
    if answered is not None and ended is not None:
        inside = (ended - answered) / 1e9 < TURNOVER_DRAIN_SECONDS
        if case.past_drain and inside:
            found.append(
                f"{INVALID}the held read ended inside the drain: nothing outlasted it"
            )
        if not case.past_drain and not inside:
            found.append(f"{INVALID}the held work did not finish inside the drain")
    cut = _number(_mapping(record.get("owner_log")).get("cut")) or 0
    needed = 2 if case.second == QUEUED else 1
    if case.past_drain and cut < needed:
        found.append(
            f"{INVALID}the owner cut {int(cut)} call(s) as its drain ran out, not "
            f"{needed}: "
            + (
                "the queued read is not shown admitted when the drain ran out"
                if case.second == QUEUED
                else "nothing was cut"
            )
        )
    return found, answered


def _second_problems(
    record: Mapping[str, Any],
    case: TurnoverCase,
    first: Mapping[str, Any],
    answered: int | None,
) -> list[str]:
    """The second read: sent where its lane sends it, refused by the retiring
    owner and never run on it, then delivered by a verified successor or
    failed explicitly."""
    found: list[str] = []
    second_name = SECOND_USERNAMES[str(record.get("row"))]
    reads = _calls(record, PERSON_TOOL)
    second = reads[1] if len(reads) == 2 else None
    if second is None:
        return [f"{INVALID}the record holds {len(reads)} reads, not two"]
    sent = _ns(second.get("began_monotonic_ns"))
    stood_sent = _ns(_mapping(record.get("stand_down")).get("sent_ns"))
    first_end = _ns(first.get("ended_monotonic_ns"))
    if case.second == AFTER and (
        sent is None
        or answered is None
        or sent < answered
        or first_end is None
        or sent > first_end
    ):
        found.append(
            f"{INVALID}the new work was not sent after the stand-down and while the "
            f"held work still ran"
        )
    if case.second == QUEUED and (
        sent is None or stood_sent is None or sent > stood_sent
    ):
        found.append(f"{INVALID}the queued read was not sent before the stand-down")
    gone = _ns(_mapping(record.get("owner_exit")).get("seen_ns"))
    gone_how = _mapping(record.get("owner_exit")).get("how")
    arrivals = [at for pages in _pages(record, second_name).values() for at in pages]
    if gone_how == "exited" and gone is not None:
        early = [at for at in arrivals if at is None or at <= gone]
        if early:
            found.append(
                f"the {'new work' if case.second == AFTER else 'queued read'} ran on "
                f"the retiring owner: {len(early)} of its pages before that owner "
                f"was seen gone"
            )
    attempts = record.get("second_attempts")
    if case.second == QUEUED:
        dispatch = [
            _mapping(item)
            for item in _sequence(attempts)
            if _mapping(item).get("attempt") == "dispatch"
        ]
        if not any(item.get("classification") == "retiring" for item in dispatch):
            found.append(
                "the queued read is not shown refused as not run: the owner's signed "
                "retiring refusal is not in the frontend's output"
            )
        if second.get("status") == UNKNOWN_OUTCOME:
            found.append(
                "the queued read was reported as an unknown outcome, as if its body "
                "had begun"
            )
    else:
        found += _classification_problems(attempts, required=True)
    outcome = branch(second)
    if outcome == SILENT:
        found.append(
            f"the second read was cut silently: outcome {second.get('outcome')!r}, "
            f"error {second.get('is_error')!r}"
        )
    elif outcome == DELIVERED:
        found += _page_problems(
            record,
            second_name,
            second,
            after=gone if gone_how == "exited" else None,
            after_label="the retiring owner was seen gone",
        )
        found += _successor_problems(record, "owner_after", second, second_name)
    return found


def turnover_problems(record: Mapping[str, Any] | None, *, daemon: bool) -> list[str]:
    """A turnover lane's verdict over its raw record: every problem, or
    nothing. Daemon only: K1 is recorded not applicable, never run."""
    if not isinstance(record, Mapping):
        return ["the row kept no record"]
    row = record.get("row")
    if not isinstance(row, str) or row not in TURNOVER_CASES:
        return [f"the record is for row {row!r}, which turns no owner over"]
    case = TURNOVER_CASES[row]
    if not daemon:
        return ["a turnover lane runs only through an owner; K1 is not applicable"]
    problems = _common(
        record,
        rows=tuple(TURNOVER_CASES),
        daemon=True,
        idle=TURNOVER_IDLE_TIMEOUT_SECONDS,
    )
    if record.get("k1") != K1_NOT_APPLICABLE:
        problems.append("the record does not say why K1 is not applicable")
    if record.get("w6") != W6_NOT_DISCHARGED:
        problems.append("the record does not say that W6 stays open")
    username = USERNAMES[row]
    reads = _calls(record, PERSON_TOOL)
    if not reads:
        problems.append(f"{INVALID}the record holds no read")
        return problems
    first = reads[0]
    invalid, answered = _turnover_invalid(record, case, first)
    problems += invalid
    owner_log = _mapping(record.get("owner_log"))
    if owner_log.get("turned_over") is not True:
        problems.append(
            "the owner's log does not show it standing down for the turnover"
        )
    owner_exit = _mapping(record.get("owner_exit"))
    if owner_exit.get("how") != "exited":
        problems.append(
            f"the owner is not shown to stand down within {TURNOVER_EXIT_SECONDS}s of "
            f"the request: {owner_exit.get('how')!r}"
        )
    if case.past_drain:
        if not _unknown_outcome(first):
            problems.append(
                f"the cut read was not reported as an unknown outcome unsafe to retry: "
                f"{_first_category(first)}, retry_safe {first.get('retry_safe')!r}"
            )
        pages = _pages(record, username)
        if len(pages["main_profile"]) != 1:
            problems.append(
                f"the cut read ran again: its profile page was requested "
                f"{len(pages['main_profile'])} times"
            )
        ended = _ns(first.get("ended_monotonic_ns"))
        later = [
            at
            for arrivals in pages.values()
            for at in arrivals
            if at is None or ended is None or at > ended
        ]
        if pages["education"] or later:
            problems.append(
                "the cut read went on after the cut: "
                + ("its education page was requested" if pages["education"] else "")
                + (f"; {len(later)} of its pages after it ended" if later else "")
            )
    else:
        if not _read_ok(first):
            problems.append(
                f"the admitted work did not complete normally inside the drain: "
                f"{_first_category(first)}"
            )
        released = _ns(
            (_gates(record, person_path(username, "main_profile")) or [{}])[0].get(
                "release_requested_monotonic_ns"
            )
        )
        problems += _page_problems(
            record,
            username,
            first,
            after=released,
            after_label="the release",
            held="main_profile",
        )
        if owner_log.get("cut"):
            problems.append(
                "the owner cut a call though its admitted work finished in time"
            )
    if case.second is not None:
        problems += _second_problems(record, case, first, answered)
    else:
        identified = _identified(record)
        others = _other_launches(record, identified[:2]) if identified else None
        if others != []:
            problems.append(
                f"an owner was launched though nothing called after the turnover: "
                f"{others}"
            )
    problems += _session_problems(
        record, [username, *([SECOND_USERNAMES[row]] if case.second else [])]
    )
    return problems


# --- Comparisons ----------------------------------------------------------------------


def invalid_evidence(problems: Sequence[str]) -> list[str]:
    """The problems that leave a row unmeasured rather than failed."""
    return [problem for problem in problems if problem.startswith(INVALID)]


def semantics(record: Mapping[str, Any]) -> dict[str, Any]:
    """What K0 compares: classifications only. A call after a retirement
    projects both safe branches to ``NO_SILENT_CUT``, and how the frontend
    classified the owner it met is a race the contract allows either way."""
    row = record.get("row")
    found: dict[str, Any] = {"row": row, "mode": record.get("mode")}
    reads = _calls(record, PERSON_TOOL)
    if row == ROW_ADMISSION:
        found["read"] = COMPLETED if reads and _read_ok(reads[0]) else "other"
        found["after"] = bool(_mapping(record.get("after")).get("idle_lines"))
    elif row == ROW_RETIREMENT:
        found["call"] = (
            NO_SILENT_CUT if reads and branch(reads[0]) != SILENT else SILENT
        )
    elif row in TURNOVER_CASES:
        found["first"] = _first_category(reads[0]) if reads else None
        found["second"] = (
            (NO_SILENT_CUT if branch(reads[1]) != SILENT else SILENT)
            if len(reads) > 1
            else None
        )
        found["cut"] = int(_number(_mapping(record.get("owner_log")).get("cut")) or 0)
        found["stood_down"] = _mapping(record.get("owner_exit")).get("how")
    return found


def problems_for(record: Mapping[str, Any] | None, *, daemon: bool) -> list[str]:
    """The verdict of whichever row of this module *record* is for."""
    row = _mapping(record).get("row")
    if row in TURNOVER_CASES:
        return turnover_problems(record, daemon=daemon)
    return idle_problems(record, daemon=daemon)


def _refusals(named: Sequence[tuple[str, Mapping[str, Any] | None, bool]]) -> list[str]:
    refusals = []
    for name, record, daemon in named:
        problems = problems_for(record, daemon=daemon)
        if problems:
            refusals.append(f"the {name} record is not valid: {problems}")
    return refusals


def semantic_differences(
    reference: Mapping[str, Any] | None, repeat: Mapping[str, Any] | None
) -> list[str]:
    """K0 against K3: both valid by their own verdict, and alike in every
    classification. A missing or invalid record is a refusal."""
    refusals = _refusals([("reference", reference, True), ("repeat", repeat, True)])
    if refusals:
        return refusals
    assert reference is not None and repeat is not None
    one, two = semantics(reference), semantics(repeat)
    return [
        f"{name}: {one[name]!r} then {two.get(name)!r}"
        for name in one
        if one[name] != two.get(name)
    ]


def comparison_refusals(
    direct: Mapping[str, Any] | None, daemon: Mapping[str, Any] | None
) -> list[str]:
    """Why K3 cannot be held to K1 on an H-R13 row: a record missing or
    invalid. O1 to O4 are the vectors' (``compare_to_direct``)."""
    return _refusals([("Direct", direct, False), ("daemon", daemon, True)])
