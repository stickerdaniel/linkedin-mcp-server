"""Calls that lose their caller, and the read that calibrates them: row H-CAL.

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

The script runs on a ``harness.RowContext``; nothing here reads a process,
and the verdict reads the raw record alone, so it can be replayed from the
published packet.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from differential.host_comparison import host_problems
from differential.synthetic_origin import (
    DEADLINE,
    GATE_DEADLINE_SECONDS,
    PEER_GONE,
    RELEASED_BY_ROW,
    SERVED,
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
