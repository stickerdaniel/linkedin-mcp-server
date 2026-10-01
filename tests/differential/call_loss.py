"""Calls that lose their caller (H-R4, H-R5), and the read that calibrates
them (H-CAL).

**The read.** ``get_person_profile`` with ``sections="experience,education"``
navigates three pages in a fixed order, one per section
(``linkedin.fields.PERSON_SECTIONS``): the profile, then
``details/experience/``, then ``details/education/``, with a delay between
each. Holding the experience page at the synthetic origin
(``SyntheticOrigin.hold``) puts the call at a known point with one section
still to come, so whether the education page is requested after the hold
ends is what says whether the read went on.

**H-CAL** is that read with nothing taken away. The host warms up with the
row's ordinary feed read, which starts the browser (and in daemon mode the
owner) outside anything held; the script then arms the gate, reads the
profile through the host, releases the held page as soon as it entered, and
the host quits normally. Its verdict (``calibration_problems``) needs: the
gate entered and served, the held page the only experience request, the
education page requested after the hold let the held one go, the call returning
every section from its own page, and a normal quit. Settlement and the
preservation are the ordinary ones ``judge_row`` holds every row to. A gate
that ran out its deadline, or whose peer was gone, says the observation is
invalid, not that the product did anything.

**Paths the frozen baseline reads.** The same: at
``0253421539fffd4c9b207ca62b6efb41a8905ed3`` ``PERSON_SECTIONS`` names the
same three suffixes, and the navigation and capture code between them is
unchanged but for comments. The K1 cell does not lean on that reading: its
own record has to show the three requests the verdict requires.

**H-R4 and H-R5** read the same profile through the same held page, and lose
the call once the held request has entered the gate (``LOSS_CASES``):

* **EOF** (``H-R4-eof``): the host closes the server's stdin, with no MCP
  shutdown and no wait for its exit.
* **Abrupt pipe loss** (``H-R4-pipe``): stdin and the read end of stdout
  closed at once. Named pipe loss, not host death.
* **Host killed** (``H-R4-host-killed``): the host is a process of its own
  (``harness.StubHost``), killed whole, so its server finds every pipe broken.
  The observer stays outside it. Windows' real-host semantics stay the manual
  protocol's.
* **Two outstanding host requests** (``H-R4-two-requests``): a second read,
  for another username, sent once the first entered, then EOF. Nothing
  observes the second reaching the owner, so the row claims only that it
  never began; owner-queued abandonment stays with the source model.
* **Server or frontend killed** (``H-R5``), host alive: Direct, the server
  the host started; daemon, only the frontend, never the owner. Killed
  through the H-R6 path, guardian found and oracle attached before the read.

The row releases the held page ``RELEASE_SECONDS`` after its entry,
scheduled from the entry and never from the host, and then watches the
origin. The read going on shows as the education page requested after the
loss; the second read going on, as any of its pages. Direct: the server must
exit by itself, and its browser, census and, where the platform answers, its
lease are read before the harness ends anything. Daemon: the frontend must
exit by itself and the owner must stay the one identified, alive; a fresh
host then reads through that same owner inside ``HOT_REUSE_WINDOW_SECONDS``
of the loss, and the owner later idles out by itself. In both, a fresh host
reads after the loss, and the preservation is the ordinary one.

The cancellation cause is recorded, never inferred: the owner's expiry line
is evidence of expiry, its absence is ``unobserved``, and neither excuses a
continuation. The contract's bound, cancellation within the expiry and a poll
of the last heartbeat the owner registered, is not claimed: nothing outside
the owner observes that heartbeat. A gate that ran out, a release missed, a
loss before the entry, or a reading taken late is invalid evidence, named
apart from a product finding (``INVALID``).

The script runs on a ``harness.RowContext``; nothing here reads a process,
and the verdict reads the raw record alone, so it can be replayed from the
published packet.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from differential import lease_probe
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
    from differential.harness import RowContext

ROW_H_CAL = "H-CAL"

PERSON_TOOL = "get_person_profile"
#: Row-chosen: a second username gives a second, distinct set of paths.
CALIBRATION_USERNAME = "synthetic-calibration"
CALIBRATION_SECTIONS = ("experience", "education")
#: The section held; the next one in ``PERSON_SECTIONS`` order is the witness.
HELD_SECTION = "experience"
NEXT_SECTION = "education"
#: Every section the read must return, ``main_profile`` always included.
EXPECTED_SECTIONS = ("main_profile", *CALIBRATION_SECTIONS)

#: The idle timeout of the call-loss rows this read calibrates, so the
#: calibration runs their configuration: a declared scenario setting, the same
#: in K1, K3 and K0, and recorded in the row's packet.
CALIBRATION_IDLE_TIMEOUT_SECONDS = 60.0

#: How long the script waits, from arming, for the held page to be asked
#: for: the profile page, its URN read and the delay before the next section
#: come first. Below the call's own bound, so a read that never gets there
#: is recorded as such before the call gives up.
ENTRY_SECONDS = 120.0
#: How long the script waits for a released hold to record its end.
GATE_END_SECONDS = 30.0

K2_NOT_APPLICABLE = {
    "status": "not applicable",
    "reason": (
        "an unfaulted calibration of the read and the gate: the plan names no "
        "historical-daemon regression witness for it, and the contract forbids "
        "inventing one"
    ),
}


def calibration_arguments() -> dict[str, Any]:
    return {
        "linkedin_username": CALIBRATION_USERNAME,
        "sections": ",".join(CALIBRATION_SECTIONS),
    }


async def calibration_script(ctx: RowContext) -> None:
    """H-CAL's scripted phase, after the warm-up read: hold, read, release.

    The release is scheduled here, alongside the read, as soon as the held
    request entered; it never waits on the host. A read that ends before
    the request entered leaves the gate unentered, and the verdict says so.
    """
    record = ctx.record
    held = person_path(CALIBRATION_USERNAME, HELD_SECTION)
    record["username"] = CALIBRATION_USERNAME
    record["held"] = {
        "path": held,
        "ordinal": 1,
        "deadline_seconds": GATE_DEADLINE_SECONDS,
    }
    gate = ctx.hold(held, ordinal=1)

    async def release_on_entry() -> None:
        if await ctx.entered(gate, ENTRY_SECONDS):
            gate.release(by=RELEASED_BY_ROW)
        else:
            record["observation_problems"].append(
                f"the held section was not requested within {ENTRY_SECONDS}s of arming"
            )

    releasing = asyncio.ensure_future(release_on_entry())
    try:
        await ctx.call(PERSON_TOOL, calibration_arguments())
    finally:
        # Done already once the request entered; otherwise nothing is left to
        # release, and the teardown releases the gate in any case.
        releasing.cancel()
        await asyncio.gather(releasing, return_exceptions=True)
    if gate.entered.is_set() and not await ctx.ended(gate, GATE_END_SECONDS):
        record["observation_problems"].append(
            f"the released hold recorded no end within {GATE_END_SECONDS}s"
        )


# --- The verdict --------------------------------------------------------------


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _ns(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, str) else ()


def _person_call(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    calls = [
        _mapping(call)
        for call in _sequence(record.get("calls"))
        if _mapping(call).get("tool") == PERSON_TOOL
    ]
    return calls[0] if len(calls) == 1 else None


def _gate(record: Mapping[str, Any], path: str | None) -> Mapping[str, Any] | None:
    gates = [
        _mapping(gate)
        for gate in _sequence(record.get("gates"))
        if _mapping(gate).get("path") == path
    ]
    return gates[0] if len(gates) == 1 else None


def _arrivals(record: Mapping[str, Any], path: str | None) -> list[int | None]:
    """Each request for exactly *path* on the origin, by its arrival."""
    return [
        _ns(_mapping(request).get("monotonic_ns"))
        for request in _sequence(record.get("requests"))
        if _mapping(request).get("path") == path
    ]


def _first(arrivals: Sequence[int | None]) -> int | None:
    """The earliest arrival, or None when there is none or one is unknown."""
    known = [at for at in arrivals if at is not None]
    if not known or len(known) != len(arrivals):
        return None
    return min(known)


def reading(record: Mapping[str, Any]) -> dict[str, Any]:
    """What the record shows, classified: labels and orderings, no times.

    ``held_first`` and ``next_after_release`` are None when the times that
    would order them are missing.
    """
    username = record.get("username")
    named = username if isinstance(username, str) and username else None
    paths = {
        section: person_path(named, section) if named is not None else None
        for section in EXPECTED_SECTIONS
    }
    gate = _gate(record, paths[HELD_SECTION])
    call = _person_call(record)
    entered = _ns((gate or {}).get("entered_monotonic_ns"))
    released = _ns((gate or {}).get("released_monotonic_ns"))
    arrivals = {section: _arrivals(record, path) for section, path in paths.items()}
    first = {section: _first(found) for section, found in arrivals.items()}
    main, held, after = first["main_profile"], first[HELD_SECTION], first[NEXT_SECTION]
    held_first = None if main is None or held is None else main <= held
    next_after_release = None if released is None or after is None else after > released
    marked = sorted(
        set(_sequence((call or {}).get("marked_sections"))) & set(EXPECTED_SECTIONS)
    )
    return {
        "named": named is not None,
        "gate": None
        if gate is None
        else (
            entered is not None,
            gate.get("terminal"),
            gate.get("released_by"),
            gate.get("ordinal"),
        ),
        "requests": {section: len(found) for section, found in arrivals.items()},
        "held_first": held_first,
        "next_after_release": next_after_release,
        "read": None
        if call is None
        else (call.get("outcome"), call.get("is_error"), tuple(marked)),
        "section_errors": sorted(
            set(_sequence((call or {}).get("section_errors"))) & set(EXPECTED_SECTIONS)
        ),
    }


def calibration_problems(
    record: Mapping[str, Any] | None, *, daemon: bool
) -> list[str]:
    """H-CAL's verdict over its raw record: every problem, or nothing.

    A missing record, or one missing any part, fails; so does a script
    error. A deadline or a peer gone at the gate is invalid evidence and
    named as such, apart from a gate never entered.
    """
    if not isinstance(record, Mapping):
        return ["the row kept no record"]
    mode = "daemon" if daemon else "direct"
    problems: list[str] = []
    if record.get("row") != ROW_H_CAL:
        problems.append(f"the record is for row {record.get('row')!r}")
    if record.get("mode") != mode:
        problems.append(f"the record is for mode {record.get('mode')!r}, not {mode}")
    if record.get("script_error"):
        problems.append(f"the row's script failed: {record['script_error']}")
    problems += [str(p) for p in _sequence(record.get("observation_problems"))]
    if record.get("idle_timeout_seconds") != CALIBRATION_IDLE_TIMEOUT_SECONDS:
        problems.append(
            f"the row ran with an idle timeout of "
            f"{record.get('idle_timeout_seconds')!r}, not the declared "
            f"{CALIBRATION_IDLE_TIMEOUT_SECONDS}"
        )
    if record.get("k2") != K2_NOT_APPLICABLE:
        problems.append("the record does not say why K2 is not applicable")
    problems += host_problems(record.get("host"))

    read = reading(record)
    if not read["named"]:
        problems.append("the record names no username")
    gate = read["gate"]
    if gate is None:
        problems.append("the record holds no gate on the held section")
    else:
        entered, terminal, released_by, ordinal = gate
        if not entered:
            problems.append("the held section's request never entered the gate")
        elif terminal == DEADLINE:
            problems.append(
                "the hold ran out its deadline before the row released it: the "
                "evidence is invalid, not a finding"
            )
        elif terminal == PEER_GONE:
            problems.append(
                "the held request's peer was gone before its answer was written: "
                "the evidence is invalid, not a finding"
            )
        elif terminal != SERVED:
            problems.append(f"the hold recorded no end: {terminal!r}")
        if entered and released_by != RELEASED_BY_ROW:
            problems.append(f"the hold was released by {released_by!r}, not by the row")
        if ordinal != 1:
            problems.append(f"the gate held request {ordinal!r}, not the first")
        # When the hold let go, not when its release was asked for: a handler
        # resumed late still records ``served``.
        timed = _gate(record, person_path(str(record.get("username")), HELD_SECTION))
        began = _ns((timed or {}).get("entered_monotonic_ns"))
        let_go = _ns((timed or {}).get("released_monotonic_ns"))
        if entered and (began is None or let_go is None):
            problems.append("the hold's entry or end has no time")
        elif entered and began is not None and let_go is not None:
            held = (let_go - began) / 1e9
            if held > GATE_DEADLINE_SECONDS:
                problems.append(
                    f"the hold let go {held:.1f}s after its entry, past the "
                    f"gate's {GATE_DEADLINE_SECONDS}s deadline: the evidence is "
                    f"invalid, not a finding"
                )
    for section, count in read["requests"].items():
        if count != 1:
            problems.append(f"the {section} page was requested {count} times, not once")
    if read["held_first"] is not True:
        problems.append("the profile page is not shown requested before the held one")
    if read["next_after_release"] is not True:
        problems.append(
            f"the {NEXT_SECTION} page is not shown requested after the hold on "
            f"the {HELD_SECTION} page let it go"
        )
    if read["read"] is None:
        problems.append(f"the record holds no single {PERSON_TOOL} call")
    else:
        outcome, is_error, marked = read["read"]
        if outcome != "returned" or is_error is not False:
            problems.append(
                f"the {PERSON_TOOL} call did not return a result: outcome "
                f"{outcome!r}, error {is_error!r}"
            )
        missing = sorted(set(EXPECTED_SECTIONS) - set(marked))
        if missing:
            problems.append(f"the read did not return the synthetic sections {missing}")
    if read["section_errors"]:
        problems.append(f"the read reported errors for {read['section_errors']}")
    problems += _call_window_problems(record)
    problems += _session_problems(record)
    return problems


def _call_window_problems(record: Mapping[str, Any]) -> list[str]:
    """The hold, and every page of the read, inside the call's own interval."""
    call = _person_call(record)
    if call is None:
        return []
    began, ended = (
        _ns(call.get("began_monotonic_ns")),
        _ns(call.get("ended_monotonic_ns")),
    )
    if began is None or ended is None or ended < began:
        return ["the read's times are missing or out of order"]
    username = record.get("username")
    if not isinstance(username, str):
        return []
    problems = []
    gate = _gate(record, person_path(username, HELD_SECTION)) or {}
    entered = _ns(gate.get("entered_monotonic_ns"))
    if entered is not None and not began <= entered <= ended:
        problems.append("the held request entered outside the read's interval")
    for section in EXPECTED_SECTIONS:
        arrivals = _arrivals(record, person_path(username, section))
        if any(at is None or not began <= at <= ended for at in arrivals):
            problems.append(f"the {section} page was requested outside the read")
    return problems


def _session_problems(record: Mapping[str, Any]) -> list[str]:
    """Every page of the read carried the staged session."""
    username = record.get("username")
    if not isinstance(username, str):
        return []
    paths = {person_path(username, section) for section in EXPECTED_SECTIONS}
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


# --- Comparisons --------------------------------------------------------------


def semantics(record: Mapping[str, Any]) -> dict[str, Any]:
    """What K0 compares: every classification, and no pid, time or path."""
    host = _mapping(record.get("host"))
    return {
        "row": record.get("row"),
        "mode": record.get("mode"),
        "host": (host.get("exited_on_quit"), host.get("exit_code")),
        **reading(record),
    }


def semantic_differences(
    reference: Mapping[str, Any] | None,
    repeat: Mapping[str, Any] | None,
    *,
    daemon: bool,
) -> list[str]:
    """K0 against its reference: both valid by their own verdict, read again
    here, and alike in every classification. A missing or invalid record is
    a refusal, never an empty difference."""
    refusals = []
    for name, record in (("reference", reference), ("repeat", repeat)):
        problems = calibration_problems(record, daemon=daemon)
        if problems:
            refusals.append(f"the {name} record is not valid: {problems}")
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
    """Why K3 cannot be held to K1 on this row: a record missing or invalid.

    The comparison itself is the vectors' (``compare_to_direct``).
    """
    refusals = []
    for name, record, is_daemon in (
        ("Direct", direct, False),
        ("daemon", daemon, True),
    ):
        problems = calibration_problems(record, daemon=is_daemon)
        if problems:
            refusals.append(f"the {name} record is not valid: {problems}")
    return refusals


# --- Calls that lose their caller: H-R4 and H-R5 ---------------------------------

#: How a losing row ends its host (``harness.TERMINATIONS``): its stdin
#: closed, both its pipes closed, the host process killed, or the server or
#: frontend the host started killed.
EOF_LOSS = "eof-loss"
PIPE_LOSS = "pipe-loss"
HOST_KILLED = "host-killed"
ACTOR_KILLED = "actor-killed"
LOSS_TERMINATIONS = frozenset({EOF_LOSS, PIPE_LOSS, HOST_KILLED, ACTOR_KILLED})

ROW_H_R4_EOF = "H-R4-eof"
ROW_H_R4_PIPE = "H-R4-pipe"
ROW_H_R4_HOST = "H-R4-host-killed"
ROW_H_R4_TWO = "H-R4-two-requests"
ROW_H_R5 = "H-R5"


@dataclass(frozen=True)
class LossCase:
    """One losing row: how it loses the host, and whether a second read is
    outstanding when it does."""

    termination: str
    second: bool = False


LOSS_CASES: dict[str, LossCase] = {
    ROW_H_R4_EOF: LossCase(EOF_LOSS),
    ROW_H_R4_PIPE: LossCase(PIPE_LOSS),
    ROW_H_R4_HOST: LossCase(HOST_KILLED),
    ROW_H_R4_TWO: LossCase(EOF_LOSS, second=True),
    ROW_H_R5: LossCase(ACTOR_KILLED),
}

#: Row-chosen, apart from the calibration's, so no request of one row can
#: stand for another's. The second read's is one more, so each of its pages
#: is a path no other read asks for.
LOSS_USERNAME = "synthetic-loss"
SECOND_USERNAME = "synthetic-second"

#: The calibration's idle timeout: the configuration H-CAL measured, the same
#: in K1, K3 and K0.
LOSS_IDLE_TIMEOUT_SECONDS = CALIBRATION_IDLE_TIMEOUT_SECONDS

#: When the row lets the held page go, counted from its entry into the gate.
#: Above the contract's cancellation objective as an owner meets it at its
#: latest: the last heartbeat it registered up to one cadence (2 s) before
#: the loss, then the expiry (10 s) and one poll (0.1 s), 12.1 s in all. And
#: below the gate's own deadline (``GATE_DEADLINE_SECONDS``, 20 s), so the
#: release, not the deadline, ends a hold. Not the contract's bound: the row
#: claims only that nothing went on after a release this long after the loss.
RELEASE_SECONDS = 15.0
#: How late the release may be asked for before the evidence is invalid:
#: with it, still three seconds inside the gate's deadline.
RELEASE_TOLERANCE_SECONDS = 2.0
#: The least time, from the loss to the hold actually letting go, for a read
#: going on after it to be a finding: the contract's objective as an owner
#: meets it at its latest, as in ``RELEASE_SECONDS``. A release scheduled from
#: the entry is late enough only if the loss came promptly after the entry; a
#: loss the harness made late leaves an owner still inside its expiry window
#: when the page comes free, so what follows says nothing.
LOSS_TO_RELEASE_SECONDS = 12.1
#: How long the origin is watched after the release for the read going on.
#: Had it gone on, the education page would follow the released one after
#: the product's ``NAV_DELAY`` (2 s) and the experience page's capture.
CONTINUATION_SECONDS = 10.0
#: How long the second read is given, once sent, to reach the server before
#: the loss. Nothing observes that it did; the row claims no more.
SECOND_SEND_SECONDS = 1.0
#: How long the lost calls may take to end in the host's client.
CALL_END_SECONDS = 60.0
#: How long the server or frontend is given to exit by itself after the loss:
#: the bound of a normal quit (``harness._HOST_EXIT_SECONDS``).
SERVER_EXIT_SECONDS = 90.0
#: The window, from the loss, in which K3's fresh read must begin for hot
#: reuse of the same owner to be shown. Below the rows' idle timeout, whose
#: quiet period cannot start before the loss while the held call is in
#: flight, so the owner that read meets cannot have retired by its own clock.
HOT_REUSE_WINDOW_SECONDS = 45.0

#: What the owner logs when it cancels a call nobody waited for
#: (``daemon_liveness``). Positive evidence of expiry; its absence is not
#: evidence of anything, since whether the log keeps INFO is a setting.
EXPIRY_LINE = "Nobody has waited for call"
CAUSE_EXPIRY = "expiry"
CAUSE_UNOBSERVED = "unobserved"
#: The record's word on the contract's timing objective, always this.
TIMING_UNCLAIMED = (
    "not claimed: nothing outside the owner observes the heartbeat it registered"
)
#: The lease where the platform's contender cannot answer (Windows).
LEASE_UNOBSERVED = "unobserved"
#: How a problem that leaves the row unmeasured starts, apart from a finding.
INVALID = "invalid evidence: "

LOSS_K2_NOT_APPLICABLE = {
    "status": "not applicable",
    "reason": (
        "the plan names a historical-daemon regression witness only for R6, R7, "
        "R11 and R12, none for a host or frontend lost mid-call, and the "
        "contract forbids inventing one"
    ),
}


def loss_event(termination: str, *, daemon: bool) -> tuple[str, str]:
    """The ``loss`` event's kind and target actor (``events.LOSSES``)."""
    if termination == EOF_LOSS:
        return "eof", "frontend"
    if termination == PIPE_LOSS:
        return "pipe", "frontend"
    if termination == HOST_KILLED:
        return "host-killed", "host_stub"
    return ("frontend-killed" if daemon else "server-killed"), "frontend"


def _read_of(username: str) -> dict[str, Any]:
    return {"linkedin_username": username, "sections": ",".join(CALIBRATION_SECTIONS)}


def _phase(ctx: RowContext, name: str, at: int | None = None) -> None:
    ctx.emit("harness", "phase", name=name, monotonic_ns=at or time.monotonic_ns())


async def _entered_or_ended(gate: Gate, read: asyncio.Future[Any]) -> bool:
    """Whether the held request entered before the read ended, within
    ``ENTRY_SECONDS`` of arming."""
    deadline = time.monotonic() + ENTRY_SECONDS
    while not gate.entered.is_set():
        if read.done() or time.monotonic() >= deadline:
            return gate.entered.is_set()
        await asyncio.sleep(0.01)
    return True


async def _sleep_until(monotonic_ns: int) -> None:
    await asyncio.sleep(max(0.0, (monotonic_ns - time.monotonic_ns()) / 1e9))


async def _release_at(gate: Gate, monotonic_ns: int) -> None:
    await _sleep_until(monotonic_ns)
    gate.release(by=RELEASED_BY_ROW)


def _settled(settlement: Any) -> bool:
    """Whether a Direct settlement reading shows the profile free for a
    fresh server: nothing on it, nothing unread, the lease not held."""
    found = _mapping(settlement)
    return (
        not found.get("error")
        and found.get("remaining") == []
        and found.get("unresolved") == []
        and found.get("lease") in (lease_probe.FREE, LEASE_UNOBSERVED)
        and found.get("guardian_exit") in (None, "exited")
    )


async def loss_script(ctx: RowContext) -> None:
    """A losing row's scripted phase, after the warm-up read.

    Prepare the loss, arm the gate, read; once the held request entered,
    schedule its release from that entry, send the second read where the row
    has one, and lose the host. Then, from this body, after the lost calls
    ended and before the client leaves: the server's own exit, Direct's
    settlement, the release, the continuation watch, the owner, and a fresh
    host's read. A read that ends before the held request entered loses
    nothing, and the record says so.
    """
    case = LOSS_CASES[ctx.row]
    record = ctx.record
    problems: list[str] = record["observation_problems"]
    seams = ctx.loss
    if seams is None:
        problems.append(f"{INVALID}the row was given no way to lose its host")
        return
    held = person_path(LOSS_USERNAME, HELD_SECTION)
    record.update(
        username=LOSS_USERNAME,
        held={"path": held, "ordinal": 1, "deadline_seconds": GATE_DEADLINE_SECONDS},
        case={"termination": case.termination, "second": case.second},
        timing_objective=TIMING_UNCLAIMED,
    )
    if case.second:
        record["second"] = {"username": SECOND_USERNAME}
    record["prepared"] = await seams.prepare(case.termination)
    owner = ctx.owner()
    record["owner_identified"] = (
        [owner.pid, owner.create_time, owner.instance_id] if owner is not None else None
    )
    gate = ctx.hold(held, ordinal=1)
    _phase(ctx, "armed")
    reads = [asyncio.ensure_future(ctx.call(PERSON_TOOL, _read_of(LOSS_USERNAME)))]
    releasing: asyncio.Future[None] | None = None
    try:
        if not await _entered_or_ended(gate, reads[0]):
            problems.append(
                f"{INVALID}the held section was not requested within "
                f"{ENTRY_SECONDS}s of arming, or the read ended first: nothing "
                f"was lost mid-call"
            )
            return
        entered = gate.entered_monotonic_ns
        assert entered is not None
        _phase(ctx, "entered", entered)
        release_at = entered + int(RELEASE_SECONDS * 1e9)
        release: dict[str, Any] = {"scheduled_ns": release_at}
        record["release"] = release
        releasing = asyncio.ensure_future(_release_at(gate, release_at))
        if case.second:
            reads.append(
                asyncio.ensure_future(ctx.call(PERSON_TOOL, _read_of(SECOND_USERNAME)))
            )
            await asyncio.sleep(SECOND_SEND_SECONDS)
        record["loss"] = await seams.lose(case.termination)
        _phase(ctx, "lost")
        # Each lost call ends in the host's client, here only waited for:
        # its record is the timed call's own.
        _, still_open = await asyncio.wait(reads, timeout=CALL_END_SECONDS)
        record["calls_open_after_loss"] = len(still_open)
        record["server_exit"] = await seams.server_exit(SERVER_EXIT_SECONDS)
        if not ctx.daemon:
            record["settlement"] = await seams.settlement()
        await releasing
        release["requested_ns"] = gate.release_requested_monotonic_ns
        _phase(ctx, "released", gate.release_requested_monotonic_ns)
        if not await ctx.ended(gate, GATE_END_SECONDS):
            problems.append(
                f"{INVALID}the released hold recorded no end within {GATE_END_SECONDS}s"
            )
        requested = gate.release_requested_monotonic_ns or time.monotonic_ns()
        await _sleep_until(requested + int(CONTINUATION_SECONDS * 1e9))
        record["watched_until_ns"] = time.monotonic_ns()
        _phase(ctx, "watched")
        if ctx.daemon:
            record["owner_after_loss"] = await seams.owner_reading("after the loss")
        if ctx.daemon or _settled(record.get("settlement")):
            record["fresh"] = await seams.fresh_read()
        else:
            # A fresh Direct server on a profile not shown free would be a
            # second browser of the harness's own making.
            record["fresh"] = {
                "made": False,
                "why": "the Direct server's profile was not shown settled",
            }
        _phase(ctx, "fresh read")
        if ctx.daemon:
            record["owner_after_fresh"] = await seams.owner_reading(
                "after the fresh read"
            )
            lines = [line for line in seams.owner_log() if EXPIRY_LINE in line]
            record["expiry_lines"] = len(lines)
            record["cause"] = CAUSE_EXPIRY if lines else CAUSE_UNOBSERVED
    finally:
        left = [task for task in (releasing, *reads) if task is not None]
        for task in left:
            task.cancel()
        await asyncio.gather(*left, return_exceptions=True)


# --- The losing rows' verdict -------------------------------------------------------


def _requests(record: Mapping[str, Any], paths: set[str]) -> list[int | None]:
    return [
        _ns(_mapping(request).get("monotonic_ns"))
        for request in _sequence(record.get("requests"))
        if _mapping(request).get("path") in paths
    ]


def _person_paths(username: str) -> set[str]:
    return {person_path(username, section) for section in EXPECTED_SECTIONS}


def _person_calls(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The person reads in the order sent: the held one, then the second."""
    return [
        _mapping(call)
        for call in _sequence(record.get("calls"))
        if _mapping(call).get("tool") == PERSON_TOOL
    ]


def _counts(arrivals: Sequence[int | None], lost: int | None) -> tuple[int, int, int]:
    """How many of *arrivals* came before the loss, at or after it, and at an
    unknown time or with no loss to order them by."""
    before = after = unknown = 0
    for at in arrivals:
        if at is None or lost is None:
            unknown += 1
        elif at < lost:
            before += 1
        else:
            after += 1
    return before, after, unknown


def _owner_kept(record: Mapping[str, Any]) -> bool:
    """Whether every owner reading after the loss names the identified owner,
    alive, and the row started no other."""
    identified = _sequence(record.get("owner_identified"))
    if len(identified) != 3:
        return False
    for label in ("owner_after_loss", "owner_after_fresh"):
        seen = _mapping(record.get(label))
        if not (
            seen.get("alive") is True
            and same_lifetime(seen.get("lifetime"), identified[:2])
            and seen.get("instance_id") == identified[2]
        ):
            return False
    return _other_launches(record, identified[:2]) == []


def _other_launches(record: Mapping[str, Any], owner: Sequence[Any]) -> list | None:
    """The owner launches the row started besides *owner*'s, or None unread.

    Counted from the lifetimes recorded after the watcher stopped
    (``owner_processes``, ``gate_processes``) the way ``host_comparison``
    counts them: on Windows a venv launcher and the interpreter it started
    with the same command are one launch, and *owner* may be either. A second
    release gate is a second start attempted, even one that never ran.
    """
    owners = record.get("owner_processes")
    gates = record.get("gate_processes")
    if not isinstance(owners, list) or not isinstance(gates, list):
        return None
    windows = str(record.get("platform", "")).startswith("win")
    launches = owner_launches(owners, windows=windows)
    others = [launch for launch in launches if not _of_launch(owner, launch, owners)]
    if len(launches) == len(others):
        # The identified owner is not among the launches at all.
        others.append(["identified owner not launched by the row", *owner])
    gate_launches = owner_launches(gates, windows=windows)
    if len(gate_launches) > 1:
        others += [["release gate", *launch] for launch in gate_launches[1:]]
    return others


def loss_reading(record: Mapping[str, Any]) -> dict[str, Any]:
    """What a losing row's record shows, classified: no time, pid or path.

    Each continuation reading counts the requests before the loss, at or
    after it, and at an unknown time.
    """
    loss = _mapping(record.get("loss"))
    lost = _ns(loss.get("monotonic_ns"))
    gate = _gate(record, person_path(LOSS_USERNAME, HELD_SECTION)) or {}
    case = LOSS_CASES.get(str(record.get("row")))
    fresh = _mapping(record.get("fresh"))
    fresh_call = _mapping(fresh.get("call"))
    began = _ns(fresh_call.get("began_monotonic_ns"))
    settlement = _mapping(record.get("settlement"))
    return {
        "loss": (loss.get("kind"), loss.get("loss"), loss.get("target")),
        "gate": (
            _ns(gate.get("entered_monotonic_ns")) is not None,
            gate.get("terminal"),
            gate.get("released_by"),
        ),
        "next": _counts(
            _requests(record, {person_path(LOSS_USERNAME, NEXT_SECTION)}), lost
        ),
        "held": _counts(
            _requests(record, {person_path(LOSS_USERNAME, HELD_SECTION)}), lost
        ),
        "second": _counts(_requests(record, _person_paths(SECOND_USERNAME)), lost)
        if case is not None and case.second
        else None,
        "calls": tuple(call.get("outcome") for call in _person_calls(record)),
        "server_exit": _mapping(record.get("server_exit")).get("how"),
        "settlement": (
            settlement.get("remaining") == [] and settlement.get("unresolved") == [],
            settlement.get("lease"),
            settlement.get("guardian_exit"),
        )
        if settlement
        else None,
        "fresh": (
            fresh.get("made"),
            fresh_call.get("outcome"),
            fresh_call.get("read_the_post"),
            fresh.get("forwarded"),
            None
            if began is None or lost is None
            else began - lost <= HOT_REUSE_WINDOW_SECONDS * 1e9,
        ),
        "owner_kept": _owner_kept(record) if record.get("mode") == "daemon" else None,
    }


def _inside_expiry_window(record: Mapping[str, Any], lost: int | None) -> bool:
    """Whether the held page was served to a browser still there sooner than
    ``LOSS_TO_RELEASE_SECONDS`` after the loss. A hold that let go because the
    browser left (``PEER_GONE``) leaves nothing to read on from."""
    gate = _gate(record, person_path(LOSS_USERNAME, HELD_SECTION)) or {}
    hold_ended = _ns(gate.get("released_monotonic_ns"))
    return (
        gate.get("terminal") == SERVED
        and lost is not None
        and hold_ended is not None
        and hold_ended >= lost
        and (hold_ended - lost) / 1e9 < LOSS_TO_RELEASE_SECONDS
    )


def _loss_invalid(record: Mapping[str, Any], case: LossCase) -> list[str]:
    """Why the record does not measure the loss it declares: each reason
    starts with ``INVALID`` and is never a product finding."""
    found: list[str] = []
    if record.get("username") != LOSS_USERNAME:
        found.append(f"the record names {record.get('username')!r} as its username")
    gate = _gate(record, person_path(LOSS_USERNAME, HELD_SECTION))
    entered = _ns((gate or {}).get("entered_monotonic_ns"))
    loss = _mapping(record.get("loss"))
    lost = _ns(loss.get("monotonic_ns"))
    if gate is None:
        found.append("the record holds no gate on the held section")
    elif entered is None:
        found.append(
            "the held section's request never entered the gate, so nothing was "
            "lost mid-call"
        )
    if not loss:
        found.append("the record holds no loss")
    else:
        if loss.get("kind") != case.termination:
            found.append(
                f"the loss made was {loss.get('kind')!r}, not the row's "
                f"{case.termination}"
            )
        if loss.get("error"):
            found.append(f"the loss failed: {loss['error']}")
        if lost is None:
            found.append("the loss has no time")
    if entered is not None and lost is not None and lost < entered:
        found.append("the loss came before the held request entered the gate")
    cleanup = _ns(record.get("cleanup_began_ns"))
    if gate is not None and entered is not None:
        hold_ended = _ns(gate.get("released_monotonic_ns"))
        if lost is not None and hold_ended is not None and hold_ended < lost:
            found.append("the hold had ended before the loss")
        if hold_ended is None:
            found.append("the hold's end has no time")
        else:
            # When the hold let go, not when its release was asked for: a
            # handler resumed late still records ``served``.
            held = (hold_ended - entered) / 1e9
            if held > GATE_DEADLINE_SECONDS:
                found.append(
                    f"the hold let go {held:.1f}s after its entry, past the "
                    f"gate's {GATE_DEADLINE_SECONDS}s deadline"
                )
            if _inside_expiry_window(record, lost):
                assert lost is not None
                after = (hold_ended - lost) / 1e9
                found.append(
                    f"the hold let go {after:.1f}s after the loss, inside an "
                    f"owner's {LOSS_TO_RELEASE_SECONDS}s expiry window: what "
                    f"followed says nothing of the read going on"
                )
        terminal = gate.get("terminal")
        if terminal == DEADLINE:
            found.append("the hold ran out its deadline before the release")
        elif terminal not in (SERVED, PEER_GONE):
            found.append(f"the hold recorded no end: {terminal!r}")
        if gate.get("released_by") != RELEASED_BY_ROW:
            found.append(f"the hold was released by {gate.get('released_by')!r}")
        requested = _ns(gate.get("release_requested_monotonic_ns"))
        if requested is None:
            found.append("no release was asked for")
        else:
            late = (requested - entered) / 1e9 - RELEASE_SECONDS
            if late < 0:
                found.append("the release was asked for before its declared time")
            elif late > RELEASE_TOLERANCE_SECONDS:
                past = (
                    ", past the gate's deadline"
                    if requested - entered >= GATE_DEADLINE_SECONDS * 1e9
                    else ""
                )
                found.append(
                    f"the release was asked for {late:.1f}s after its declared "
                    f"time{past}"
                )
            # The watch the script recorded finishing, not the later cleanup:
            # time spent elsewhere before cleanup is no watch.
            watched = _ns(record.get("watched_until_ns"))
            if (
                watched is None
                or watched - requested < CONTINUATION_SECONDS * 1e9
                or cleanup is None
                or cleanup < watched
            ):
                found.append(
                    f"the origin was watched for less than {CONTINUATION_SECONDS}s "
                    f"after the release"
                )
    calls = _person_calls(record)
    began = _ns((calls[0] if calls else {}).get("began_monotonic_ns"))
    if began is None or entered is None or began > entered:
        found.append("the held read is not shown sent before its page entered the gate")
    next_page = {person_path(LOSS_USERNAME, NEXT_SECTION)}
    if _counts(_requests(record, next_page), lost)[0]:
        found.append(f"the read had reached the {NEXT_SECTION} page before the loss")
    if case.second:
        second = calls[1] if len(calls) > 1 else {}
        sent = _ns(second.get("began_monotonic_ns"))
        ended = _ns(second.get("ended_monotonic_ns"))
        if sent is None or lost is None or sent > lost:
            found.append("the second read is not shown sent before the loss")
        elif ended is not None and ended < lost:
            found.append("the second read had ended before the loss")
        if _counts(_requests(record, _person_paths(SECOND_USERNAME)), lost)[0]:
            found.append(
                "the second read had begun before the loss, so it was not "
                "outstanding unstarted"
            )
    # Every reading after the loss was taken after it and before the harness
    # began its cleanup; one outside that window says nothing of the product.
    readings = [
        ("the server's exit", _mapping(record.get("server_exit"))),
        ("the settlement", _mapping(record.get("settlement"))),
        ("the owner reading after the loss", _mapping(record.get("owner_after_loss"))),
        (
            "the owner reading after the fresh read",
            _mapping(record.get("owner_after_fresh")),
        ),
    ]
    for label, reading in readings:
        if not reading:
            continue
        seen = _ns(reading.get("seen_ns"))
        if seen is None or lost is None or seen < lost:
            found.append(f"{label} is not shown read after the loss")
        elif cleanup is None or seen > cleanup:
            found.append(
                f"{label} was read after the harness's cleanup began, so it "
                f"cannot be credited to the product"
            )
    return [f"{INVALID}{reason}" for reason in found]


def _settlement_findings(record: Mapping[str, Any], case: LossCase) -> list[str]:
    """Direct: what settled by itself after the loss, read before cleanup."""
    settlement = _mapping(record.get("settlement"))
    if not settlement:
        return ["the profile was not read after the loss"]
    found = []
    if settlement.get("error"):
        found.append(
            f"reading the profile after the loss failed: {settlement['error']}"
        )
    if settlement.get("remaining") != [] or settlement.get("unresolved") != []:
        found.append(
            f"the profile's browser is not shown gone after the loss: still "
            f"{settlement.get('remaining')!r}, unreadable "
            f"{settlement.get('unresolved')!r}"
        )
    windows = str(record.get("platform", "")).startswith("win")
    lease = settlement.get("lease")
    if windows:
        if lease not in (LEASE_UNOBSERVED, lease_probe.FREE):
            found.append(f"the profile lease was {lease!r} after the loss")
    elif lease != lease_probe.FREE:
        found.append(f"the profile lease was {lease!r} after the loss, not free")
    # No guardian runs on Windows, where per-launch Jobs hold the browser.
    if (
        case.termination == ACTOR_KILLED
        and not windows
        and settlement.get("guardian_exit") != "exited"
    ):
        found.append(
            f"the killed server's guardian is not shown to drain and exit: "
            f"{settlement.get('guardian_exit')!r}"
        )
    return found


def _hot_reuse_findings(record: Mapping[str, Any]) -> list[str]:
    """Daemon: the identified owner kept, and the fresh read through it
    inside the declared window; a successor is recorded apart and fails."""
    identified = _sequence(record.get("owner_identified"))
    if len(identified) != 3:
        return ["the owner was never identified before the loss"]
    found = []
    for label, words in (
        ("owner_after_loss", "after the loss"),
        ("owner_after_fresh", "after the fresh read"),
    ):
        seen = _mapping(record.get(label))
        if not seen:
            found.append(f"the owner was not read {words}")
            continue
        if seen.get("alive") is not True:
            found.append(
                f"the identified owner is not shown alive {words}: hot reuse of "
                f"the same owner is not shown"
            )
        if (
            not same_lifetime(seen.get("lifetime"), identified[:2])
            or seen.get("instance_id") != identified[2]
        ):
            found.append(
                f"the owner's lifetime changed {words}: {seen.get('lifetime')!r}, "
                f"instance {seen.get('instance_id')!r}, not the identified "
                f"{list(identified)}; hot reuse of the same owner is not shown"
            )
    successors = _other_launches(record, identified[:2])
    if successors is None:
        found.append("the row's owner and release gate lifetimes were not recorded")
    elif successors:
        found.append(
            f"the row started another owner, recorded apart as a successor: "
            f"{successors}; hot reuse of the same owner is not shown"
        )
    lost = _ns(_mapping(record.get("loss")).get("monotonic_ns"))
    fresh_call = _mapping(_mapping(record.get("fresh")).get("call"))
    began = _ns(fresh_call.get("began_monotonic_ns"))
    if began is not None and lost is not None:
        after = (began - lost) / 1e9
        if after > HOT_REUSE_WINDOW_SECONDS:
            # The row's own waits (the lost calls, the frontend's exit, the
            # watch) come first, and each has its own verdict; a late fresh
            # read leaves hot reuse untested, which is no finding.
            found.append(
                f"{INVALID}the fresh read began {after:.1f}s after the loss, "
                f"outside the declared {HOT_REUSE_WINDOW_SECONDS}s window: hot "
                f"reuse is not shown"
            )
    cause = record.get("cause")
    if cause not in (CAUSE_EXPIRY, CAUSE_UNOBSERVED):
        found.append(
            f"the cancellation cause is recorded as {cause!r}; only the owner's "
            f"expiry line, or its absence as {CAUSE_UNOBSERVED!r}, is observed"
        )
    return found


def _loss_findings(
    record: Mapping[str, Any], case: LossCase, *, daemon: bool
) -> list[str]:
    """What the product did after the loss, judged."""
    found = []
    lost = _ns(_mapping(record.get("loss")).get("monotonic_ns"))
    host = _mapping(record.get("host"))
    if host.get("error"):
        found.append(f"the host session failed before its loss: {host['error']}")
    if host.get("lost") != case.termination:
        found.append(
            f"the host records the loss {host.get('lost')!r}, not {case.termination}"
        )
    # Inside an owner's expiry window the read may still go on; that case is
    # invalid evidence (``_loss_invalid``), so nothing after it is judged.
    judged = not _inside_expiry_window(record, lost)
    next_page = {person_path(LOSS_USERNAME, NEXT_SECTION)}
    _, after, unknown = _counts(_requests(record, next_page), lost)
    if judged and (after or unknown):
        found.append(
            f"the read went on after the loss: the {NEXT_SECTION} page was "
            f"requested {after + unknown} times after it"
        )
    held_page = {person_path(LOSS_USERNAME, HELD_SECTION)}
    _, after, unknown = _counts(_requests(record, held_page), lost)
    if judged and (after or unknown):
        found.append("the held page was asked for again after the loss")
    if judged and case.second:
        second_pages = _person_paths(SECOND_USERNAME)
        _, after, unknown = _counts(_requests(record, second_pages), lost)
        if after or unknown:
            found.append(
                f"the second read went on after the loss: {after + unknown} of "
                f"its pages were requested"
            )
    who = "frontend" if daemon else "Direct server"
    how = _mapping(record.get("server_exit")).get("how")
    if how != "exited":
        found.append(
            f"the {who} is not shown to exit by itself within "
            f"{SERVER_EXIT_SECONDS}s of the loss: {how!r}"
        )
    if host.get("killed_by_harness") or host.get("stop_ns") is not None:
        found.append(f"the harness had to end the {who} after the loss")
    if daemon:
        found += _hot_reuse_findings(record)
    else:
        found += _settlement_findings(record, case)
        if "cause" in record:
            found.append("a Direct record names a cancellation cause")
    fresh = _mapping(record.get("fresh"))
    call = _mapping(fresh.get("call"))
    if fresh.get("made") is not True:
        found.append(f"no read was made after the loss: {fresh.get('why')!r}")
    elif (
        call.get("outcome") != "returned"
        or call.get("is_error") is not False
        or call.get("read_the_post") is not True
    ):
        found.append(
            f"the read after the loss did not return the synthetic post: "
            f"{call.get('outcome')!r}, error {call.get('is_error')!r}"
        )
    else:
        if fresh.get("quit_problems"):
            found.append(
                f"the fresh host did not quit normally: {fresh['quit_problems']}"
            )
        if daemon and fresh.get("forwarded") is not True:
            found.append("the fresh frontend did not forward to the shared owner")
        if not daemon and fresh.get("forwarded"):
            found.append("the fresh Direct host forwarded to a shared owner")
    found += _session_problems(record)
    return found


def loss_problems(record: Mapping[str, Any] | None, *, daemon: bool) -> list[str]:
    """A losing row's verdict over its raw record: every problem, or nothing.

    Invalid evidence starts with ``INVALID``; every other problem is a
    finding about the product, or a record missing a part. A missing expiry
    line is never one of them, and an expiry line excuses nothing.
    """
    if not isinstance(record, Mapping):
        return ["the row kept no record"]
    row = record.get("row")
    case = LOSS_CASES.get(row) if isinstance(row, str) else None
    if case is None:
        return [f"the record is for row {row!r}, which loses no host"]
    mode = "daemon" if daemon else "direct"
    problems: list[str] = []
    if record.get("mode") != mode:
        problems.append(f"the record is for mode {record.get('mode')!r}, not {mode}")
    if record.get("script_error"):
        problems.append(f"the row's script failed: {record['script_error']}")
    problems += [str(p) for p in _sequence(record.get("observation_problems"))]
    if record.get("idle_timeout_seconds") != LOSS_IDLE_TIMEOUT_SECONDS:
        problems.append(
            f"the row ran with an idle timeout of "
            f"{record.get('idle_timeout_seconds')!r}, not the declared "
            f"{LOSS_IDLE_TIMEOUT_SECONDS}"
        )
    if record.get("k2") != LOSS_K2_NOT_APPLICABLE:
        problems.append("the record does not say why K2 is not applicable")
    if record.get("timing_objective") != TIMING_UNCLAIMED:
        problems.append(
            "the record claims the contract's cancellation bound, which nothing "
            "here observes"
        )
    problems += _loss_invalid(record, case)
    # With no loss made there is nothing of the product's to judge: what it
    # did next was never the answer to a loss.
    if record.get("loss"):
        problems += _loss_findings(record, case, daemon=daemon)
    return problems


def invalid_evidence(problems: Sequence[str]) -> list[str]:
    """The problems that leave a row unmeasured rather than failed."""
    return [problem for problem in problems if problem.startswith(INVALID)]


def loss_semantics(record: Mapping[str, Any]) -> dict[str, Any]:
    """What K0 compares on a losing row: every classification, and no pid,
    time or path. The cancellation cause is not one: whether the owner's
    expiry or the frontend's disconnect cancelled first is a race the
    contract allows either way."""
    return {
        "row": record.get("row"),
        "mode": record.get("mode"),
        **loss_reading(record),
    }


def _valid_or_refused(
    named: Sequence[tuple[str, Mapping[str, Any] | None, bool]],
    verdict: Callable[..., list[str]],
) -> list[str]:
    refusals = []
    for name, record, is_daemon in named:
        problems = verdict(record, daemon=is_daemon)
        if problems:
            refusals.append(f"the {name} record is not valid: {problems}")
    return refusals


def loss_semantic_differences(
    reference: Mapping[str, Any] | None, repeat: Mapping[str, Any] | None
) -> list[str]:
    """K0 against K3 on a losing row: both valid by their own verdict, and
    alike in every classification. A missing or invalid record is a refusal."""
    refusals = _valid_or_refused(
        [("reference", reference, True), ("repeat", repeat, True)], loss_problems
    )
    if refusals:
        return refusals
    assert reference is not None and repeat is not None
    one, two = loss_semantics(reference), loss_semantics(repeat)
    return [
        f"{name}: {one[name]!r} then {two.get(name)!r}"
        for name in one
        if one[name] != two.get(name)
    ]


def loss_comparison(
    direct: Mapping[str, Any] | None, daemon: Mapping[str, Any] | None
) -> list[str]:
    """Why K3 is not shown as safe as K1 on a losing row beyond O1 to O4
    (``compare_to_direct``): a record missing or invalid.

    Nothing going on after the loss, and a read succeeding after it, are each
    record's own verdict, so two valid records agree on both; a record that
    fails either is refused here rather than compared.
    """
    return _valid_or_refused(
        [("Direct", direct, False), ("daemon", daemon, True)], loss_problems
    )
