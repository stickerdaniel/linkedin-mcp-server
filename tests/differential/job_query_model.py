"""H-R11's routine branch as a source model: the exact functions, Win32 doubles.

**Claim.** At the routine drain's decision point for one member of the
adopted Job, where no Job the owner holds claimed the member and at least one
of them could not be asked, the baseline selects ``TerminateProcess`` for it
and the candidate counts it as remaining and does not. Around that branch both
revisions keep the same positive controls: a member a held Job claims is
spared, a later positive answer outweighs an earlier failure, a member known
to be in no held Job is still ended, an unanswered inventory, open or
membership query is never read as empty, the exclusions are never opened and
every handle opened is closed.

**Evidence.** ``source-model``. The definitions of ``_drain_exclusions``,
``_in_another_owned_job`` and ``_drain_adopted_windows_job_members`` are taken
from each revision's source text and run, unchanged, against Win32 doubles
whose answers each case scripts per iteration of the drain. What the model
shows is the branch each revision's code selects in a modelled state. It
shows nothing about native API delivery, which process a native termination
ended, or O2 across the system; the native experiments
(``test_failed_job_query_row.py``) carry those, and no result of one kind is
counted as the other (review e1ex, E1EX-02).
"""

from __future__ import annotations

import __future__
import ast
import contextlib
import hashlib
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import CodeType, SimpleNamespace
from typing import Any

from differential.job_query import reached, shim_namespace

MODULE = "linkedin_mcp_server.process_tree"
#: The routine, as the drain reaches it: the exclusions, the held-Job helper
#: and the routine drain itself.
ROUTINE = (
    "_drain_exclusions",
    "_in_another_owned_job",
    "_drain_adopted_windows_job_members",
)
_CONSTANTS = ("_JOB_POLL_SECONDS",)

SOURCE_MODEL = "source-model"
BASELINE = "baseline"
CANDIDATE = "candidate"

#: The adopted Job's handle, and the gate the drain must never end.
ADOPTED_JOB = 123
GATE = 3572
#: The installer's Job, held by the owner, and a second held Job.
INSTALLER_JOB = 55
BROWSER_JOB = 56

#: What a Win32 membership query answers.
IN = "in"
OUT = "out"
ERROR = "error"

_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
#: ``JOBOBJECTINFOCLASS.JobObjectBasicProcessIdList``.
_BASIC_PROCESS_ID_LIST = 3


class ModelRefused(RuntimeError):
    """The source does not define the routine the model runs."""


def source_sha256(text: str) -> str:
    """One revision's source by content, whatever line endings its checkout has."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Routine:
    """One revision's routine, compiled from its own source text."""

    revision: str
    sha256: str
    code: CodeType

    @classmethod
    def load(cls, revision: str, source: str) -> Routine:
        """The exact definitions from *source*; refused unless each is there once."""
        chosen: list[ast.stmt] = []
        names: list[str] = []
        for node in ast.parse(source).body:
            if isinstance(node, ast.FunctionDef) and node.name in ROUTINE:
                chosen.append(node)
                names.append(node.name)
            elif isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if any(name in _CONSTANTS for name in targets):
                    chosen.append(node)
                    names += targets
        for name in (*ROUTINE, *_CONSTANTS):
            if names.count(name) != 1:
                raise ModelRefused(
                    f"{revision}: {name} is defined {names.count(name)} times in "
                    f"the source"
                )
        code = compile(
            ast.Module(body=chosen, type_ignores=[]),
            f"<{revision} {MODULE}>",
            "exec",
            flags=__future__.annotations.compiler_flag,
            dont_inherit=True,
        )
        return cls(revision, source_sha256(source), code)

    def namespace(self) -> dict[str, Any]:
        """A fresh module namespace, named as the shim's frame check expects."""
        namespace: dict[str, Any] = {
            "__name__": MODULE,
            "Any": Any,
            "contextlib": contextlib,
            "os": os,
        }
        exec(self.code, namespace)
        return namespace


class Win32Error(Exception):
    """Stands in for ``pywintypes.error``, which every failed call here raises."""

    def __init__(self, winerror: int, funcname: str, strerror: str) -> None:
        super().__init__(winerror, funcname, strerror)
        self.winerror = winerror


def _at(answers: Sequence[Any], iteration: int) -> Any:
    """The answer for *iteration*, the last one standing for every later one."""
    return answers[min(max(iteration, 1), len(answers)) - 1]


@dataclass(frozen=True)
class Member:
    """One process id in the adopted Job, and how Win32 answers about it.

    Every answer is a sequence read by the drain's iteration, which is its
    count of inventory queries so far.
    """

    pid: int
    #: Per held Job handle: whether it holds this member.
    held: Mapping[int, Sequence[str]] = field(default_factory=dict)
    #: Whether the adopted Job still holds it when asked through its handle.
    adopted: Sequence[str] = (IN,)
    opens: Sequence[bool] = (True,)
    terminates: Sequence[bool] = (True,)
    created: float = 9.0


class _Handle:
    """A ``PyHANDLE`` from ``OpenProcess``: closed once, by ``Close``."""

    def __init__(self, world: World, pid: int, access: int) -> None:
        self.world, self.pid, self.access = world, pid, access
        self.closes = 0

    def Close(self) -> None:
        self.closes += 1

    def __int__(self) -> int:
        return 0x4000 + self.pid


class World:
    """The Win32 the routine drain sees: its adopted Job, the Jobs the owner
    holds, the members, and a clock that only its ``sleep`` moves.

    One object stands for ``win32api``, ``win32job`` and ``time``: their names
    do not overlap. A process the drain ended leaves the inventory, as a
    terminated process leaves its Job.
    """

    JobObjectBasicProcessIdList = _BASIC_PROCESS_ID_LIST

    def __init__(
        self,
        members: Sequence[Member] = (),
        *,
        held: Sequence[int | None] = (INSTALLER_JOB, None),
        inventory: Sequence[bool] = (True,),
        spared: Sequence[int] = (),
    ) -> None:
        self.members = {member.pid: member for member in members}
        #: The owner's live Jobs; None is one whose handle is already gone.
        self.held = tuple(held)
        self.inventory = tuple(inventory)
        #: Ids in the inventory that the drain must leave alone.
        self.spared = tuple(spared)
        self.iteration = 0
        self.now = 0.0
        self.calls = 0
        self.dead: set[int] = set()
        self.asked_to_open: list[int] = []
        self.handles: list[_Handle] = []
        #: Every ``TerminateProcess`` call, as (pid, iteration), whatever it did.
        self.selected: list[tuple[int, int]] = []

    # --- win32job -------------------------------------------------------------

    def QueryInformationJobObject(self, job: Any, info: int) -> tuple[int, ...]:
        self.calls += 1
        if job != ADOPTED_JOB or info != _BASIC_PROCESS_ID_LIST:
            raise Win32Error(87, "QueryInformationJobObject", "wrong Job or class")
        self.iteration += 1
        if not _at(self.inventory, self.iteration):
            raise Win32Error(5, "QueryInformationJobObject", "Access is denied.")
        alive = [pid for pid in self.members if pid not in self.dead]
        return (*self.spared, *alive)

    def IsProcessInJob(self, handle: _Handle, job: Any) -> bool:
        self.calls += 1
        member = self.members[handle.pid]
        if job == ADOPTED_JOB:
            answer = _at(member.adopted, self.iteration)
        elif job in self.held:
            answer = _at(member.held.get(job, (OUT,)), self.iteration)
        else:
            raise Win32Error(6, "IsProcessInJob", "The handle is invalid.")
        if answer == ERROR:
            raise Win32Error(5, "IsProcessInJob", "Access is denied.")
        return answer == IN

    # --- win32api -------------------------------------------------------------

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> _Handle:
        self.calls += 1
        self.asked_to_open.append(pid)
        member = self.members.get(pid)
        if member is None or pid in self.dead or not _at(member.opens, self.iteration):
            raise Win32Error(87, "OpenProcess", "The parameter is incorrect.")
        handle = _Handle(self, pid, access)
        self.handles.append(handle)
        return handle

    def TerminateProcess(self, handle: _Handle, status: int) -> None:
        self.calls += 1
        self.selected.append((handle.pid, self.iteration))
        if handle.closes or not handle.access & _PROCESS_TERMINATE:
            raise Win32Error(5, "TerminateProcess", "Access is denied.")
        if not _at(self.members[handle.pid].terminates, self.iteration):
            raise Win32Error(5, "TerminateProcess", "Access is denied.")
        self.dead.add(handle.pid)

    # --- time -------------------------------------------------------------------

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def modules(self) -> tuple[Any, Any, Any, Any]:
        """What ``_windows_modules`` returns: win32api, win32con, win32job, winerror."""
        con = SimpleNamespace(
            PROCESS_TERMINATE=_PROCESS_TERMINATE,
            PROCESS_QUERY_LIMITED_INFORMATION=_PROCESS_QUERY_LIMITED_INFORMATION,
        )
        return self, con, self, SimpleNamespace()


@dataclass(frozen=True)
class Outcome:
    """What one revision's routine did in one modelled state."""

    returned: bool | None
    raised: str | None
    #: The first iteration each member was selected for ``TerminateProcess``.
    selected: Mapping[int, int]
    iterations: int
    #: Handles opened and not closed exactly once.
    unclosed: tuple[int, ...]
    #: Excluded ids the drain asked to open anyway.
    opened_spared: tuple[int, ...]
    #: Win32 calls made at all.
    calls: int
    #: Members the shim recorded a planted failure for, in a planted case.
    witnessed: tuple[Any, ...] = ()


@dataclass(frozen=True)
class Expect:
    returned: bool
    #: Member -> the iteration it is first selected for termination.
    selected: Mapping[int, int] = field(default_factory=dict)
    iterations: int | None = None
    calls: int | None = None
    witnessed: tuple[Any, ...] | None = None


#: The drain's deadline in every case: a hundred polls of the real interval.
_DEADLINE_SECONDS = 1.0


@dataclass(frozen=True)
class Case:
    """One modelled state, and what each revision's routine must do in it."""

    name: str
    claim: str
    world: Callable[[], World]
    expect: Mapping[str, Expect]
    adopted: bool = True
    #: Fail the held-Job queries with the declared shim instead of the double.
    planted: bool = False

    def run(self, routine: Routine) -> Outcome:
        world = self.world()
        namespace = routine.namespace()
        namespace.update(
            _adopted_windows_job=ADOPTED_JOB if self.adopted else None,
            _adopted_windows_gate=GATE,
            _live_windows_jobs=[SimpleNamespace(job_handle=h) for h in world.held],
            _windows_modules=world.modules,
            time=world,
        )
        witnessed: tuple[Any, ...] = ()
        with tempfile.TemporaryDirectory(prefix="h-r11-model-") as scratch:
            record = Path(scratch) / "reached.jsonl"
            if self.planted:
                shim_namespace()["install"](
                    world,
                    Win32Error,
                    lambda handle: (handle.pid, world.members[handle.pid].created),
                    str(record),
                    created=5.0,
                )
            returned: bool | None = None
            raised: str | None = None
            try:
                returned = namespace["_drain_adopted_windows_job_members"](
                    world.now + _DEADLINE_SECONDS
                )
            except BaseException as exc:  # noqa: BLE001 - the case's own finding
                raised = repr(exc)
            if self.planted:
                witnessed = tuple(
                    line.get("member")
                    for line in reached(record)
                    if line.get("pid") == os.getpid() and line.get("pid_created") == 5.0
                )
        selected: dict[int, int] = {}
        for pid, iteration in world.selected:
            selected.setdefault(pid, iteration)
        return Outcome(
            returned=returned,
            raised=raised,
            selected=selected,
            iterations=world.iteration,
            unclosed=tuple(h.pid for h in world.handles if h.closes != 1),
            opened_spared=tuple(
                pid
                for pid in world.asked_to_open
                if pid in (0, os.getpid(), GATE) or pid in world.spared
            ),
            calls=world.calls,
            witnessed=tuple(sorted(set(witnessed), key=str)),
        )

    def check(self, revision: str, outcome: Outcome) -> list[str]:
        """Why *outcome* is not what *revision*'s routine must do here."""
        expect = self.expect[revision]
        problems = []
        if outcome.raised is not None:
            problems.append(f"the drain raised {outcome.raised}")
        if outcome.returned is not expect.returned:
            problems.append(
                f"the drain returned {outcome.returned!r}, not {expect.returned!r}"
            )
        if dict(outcome.selected) != dict(expect.selected):
            problems.append(
                f"TerminateProcess was selected for {dict(outcome.selected)} "
                f"(member: first iteration), not {dict(expect.selected)}"
            )
        if expect.iterations is not None and outcome.iterations != expect.iterations:
            problems.append(
                f"the drain took {outcome.iterations} iterations, not "
                f"{expect.iterations}"
            )
        if expect.calls is not None and outcome.calls != expect.calls:
            problems.append(f"{outcome.calls} Win32 calls, not {expect.calls}")
        if expect.witnessed is not None and outcome.witnessed != expect.witnessed:
            problems.append(
                f"the shim recorded failures for {outcome.witnessed}, not "
                f"{expect.witnessed}"
            )
        if outcome.unclosed:
            problems.append(f"handles not closed exactly once: {outcome.unclosed}")
        if outcome.opened_spared:
            problems.append(f"excluded ids were opened: {outcome.opened_spared}")
        return problems


def _both(expect: Expect) -> dict[str, Expect]:
    return {BASELINE: expect, CANDIDATE: expect}


def _installer(pid: int = 700, **fields: Any) -> Member:
    return Member(pid, **fields)


#: Every modelled state, the discriminating ones first.
CASES: tuple[Case, ...] = (
    Case(
        "unknown",
        "every held Job unanswered: the baseline selects TerminateProcess, the "
        "candidate counts the member and never completes",
        lambda: World([_installer(held={INSTALLER_JOB: (ERROR,)})]),
        {
            BASELINE: Expect(True, {700: 1}, iterations=2),
            CANDIDATE: Expect(False),
        },
    ),
    Case(
        "planted",
        "the declared shim fails the held-Job query of the real helper, and "
        "only that one: the same branches, with a positive record of each call",
        lambda: World([_installer(held={INSTALLER_JOB: (IN,)})]),
        {
            BASELINE: Expect(True, {700: 1}, iterations=2, witnessed=(700,)),
            CANDIDATE: Expect(False, witnessed=(700,)),
        },
        planted=True,
    ),
    Case(
        "mixed",
        "an unanswered member, a claimed one and one in no held Job, together",
        lambda: World(
            [
                _installer(700, held={INSTALLER_JOB: (ERROR,)}),
                _installer(701, held={INSTALLER_JOB: (IN,)}),
                _installer(702, held={INSTALLER_JOB: (OUT,)}),
            ]
        ),
        {
            BASELINE: Expect(True, {700: 1, 702: 1}, iterations=2),
            CANDIDATE: Expect(False, {702: 1}),
        },
    ),
    Case(
        "unknown-then-claimed",
        "unanswered, then claimed by a held Job: only the baseline ended it",
        lambda: World([_installer(held={INSTALLER_JOB: (ERROR, IN)})]),
        {
            BASELINE: Expect(True, {700: 1}, iterations=2),
            CANDIDATE: Expect(True, iterations=2),
        },
    ),
    Case(
        "unknown-then-out",
        "unanswered, then known to be in no held Job: an independent later "
        "answer may still end it",
        lambda: World([_installer(held={INSTALLER_JOB: (ERROR, OUT)})]),
        {
            BASELINE: Expect(True, {700: 1}, iterations=2),
            CANDIDATE: Expect(True, {700: 2}, iterations=3),
        },
    ),
    Case(
        "unknown-then-left",
        "unanswered, then gone from the adopted Job",
        lambda: World([_installer(held={INSTALLER_JOB: (ERROR,)}, adopted=(IN, OUT))]),
        {
            BASELINE: Expect(True, {700: 1}, iterations=2),
            CANDIDATE: Expect(True, iterations=2),
        },
    ),
    Case(
        "claimed",
        "a held Job holds it: spared",
        lambda: World([_installer(held={INSTALLER_JOB: (IN,)})]),
        _both(Expect(True, iterations=1)),
    ),
    Case(
        "later-claim-dominates",
        "one held Job unanswered, a later one holds it: spared",
        lambda: World(
            [_installer(held={INSTALLER_JOB: (ERROR,), BROWSER_JOB: (IN,)})],
            held=(INSTALLER_JOB, BROWSER_JOB),
        ),
        _both(Expect(True, iterations=1)),
    ),
    Case(
        "out",
        "every held Job answers no: ended",
        lambda: World([_installer(held={INSTALLER_JOB: (OUT,)})]),
        _both(Expect(True, {700: 1}, iterations=2)),
    ),
    Case(
        "inventory-unanswered",
        "the adopted Job's inventory never answers: never read as empty",
        lambda: World(inventory=(False,)),
        _both(Expect(False)),
    ),
    Case(
        "inventory-unanswered-once",
        "an unanswered inventory is not the empty one that follows it",
        lambda: World(inventory=(False, True)),
        _both(Expect(True, iterations=2)),
    ),
    Case(
        "open-refused",
        "the member cannot be opened: counted, never empty",
        lambda: World([_installer(held={INSTALLER_JOB: (OUT,)}, opens=(False,))]),
        _both(Expect(False)),
    ),
    Case(
        "adopted-unanswered",
        "the adopted Job's own membership query fails: counted, never ended",
        lambda: World([_installer(held={INSTALLER_JOB: (OUT,)}, adopted=(ERROR,))]),
        _both(Expect(False)),
    ),
    Case(
        "terminate-refused",
        "TerminateProcess fails: counted, and tried again",
        lambda: World([_installer(held={INSTALLER_JOB: (OUT,)}, terminates=(False,))]),
        _both(Expect(False, {700: 1})),
    ),
    Case(
        "left-the-job",
        "the id left the adopted Job: neither counted nor ended",
        lambda: World([_installer(held={INSTALLER_JOB: (ERROR,)}, adopted=(OUT,))]),
        _both(Expect(True, iterations=1)),
    ),
    Case(
        "exclusions",
        "the owner, its gate and the idle id are never opened",
        lambda: World(spared=(0, os.getpid(), GATE)),
        _both(Expect(True, iterations=1)),
    ),
    Case(
        "empty",
        "an answered, empty inventory completes",
        lambda: World(),
        _both(Expect(True, iterations=1)),
    ),
    Case(
        "no-adopted-job",
        "Direct: no adopted Job, so nothing is asked at all",
        lambda: World([_installer(held={INSTALLER_JOB: (ERROR,)})]),
        _both(Expect(True, iterations=0, calls=0)),
        adopted=False,
    ),
)


@dataclass(frozen=True)
class RoutineModel:
    """The source-model calibration of H-R11's routine branch, for one pair of
    sources, run in this process.

    ``sha256`` names each revision's source by content, so a native packet
    can be bound to the code the model ran. ``problems`` is empty only when
    every case matched what each revision must do, the discriminating ones
    included.
    """

    evidence: str
    sha256: Mapping[str, str]
    outcomes: tuple[tuple[str, str, Outcome], ...]
    problems: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.problems


def calibrate(sources: Mapping[str, str]) -> RoutineModel:
    """Run every case against the baseline's and the candidate's routine."""
    problems: list[str] = []
    routines: dict[str, Routine] = {}
    for revision in (BASELINE, CANDIDATE):
        source = sources.get(revision)
        if source is None:
            problems.append(f"{revision}: no source to model")
            continue
        try:
            routines[revision] = Routine.load(revision, source)
        except (ModelRefused, SyntaxError) as exc:
            problems.append(str(exc))
    outcomes = []
    for case in CASES:
        for revision, routine in routines.items():
            outcome = case.run(routine)
            outcomes.append((case.name, revision, outcome))
            problems += [
                f"{case.name}, {revision}: {problem}"
                for problem in case.check(revision, outcome)
            ]
    return RoutineModel(
        evidence=SOURCE_MODEL,
        sha256={revision: routine.sha256 for revision, routine in routines.items()},
        outcomes=tuple(outcomes),
        problems=tuple(problems),
    )
