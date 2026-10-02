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
host calls again and reads. Where Direct then refuses that read over a login
browser whose close it never confirmed (its own ``LEFT_OPEN_LINE``), that is
the baseline's answer and recorded as such, not a problem of the row.
**Second frontend** (``ROW_SECOND``, daemon only): a second host calls while
the first frontend's login waits. The owner takes the profile before its
latch is asked (``sequential_tool_middleware`` ahead of the tool's readiness
gate), and the login holds the profile until it is done, so the second read
waits at the owner for up to ``BROWSER_WAIT`` and meets no marker; a marker
of its own (the latch) is the branch the models cover. The row releases the
completion once the owner's own progress for that wait reaches the second
host (``PROFILE_WAIT_PROGRESS``, ``SECOND_WAITING_SECONDS``), which keeps
the login inside ``LOGIN_TIMEOUT`` and the second read inside
``BROWSER_WAIT``; without that report the cell is invalid, whatever the
frontend said. A busy answer to the second read sooner than its
``BROWSER_WAIT`` after the read was sent, or one whose budget could only
run out once the login was over, is a finding; any other busy answer leaves
the order unshown, invalid evidence, since nothing stamps where the owner's
wait began. Nothing reads on the stale generation, one fresh session is
issued, and both reads end on it. A release after the login's last ask is
the row's only when the login's own process says it ran out its
``LOGIN_TIMEOUT``; one that says it failed otherwise failed on its own, a
finding, and one that says neither leaves the order unshown. **Failed login**
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
Windows open it in the runner's own desktop session, where the frozen
baseline's login served its wall and completed on every leg. A login that
never opens its wall leaves the cell invalid (``the login never asked for its
completion``), never a finding. A cold read that its own server answered
without any request after the rejection, its log saying the browser did not
start (``START_FAILED_LINE``), is the opposite: the product failed the
scenario before a login could be asked for, a finding.

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
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from differential import model_coverage
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
    _lines,
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
from linkedin_mcp_server.config.schema import DEFAULT_BROWSER_WAIT_SECONDS

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
#: How long a server waits for a profile another process holds before it
#: answers that the browser is busy (``BROWSER_WAIT``): the product's own
#: default, pinned so the record says it, never stretched. The second
#: frontend's read waits this long at the owner while the login holds the
#: profile, which it does until about 18 s after its wall (``ROW_SECOND``).
BROWSER_WAIT_SECONDS = DEFAULT_BROWSER_WAIT_SECONDS

#: The cell's environment, over the row's: the same in every column. The
#: automatic import is off, so no real browser profile or keystore is ever
#: asked; the proxy alone would already keep it off (``_auto_import_allowed``).
ENVIRONMENT = {
    EnvironmentKeys.LOGIN_TIMEOUT: f"{LOGIN_TIMEOUT_SECONDS:g}",
    EnvironmentKeys.TOOL_TIMEOUT: f"{TOOL_TIMEOUT_SECONDS:g}",
    EnvironmentKeys.LOGIN_INLINE_WAIT: f"{LOGIN_INLINE_WAIT_SECONDS:g}",
    EnvironmentKeys.BROWSER_WAIT: f"{BROWSER_WAIT_SECONDS:g}",
    EnvironmentKeys.AUTO_IMPORT_FROM_BROWSER: "false",
}
#: What the record says of the bounds, read back by the verdict.
BOUNDS = {
    "login_timeout_seconds": LOGIN_TIMEOUT_SECONDS,
    "tool_timeout_seconds": TOOL_TIMEOUT_SECONDS,
    "login_inline_wait_seconds": LOGIN_INLINE_WAIT_SECONDS,
    "login_settle_seconds": LOGIN_SETTLE_SECONDS,
    "browser_wait_seconds": BROWSER_WAIT_SECONDS,
}

#: The row's waits, each from its own start.
#: The warm browser gone after ``close_session``.
CLOSE_SECONDS = 60.0
#: From the cold read to the login's first ask: a cold browser start and its
#: validation, the owner's answer, and a headed login browser's start.
LOGIN_START_SECONDS = 120.0
#: From the second host's start to the owner saying the second read waits
#: for the profile (``_second_waiting``): a frontend's start, measured at 1
#: to 4 s, its tool lookup and the call reaching the owner. The login looks
#: for the session only once its manual wait starts, about 15 s after its
#: wall, and lets go of the profile about 3 s after it finds one. Released by
#: this bound's end, about 15 s after the wall, it lets go by about 18.5 s
#: after the wall: inside ``LOGIN_TIMEOUT``, and inside the second read's
#: ``BROWSER_WAIT``, which starts with that report, after the wall.
SECOND_WAITING_SECONDS = 15.0
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
#: ``sequential_tool_middleware``: a server's wait for a profile another
#: process holds ran out, and it answered that the browser is busy.
PROFILE_WAIT_LINE = "gave up waiting for the shared browser"
#: ``sequential_tool_middleware``: the progress a server reports for a call
#: that is about to wait for a profile another process holds, sent right
#: before that wait starts and relayed by a frontend to the host that called.
#: It is the owner's own word that the call got past the frontend's tool
#: lookup and session to the profile, which no line of the frontend's is.
PROFILE_WAIT_PROGRESS = "waiting for it to hand over"
#: How the process that ran a login says it ended without a session: a
#: server's login task (``bootstrap``, once its frontend or Direct server
#: reads the finished task) and ``--login`` (``setup.run_profile_creation``),
#: each followed by the login's own error.
LOGIN_FAILED_LINES = ("LinkedIn login bootstrap failed", "Profile creation failed")
#: ``core.auth``: that error for a manual wait that ran out. Raised only once
#: the wait's own deadline, its whole ``LOGIN_TIMEOUT`` from its start, has
#: passed, so it is the login's word that it kept its budget.
LOGIN_TIMED_OUT = "Manual login timeout"
#: ``core.browser``: a browser launch that failed.
START_FAILED_LINE = "Failed to start browser"
#: ``drivers.browser``: a server refusing to launch because a close of its
#: own was never confirmed, until it is restarted.
LEFT_OPEN_LINE = "A previous browser on this profile did not shut down cleanly"

#: How the second frontend met the repair, read from its own lines. Its call
#: waited at the owner for the profile the login held, and it repaired
#: nothing; or a marker reached it, and it repaired and ran its read again.
MET_PROFILE = "profile"
MET_MARKER = "marker"

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


def _count(lines: Sequence[str], text: str) -> int:
    return sum(1 for line in lines if text in line)


def profile_wait_started(progress: Sequence[Any]) -> int | None:
    """When the host first heard the owner say its read waits for the
    profile (``PROFILE_WAIT_PROGRESS``), or ``None``."""
    for item in progress:
        heard = _mapping(item)
        if PROFILE_WAIT_PROGRESS in str(heard.get("message") or ""):
            return _ns(heard.get("seen_ns"))
    return None


def _second_waiting(seams: AuthSeams, second: asyncio.Future[Any]) -> bool:
    """Whether the second host's read waits at the owner for the profile the
    login holds, and is not over: the owner's own report of that wait reached
    the second host. Nothing the frontend says can show it, its heartbeats
    least of all: they start before its fresh tool lookup and session, so
    they beat on while the call has not reached the owner at all."""
    return (
        not second.done() and profile_wait_started(seams.second_progress()) is not None
    )


def _login_over(seams: AuthSeams) -> bool:
    """Whether the first frontend says its login ended, either way: it waits
    for the login task, which lets go of the profile before it ends. Seen by
    a poll, so the time it is seen bounds the profile's release from above
    only."""
    flags = _flags(seams.host_output())
    return bool(flags["signed_in"] or flags["not_replayed"])


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


def login_failures(lines: Sequence[str]) -> list[str]:
    """Each line in which the process that ran the login says it ended
    without a session (``LOGIN_FAILED_LINES``), in order."""
    return [line for line in lines if any(text in line for text in LOGIN_FAILED_LINES)]


def _flags(lines: Sequence[str]) -> dict[str, int]:
    """How often the frontend or Direct server said each of its repair ends,
    and that it refused a launch over a close it never confirmed."""
    return {
        "replayed": sum(1 for line in lines if REPLAY_LINE in line),
        "not_replayed": sum(
            1 for line in lines if any(text in line for text in NOT_REPLAYED_LINES)
        ),
        "signed_in": sum(1 for line in lines if SIGNED_IN_LINE in line),
        "peer": sum(1 for line in lines if PEER_LINE in line),
        "left_open": sum(1 for line in lines if LEFT_OPEN_LINE in line),
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
        if asked is not None:
            _phase(ctx, "login waiting", asked)
        elif not (read.done() and _start_failures(ctx, seams)):
            # A read its server already failed because its browser did not
            # start is the verdict's finding, not missing evidence.
            problems.append(
                f"{INVALID}the login never asked for its completion within "
                f"{LOGIN_START_SECONDS}s of the cold read"
            )
        if ctx.row == ROW_SECOND and asked is not None:
            pending = second = asyncio.ensure_future(seams.second_host())
            # Released once the owner says the second read waits for the
            # profile, so the record proves it was there while the login held
            # it. Released after the bound anyway, so the login still ends
            # inside its own budget; the verdict then reads the cell invalid
            # from the second host's own progress, which it keeps.
            await _until(
                lambda: _second_waiting(seams, pending), SECOND_WAITING_SECONDS
            )
        if case.release and asked is not None:
            record["release"] = seams.release()
            _phase(ctx, "released", _ns(record["release"].get("released_ns")))
        if second is not None:
            # When the first frontend is seen saying its login ended: the
            # latest the login can have let go of the profile. It still held
            # it at its page's last ask, which the origin records.
            over = await _until(lambda: _login_over(seams), COMPLETION_SECONDS)
            record["login_over_ns"] = time.monotonic_ns() if over else None
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
        record["login_failures"] = login_failures(seams.host_output())
        record["start_failures"] = _start_failures(ctx, seams)
        if ctx.daemon:
            record["marked"] = marked(seams.owner_log())
            record["marks"] = len(record["marked"])
            record["profile_waits_ran_out"] = _count(
                seams.owner_log(), PROFILE_WAIT_LINE
            )
            record["owner_after"] = await seams.owner_reading("after the repair")
    finally:
        await _settle_tasks([read, second])


def _start_failures(ctx: RowContext, seams: AuthSeams) -> int:
    """How often the server that reads said its browser did not start: the
    owner in daemon mode, the host's own server in Direct."""
    lines = seams.owner_log() if ctx.daemon else seams.host_output()
    return _count(lines, START_FAILED_LINE)


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


RELEASED_LATE = (
    f"{INVALID}the completion was released after the login's last ask, and the "
    f"login says it ran out its LOGIN_TIMEOUT"
)
LATE_RELEASE_UNSHOWN = (
    f"{INVALID}the completion was released after the login's last ask, and the "
    f"login's own process does not say how it ended"
)


def _said(line: str) -> str:
    """The message of a server's JSON log line, or the line as it stands."""
    try:
        found = json.loads(line)
    except ValueError:
        return line
    message = found.get("message") if isinstance(found, dict) else None
    return message if isinstance(message, str) else line


def _login_failures(record: Mapping[str, Any]) -> list[str]:
    """How the login's own process said it ended without a session:
    ``--login``'s terminal, or the lines of the frontend or Direct server
    that ran the repair's login."""
    if record.get("row") == ROW_LOGIN:
        command = _command(record, "login_command")
        return login_failures([line for _, line in _lines(command)])
    return [str(line) for line in _sequence(record.get("login_failures"))]


def late_release(record: Mapping[str, Any]) -> str | None:
    """How a release after the login's last ask reads, or ``None`` when the
    login still asked after it.

    How long the login's page asked says nothing about how long the login
    waited: a renderer held up stops the asks while the login's own wait runs
    on, measured as 2.1 s of asks from a login that waited its whole 60 s.
    So only the login's own word on how it ended is read, the first it gave.
    Its manual wait ran out (``LOGIN_TIMED_OUT``), which it says only once its
    whole ``LOGIN_TIMEOUT`` has passed: the row released too late, invalid
    evidence (``RELEASED_LATE``), and nothing that follows from a release is
    judged. It failed for any other reason: the product's, a finding, judged
    with everything that follows from it. It said neither: the order is not
    shown (``LATE_RELEASE_UNSHOWN``), invalid evidence.
    """
    login = _login(record)
    released = _ns(login.get("released_ns"))
    last = _ns(login.get("last_poll_ns"))
    if released is None or last is None or last >= released:
        return None
    failures = _login_failures(record)
    if not failures:
        return LATE_RELEASE_UNSHOWN
    if LOGIN_TIMED_OUT in failures[0]:
        return RELEASED_LATE
    return (
        f"the login stopped asking for its completion before the release and "
        f"failed for a reason other than its LOGIN_TIMEOUT: {_said(failures[0])!r}"
    )


def _released_late(record: Mapping[str, Any]) -> bool:
    """Whether the release came after the login's last ask in a way that is
    the row's, or not shown to be the product's: nothing after it is
    judged."""
    return (late_release(record) or "").startswith(INVALID)


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
    late = late_release(record)
    if late is not None:
        found.append(late)
        if _released_late(record):
            return found
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
    failures = record.get("start_failures")
    reached = [
        r for r in _requests(record) if (_ns(r.get("monotonic_ns")) or 0) > rejected
    ]
    if (
        not reached
        and isinstance(failures, int)
        and failures > 0
        and reads[0].get("outcome") == "returned"
        and reads[0].get("is_error") is True
    ):
        # Nothing reached the origin, so no wall and no login, and the
        # server says why: a finding, never the missing evidence it reads as.
        return (
            [
                *found,
                f"the cold read failed before any browser reached the origin: the "
                f"server's browser did not start ({failures} failed start(s))",
            ],
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
    the login completed returned the post, or Direct refused it over its own
    login browser's unconfirmed close (``direct_read_again``)."""
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
    elif seen is None or (_ns(again.get("began_monotonic_ns")) or 0) < seen:
        found.append(f"{INVALID}the read again was sent before the login completed")
    elif direct_read_again(record) is None:
        found.append("the read after the completed login did not return the post")
    return found


def direct_read_again(record: Mapping[str, Any]) -> str | None:
    """K1: how Direct answered the host's read after its login completed:
    ``read`` with the post, ``left open`` when it refused to launch because
    its own login browser's close was never confirmed (``LEFT_OPEN_LINE``),
    which it keeps refusing until a restart, or ``None`` for anything else.

    ``left open`` is the baseline's own answer, so it leaves K1 valid and is
    never held against the daemon: measured on macOS, where the frozen
    baseline's login exported the new session and wrote its generation, its
    browser was then shown gone, and the read sent after both was refused in
    under 0.1 s."""
    reads = [c for c in _calls(record) if c.get("tool") == WARM_TOOL]
    again = reads[2] if len(reads) == 3 else {}
    if _read_the_post(again):
        return "read"
    left_open = _mapping(record.get("host_lines")).get("left_open")
    if (
        again.get("outcome") == "returned"
        and again.get("is_error") is True
        and isinstance(left_open, int)
        and left_open > 0
    ):
        return "left open"
    return None


def second_met(record: Mapping[str, Any]) -> str:
    """How the second frontend met the repair, by its own lines: a repair
    end of its own means a marker reached it (``MET_MARKER``); none, that
    its call waited at the owner for the profile (``MET_PROFILE``)."""
    lines = _mapping(_mapping(record.get("second")).get("lines"))
    ends = ("replayed", "not_replayed", "signed_in", "peer")
    return MET_MARKER if any(lines.get(end) for end in ends) else MET_PROFILE


def second_failure(record: Mapping[str, Any], call: Mapping[str, Any]) -> str:
    """Why the second host's read did not end on the fresh session.

    Where the owner's log says a wait for the profile ran out
    (``profile_waits_ran_out``), the host's call bounds that wait from
    outside only: it began no sooner than the call was sent and ended no
    later than the answer reached the host. The owner gives up only once its
    whole ``BROWSER_WAIT`` has passed on the same monotonic clock, right
    after a last failed try for the profile. So an answer sooner than that
    budget after the call was sent is the owner refusing early, and a budget
    that could only run out after the first frontend said its login was
    over (``login_over_ns``, when the profile was free) is the owner refusing
    early or passing over a free profile: findings both.

    Nothing shows the owner waited its whole budget while the login held the
    profile: its log stamps no start of that wait, and the host hears the
    owner's report of the start and its answer each a frontend hop late, by
    however long the frontend is held up: 2.2 s either way in a probe. Any
    other busy answer is that order
    unshown, invalid evidence, never the release's miss. Without the
    owner's word that a wait ran out, the failed read stands as one."""
    failed = (
        f"the second host's read did not end on the new generation: outcome "
        f"{call.get('outcome')!r}, error {call.get('is_error')!r}"
    )
    ran_out = record.get("profile_waits_ran_out")
    began = _ns(call.get("began_monotonic_ns"))
    ended = _ns(call.get("ended_monotonic_ns"))
    if not isinstance(ran_out, int) or ran_out < 1 or began is None or ended is None:
        return failed
    spent = (ended - began) / 1e9
    if spent < BROWSER_WAIT_SECONDS:
        return (
            f"the owner answered the second read busy {spent:.1f}s after it was "
            f"sent, before its BROWSER_WAIT of {BROWSER_WAIT_SECONDS:g}s could "
            f"run out"
        )
    over = _ns(record.get("login_over_ns"))
    budget_out = began + int(BROWSER_WAIT_SECONDS * 1e9)
    if over is not None and budget_out > over:
        return (
            f"the owner answered the second read busy although the first "
            f"frontend's login was over {(budget_out - over) / 1e9:.1f}s before "
            f"its BROWSER_WAIT of {BROWSER_WAIT_SECONDS:g}s could run out: it "
            f"gave up early or passed over the free profile"
        )
    return (
        f"{INVALID}the owner answered the second read busy {spent:.1f}s after it "
        f"was sent, and whether it waited its whole BROWSER_WAIT of "
        f"{BROWSER_WAIT_SECONDS:g}s while the login held the profile is not "
        f"shown: nothing stamps where its wait began"
    )


def _second(record: Mapping[str, Any]) -> list[str]:
    """The owner said the second host's read waited for the profile before
    the release, and the read ended on the fresh session; a frontend a
    marker reached ran it again once."""
    second = _mapping(record.get("second"))
    found: list[str] = []
    released = _ns(_login(record).get("released_ns"))
    if not second.get("made"):
        return [f"{INVALID}the second host never ran: {second.get('why')!r}"]
    call = _mapping(second.get("call"))
    began = _ns(call.get("began_monotonic_ns"))
    waiting = profile_wait_started(_sequence(second.get("progress")))
    if (
        waiting is None
        or released is None
        or began is None
        or waiting < began
        or waiting > released
    ):
        found.append(
            f"{INVALID}the owner did not say the second host's read waited for "
            f"the profile before the release"
        )
    if second.get("forwarded") is not True:
        found.append("the second host's call was not forwarded to the owner")
    lines = _mapping(second.get("lines"))
    if second_met(record) == MET_MARKER and lines.get("replayed") != 1:
        found.append(
            f"the second frontend repaired on a marker but did not run its read "
            f"again exactly once: {lines.get('replayed')!r}"
        )
    if not _read_the_post(call):
        found.append(second_failure(record, call))
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
    elif read.get("outcome") == "returned" and read.get("is_error") is not True:
        # Measured in both columns: a failed sign-in answers the cold read
        # with an error. Anything else tells the user it did not fail.
        found.append("the cold read was answered without an error after a failed login")
    flags = _mapping(record.get("host_lines"))
    if flags.get("signed_in"):
        found.append("the frontend said it signed in although the login failed")
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
    if _released_late(record):
        return problems
    problems += _completed(record, daemon=daemon)
    if daemon:
        # The owner marks the second read too only where it reached the latch.
        marker = row == ROW_SECOND and second_met(record) == MET_MARKER
        problems += _replayed(record, read, 2 if marker else 1)
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
    if _released_late(record):
        return problems
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
        found["second_met"] = second_met(record)
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

#: What the response-loss lane stands beside while it stays open: latch
#: models, which are not response-loss tests, so never counted as its
#: coverage (``RESPONSE_LOSS_OPEN``).
RESPONSE_LOSS_REFERENCES: tuple[str, ...] = (
    "tests/test_bootstrap.py::TestTheOwnerStaysQuiescentUntilANewSessionLands"
    "::test_every_later_call_names_the_same_broken_session",
    "tests/test_bootstrap.py::TestTheOwnerStaysQuiescentUntilANewSessionLands"
    "::test_the_gate_refuses_before_it_can_reach_a_browser",
    "tests/test_bootstrap.py::TestTheOwnerStaysQuiescentUntilANewSessionLands"
    "::test_an_abandoned_login_leaves_it_latched",
)

MODEL_COVERAGE = model_coverage.AUTH_MODEL_COVERAGE
