"""Auth repair and response loss (H-R16), and ``--login`` beside a live owner
(H-R10a-login).

**The sign-in is the synthetic origin's** (``synthetic_origin``: the wall, the
completion request, ``LoginFixture``). The harness rejects the staged session
at the origin, which is what an expired session looks like from the product's
side, and that rejection is the row's recorded authorization
(``session.ORIGIN_REJECTED``): it comes first, and the session read right
after it still shows the original generation. The product's own login then
opens the wall headed, its page asks for completion again and again, and only
the row's release lets an answer set a fresh ``li_at``. Each cell declares
the same bounds in K1, K3 and K0 (``ENVIRONMENT``): ``LOGIN_TIMEOUT``, the
inline wait, the tool budget, and the passive bound a failed login has to be
gone in. Three ends are told apart and never merged: completion, a frontend
whose wait ran out while the login went on, and the login itself failing,
the only one that counts as a settled failed login.

**H-R16, cold** (``ROW_COLD``). After the warm-up read the host closes the
browser (``close_session``) and the row waits for it to be gone, so the next
read starts one cold, whose startup validation meets the wall. K3: the owner
marks the failure replayable, the frontend signs in, the row releases the
completion once the login asks for it, and the frontend runs the read again
once: the host's one call returns the post. K1 frozen: Direct detects, starts
its own login and answers that a login started; once the login completed the
host calls again and reads. **Second frontend** (``ROW_SECOND``, daemon
only): a second host calls while the first frontend's login waits; the owner
answers both from its latch and opens no browser on the stale generation, one
fresh session is issued, and both reads end on it. **Failed login**
(``ROW_FAILED``): the completion is never released; no replay, the login
settles failed inside its own budget, and the session's fate is read without
anything that could sign in again (``must-not-repair``: the origin's own
judgement of the session on disk, no browser).

**H-R10a-login** (``ROW_LOGIN``, POSIX terminal). K1 frozen: after the host
quit and its server settled, ``--login``, the completion released once the
login asks, the session replaced. K3: the same beside the idle owner, its
retirement confirmed on the terminal after a fresh checkpoint, the owner
retiring before the login takes the profile. The confirmed command is the
authorization (``session.LOGIN``), recorded before the command starts.

Both expect an authorized replacement (``REPLACED_AFTER_AUTHORIZATION``) or,
for the failed login, an authorized loss (``LOST_AFTER_AUTHORIZATION``): the
original generation's own outcome is read as always, and the lineage beside
it (``session.replacement_lineage``). An unexpected loss of the original, a
session issued with nobody authorizing it, or a fresh session destroyed by a
second repair fails, whatever a later sign-in achieved.

**The login is headed.** On Linux it opens on the job's display; macOS and
Windows open it in the runner's own desktop session, which no row before
these measured. A login that never opens its wall leaves the cell invalid
(``the login never asked for its completion``), never a finding.

**Lanes left to models.** A lost marker response needs the frontend's owner
hop routed through a relay; ``owner_hop_relay`` proves such a relay's
boundary process-free, but the frontend reaches its owner where the owner's
own descriptor says, so routing the real hop through the relay means editing
the product's daemon state (the plan's STOP 10), and the lane stays open
(``RESPONSE_LOSS_OPEN``) beside its models (``MODEL_COVERAGE``): the latch
models in ``tests/test_bootstrap.py`` show a later call meets the same
marker, and no test drops a marker response. The ``browser_open`` marker and
the import beside an idle owner (``IMPORT_NOT_NATIVE``) are mapped the same
way.

K2 is recorded not applicable (``K2_NOT_APPLICABLE``). The scripts run on a
``harness.RowContext`` with its ``auth`` seams (and ``commands`` for the
login); the verdicts read the raw record alone, so each can be replayed from
the published packet. Invalid evidence starts with ``INVALID``.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from differential.call_loss import (
    CALIBRATION_IDLE_TIMEOUT_SECONDS,
    INVALID,
    _phase,
    _settled,
)
from differential.host_comparison import host_problems
from differential.owner_loss import (
    _identified,
    _identity,
    _launches,
    _mapping,
    _ns,
    _sequence,
    _settle_tasks,
)
from differential.profile_commands import (
    IDLE_EXIT_LINE,
    LOGIN_ARGS,
    LOGIN_OPENED,
    OUTPUT_END_SECONDS,
    PROFILE_SAVED,
    PROMPT_SECONDS,
    REFUSAL_SECONDS,
    RETIRE_PROMPT,
    RETIRING_LINE,
    STANDING_DOWN_LINE,
    TerminalCommand,
    _answered_after,
    _checkpoint_before_answer,
    _command,
    _first,
    _ran,
    _seen,
)
from differential.retirement_race import WARM_TOOL
from differential.session import (
    LOGIN,
    LOST_AFTER_AUTHORIZATION,
    ORIGIN_REJECTED,
    REPLACED_AFTER_AUTHORIZATION,
)
from differential.synthetic_origin import ALLOWED_HOSTS, COMPLETED_BY_ROW
from linkedin_mcp_server.config.loaders import EnvironmentKeys

if TYPE_CHECKING:
    from differential.harness import AuthSeams, RowContext

ROW_COLD = "H-R16-cold"
ROW_SECOND = "H-R16-second"
ROW_FAILED = "H-R16-failed"
ROW_LOGIN = "H-R10a-login"

#: The calibration's idle timeout, the same in K1, K3 and K0: far above the
#: time from the warm-up read to the cold one, so the owner cannot idle out
#: between them, and the configuration the other call rows measured.
AUTH_IDLE_TIMEOUT_SECONDS = CALIBRATION_IDLE_TIMEOUT_SECONDS

K2_NOT_APPLICABLE = {
    "status": "not applicable",
    "reason": (
        "the plan names a historical-daemon regression witness only for R6, R7, "
        "R11 and R12, none for an auth repair or a login beside an owner, and "
        "the contract forbids inventing one"
    ),
}
#: Why the second frontend has no K1 column.
K1_NOT_APPLICABLE = {
    "status": "not applicable",
    "reason": (
        "a Direct server has no frontend repairing for an owner: a second host "
        "there is a second server meeting the first one's profile lease, which "
        "H-R2 and H-R10b measure, not a repair made on another's behalf"
    ),
}

# --- The cell's declared bounds -----------------------------------------------------

#: How long the product's login waits for the sign-in (``LOGIN_TIMEOUT``).
#: Far above a completed sign-in here (the product's own 15 s for the saved
#: account chooser, then the cookie it finds at once), and short enough that
#: a login never released fails inside the row.
LOGIN_TIMEOUT_SECONDS = 60.0
#: The tool budget (``TOOL_TIMEOUT``). The frontend waits five sixths of it,
#: less what the call spent, for the login it started
#: (``AUTH_REPAIR_LOGIN_WAIT_FRACTION``): 100 s, above a completed login and
#: below the client's own 240 s call bound (``harness._CALL_SECONDS``).
TOOL_TIMEOUT_SECONDS = 120.0
#: The inline wait a missing session's login is given (``LOGIN_INLINE_WAIT``).
LOGIN_INLINE_WAIT_SECONDS = 10.0
#: How long after its own budget a failed login may take to be gone: its
#: browser closed and its asks ended. Passive: the row only waits.
LOGIN_SETTLE_SECONDS = 60.0

#: The cell's environment, over the row's: the same in every column. The
#: automatic import is off, so no real browser profile or keystore is ever
#: asked; the proxy alone would already keep it off (``_auto_import_allowed``).
ENVIRONMENT = {
    EnvironmentKeys.LOGIN_TIMEOUT: f"{LOGIN_TIMEOUT_SECONDS:g}",
    EnvironmentKeys.TOOL_TIMEOUT: f"{TOOL_TIMEOUT_SECONDS:g}",
    EnvironmentKeys.LOGIN_INLINE_WAIT: f"{LOGIN_INLINE_WAIT_SECONDS:g}",
    EnvironmentKeys.AUTO_IMPORT_FROM_BROWSER: "false",
}
#: What the record says of the bounds, read back by the verdict.
BOUNDS = {
    "login_timeout_seconds": LOGIN_TIMEOUT_SECONDS,
    "tool_timeout_seconds": TOOL_TIMEOUT_SECONDS,
    "login_inline_wait_seconds": LOGIN_INLINE_WAIT_SECONDS,
    "login_settle_seconds": LOGIN_SETTLE_SECONDS,
}

#: The row's waits, each from its own start.
#: The warm browser gone after ``close_session``.
CLOSE_SECONDS = 60.0
#: From the cold read to the login's first ask: a cold browser start and its
#: validation, the owner's answer, and a headed login browser's start.
LOGIN_START_SECONDS = 120.0
#: From the second host's start to the owner answering it from its latch.
SECOND_MARK_SECONDS = 90.0
#: From the release to the session issued, the generation written and the
#: login browser gone: the product's own 15 s, its export and its close.
COMPLETION_SECONDS = 90.0
#: The cold read's end, from its start: the tool budget and its margin.
READ_SECONDS = TOOL_TIMEOUT_SECONDS + 30.0
#: The second host's whole session.
SECOND_SECONDS = 240.0
#: The failed login gone, from its wall: its budget and the settle bound.
FAILED_SETTLE_SECONDS = LOGIN_TIMEOUT_SECONDS + LOGIN_SETTLE_SECONDS
#: How often the row looks at the origin's sign-in or the owner's log.
POLL_SECONDS = 0.1

CLOSE_TOOL = "close_session"
READ_ARGUMENTS = {"num_posts": 1}

# --- What the product says ---------------------------------------------------------

#: ``daemon_auth``: the owner marking a failure, with what it marked, and
#: the frontend's ends.
MARKED_LINE = "Asking the client to sign in"
_MARKED = re.compile(
    r"Asking the client to sign in \((?P<reason>\w+), "
    r"replayable=(?P<replayable>True|False)\)"
)
REPLAY_LINE = "Signed in; running the call again"
NOT_REPLAYED_LINES = (
    "The sign-in did not finish in time; not replaying",
    "Signed in; not repeating",
)
SIGNED_IN_LINE = "The sign-in finished"
PEER_LINE = "Another client already signed in"

#: Ends of the cold read's login, at the moment the read ended.
COMPLETED = "completed"
WAITING = "waiting"
FAILED = "failed"


@dataclass(frozen=True)
class AuthCase:
    """One row here: its script, what it expects, and its columns."""

    script: Callable[[RowContext], Awaitable[None]]
    expect_session: str
    #: The row releases the completion once the login asks.
    release: bool
    #: A K1 frozen column exists.
    direct: bool
    #: Every cell needs a terminal (POSIX only).
    terminal: bool = False
    #: The row runs ``--login`` (``CommandSeams``).
    commands: bool = False


# --- The scripts -------------------------------------------------------------------


def _seams(ctx: RowContext) -> AuthSeams | None:
    if ctx.auth is None:
        ctx.record["observation_problems"].append(
            f"{INVALID}the row was given no way to stage a sign-in"
        )
    return ctx.auth


async def _until(check: Callable[[], Any], seconds: float) -> Any:
    """Whatever *check* answers once it is truthy, or its last answer."""
    deadline = time.monotonic() + seconds
    while True:
        found = check()
        if found or time.monotonic() >= deadline:
            return found
        await asyncio.sleep(POLL_SECONDS)


def _marks(seams: AuthSeams) -> int:
    return sum(1 for line in seams.owner_log() if MARKED_LINE in line)


def marked(lines: Sequence[str]) -> list[dict[str, Any]]:
    """Each failure the owner marked for the client, as its log says: the
    reason, and whether it marked the call replayable."""
    found: list[dict[str, Any]] = []
    for line in lines:
        match = _MARKED.search(line)
        if match is not None:
            found.append(
                {
                    "reason": match.group("reason"),
                    "replayable": match.group("replayable") == "True",
                }
            )
        elif MARKED_LINE in line:
            found.append({"reason": None, "replayable": None})
    return found


def _flags(lines: Sequence[str]) -> dict[str, int]:
    """How often the frontend or Direct server said each of its repair ends."""
    return {
        "replayed": sum(1 for line in lines if REPLAY_LINE in line),
        "not_replayed": sum(
            1 for line in lines if any(text in line for text in NOT_REPLAYED_LINES)
        ),
        "signed_in": sum(1 for line in lines if SIGNED_IN_LINE in line),
        "peer": sum(1 for line in lines if PEER_LINE in line),
    }


async def _first_ask(seams: AuthSeams, seconds: float) -> int | None:
    found = await _until(lambda: seams.login().get("first_poll_ns"), seconds)
    return _ns(found)


async def _completion(
    seams: AuthSeams, generation: Any, *, daemon: bool
) -> dict[str, Any]:
    """The issued session and a generation other than *generation* on disk,
    each seen within ``COMPLETION_SECONDS``; in Direct also the login's
    browser gone, which the host's next read must not meet. Not in daemon
    mode: there the replay already opened the owner's browser, and the
    frontend replays only once its login has ended."""
    began = time.monotonic()
    found: dict[str, Any] = {}
    issued = await _until(lambda: seams.login().get("issued"), COMPLETION_SECONDS)
    found["issued_seen_ns"] = time.monotonic_ns() if issued else None

    def written() -> Any:
        seen = seams.snapshot("completion")
        return seen if seen.get("generation") not in (None, generation) else None

    left = max(0.0, COMPLETION_SECONDS - (time.monotonic() - began))
    seen = await _until(written, left)
    found["generation_seen_ns"] = _mapping(seen).get("seen_ns") if seen else None
    if not daemon:
        left = max(1.0, COMPLETION_SECONDS - (time.monotonic() - began))
        found["browser_gone"] = await seams.browser_gone(left)
    return found


async def repair_script(ctx: RowContext) -> None:
    """H-R16's three cells, after the warm-up read: the browser closed, the
    staged session rejected, and a cold read that meets the wall."""
    record = ctx.record
    problems: list[str] = record["observation_problems"]
    case = CASES[ctx.row]
    record.update(bounds=dict(BOUNDS), release=case.release)
    seams = _seams(ctx)
    if seams is None:
        return
    if ctx.daemon:
        record["owner_identified"] = _identity(ctx)
        if record["owner_identified"] is None:
            problems.append(f"{INVALID}the owner was never identified")
            return
    _phase(ctx, "close")
    closed = await ctx.call(CLOSE_TOOL, {})
    if closed.get("is_error") is not False:
        problems.append(f"{INVALID}close_session did not return a successful result")
        return
    record["browser_closed"] = await seams.browser_gone(CLOSE_SECONDS)
    if record["browser_closed"].get("remaining") != []:
        problems.append(
            f"{INVALID}the warm browser was not shown gone within {CLOSE_SECONDS}s, "
            f"so the next read would not start one cold"
        )
        return
    # The authorization, recorded with the session read right after it.
    record["rejection"] = seams.reject()
    _phase(ctx, "rejected", _ns(record["rejection"].get("monotonic_ns")))
    read = asyncio.ensure_future(ctx.call(WARM_TOOL, dict(READ_ARGUMENTS)))
    second: asyncio.Future[Any] | None = None
    try:
        asked = await _first_ask(seams, LOGIN_START_SECONDS)
        record["first_ask_ns"] = asked
        if asked is None:
            problems.append(
                f"{INVALID}the login never asked for its completion within "
                f"{LOGIN_START_SECONDS}s of the cold read"
            )
        else:
            _phase(ctx, "login waiting", asked)
        if ctx.row == ROW_SECOND and asked is not None:
            second = asyncio.ensure_future(seams.second_host())
            met = await _until(lambda: _marks(seams) >= 2, SECOND_MARK_SECONDS)
            record["second_marked_ns"] = time.monotonic_ns() if met else None
        if case.release and asked is not None:
            record["release"] = seams.release()
            _phase(ctx, "released", _ns(record["release"].get("released_ns")))
        await asyncio.wait({read}, timeout=READ_SECONDS)
        record["read_open"] = not read.done()
        record["read_ended_login"] = seams.login()
        if case.release and asked is not None:
            generation = _mapping(record["rejection"].get("snapshot")).get("generation")
            record["completion"] = await _completion(
                seams, generation, daemon=ctx.daemon
            )
            if not ctx.daemon:
                # Direct answered that a login started; once it completed, the
                # host calls again.
                _phase(ctx, "read again")
                await ctx.call(WARM_TOOL, dict(READ_ARGUMENTS))
        elif asked is not None:
            # Never released: the login's own budget ends it. Waited for, and
            # bounded from its wall; nothing here ends it.
            walls = _sequence(seams.login().get("walls"))
            wall = _ns(walls[0]) if walls else None
            spent = (time.monotonic_ns() - wall) / 1e9 if wall else 0.0
            record["failed_settlement"] = await seams.browser_gone(
                max(1.0, FAILED_SETTLE_SECONDS - spent)
            )
            record["failed_login"] = seams.login()
        if second is not None:
            await asyncio.wait({second}, timeout=SECOND_SECONDS)
            record["second"] = second.result() if second.done() else None
        record["host_lines"] = _flags(seams.host_output())
        if ctx.daemon:
            record["marked"] = marked(seams.owner_log())
            record["marks"] = len(record["marked"])
            record["owner_after"] = await seams.owner_reading("after the repair")
    finally:
        await _settle_tasks([read, second])


async def login_script(ctx: RowContext) -> None:
    """H-R10a-login, after the warm-up read: the host quits, then ``--login``
    runs beside whatever was left, and the completion is released once the
    login asks for it."""
    record = ctx.record
    problems: list[str] = record["observation_problems"]
    record.update(command=list(LOGIN_ARGS), terminal=True, bounds=dict(BOUNDS))
    seams, commands = _seams(ctx), ctx.commands
    if seams is None:
        return
    if commands is None:
        problems.append(f"{INVALID}the row was given no way to run a command")
        return
    if ctx.daemon:
        record["owner_identified"] = _identity(ctx)
        if record["owner_identified"] is None:
            problems.append(f"{INVALID}the owner was never identified")
            return
    host_quit = getattr(ctx.transport, "host_quit", None)
    if host_quit is None:
        problems.append(f"{INVALID}the row's host cannot be quit from its script")
        return
    await host_quit()
    record["host_quit_ns"] = time.monotonic_ns()
    _phase(ctx, "host quit", record["host_quit_ns"])
    if not ctx.daemon:
        record["settlement"] = await commands.settlement()
        if not _settled(record["settlement"]):
            problems.append(
                f"{INVALID}the Direct server's profile was not shown settled "
                f"after the host quit; nothing was run on it"
            )
            return
    else:
        record["owner_after_quit"] = await commands.owner_reading("after the host quit")
    # The user's decision to sign in again, recorded before the command can
    # touch anything, with the session read right after.
    record["authorization"] = seams.authorize(LOGIN)
    command = await commands.start(LOGIN_ARGS, terminal=True, label="login")
    try:
        await _answer_login(ctx, seams, commands, command)
    finally:
        if command.returncode is None:
            await command.wait(REFUSAL_SECONDS)
        record["login_command"] = await commands.finish(command, OUTPUT_END_SECONDS)
    _phase(ctx, "command ended", command.exited_ns)
    if ctx.daemon:
        lines = commands.owner_log()
        record["owner_lines"] = {
            "standing_down": sum(1 for line in lines if STANDING_DOWN_LINE in line),
            "idle_exit": sum(1 for line in lines if IDLE_EXIT_LINE in line),
        }


async def _answer_login(
    ctx: RowContext, seams: AuthSeams, commands: Any, command: TerminalCommand
) -> None:
    """Beside an owner, check it and confirm its retirement; then release the
    completion once the login asks, and wait for the command's end."""
    record = ctx.record
    problems: list[str] = record["observation_problems"]
    if ctx.daemon:
        if await command.expect(RETIRE_PROMPT, PROMPT_SECONDS) is None:
            # The owner was recorded and the terminal interactive: a command
            # that never asks is the record's finding, not missing evidence.
            await command.wait(REFUSAL_SECONDS)
            return
        record["before_answer"] = await ctx.checkpoint(
            "before the retirement answer",
            actor=(
                (owner.process, owner.pid, owner.create_time)
                if (owner := ctx.owner())
                else None
            ),
        )
        command.answer("y")
        record["owner_exit"] = await commands.owner_exit(PROMPT_SECONDS + 30.0)
    asked = await _first_ask(seams, LOGIN_START_SECONDS)
    record["first_ask_ns"] = asked
    if asked is None:
        problems.append(
            f"{INVALID}the login never asked for its completion within "
            f"{LOGIN_START_SECONDS}s"
        )
        return
    _phase(ctx, "login waiting", asked)
    record["release"] = seams.release()
    await command.wait(COMPLETION_SECONDS)


CASES: dict[str, AuthCase] = {
    ROW_COLD: AuthCase(
        repair_script, REPLACED_AFTER_AUTHORIZATION, release=True, direct=True
    ),
    ROW_SECOND: AuthCase(
        repair_script, REPLACED_AFTER_AUTHORIZATION, release=True, direct=False
    ),
    ROW_FAILED: AuthCase(
        repair_script, LOST_AFTER_AUTHORIZATION, release=False, direct=True
    ),
    ROW_LOGIN: AuthCase(
        login_script,
        REPLACED_AFTER_AUTHORIZATION,
        release=True,
        direct=True,
        terminal=True,
        commands=True,
    ),
}
ROWS = tuple(CASES)
REPAIR_ROWS = (ROW_COLD, ROW_SECOND, ROW_FAILED)

# --- Reading a record --------------------------------------------------------------


def _calls(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [_mapping(call) for call in _sequence(record.get("calls"))]


def _read_the_post(call: Mapping[str, Any]) -> bool:
    return (
        call.get("outcome") == "returned"
        and call.get("is_error") is False
        and call.get("read_the_post") is True
    )


def _login(record: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(record.get("login"))


def _issued(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [_mapping(item) for item in _sequence(_login(record).get("issued"))]


def _requests(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [_mapping(request) for request in _sequence(record.get("requests"))]


def repair_reading(record: Mapping[str, Any]) -> dict[str, Any]:
    """How the cold read's login stood when that read ended, and whether a
    failed login was then seen settled: the three ends kept apart."""
    reads = [c for c in _calls(record) if c.get("tool") == WARM_TOOL]
    cold = reads[1] if len(reads) > 1 else {}
    settled = _mapping(record.get("failed_settlement"))
    return {
        "at_read_end": login_end(record, _ns(cold.get("ended_monotonic_ns"))),
        "settled_failed": login_end(record, _ns(settled.get("seen_ns"))) == FAILED
        and settled.get("remaining") == [],
    }


def login_end(record: Mapping[str, Any], at: int | None) -> str:
    """How the login stood at *at*: ``completed`` once a session was issued
    by then, ``waiting`` while it still asked after *at*, else ``failed``."""
    issued = [_ns(item.get("issued_ns")) for item in _issued(record)]
    if at is not None and any(seen is not None and seen <= at for seen in issued):
        return COMPLETED
    last = _ns(_login(record).get("last_poll_ns"))
    if at is None or (last is not None and last > at):
        return WAITING
    return FAILED


def _common(record: Mapping[str, Any], *, daemon: bool) -> list[str]:
    problems: list[str] = []
    mode = "daemon" if daemon else "direct"
    if record.get("mode") != mode:
        problems.append(f"the record is for mode {record.get('mode')!r}, not {mode}")
    if record.get("script_error"):
        problems.append(f"the row's script failed: {record['script_error']}")
    problems += [str(p) for p in _sequence(record.get("observation_problems"))]
    if record.get("idle_timeout_seconds") != AUTH_IDLE_TIMEOUT_SECONDS:
        problems.append(
            f"the row ran with an idle timeout of "
            f"{record.get('idle_timeout_seconds')!r}, not the declared "
            f"{AUTH_IDLE_TIMEOUT_SECONDS}"
        )
    if record.get("k2") != K2_NOT_APPLICABLE:
        problems.append("the record does not say why K2 is not applicable")
    if record.get("environment") != ENVIRONMENT or record.get("bounds") != BOUNDS:
        problems.append(
            f"{INVALID}the cell did not run with its declared bounds: "
            f"{record.get('environment')!r}"
        )
    left = _sequence(record.get("left_running"))
    if left:
        problems.append(
            f"{INVALID}a harness failure: the row left {list(left)} running, and "
            f"the harness ended it"
        )
    calls = _calls(record)
    if not calls or calls[0].get("tool") != WARM_TOOL or not _read_the_post(calls[0]):
        problems.append(f"{INVALID}the warm-up read is not recorded as returned")
    forwarded = _mapping(record.get("egress")).get("forwarded")
    if not isinstance(forwarded, list):
        problems.append("the row's egress through its proxy was not recorded")
    elif set(forwarded) - set(ALLOWED_HOSTS):
        problems.append(
            f"the proxy forwarded the row to hosts outside the synthetic origin: "
            f"{sorted(set(forwarded) - set(ALLOWED_HOSTS))}"
        )
    login = _login(record)
    if login.get("closed_ns") is None:
        problems.append(f"{INVALID}the sign-in was not closed by the teardown")
    return problems


def _authorized(record: Mapping[str, Any], kind: str, *, by: int | None) -> list[str]:
    """The row's recorded authorization: of *kind*, at or before *by*, with
    the session read after it."""
    authorization = _mapping(record.get("authorization"))
    at = _ns(authorization.get("at_ns"))
    if record.get("authorized") != kind or authorization.get("kind") != kind:
        return [f"{INVALID}no {kind} authorization was recorded"]
    if at is None or by is None or at > by:
        return [f"{INVALID}the {kind} authorization was not recorded first"]
    snapshot = _mapping(authorization.get("snapshot"))
    if _ns(snapshot.get("seen_ns")) is None or (_ns(snapshot.get("seen_ns")) or 0) < at:
        return [f"{INVALID}the session was not read after the {kind} authorization"]
    return []


def _one_issue(record: Mapping[str, Any]) -> list[str]:
    """One fresh session, issued after the row's release, which came after
    the login's first ask."""
    login = _login(record)
    found: list[str] = []
    asked = _ns(login.get("first_poll_ns"))
    released = _ns(login.get("released_ns"))
    if login.get("released_by") != COMPLETED_BY_ROW or released is None:
        return [f"{INVALID}the row did not release the completion"]
    if asked is None or released < asked:
        found.append(f"{INVALID}the completion was released before the login asked")
    issued = _issued(record)
    if not issued:
        found.append("no fresh session was issued after the release")
    elif len(issued) > 1:
        found.append(f"{len(issued)} fresh sessions were established, not at most one")
    elif (_ns(issued[0].get("issued_ns")) or 0) < released:
        found.append(f"{INVALID}a session was issued before the release")
    return found


# --- H-R16: the verdict ------------------------------------------------------------


def _stale(record: Mapping[str, Any], after: int | None) -> list[Mapping[str, Any]]:
    """The ``/feed/`` requests after *after* that carried a rejected session."""
    rejected = {
        str(value)
        for item in _sequence(_login(record).get("rejections"))
        for value in _sequence(_mapping(item).get("digests"))
    }
    return [
        request
        for request in _requests(record)
        if str(request.get("path", "")).split("?", 1)[0] == "/feed/"
        and set(_sequence(request.get("session_digests"))) & rejected
        and after is not None
        and (_ns(request.get("monotonic_ns")) or 0) > after
    ]


def _repair_setup(
    record: Mapping[str, Any],
) -> tuple[list[str], Mapping[str, Any] | None, int | None]:
    """The close, the rejection and the cold read: their problems, the cold
    read, and when the rejection was made."""
    found: list[str] = []
    calls = _calls(record)
    closes = [call for call in calls if call.get("tool") == CLOSE_TOOL]
    if len(closes) != 1 or closes[0].get("is_error") is not False:
        return [f"{INVALID}the browser was not closed before the rejection"], None, None
    gone = _mapping(record.get("browser_closed"))
    if gone.get("remaining") != []:
        return [f"{INVALID}the warm browser was not shown gone"], None, None
    rejections = _sequence(_login(record).get("rejections"))
    rejected = _ns(_mapping(rejections[0]).get("monotonic_ns")) if rejections else None
    if len(rejections) != 1 or rejected is None:
        return [f"{INVALID}the staged session was not rejected once"], None, None
    if (_ns(gone.get("seen_ns")) or 0) > rejected:
        found.append(f"{INVALID}the session was rejected before the browser was gone")
    if len(_sequence(_mapping(rejections[0]).get("digests"))) != 1:
        found.append(f"{INVALID}the rejection did not take back the staged session")
    found += _authorized(record, ORIGIN_REJECTED, by=rejected)
    reads = [
        call
        for call in calls
        if call.get("tool") == WARM_TOOL
        and (_ns(call.get("began_monotonic_ns")) or 0) > rejected
    ]
    if not reads:
        return (
            [*found, f"{INVALID}no read was sent after the rejection"],
            None,
            rejected,
        )
    if not any(
        request.get("redirected") is True for request in _stale(record, rejected)
    ):
        found.append(f"{INVALID}the stale session never met the wall")
    asked = _ns(_login(record).get("first_poll_ns"))
    began = _ns(reads[0].get("began_monotonic_ns"))
    if asked is None or began is None or asked < began:
        found.append(f"{INVALID}the login never asked for its completion")
    # Once the login waited, nothing may read on the session that failed: an
    # owner reopening its browser on the stale generation would.
    stale = len(_stale(record, asked))
    if asked is not None and stale:
        found.append(
            f"a browser read on the stale generation during the repair: {stale} "
            f"/feed/ request(s) after the login waited"
        )
    return found, reads[0], rejected


def _completed(record: Mapping[str, Any], *, daemon: bool) -> list[str]:
    completion = _mapping(record.get("completion"))
    found: list[str] = []
    if completion.get("generation_seen_ns") is None:
        found.append("the login did not write a new generation after its release")
    if not daemon and _mapping(completion.get("browser_gone")).get("remaining") != []:
        found.append(f"{INVALID}the login's browser was not shown gone after it")
    return found


def _replayed(
    record: Mapping[str, Any], read: Mapping[str, Any], marks: int
) -> list[str]:
    """K3: the cold read answered by one read-only replay, nothing called
    again by the host."""
    found: list[str] = []
    flags = _mapping(record.get("host_lines"))
    if not _read_the_post(read):
        found.append(
            f"the cold read was not answered after the sign-in: outcome "
            f"{read.get('outcome')!r}, error {read.get('is_error')!r}"
        )
    if flags.get("replayed") != 1:
        found.append(
            f"the frontend did not run the read again exactly once: "
            f"{flags.get('replayed')!r}"
        )
    if record.get("marks") != marks:
        found.append(
            f"the owner marked {record.get('marks')!r} failure(s), not {marks}"
        )
    kinds = [_mapping(item) for item in _sequence(record.get("marked"))]
    if flags.get("replayed") and any(k.get("replayable") is not True for k in kinds):
        found.append("a call the owner marked not replayable was run again")
    if any(k.get("reason") != "stale" for k in kinds):
        found.append(
            f"the owner did not mark the cold read's failure as a stale session: "
            f"{[k.get('reason') for k in kinds]}"
        )
    if len([c for c in _calls(record) if c.get("tool") == WARM_TOOL]) != 2:
        found.append(f"{INVALID}the host called again beside the replay")
    return found


def _restarted(record: Mapping[str, Any], read: Mapping[str, Any]) -> list[str]:
    """K1: Direct answered that a login started; the host's next read after
    the login completed returned the post."""
    found: list[str] = []
    if _read_the_post(read) or read.get("outcome") != "returned":
        found.append(
            f"Direct did not answer the cold read with a started login: outcome "
            f"{read.get('outcome')!r}, error {read.get('is_error')!r}"
        )
    reads = [c for c in _calls(record) if c.get("tool") == WARM_TOOL]
    again = reads[2] if len(reads) == 3 else None
    completion = _mapping(record.get("completion"))
    seen = _ns(completion.get("generation_seen_ns"))
    if again is None:
        found.append(f"{INVALID}the host did not read again after the login")
    elif not _read_the_post(again):
        found.append("the read after the completed login did not return the post")
    elif seen is None or (_ns(again.get("began_monotonic_ns")) or 0) < seen:
        found.append(f"{INVALID}the read again was sent before the login completed")
    return found


def _second(record: Mapping[str, Any]) -> list[str]:
    """The second frontend met the repair while the login waited, and its
    read ended on the fresh session."""
    second = _mapping(record.get("second"))
    found: list[str] = []
    marked = _ns(record.get("second_marked_ns"))
    released = _ns(_login(record).get("released_ns"))
    if not second.get("made"):
        return [f"{INVALID}the second host never ran: {second.get('why')!r}"]
    if marked is None or released is None or marked > released:
        found.append(
            f"{INVALID}the second frontend was not shown to meet the repair before "
            f"the release"
        )
    if second.get("forwarded") is not True:
        found.append("the second host's call was not forwarded to the owner")
    call = _mapping(second.get("call"))
    if not _read_the_post(call):
        found.append(
            f"the second host's read did not end on the new generation: outcome "
            f"{call.get('outcome')!r}, error {call.get('is_error')!r}"
        )
    elif (_ns(call.get("ended_monotonic_ns")) or 0) < (released or 0):
        found.append(f"{INVALID}the second host's read ended before the release")
    if second.get("quit_problems"):
        found.append(f"the second host's quit: {second['quit_problems']}")
    return found


def _failed(
    record: Mapping[str, Any], read: Mapping[str, Any], *, daemon: bool
) -> list[str]:
    """Never released: no replay, nothing issued, the login settled failed
    inside its own budget."""
    found: list[str] = []
    login = _login(record)
    if login.get("released_ns") is not None:
        found.append(f"{INVALID}the completion was released in a failed-login cell")
    if _issued(record):
        found.append("a session was issued although the completion never was")
    if _read_the_post(read):
        found.append("the cold read returned the post although the login failed")
    flags = _mapping(record.get("host_lines"))
    if flags.get("replayed"):
        found.append("the frontend ran the read again after a failed login")
    if daemon and record.get("marks") != 1:
        found.append(f"the owner marked {record.get('marks')!r} failure(s), not 1")
    settled = _mapping(record.get("failed_settlement"))
    seen = _ns(settled.get("seen_ns"))
    walls = _sequence(login.get("walls"))
    wall = _ns(walls[0]) if walls else None
    last = _ns(login.get("last_poll_ns"))
    if wall is None:
        found.append(f"{INVALID}the login never served its wall")
    elif settled.get("remaining") != [] or seen is None:
        found.append(
            f"the failed login did not settle within its budget: its browser was "
            f"still there {FAILED_SETTLE_SECONDS}s after its wall"
        )
    elif (seen - wall) / 1e9 > FAILED_SETTLE_SECONDS + 5.0:
        found.append(
            f"the failed login settled {(seen - wall) / 1e9:.1f}s after its wall, "
            f"beyond {FAILED_SETTLE_SECONDS}s"
        )
    elif last is not None and last > seen:
        found.append("the failed login still asked after its browser was gone")
    return found


def repair_problems(record: Mapping[str, Any] | None, *, daemon: bool) -> list[str]:
    """H-R16's verdict, any of its three cells: every problem, or nothing."""
    if not isinstance(record, Mapping):
        return ["the row kept no record"]
    row = record.get("row")
    if row not in REPAIR_ROWS:
        return [f"the record is for row {row!r}, which repairs no session"]
    problems = _common(record, daemon=daemon)
    problems += host_problems(record.get("host"))
    if not daemon and not CASES[str(row)].direct:
        return [*problems, f"{row} has no Direct column: {K1_NOT_APPLICABLE['reason']}"]
    if daemon and _identified(record) is None:
        return [*problems, f"{INVALID}the owner was never identified"]
    found, read, _ = _repair_setup(record)
    problems += found
    if read is None:
        return problems
    if row == ROW_FAILED:
        return problems + _failed(record, read, daemon=daemon)
    problems += _one_issue(record)
    problems += _completed(record, daemon=daemon)
    if daemon:
        problems += _replayed(record, read, 2 if row == ROW_SECOND else 1)
        identified = _identified(record)
        if identified is None or _launches(record, identified) != ([], []):
            problems.append("the row launched another owner beside the identified one")
    else:
        problems += _restarted(record, read)
    if row == ROW_SECOND:
        problems += _second(record)
    return problems


# --- H-R10a-login: the verdict -----------------------------------------------------


def login_problems(record: Mapping[str, Any] | None, *, daemon: bool) -> list[str]:
    """H-R10a-login's verdict over its raw record: every problem, or nothing."""
    if not isinstance(record, Mapping):
        return ["the row kept no record"]
    if record.get("row") != ROW_LOGIN:
        return [f"the record is for row {record.get('row')!r}, which runs no login"]
    problems = _common(record, daemon=daemon)
    problems += host_problems(record.get("host"))
    if daemon and _identified(record) is None:
        return [*problems, f"{INVALID}the owner was never identified"]
    command = _command(record, "login_command")
    problems += _ran(command, "login", terminal=True)
    if not command:
        return problems
    started = _ns(command.get("started_ns"))
    quit_ns = _ns(record.get("host_quit_ns"))
    if quit_ns is None or started is None or started < quit_ns:
        problems.append(f"{INVALID}the login did not start after the host quit")
    problems += _authorized(record, LOGIN, by=started)
    asked = _ns(_login(record).get("first_poll_ns"))
    if asked is None or started is None or asked < started:
        problems.append(f"{INVALID}the login never asked for its completion")
    if not daemon:
        settlement = _mapping(record.get("settlement"))
        settled = _ns(settlement.get("seen_ns"))
        if not _settled(settlement):
            problems.append(
                f"{INVALID}the Direct server's profile was not shown settled"
            )
        elif settled is None or started is None or settled > started:
            problems.append(f"{INVALID}the settlement was not read before the login")
        if _first(command, RETIRE_PROMPT) is not None:
            problems.append("a Direct login asked to retire a shared browser")
    else:
        problems += _retired_for_login(record, command, asked)
    problems += _one_issue(record)
    if command.get("returncode") != 0 or _first(command, PROFILE_SAVED) is None:
        problems.append(
            f"the login did not save the new session: exit "
            f"{command.get('returncode')!r}"
        )
    return problems


def _retired_for_login(
    record: Mapping[str, Any], command: Mapping[str, Any], asked: int | None
) -> list[str]:
    """K3: the retirement confirmed after its prompt and a fresh checkpoint,
    the owner gone on that request, and the login opening only after it."""
    answered = _answered_after(command, RETIRE_PROMPT, 0, "y")
    if answered is None:
        if _seen(command, RETIRE_PROMPT) is None:
            return ["the login never asked to retire the recorded owner on a terminal"]
        return [f"{INVALID}the retirement was not confirmed after its prompt"]
    found = _checkpoint_before_answer(record, command, answered)
    lines = _mapping(record.get("owner_lines"))
    gone = _mapping(record.get("owner_exit"))
    seen = _ns(gone.get("seen_ns"))
    if gone.get("how") != "exited" or seen is None or seen < answered:
        found.append(
            f"the owner is not shown to exit after the confirmed retirement: "
            f"{gone.get('how')!r}"
        )
    if lines.get("idle_exit"):
        found.append(
            f"{INVALID}the owner's log says it idled out, so its exit is not the "
            f"retirement's"
        )
    if lines.get("standing_down") != 1:
        found.append(
            f"the owner's log does not say once that a profile command asked: "
            f"{lines.get('standing_down')!r}"
        )
    retiring = _first(command, RETIRING_LINE)
    if retiring is None:
        found.append("the login did not report the owner retiring")
    elif retiring < answered:
        found.append("the login reported a retirement before the user confirmed it")
    opened = _first(command, LOGIN_OPENED)
    if opened is not None and opened < answered:
        found.append("the login opened its browser before the retirement was confirmed")
    if asked is not None and asked < answered:
        found.append("the login asked for its completion before the owner retired")
    identified = _identified(record)
    if identified is None or _launches(record, identified) != ([], []):
        found.append("the row launched another owner beside the one that retired")
    return found


# --- The verdicts, by row ----------------------------------------------------------


def problems_for(record: Mapping[str, Any] | None, *, daemon: bool) -> list[str]:
    """The verdict of whichever row of this module *record* is for."""
    if _mapping(record).get("row") == ROW_LOGIN:
        return login_problems(record, daemon=daemon)
    return repair_problems(record, daemon=daemon)


def invalid_evidence(problems: Sequence[str]) -> list[str]:
    """The problems that leave a row unmeasured rather than failed."""
    return [problem for problem in problems if problem.startswith(INVALID)]


def semantics(record: Mapping[str, Any]) -> dict[str, Any]:
    """What K0 compares: classifications only, no pid, time, digest or path.

    How the login stood when the cold read ended is recorded, not compared:
    whether a failing login or the frontend's wait ends first is timing.
    """
    row = record.get("row")
    issued = _issued(record)
    found: dict[str, Any] = {
        "row": row,
        "mode": record.get("mode"),
        "issued": len(issued),
        "released": _login(record).get("released_ns") is not None,
        "lineage": _mapping(record.get("lineage")).get("reading"),
    }
    if row == ROW_LOGIN:
        command = _command(record, "login_command")
        found["exit"] = command.get("returncode")
        found["saved"] = _first(command, PROFILE_SAVED) is not None
        found["asked_to_retire"] = _first(command, RETIRE_PROMPT) is not None
        found["retired"] = _mapping(record.get("owner_exit")).get("how") == "exited"
        return found
    reads = [c for c in _calls(record) if c.get("tool") == WARM_TOOL]
    found["reads"] = [_read_the_post(call) for call in reads]
    found["replayed"] = _mapping(record.get("host_lines")).get("replayed")
    found["marks"] = record.get("marks")
    if row == ROW_SECOND:
        found["second"] = _read_the_post(
            _mapping(_mapping(record.get("second")).get("call"))
        )
    return found


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
    """Why K3 cannot be held to K1 on a row here: a record missing or invalid.
    O1 to O4, the lineage among them, are the vectors' (``compare_to_direct``);
    how each mode answers the cold read differs by design."""
    return _refusals([("Direct", direct, False), ("daemon", daemon, True)])


# --- What stays with the models ----------------------------------------------------

#: The lane whose native claim needs positive boundary evidence the row
#: cannot get, and why: the plan's STOP 5 and 10.
RESPONSE_LOSS_OPEN = (
    "a marker response dropped on the owner hop is not observed natively: the "
    "relay's boundary is proved process-free (owner_hop_relay), but the "
    "frontend reaches its owner at the address the owner's own descriptor "
    "publishes, so routing the real hop through the relay means editing the "
    "product's daemon state, a seam the plan stops at (STOP 10); the latch "
    "models mapped here show a later call meets the same marker and are not "
    "response-loss tests, and no test drops a marker response, so the lane "
    "stays open"
)
IMPORT_NOT_NATIVE = (
    "the import asks the OS keystore before it reads a cookie, on every "
    "platform (extract._resolve_keystore): on Linux that is secret-tool, a "
    "Secret Service read wherever one answers, and the peanuts fallback only "
    "follows its failure, so a disposable synthetic profile cannot be shown "
    "to import without a keystore being asked; macOS and Windows read the "
    "keychain or DPAPI first. Not run natively on any leg; mapped to its model"
)

#: Every R16 and R10a-login branch the native cells do not reach, with the
#: exact existing tests that model it: counted as model coverage, never as
#: native. Each entry's first element names the row it stands beside.
MODEL_COVERAGE: dict[str, tuple[str, tuple[str, ...]]] = {
    "response loss (latch models, not response-loss tests)": (
        ROW_COLD,
        (
            "tests/test_bootstrap.py::TestTheOwnerStaysQuiescentUntilANewSessionLands"
            "::test_every_later_call_names_the_same_broken_session",
            "tests/test_bootstrap.py::TestTheOwnerStaysQuiescentUntilANewSessionLands"
            "::test_the_gate_refuses_before_it_can_reach_a_browser",
            "tests/test_bootstrap.py::TestTheOwnerStaysQuiescentUntilANewSessionLands"
            "::test_an_abandoned_login_leaves_it_latched",
        ),
    ),
    "browser_open marker": (
        ROW_COLD,
        (
            "tests/test_daemon_auth.py::TestTheFrontendActsOnTheMarker"
            "::test_no_login_starts_while_the_profile_may_still_be_held",
        ),
    ),
    "frontend wait expiring while the login continues": (
        ROW_FAILED,
        (
            "tests/test_daemon_auth.py::TestTheRepairRunsForReal"
            "::test_a_sign_in_slower_than_the_wait_gives_up_without_replaying",
        ),
    ),
    "non-replayable or mutating call repaired, never replayed": (
        ROW_COLD,
        (
            "tests/test_daemon_auth.py::TestTheFrontendActsOnTheMarker"
            "::test_a_call_that_had_already_started_is_never_run_again",
            "tests/test_daemon_auth.py::TestTheRepairRunsForReal"
            "::test_a_tool_that_changes_something_is_never_replayed",
        ),
    ),
    "two frontends meeting one dead session": (
        ROW_SECOND,
        (
            "tests/test_bootstrap.py::TestTwoClientsMeetingOneDeadSession"
            "::test_the_generation_stops_the_second_client",
        ),
    ),
    "import beside an idle owner": (
        ROW_LOGIN,
        (
            "tests/test_cli_main.py::TestRetiringASharedBrowser"
            "::test_import_waits_for_the_profile_after_an_idle_owner_retires",
        ),
    ),
}
