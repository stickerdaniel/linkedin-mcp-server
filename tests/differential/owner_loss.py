"""The owner lost before dispatch (H-R8) and after dispatch of a mutating
tool (H-R9).

**H-R8, the owner unreachable before dispatch.** The host warms up through
the owner with the row's feed read; then, between calls, the row takes the
owner the frontend is attached to out of service, and the host reads
``get_person_profile`` (the calibrated read, ``call_loss``) for a username
of the lane's own. The frontend's heartbeat preflight is the first thing a
call sends, and only a validated 200 dispatches it, so every lane here asks
the same question: the preflight fails, no tool request reaches the failed
instance, and recovery serves the read on an owner that can. Each lane in
K1 frozen (the same host requests through Direct, no hop and no fault: a
Direct server has no owner to lose), K3 and K0.

* **Unreachable** (``H-R8-unreachable``): the harness kills the identified
  owner through the handle the row tied it by, waits for its death and
  reads it gone, then the host calls. Its own lane: the H-R6 mapping is not
  credited (plan F8). The preflight goes unanswered, recovery elects a
  successor, and the read succeeds there.
* **Owner error** (``H-R8-owner-error``, POSIX only): the harness stops the
  identified owner (SIGSTOP), calls, sees the frontend's preflight fail,
  and resumes the owner (SIGCONT) ``STOP_SECONDS`` after the stop, on every
  path, the teardown included. The stopped owner may stay eligible:
  recovery may wait for it, which delivers the read on that owner once it
  runs again, or fail explicitly; neither runs anything on it while it is
  stopped. A later read then works. Windows has no portable stop: a
  reasoned skip.
* **Declared responder** (``H-R8-responder-404``, ``H-R8-responder-500``):
  native transport, synthetic answer. The owner is killed as above and a
  loopback listener of the row's own (``DeclaredResponder``) is bound on its
  old address, answering every request with the lane's status. Port not
  bound again: invalid evidence. The real frontend meets it, real election
  publishes a successor on its own port, and only the successor receives
  the tool dispatch. The responder records every request it gets, so a
  dispatch reaching it, which carries the call's header on a path other
  than the heartbeat's, is seen. These cells claim the classification and
  zero dispatches to the failed instance, and a burial category only where
  the frontend's own line shows one (``BURIAL_LINE``): a 500 buries nothing
  in the frontend, while the election may write the responder off locally
  once its listing fails, and a successor alone proves neither.
* **Retiring**: PR 2's retirement-wins lanes (``RETIRING_ELSEWHERE``), not
  duplicated here.

What a frontend's output says is all that classifies: the preflight line
and its class where it names one, the election's and the replay's lines,
the burial line. An unanswered preflight's line names no class, so
``unreachable`` and ``owner_error`` are told apart only in the unit matrix
(``UNIT_MATRIX``), which also keeps every other classification. A dispatch
to the dead owner of the kill lanes is excluded, not observed: that owner
was confirmed dead before the call was sent, the frontend reported its
failed preflight and then replayed a call it had not sent, and every page
of the read arrived after the successor's launch (``UNOBSERVED``).

**H-R9, the owner lost after dispatch.** ``send_message`` with
``confirm_send`` to a synthetic recipient, a mutating tool whose first
navigation is the recipient's profile page. The origin holds that page
(``synthetic_origin.person_path``, the person page every read here gets),
and once it entered, K1 frozen kills the Direct server through its tied
handle and K3 kills the identified owner, the frontend alive. The hold is
released ``RELEASE_SECONDS`` after its entry, below the gate's deadline.
K3: an error result with ``status`` ``outcome_unknown`` and ``retry_safe``
false, no second navigation to the recipient's page (no replay), then a
read through the same host on a verified successor. K1: the connection is
lost, the profile settles by itself before anything is cleaned up, and a
fresh host reads. Compared: no duplicate mutation dispatch, O1, O3 and O4.
The separate-process after-effect control stays where it is
(``AFTER_EFFECT_CONTROL``).

**Nothing of H-R9 reaches LinkedIn** (``FENCE``): the tool drives only the
browser, whose only egress is the row's proxy; the proxy tunnels the two
allowed names to the loopback origin and refuses everything else; the CI
step maps those names to loopback and the native cells refuse to start
otherwise; and the origin has no Message action on the profile page and
no messaging page at all, so the product's own resolution ends at
``message_unavailable``. The record keeps the hosts the proxy forwarded,
and the verdict holds them to the allowed names.

A loss before its trigger, a hold that ran out its deadline, a stop never
resumed, a responder never bound or never met, a reading out of its window:
invalid evidence (``INVALID``), apart from a finding. K2 is recorded not
applicable for every row here. The scripts run on a ``harness.RowContext``
with its ``owner_loss`` seams; nothing here reads a process, and the
verdicts read the raw record alone, so each can be replayed from the
published packet.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import socketserver
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

from differential.call_loss import (
    CALIBRATION_IDLE_TIMEOUT_SECONDS,
    CONTINUATION_SECONDS,
    ENTRY_SECONDS,
    EXPECTED_SECTIONS,
    GATE_END_SECONDS,
    INVALID,
    LEASE_UNOBSERVED,
    PERSON_TOOL,
    RELEASE_SECONDS,
    RELEASE_TOLERANCE_SECONDS,
    _entered_or_ended,
    _other_launches,
    _phase,
    _read_of,
    _release_at,
    _settled,
    _sleep_until,
)
from differential.host_comparison import _of_launch, host_problems, same_lifetime
from differential.lease_probe import FREE
from differential.retirement_race import (
    DELIVERED,
    FAILED,
    NO_SILENT_CUT,
    SILENT,
    UNANSWERED,
    WARM_TOOL,
    _browsers_of,
    _calls,
    _members,
    _read_nothing,
    _emit_attempts,
    _page_problems,
    _read_ok,
    _session_problems,
    attempts_in,
    branch,
    call_classification,
)
from differential.synthetic_origin import (
    ALLOWED_HOSTS,
    DEADLINE,
    GATE_DEADLINE_SECONDS,
    PEER_GONE,
    RELEASED_BY_ROW,
    SERVED,
    person_path,
)
from linkedin_mcp_server.daemon_liveness import CALL_HEADER, HEARTBEAT_PATH

if TYPE_CHECKING:
    from differential.harness import OwnerLossSeams, RowContext

ROW_UNREACHABLE = "H-R8-unreachable"
ROW_OWNER_ERROR = "H-R8-owner-error"
ROW_RESPONDER_404 = "H-R8-responder-404"
ROW_RESPONDER_500 = "H-R8-responder-500"
ROW_H_R9 = "H-R9"

#: How an H-R8 lane takes the owner out of service.
KILL = "kill"
STOP = "stop"
RESPOND = "respond"


@dataclass(frozen=True)
class OwnerFault:
    """One H-R8 lane: what the row does to the owner between calls, the
    status a declared responder answers with, and the class the frontend's
    preflight line must name."""

    fault: str
    expected: str
    status: int | None = None

    @property
    def kills(self) -> bool:
        return self.fault in (KILL, RESPOND)


H_R8_CASES: dict[str, OwnerFault] = {
    ROW_UNREACHABLE: OwnerFault(KILL, UNANSWERED),
    ROW_OWNER_ERROR: OwnerFault(STOP, UNANSWERED),
    ROW_RESPONDER_404: OwnerFault(RESPOND, "route_missing", 404),
    ROW_RESPONDER_500: OwnerFault(RESPOND, "owner_error", 500),
}
ROWS = (*H_R8_CASES, ROW_H_R9)

#: Each lane's own usernames, so no request of one row stands for another's.
USERNAMES = {
    ROW_UNREACHABLE: "synthetic-unreachable",
    ROW_OWNER_ERROR: "synthetic-stopped",
    ROW_RESPONDER_404: "synthetic-responder-404",
    ROW_RESPONDER_500: "synthetic-responder-500",
}
#: The owner-error lane's later read, after the owner runs again.
LATER_USERNAME = "synthetic-stopped-later"

#: H-R9's recipient, and the read through the successor after the send.
RECIPIENT = "synthetic-r9"
FOLLOW_USERNAME = "synthetic-r9-follow"
MESSAGE_TOOL = "send_message"
MESSAGE_TEXT = "linkedin-mcp synthetic differential message"
#: What a request for a messaging page starts with; the origin serves none.
MESSAGING_PREFIX = "/messaging"

#: The calibration's idle timeout: the configuration H-CAL measured, the same
#: in K1, K3 and K0. Above the stop lane's whole stop and read, so the owner
#: resumed after ``STOP_SECONDS`` cannot have idled out by its own clock.
OWNER_LOSS_IDLE_TIMEOUT_SECONDS = CALIBRATION_IDLE_TIMEOUT_SECONDS

#: When the stopped owner is resumed, counted from the stop. Past the
#: preflight's bound, so the call meets the stopped owner, and short of the
#: election's own budget (90 s), so recovery may still be waiting for it.
STOP_SECONDS = 20.0
#: How late the resume may come after ``STOP_SECONDS`` before the experiment
#: is not the one declared: a stop that ran on toward the idle timeout lets a
#: healthy owner retire as it wakes, which would read as a replacement.
RESUME_TOLERANCE_SECONDS = 5.0
#: How long the frontend's preflight may take to fail against the stopped
#: owner: its own read timeout (``daemon_liveness.HEARTBEAT_SECONDS``, 2 s)
#: and the frontend's scheduling.
PREFLIGHT_BOUND_SECONDS = 10.0
#: How long one call may take to end: an election (90 s) and its settlement
#: (15 s), a listing and a read.
CALL_END_SECONDS = 180.0
#: How far apart a process's create time and the harness's wall clock may be
#: read (psutil's Linux boot time is in whole seconds).
START_TOLERANCE_SECONDS = 1.0

K2_NOT_APPLICABLE = {
    "status": "not applicable",
    "reason": (
        "the plan names a historical-daemon regression witness only for R6, R7, "
        "R11 and R12, none for an owner lost before or after dispatch, and the "
        "contract forbids inventing one"
    ),
}
#: What a Direct column of H-R8 does: the same host requests with no hop.
DIRECT_NO_FAULT = "not applicable: a Direct server has no owner to lose"
RETIRING_ELSEWHERE = (
    "RETIRING: the retirement-wins lanes of H-R13 and the turnover lanes "
    "(retirement_race.ROW_RETIREMENT, ROW_REFUSED, ROW_QUEUED), not repeated here"
)
UNIT_MATRIX = (
    "every other preflight answer and refusal: token_rejected, retiring, "
    "unexpected_status, unmarked_refused, and unreachable told from owner_error, "
    "stay with tests/test_daemon_proxy.py::TestThePreflightDecides "
    "(_PREFLIGHT_ROWS, 21 variants) and TestAnOwnersOwnRefusal"
)
UNOBSERVED = {
    KILL: (
        "whether the frontend opened a connection to the dead owner's port for "
        "the tool request: nothing listens there and no production seam records "
        "it; excluded by the owner's death confirmed before the send, the "
        "frontend's failed-preflight and replay lines, and every page of the read "
        "after the successor's launch"
    ),
    STOP: (
        "the preflight's class: the frontend's line names none, so owner_error "
        "and unreachable are told apart only in the unit matrix"
    ),
    RESPOND: (
        "nothing of the dispatch: the responder records every request on the "
        "old address, and a dispatch is one carrying the call's header off the "
        "heartbeat path"
    ),
}
AFTER_EFFECT_CONTROL = (
    "tests/test_daemon_proxy.py::TestAnOwnerProcessKilledMidCall::"
    "test_the_call_is_reported_as_unknown_and_never_repeated, a separate-process "
    "stand-in owner killed after its effect: a transport control, not native"
)
FENCE = (
    "send_message drives only the browser; the browser's only egress is the "
    "row's proxy, which tunnels www.linkedin.com and static.licdn.com to the "
    "loopback origin and refuses every other name and plain HTTP; the CI step "
    "maps those names to loopback and the cells refuse to start otherwise; the "
    "origin's profile page has no Message action and it serves no messaging "
    "page, so the product stops at message_unavailable"
)

#: What the frontend logs as it writes an owner off (``daemon_proxy``), with
#: the classification in parentheses; and the election's two probe verdicts
#: (``daemon_election``): refused, which buries locally, and silent.
BURIAL_LINE = "Not using this shared browser owner again ("
ELECTION_REFUSED_LINE = "The published daemon is not answering; electing a new one"
ELECTION_SILENT_LINE = "The published daemon has not answered yet; will ask again"
#: What the frontend logs as it answers a lost mutating call.
UNKNOWN_OUTCOME_LINE = "Owner lost mid-call; reporting an unknown outcome"
UNKNOWN_OUTCOME = "outcome_unknown"


def _s(seconds: float) -> int:
    return int(seconds * 1e9)


def message_arguments() -> dict[str, Any]:
    return {
        "linkedin_username": RECIPIENT,
        "message": MESSAGE_TEXT,
        "confirm_send": True,
    }


# --- The declared responder -------------------------------------------------------

#: The most of a request body the responder reads, to name its JSON-RPC method.
_BODY_LIMIT = 65536


class _ResponderHandler(BaseHTTPRequestHandler):
    server: DeclaredResponder

    def _answer(self) -> None:
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(min(max(length, 0), _BODY_LIMIT)) if length else b""
        try:
            method = json.loads(body).get("method") if body else None
        except (ValueError, AttributeError):
            method = None
        self.server.note(
            {
                "method": self.command,
                "path": self.path.split("?", 1)[0],
                # Whether each header is there, never its value: the bearer
                # token is the owner's.
                "call": CALL_HEADER in self.headers,
                "authorized": "authorization" in self.headers,
                "rpc": method if isinstance(method, str) else None,
                "monotonic_ns": time.monotonic_ns(),
            }
        )
        payload = b'{"synthetic": "declared responder"}'
        self.send_response(self.server.status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_DELETE = _answer

    def log_message(self, format: str, *args: Any) -> None:
        return None


class DeclaredResponder(ThreadingHTTPServer):
    """A loopback listener on a dead owner's address, answering every request
    with one declared status and recording each.

    Bound without ``SO_REUSEADDR`` on Windows, where it would let this bind
    beside a listener still there; elsewhere with it, where it only lets a
    listener bind past connections lingering from the dead one. A failed
    bind raises ``OSError``: the row's evidence is then invalid.
    """

    daemon_threads = True

    def __init__(self, host: str, port: int, status: int) -> None:
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.allow_reuse_address = os.name != "nt"
        self.allow_reuse_port = False
        self.status = status
        self._requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        super().__init__((host, port), _ResponderHandler)

    def server_bind(self) -> None:
        """Bind, and name the server by its address. ``HTTPServer``'s own
        names it through ``socket.getfqdn``, a reverse lookup that was seen
        to take past this bind's 30 s bound on a macOS runner."""
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def note(self, request: dict[str, Any]) -> None:
        with self._lock:
            self._requests.append(request)

    def requests(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(request) for request in self._requests]

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self.shutdown()
            self._thread.join(timeout=10)
            self._thread = None
        self.server_close()


def is_dispatch(request: Mapping[str, Any]) -> bool:
    """Whether a request the responder got belongs to a call's dispatch: it
    carries the call's header (``daemon_proxy`` sends it on the forwarded
    call and on the heartbeat only) and is not the heartbeat."""
    return request.get("call") is True and request.get("path") != HEARTBEAT_PATH


def is_preflight(request: Mapping[str, Any]) -> bool:
    return request.get("call") is True and request.get("path") == HEARTBEAT_PATH


# --- Readings the scripts take ----------------------------------------------------


def burial_reading(lines: Sequence[str]) -> dict[str, Any]:
    """Who wrote an owner off in *lines*: the frontend, by class, and the
    election, by how many probes it refused or found silent."""
    frontend = []
    for line in lines:
        if BURIAL_LINE in line:
            rest = line.split(BURIAL_LINE, 1)[1]
            frontend.append(rest.split(")", 1)[0])
    return {
        "frontend": frontend,
        "election_refused": sum(1 for line in lines if ELECTION_REFUSED_LINE in line),
        "election_silent": sum(1 for line in lines if ELECTION_SILENT_LINE in line),
    }


async def _preflight_seen(
    output: Callable[[], list[str]], mark: int, seconds: float
) -> int | None:
    """When a failed preflight is first reported past *mark*, within
    *seconds*, on the monotonic clock; None if it never is."""
    deadline = time.monotonic() + seconds
    while True:
        if any(a["attempt"] == "preflight" for a in attempts_in(output()[mark:])):
            return time.monotonic_ns()
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.05)


async def _settle_tasks(tasks: Sequence[asyncio.Future[Any] | None]) -> None:
    left = [task for task in tasks if task is not None]
    for task in left:
        task.cancel()
    await asyncio.gather(*left, return_exceptions=True)


async def _called(ctx: RowContext, tool: str, arguments: dict[str, Any]) -> None:
    """One call through the host, waited for within ``CALL_END_SECONDS``; its
    record is the timed call's own, however it ended."""
    call = asyncio.ensure_future(ctx.call(tool, arguments))
    try:
        await asyncio.wait({call}, timeout=CALL_END_SECONDS)
    finally:
        await _settle_tasks([call])


def _identity(ctx: RowContext) -> list[Any] | None:
    owner = ctx.owner()
    return [owner.pid, owner.create_time, owner.instance_id] if owner else None


# --- H-R8: the script -------------------------------------------------------------


async def unreachable_script(ctx: RowContext) -> None:
    """An H-R8 lane's scripted phase, after the warm-up read."""
    case = H_R8_CASES[ctx.row]
    record = ctx.record
    problems: list[str] = record["observation_problems"]
    username = USERNAMES[ctx.row]
    record.update(
        username=username,
        case={"fault": case.fault, "expected": case.expected, "status": case.status},
        retiring=RETIRING_ELSEWHERE,
        unit_matrix=UNIT_MATRIX,
        unobserved=UNOBSERVED[case.fault],
    )
    seams = ctx.owner_loss
    if seams is None:
        problems.append(f"{INVALID}the row was given no way to lose its owner")
        return
    if not ctx.daemon:
        record["fault"] = DIRECT_NO_FAULT
        if case.kills:
            # Traced over the same interval as the owner it stands in for.
            record["prepared"] = await seams.prepare()
        await _read(ctx, seams, "read", username)
        if case.fault == STOP:
            await _read(ctx, seams, "later", LATER_USERNAME)
        return
    record["owner_identified"] = _identity(ctx)
    if record["owner_identified"] is None:
        problems.append(f"{INVALID}the owner was never identified")
        return
    if case.fault == STOP:
        await _stopped(ctx, seams, username)
        return
    record["prepared"] = await seams.prepare()
    fault = await seams.kill()
    record["fault"] = fault
    if fault.get("exit") != "killed":
        problems.append(
            f"{INVALID}the owner was not shown killed before the call: "
            f"{fault.get('exit')!r}"
        )
        return
    _phase(ctx, "owner killed", fault.get("monotonic_ns"))
    record["owner_after_fault"] = await seams.owner_reading("after the kill")
    if case.fault == RESPOND:
        assert case.status is not None
        record["responder"] = await seams.respond(case.status)
        if not record["responder"].get("bound"):
            problems.append(
                f"{INVALID}the dead owner's address could not be bound again: "
                f"{record['responder'].get('error')!r}"
            )
            return
        _phase(ctx, "responder bound", record["responder"].get("bound_ns"))
    try:
        await _read(ctx, seams, "read", username)
    finally:
        if case.fault == RESPOND:
            record["responder"]["requests"] = seams.responder_requests()
            record["responder"].update(seams.stop_responding())
    record["owner_after_read"] = await seams.owner_reading("after the read")


async def _read(
    ctx: RowContext, seams: OwnerLossSeams, label: str, username: str
) -> None:
    """One read, with what the frontend reported meanwhile: its attempts and
    whoever wrote an owner off."""
    mark = len(seams.host_output())
    _phase(ctx, f"{label} sent")
    await _called(ctx, PERSON_TOOL, _read_of(username))
    _phase(ctx, f"{label} returned")
    window = seams.host_output()[mark:]
    attempts = attempts_in(window)
    burial = burial_reading(window)
    ctx.record[f"{label}_window"] = {"attempts": attempts, "burial": burial}
    _emit_attempts(ctx, label, attempts)
    if label == "read":
        # Only a category the frontend's own line names is claimed.
        ctx.record["burial"] = {"claimed": next(iter(burial["frontend"]), None)}


async def _stopped(ctx: RowContext, seams: OwnerLossSeams, username: str) -> None:
    """The owner stopped, the read sent and its preflight seen failing, the
    owner resumed at its declared time, then the read's end and a later one."""
    record = ctx.record
    problems: list[str] = record["observation_problems"]
    if seams.stop is None or seams.resume is None:
        problems.append(f"{INVALID}the row was given no way to stop its owner")
        return
    stop = seams.stop()
    record["fault"] = stop
    stopped = stop.get("stopped_ns")
    if not isinstance(stopped, int):
        problems.append(
            f"{INVALID}the owner was not stopped: {stop.get('error')!r}; nothing "
            f"was raced"
        )
        return
    _phase(ctx, "owner stopped", stopped)
    mark = len(seams.host_output())
    read: asyncio.Future[Any] | None = None
    try:
        _phase(ctx, "read sent")
        read = asyncio.ensure_future(ctx.call(PERSON_TOOL, _read_of(username)))
        record["preflight_failed_ns"] = await _preflight_seen(
            seams.host_output, mark, STOP_SECONDS
        )
        await _sleep_until(stopped + _s(STOP_SECONDS))
    finally:
        # On every path, before anything else can wait: resumed by the row,
        # at its declared time or as soon as the script is leaving. The
        # seam is synchronous, so a cancellation cannot come between.
        stop.update(seams.resume())
    if isinstance(stop.get("resumed_ns"), int):
        _phase(ctx, "owner resumed", stop["resumed_ns"])
    try:
        if read is not None:
            await asyncio.wait({read}, timeout=CALL_END_SECONDS)
    finally:
        await _settle_tasks([read])
    _phase(ctx, "read returned")
    window = seams.host_output()[mark:]
    attempts = attempts_in(window)
    burial = burial_reading(window)
    record["read_window"] = {"attempts": attempts, "burial": burial}
    _emit_attempts(ctx, "read", attempts)
    record["burial"] = {"claimed": next(iter(burial["frontend"]), None)}
    record["owner_after_read"] = await seams.owner_reading("after the read")
    await _read(ctx, seams, "later", LATER_USERNAME)
    record["owner_after_later"] = await seams.owner_reading("after the later read")


# --- H-R9: the script -------------------------------------------------------------


async def message_script(ctx: RowContext) -> None:
    """H-R9's scripted phase, after the warm-up read: tie the actor, hold the
    recipient's page, send, kill once it entered, and observe."""
    record = ctx.record
    problems: list[str] = record["observation_problems"]
    held = person_path(RECIPIENT, "main_profile")
    record.update(
        username=RECIPIENT,
        tool=MESSAGE_TOOL,
        held={"path": held, "ordinal": 1, "deadline_seconds": GATE_DEADLINE_SECONDS},
        after_effect_control=AFTER_EFFECT_CONTROL,
        fence=FENCE,
    )
    seams = ctx.owner_loss
    if seams is None:
        problems.append(f"{INVALID}the row was given no way to lose its actor")
        return
    if ctx.daemon:
        record["owner_identified"] = _identity(ctx)
        if record["owner_identified"] is None:
            problems.append(f"{INVALID}the owner was never identified")
            return
    record["prepared"] = await seams.prepare()
    gate = ctx.hold(held, ordinal=1)
    _phase(ctx, "armed")
    mark = len(seams.host_output())
    send = asyncio.ensure_future(ctx.call(MESSAGE_TOOL, message_arguments()))
    releasing: asyncio.Future[None] | None = None
    try:
        if not await _entered_or_ended(gate, send):
            problems.append(
                f"{INVALID}the recipient's page was not requested within "
                f"{ENTRY_SECONDS}s of arming, or the call ended first: nothing was "
                f"lost after dispatch"
            )
            return
        entered = gate.entered_monotonic_ns
        assert entered is not None
        _phase(ctx, "entered", entered)
        release: dict[str, Any] = {"scheduled_ns": entered + _s(RELEASE_SECONDS)}
        record["release"] = release
        releasing = asyncio.ensure_future(_release_at(gate, release["scheduled_ns"]))
        fault = await seams.kill()
        record["fault"] = fault
        if fault.get("exit") == "killed":
            _phase(
                ctx,
                "owner killed" if ctx.daemon else "server killed",
                fault.get("monotonic_ns"),
            )
        await asyncio.wait({send}, timeout=CALL_END_SECONDS)
        record["call_open"] = not send.done()
        _phase(ctx, "returned")
        window = seams.host_output()[mark:]
        attempts = attempts_in(window)
        record["send_window"] = {
            "attempts": attempts,
            "unknown_lines": sum(1 for line in window if UNKNOWN_OUTCOME_LINE in line),
        }
        _emit_attempts(ctx, "send", attempts)
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
        await _sleep_until(requested + _s(CONTINUATION_SECONDS))
        record["watched_until_ns"] = time.monotonic_ns()
        _phase(ctx, "watched")
        if ctx.daemon:
            record["owner_after_call"] = await seams.owner_reading("after the call")
            await _read(ctx, seams, "follow", FOLLOW_USERNAME)
            record["owner_after_read"] = await seams.owner_reading(
                "after the following read"
            )
        elif _settled(record.get("settlement")):
            record["fresh"] = await seams.fresh_read()
        else:
            # A fresh Direct server on a profile not shown free would be a
            # second browser of the harness's own making.
            record["fresh"] = {
                "made": False,
                "why": "the Direct server's profile was not shown settled",
            }
        _phase(ctx, "followed")
    finally:
        await _settle_tasks([releasing, send])


# --- Reading a record -------------------------------------------------------------


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


def _identified(record: Mapping[str, Any]) -> Sequence[Any] | None:
    identified = _sequence(record.get("owner_identified"))
    return identified if len(identified) == 3 else None


def _requests_for(record: Mapping[str, Any], path: str) -> list[Mapping[str, Any]]:
    return [
        _mapping(request)
        for request in _sequence(record.get("requests"))
        if _mapping(request).get("path") == path
    ]


def _launches(
    record: Mapping[str, Any], identified: Sequence[Any]
) -> tuple[list[list[Any]], list[list[Any]]] | None:
    """The owner launches the row made besides the identified owner's, and the
    release gates beyond one for each, or None unread.

    Counted by ``call_loss._other_launches`` from the lifetimes recorded after
    the watcher stopped, so a Windows venv launcher and its interpreter are
    one launch. That count takes every gate past the first as extra; one gate
    per launch is a launch's own, so only those beyond it are kept.
    """
    others = _other_launches(record, identified[:2])
    if others is None:
        return None
    # An election candidate the backoff started while the identified owner
    # held the lock, stopped or not, that is shown to have read nothing: gone,
    # and no browser of its own (``retirement_race._read_nothing``).
    others = [
        entry
        for entry in others
        if not (type(entry[0]) is int and _read_nothing(entry, record))
    ]
    owners = [list(entry) for entry in others if type(entry[0]) is int]
    gates = [list(entry) for entry in others if entry[0] == "release gate"]
    # The identified owner not among the row's launches at all.
    strays = [
        list(entry)
        for entry in others
        if type(entry[0]) is str and entry[0] != "release gate"
    ]
    return owners, [*strays, *gates[len(owners) :]]


def _successor_problems(
    record: Mapping[str, Any],
    label: str,
    began: float | None,
    ended: float | None,
    username: str,
) -> list[str]:
    """A successor this window started, which read *username*'s pages: published
    after it, other than the lost owner, the one launch the row made besides
    that owner's, launched between *began* and *ended* on the wall clock, and
    every one of those pages read through a browser it launched."""
    identified = _identified(record)
    if identified is None:
        return ["the lost owner was never identified"]
    seen = _mapping(record.get(label))
    lifetime = _sequence(seen.get("lifetime"))
    if len(lifetime) != 2:
        return [f"no successor is published after the read: {seen.get('problem')!r}"]
    found = []
    if (
        same_lifetime(lifetime, identified[:2])
        or seen.get("instance_id") == identified[2]
    ):
        found.append("the read was answered by the lost owner, not a successor")
    launched = _launches(record, identified)
    if launched is None:
        return [*found, "the row's owner and release gate lifetimes were not recorded"]
    owners, extra = launched
    processes = _sequence(record.get("owner_processes"))
    mine = [entry for entry in owners if _of_launch(lifetime, entry, processes)]
    if len(mine) != 1:
        found.append("the successor is not an owner the row was seen to launch")
    others = [entry for entry in owners if entry not in mine]
    if others or extra:
        found.append(
            f"the row launched other owners besides the successor: {others + extra}"
        )
    start = _number(mine[0][-1]) if len(mine) == 1 else None
    if began is None or ended is None or start is None:
        found.append("the successor's launch cannot be placed against the window")
    elif (
        not began - START_TOLERANCE_SECONDS <= start <= ended + START_TOLERANCE_SECONDS
    ):
        found.append(
            f"the successor was not started inside the window: launched "
            f"{start - began:+.1f}s from its start"
        )
    # Which launch read a page is told by the browser it went through, never
    # by when the processes started: an owner releases its lock before it
    # exits, so a launch can read and be gone before the successor takes over.
    pages = [
        _number(_mapping(request).get("t"))
        for request in _sequence(record.get("requests"))
        if str(_mapping(request).get("path", "")).startswith(f"/in/{username}/")
        and began is not None
        and (_number(_mapping(request).get("t")) or 0.0) >= began
    ]
    browsers = _browsers_of(_members(mine[0], record), record) if mine else None
    if not pages:
        found.append("the window read no page")
    elif browsers is None:
        found.append(
            "the row's browsers and who launched them were not recorded, so the "
            "successor is not shown to have read the pages"
        )
    else:
        unread = [
            page
            for page in pages
            if page is None
            or not any(
                (_number(root[1]) or 0.0) - START_TOLERANCE_SECONDS <= page
                and (root[2] is None or page <= (_number(root[2]) or 0.0))
                for root in browsers
            )
        ]
        if unread:
            found.append(
                f"{len(unread)} of the pages were read through no browser of the "
                f"successor: the successor did not read them"
            )
    return found


def _owner_kept(record: Mapping[str, Any], label: str) -> bool:
    """The identified owner, alive and the same lifetime, and no other started."""
    identified = _identified(record)
    seen = _mapping(record.get(label))
    if identified is None:
        return False
    launched = _launches(record, identified)
    return (
        seen.get("alive") is True
        and same_lifetime(seen.get("lifetime"), identified[:2])
        and seen.get("instance_id") == identified[2]
        and launched is not None
        and launched == ([], [])
    )


def _common(record: Mapping[str, Any], *, daemon: bool) -> list[str]:
    problems: list[str] = []
    mode = "daemon" if daemon else "direct"
    if record.get("mode") != mode:
        problems.append(f"the record is for mode {record.get('mode')!r}, not {mode}")
    if record.get("script_error"):
        problems.append(f"the row's script failed: {record['script_error']}")
    problems += [str(p) for p in _sequence(record.get("observation_problems"))]
    if record.get("idle_timeout_seconds") != OWNER_LOSS_IDLE_TIMEOUT_SECONDS:
        problems.append(
            f"the row ran with an idle timeout of "
            f"{record.get('idle_timeout_seconds')!r}, not the declared "
            f"{OWNER_LOSS_IDLE_TIMEOUT_SECONDS}"
        )
    if record.get("k2") != K2_NOT_APPLICABLE:
        problems.append("the record does not say why K2 is not applicable")
    if record.get("left_stopped"):
        problems.append(
            f"{INVALID}a harness failure: the row left its owner stopped, and the "
            f"harness resumed it"
        )
    calls = [_mapping(call) for call in _sequence(record.get("calls"))]
    if not calls or calls[0].get("tool") != WARM_TOOL or not _read_ok_feed(calls[0]):
        problems.append(f"{INVALID}the warm-up read is not recorded as returned")
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


def _read_ok_feed(call: Mapping[str, Any]) -> bool:
    return (
        call.get("outcome") == "returned"
        and call.get("is_error") is False
        and call.get("read_the_post") is True
    )


def _in_window(
    record: Mapping[str, Any], reading: str, after: int | None, label: str
) -> list[str]:
    """A reading taken after *after* and before the harness's cleanup."""
    seen = _ns(_mapping(record.get(reading)).get("seen_ns"))
    cleanup = _ns(record.get("cleanup_began_ns"))
    if seen is None or after is None or seen < after:
        return [f"{INVALID}{label} is not shown read after the fault"]
    if cleanup is None or seen > cleanup:
        return [
            f"{INVALID}{label} was read after the harness's cleanup began, so it "
            f"cannot be credited to the product"
        ]
    return []


# --- H-R8: the verdict ------------------------------------------------------------


def _h_r8_reads(
    record: Mapping[str, Any], case: OwnerFault
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None, list[str]]:
    reads = _calls(record, PERSON_TOOL)
    wanted = 2 if case.fault == STOP else 1
    if len(reads) != wanted:
        return (
            None,
            None,
            [f"{INVALID}the record holds {len(reads)} reads, not {wanted}"],
        )
    return reads[0], reads[1] if wanted == 2 else None, []


def _direct_h_r8(record: Mapping[str, Any], case: OwnerFault) -> list[str]:
    """K1: the same host requests through Direct, each read delivered from its
    own pages, and no owner fault named."""
    problems: list[str] = []
    if record.get("fault") != DIRECT_NO_FAULT:
        problems.append("a Direct record names an owner fault")
    for name in ("responder", "owner_identified"):
        if record.get(name) is not None:
            problems.append(f"a Direct record holds {name}")
    read, later, invalid = _h_r8_reads(record, case)
    problems += invalid
    for call, username, label in (
        (read, USERNAMES[str(record.get("row"))], "read"),
        (later, LATER_USERNAME, "later read"),
    ):
        if call is None:
            continue
        if not _read_ok(call):
            problems.append(
                f"the {label} did not return its sections: outcome "
                f"{call.get('outcome')!r}, error {call.get('is_error')!r}"
            )
        problems += [f"{label}: {p}" for p in _page_problems(record, username, call)]
    return problems


def _kill_invalid(record: Mapping[str, Any], read: Mapping[str, Any]) -> list[str]:
    """Why a kill lane's record does not measure an owner dead before the call."""
    found: list[str] = []
    fault = _mapping(record.get("fault"))
    killed_at = _ns(fault.get("monotonic_ns"))
    if fault.get("exit") != "killed" or killed_at is None:
        found.append(
            f"{INVALID}the owner is not shown killed before the call: "
            f"{fault.get('exit')!r}"
        )
        return found
    sent = _ns(read.get("began_monotonic_ns"))
    if sent is None or sent < killed_at:
        found.append(f"{INVALID}the read was not sent after the owner was killed")
    after = _mapping(record.get("owner_after_fault"))
    if after.get("alive") is not False:
        found.append(
            f"{INVALID}the killed owner is not shown gone before the call: alive "
            f"{after.get('alive')!r}"
        )
    found += _in_window(record, "owner_after_fault", killed_at, "the owner reading")
    found += _in_window(record, "owner_after_read", killed_at, "the successor reading")
    return found


def _responder_invalid(
    record: Mapping[str, Any], read: Mapping[str, Any], case: OwnerFault
) -> list[str]:
    found: list[str] = []
    responder = _mapping(record.get("responder"))
    if responder.get("bound") is not True:
        return [
            f"{INVALID}the dead owner's address was not bound again: "
            f"{responder.get('error')!r}"
        ]
    if responder.get("status") != case.status:
        found.append(
            f"{INVALID}the responder answered {responder.get('status')!r}, not the "
            f"lane's {case.status}"
        )
    bound = _ns(responder.get("bound_ns"))
    sent = _ns(read.get("began_monotonic_ns"))
    if bound is None or sent is None or bound > sent:
        found.append(f"{INVALID}the responder was not bound before the read was sent")
    requests = [_mapping(r) for r in _sequence(responder.get("requests"))]
    if not any(is_preflight(request) for request in requests):
        found.append(
            f"{INVALID}the frontend never met the responder: no preflight reached "
            f"the dead owner's address"
        )
    return found


def _stop_invalid(record: Mapping[str, Any], read: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    if str(record.get("platform", "")).startswith("win"):
        found.append(f"{INVALID}Windows has no portable stop; the lane is a skip")
    fault = _mapping(record.get("fault"))
    stopped = _ns(fault.get("stopped_ns"))
    resumed = _ns(fault.get("resumed_ns"))
    if stopped is None:
        return [*found, f"{INVALID}the owner was not stopped: {fault.get('error')!r}"]
    if resumed is None or fault.get("resumed_by") != "row":
        found.append(
            f"{INVALID}a harness failure: the stopped owner was not resumed by the "
            f"row (by {fault.get('resumed_by')!r}: {fault.get('resume_error')!r})"
        )
    sent = _ns(read.get("began_monotonic_ns"))
    if sent is None or sent < stopped or (resumed is not None and sent > resumed):
        found.append(f"{INVALID}the read was not sent while the owner was stopped")
    if resumed is not None and (resumed - stopped) / 1e9 < STOP_SECONDS:
        found.append(
            f"{INVALID}the owner was resumed {(resumed - stopped) / 1e9:.1f}s after "
            f"the stop, before its declared {STOP_SECONDS}s"
        )
    if (
        resumed is not None
        and (resumed - stopped) / 1e9 > STOP_SECONDS + RESUME_TOLERANCE_SECONDS
    ):
        found.append(
            f"{INVALID}the owner was resumed {(resumed - stopped) / 1e9:.1f}s after "
            f"the stop, past its declared {STOP_SECONDS}s and "
            f"{RESUME_TOLERANCE_SECONDS}s tolerance: what it did on waking is "
            f"not this lane's to judge"
        )
    return found


def _classification_problems(
    record: Mapping[str, Any], case: OwnerFault, attempts: Any
) -> list[str]:
    """The frontend's own report that the preflight failed, with the lane's
    class; and nothing that says the call reached the failed owner."""
    found = []
    items = [_mapping(item) for item in _sequence(attempts)]
    first = items[0] if items else {}
    if first.get("attempt") != "preflight":
        found.append(
            f"the frontend reported no failed preflight first: "
            f"{[dict(item) for item in items]}"
        )
    classification = call_classification(attempts)
    if classification != case.expected:
        found.append(
            f"the frontend classified the failed owner as {classification!r}, not "
            f"{case.expected!r}"
        )
    if any(item.get("attempt") == "dispatch" for item in items):
        found.append(
            "the failed owner refused the call itself: a dispatch reached it, past "
            "the preflight"
        )
    return found


def _burial_problems(record: Mapping[str, Any]) -> list[str]:
    """A burial category claimed is one a frontend line shows."""
    claimed = _mapping(record.get("burial")).get("claimed")
    if claimed is None:
        return []
    shown = _sequence(
        _mapping(_mapping(record.get("read_window")).get("burial")).get("frontend")
    )
    if claimed not in shown:
        return [
            f"the record claims the burial {claimed!r}, which no frontend line shows"
        ]
    return []


def _lost_owner_findings(
    record: Mapping[str, Any], case: OwnerFault, read: Mapping[str, Any]
) -> list[str]:
    """Kill and responder lanes: the read recovered to a verified successor,
    replayed only because nothing was sent, with nothing dispatched to the
    failed owner."""
    found: list[str] = []
    username = USERNAMES[str(record.get("row"))]
    attempts = _mapping(record.get("read_window")).get("attempts")
    found += _classification_problems(record, case, attempts)
    items = [_mapping(item) for item in _sequence(attempts)]
    if not any(item.get("attempt") == "replay" for item in items):
        found.append(
            "the frontend is not shown to replay the call it had not sent: no "
            "replay line"
        )
    if branch(read) != DELIVERED:
        found.append(
            f"the read did not succeed on a successor: outcome "
            f"{read.get('outcome')!r}, error {read.get('is_error')!r}"
        )
        return found
    fault = _ns(_mapping(record.get("fault")).get("monotonic_ns"))
    found += _page_problems(
        record, username, read, after=fault, after_label="the owner was killed"
    )
    found += _successor_problems(
        record,
        "owner_after_read",
        _number(read.get("began")),
        _number(read.get("ended")),
        username,
    )
    if case.fault == RESPOND:
        requests = [
            _mapping(r)
            for r in _sequence(_mapping(record.get("responder")).get("requests"))
        ]
        dispatched = [dict(r) for r in requests if is_dispatch(r)]
        if dispatched:
            found.append(
                f"a tool dispatch reached the failed instance: {len(dispatched)} "
                f"request(s) carrying the call off the heartbeat path"
            )
    return found


def _stop_findings(
    record: Mapping[str, Any],
    read: Mapping[str, Any],
    later: Mapping[str, Any] | None,
    case: OwnerFault,
) -> list[str]:
    """The stopped owner: a bounded failed preflight, nothing run on it while
    stopped, the read delivered after it ran again or failed explicitly, and a
    later read through it."""
    found: list[str] = []
    username = USERNAMES[ROW_OWNER_ERROR]
    attempts = _mapping(record.get("read_window")).get("attempts")
    found += _classification_problems(record, case, attempts)
    fault = _mapping(record.get("fault"))
    stopped = _ns(fault.get("stopped_ns"))
    resumed = _ns(fault.get("resumed_ns"))
    sent = _ns(read.get("began_monotonic_ns"))
    seen = _ns(record.get("preflight_failed_ns"))
    if seen is None or sent is None or (seen - sent) / 1e9 > PREFLIGHT_BOUND_SECONDS:
        found.append(
            f"the frontend's preflight is not shown to fail within "
            f"{PREFLIGHT_BOUND_SECONDS}s while the owner was stopped"
        )
    elif resumed is not None and seen > resumed:
        found.append("the preflight failure was seen only after the owner resumed")
    pages = [
        _ns(_mapping(request).get("monotonic_ns"))
        for section in EXPECTED_SECTIONS
        for request in _requests_for(record, person_path(username, section))
    ]
    if stopped is not None and any(
        at is None or resumed is None or at < resumed for at in pages
    ):
        found.append(
            "a tool ran on the stopped owner: a page of the read before it resumed"
        )
    outcome = branch(read)
    if outcome == SILENT:
        found.append(
            f"the read against the stopped owner was cut silently: outcome "
            f"{read.get('outcome')!r}, error {read.get('is_error')!r}"
        )
    elif outcome == DELIVERED:
        found += _page_problems(
            record, username, read, after=resumed, after_label="the owner resumed"
        )
    elif pages:
        found.append(f"the failed read still requested {len(pages)} page(s)")
    if not _owner_kept(record, "owner_after_read"):
        found.append(
            "the stopped owner is not shown kept: alive, the same lifetime and the "
            "only owner started, after the read"
        )
    if later is None or not _read_ok(later):
        found.append("the later read did not succeed after the owner resumed")
    else:
        found += [
            f"later read: {p}"
            for p in _page_problems(
                record, LATER_USERNAME, later, after=resumed, after_label="the resume"
            )
        ]
        later_attempts = _sequence(_mapping(record.get("later_window")).get("attempts"))
        if later_attempts:
            found.append(
                f"the later read met a refusal or an election: {list(later_attempts)}"
            )
    if not _owner_kept(record, "owner_after_later"):
        found.append("the later read is not shown served by the owner that was stopped")
    found += _in_window(record, "owner_after_read", resumed, "the owner reading")
    return found


def h_r8_problems(record: Mapping[str, Any] | None, *, daemon: bool) -> list[str]:
    """An H-R8 lane's verdict over its raw record: every problem, or nothing.
    Invalid evidence starts with ``INVALID``."""
    if not isinstance(record, Mapping):
        return ["the row kept no record"]
    row = record.get("row")
    case = H_R8_CASES.get(row) if isinstance(row, str) else None
    if case is None:
        return [f"the record is for row {row!r}, which loses no owner before dispatch"]
    problems = _common(record, daemon=daemon)
    problems += host_problems(record.get("host"))
    if record.get("username") != USERNAMES[str(row)]:
        problems.append(
            f"{INVALID}the record names {record.get('username')!r} as its username"
        )
    for name, value in (
        ("retiring", RETIRING_ELSEWHERE),
        ("unit_matrix", UNIT_MATRIX),
        ("unobserved", UNOBSERVED[case.fault]),
    ):
        if record.get(name) != value:
            problems.append(f"the record does not carry its {name} statement")
    if not daemon:
        problems += _direct_h_r8(record, case)
        problems += _session_problems(record, _usernames(record))
        return problems
    if _identified(record) is None:
        return [*problems, f"{INVALID}the owner was never identified"]
    read, later, invalid = _h_r8_reads(record, case)
    problems += invalid
    if read is None:
        return problems
    if case.fault == STOP:
        problems += _stop_invalid(record, read)
        if _ns(_mapping(record.get("fault")).get("stopped_ns")) is not None:
            problems += _stop_findings(record, read, later, case)
    else:
        problems += _kill_invalid(record, read)
        if case.fault == RESPOND:
            problems += _responder_invalid(record, read, case)
        if _mapping(record.get("fault")).get("exit") == "killed":
            problems += _lost_owner_findings(record, case, read)
    problems += _burial_problems(record)
    problems += _session_problems(record, _usernames(record))
    return problems


def _usernames(record: Mapping[str, Any]) -> list[str]:
    row = str(record.get("row"))
    if row == ROW_H_R9:
        return [RECIPIENT, FOLLOW_USERNAME]
    names = [USERNAMES[row]] if row in USERNAMES else []
    return [*names, LATER_USERNAME] if row == ROW_OWNER_ERROR else names


# --- H-R9: the verdict ------------------------------------------------------------


def _unknown_outcome(call: Mapping[str, Any]) -> bool:
    return (
        call.get("outcome") == "returned"
        and call.get("is_error") is True
        and call.get("status") == UNKNOWN_OUTCOME
        and call.get("retry_safe") is False
    )


def send_category(call: Mapping[str, Any] | None) -> str:
    """How the mutating call ended for its caller: ``outcome_unknown``, an
    explicit failure, a success, or nothing at all."""
    call = _mapping(call)
    if _unknown_outcome(call):
        return UNKNOWN_OUTCOME
    outcome = call.get("outcome")
    if outcome == "raised" or (outcome == "returned" and call.get("is_error") is True):
        return FAILED
    if outcome == "returned":
        return "success"
    return SILENT


def _h_r9_invalid(record: Mapping[str, Any], send: Mapping[str, Any]) -> list[str]:
    """Why the record does not measure an actor lost after the mutating call's
    first navigation entered the gate."""
    found: list[str] = []
    if record.get("username") != RECIPIENT:
        found.append(f"the record names {record.get('username')!r} as its recipient")
    held = person_path(RECIPIENT, "main_profile")
    gates = [
        _mapping(gate)
        for gate in _sequence(record.get("gates"))
        if _mapping(gate).get("path") == held
    ]
    gate = gates[0] if len(gates) == 1 else {}
    entered = _ns(gate.get("entered_monotonic_ns"))
    fault = _mapping(record.get("fault"))
    killed = _ns(fault.get("monotonic_ns"))
    if not gate:
        found.append("the record holds no gate on the recipient's page")
    elif entered is None:
        found.append(
            "the recipient's page never entered the gate, so nothing was lost after "
            "dispatch"
        )
    if fault.get("exit") != "killed" or killed is None:
        found.append(f"the actor is not shown killed: {fault.get('exit')!r}")
    if entered is not None and killed is not None and killed < entered:
        found.append("the loss came before the recipient's page entered the gate")
    sent = _ns(send.get("began_monotonic_ns"))
    if sent is None or entered is None or sent > entered:
        found.append("the message is not shown sent before its page entered the gate")
    if gate and entered is not None:
        ended = _ns(gate.get("released_monotonic_ns"))
        if killed is not None and ended is not None and ended < killed:
            found.append("the hold had ended before the kill")
        terminal = gate.get("terminal")
        if terminal == DEADLINE:
            found.append("the hold ran out its deadline before the release")
        elif terminal not in (SERVED, PEER_GONE):
            found.append(f"the hold recorded no end: {terminal!r}")
        if gate.get("released_by") != RELEASED_BY_ROW:
            found.append(f"the hold was released by {gate.get('released_by')!r}")
        requested = _ns(gate.get("release_requested_monotonic_ns"))
        cleanup = _ns(record.get("cleanup_began_ns"))
        if requested is None:
            found.append("no release was asked for")
        else:
            late = (requested - entered) / 1e9 - RELEASE_SECONDS
            if late < 0:
                found.append("the release was asked for before its declared time")
            elif late > RELEASE_TOLERANCE_SECONDS:
                found.append(
                    f"the release was asked for {late:.1f}s after its declared time"
                )
            if cleanup is None or cleanup - requested < _s(CONTINUATION_SECONDS):
                found.append(
                    f"the origin was watched for less than {CONTINUATION_SECONDS}s "
                    f"after the release"
                )
    found = [f"{INVALID}{reason}" for reason in found]
    for reading, label in (
        ("settlement", "the settlement"),
        ("owner_after_call", "the owner reading after the call"),
        ("owner_after_read", "the successor reading"),
    ):
        if record.get(reading) is not None:
            found += _in_window(record, reading, killed, label)
    return found


def _h_r9_findings(
    record: Mapping[str, Any], send: Mapping[str, Any], *, daemon: bool
) -> list[str]:
    found: list[str] = []
    killed = _ns(_mapping(record.get("fault")).get("monotonic_ns"))
    recipient = _requests_for(record, person_path(RECIPIENT, "main_profile"))
    if len(recipient) != 1:
        found.append(
            f"the recipient's page was requested {len(recipient)} times, not once: "
            f"the mutating call was replayed or never dispatched"
        )
    late = [
        r
        for r in recipient
        if killed is None
        or _ns(r.get("monotonic_ns")) is None
        or (_ns(r.get("monotonic_ns")) or 0) >= killed
    ]
    if late:
        found.append(
            f"the recipient's page was navigated to again after the kill: "
            f"{len(late)} request(s)"
        )
    messaging = [
        _mapping(r).get("path")
        for r in _sequence(record.get("requests"))
        if str(_mapping(r).get("path") or "").startswith(MESSAGING_PREFIX)
    ]
    if messaging:
        found.append(f"the call went on toward the composer: {messaging}")
    egress = _mapping(record.get("egress"))
    forwarded = egress.get("forwarded")
    if not isinstance(forwarded, list):
        found.append("the row's egress through its proxy was not recorded")
    elif set(forwarded) - set(ALLOWED_HOSTS):
        found.append(
            f"the proxy forwarded the row to hosts outside the synthetic origin: "
            f"{sorted(set(forwarded) - set(ALLOWED_HOSTS))}"
        )
    if record.get("call_open"):
        found.append(f"the mutating call did not end within {CALL_END_SECONDS}s")
    category = send_category(send)
    if daemon:
        found += _h_r9_daemon(record, send, category)
    else:
        found += _h_r9_direct(record, category)
    return found


def _h_r9_daemon(
    record: Mapping[str, Any], send: Mapping[str, Any], category: str
) -> list[str]:
    found: list[str] = []
    if category != UNKNOWN_OUTCOME:
        detail = (
            "reported as a success"
            if category == "success"
            else f"{category}, retry_safe {send.get('retry_safe')!r}"
        )
        if send.get("retry_safe") is True:
            detail += ", retry_safe true"
        found.append(
            f"the lost mutating call was not reported as an unknown outcome unsafe "
            f"to retry: {detail}"
        )
    attempts = [
        _mapping(item)
        for item in _sequence(_mapping(record.get("send_window")).get("attempts"))
    ]
    if any(item.get("attempt") == "replay" for item in attempts):
        found.append("the frontend replayed the mutating call")
    follows = [_mapping(call) for call in _calls(record, PERSON_TOOL)]
    follow = follows[0] if len(follows) == 1 else None
    if follow is None or not _read_ok(follow):
        found.append("the read after the lost call did not succeed through the host")
        return found
    killed = _ns(_mapping(record.get("fault")).get("monotonic_ns"))
    found += [
        f"following read: {p}"
        for p in _page_problems(
            record, FOLLOW_USERNAME, follow, after=killed, after_label="the kill"
        )
    ]
    found += _successor_problems(
        record,
        "owner_after_read",
        _number(send.get("began")),
        _number(follow.get("ended")),
        FOLLOW_USERNAME,
    )
    return found


def _h_r9_direct(record: Mapping[str, Any], category: str) -> list[str]:
    found: list[str] = []
    if category not in (FAILED, UNKNOWN_OUTCOME):
        found.append(
            f"the call whose server was killed is not shown failed to its caller: "
            f"{category}"
        )
    host = _mapping(record.get("host"))
    if host.get("error"):
        found.append(f"the host session failed: {host['error']}")
    settlement = _mapping(record.get("settlement"))
    if not settlement:
        found.append("the profile was not read after the kill")
    else:
        if settlement.get("error"):
            found.append(
                f"reading the profile after the kill failed: {settlement['error']}"
            )
        if settlement.get("remaining") != [] or settlement.get("unresolved") != []:
            found.append(
                f"the profile's browser is not shown gone after the kill: still "
                f"{settlement.get('remaining')!r}, unreadable "
                f"{settlement.get('unresolved')!r}"
            )
        windows = str(record.get("platform", "")).startswith("win")
        lease = settlement.get("lease")
        if lease not in ((LEASE_UNOBSERVED, FREE) if windows else (FREE,)):
            found.append(f"the profile lease was {lease!r} after the kill, not free")
        if not windows and settlement.get("guardian_exit") != "exited":
            found.append(
                f"the killed server's guardian is not shown to drain and exit: "
                f"{settlement.get('guardian_exit')!r}"
            )
    fresh = _mapping(record.get("fresh"))
    call = _mapping(fresh.get("call"))
    if fresh.get("made") is not True:
        found.append(f"no read was made after the kill: {fresh.get('why')!r}")
    elif (
        call.get("outcome") != "returned"
        or call.get("is_error") is not False
        or call.get("read_the_post") is not True
    ):
        found.append(
            f"the fresh host's read did not return the synthetic post: "
            f"{call.get('outcome')!r}, error {call.get('is_error')!r}"
        )
    else:
        if fresh.get("quit_problems"):
            found.append(
                f"the fresh host did not quit normally: {fresh['quit_problems']}"
            )
        if fresh.get("forwarded"):
            found.append("the fresh Direct host forwarded to a shared owner")
    return found


def h_r9_problems(record: Mapping[str, Any] | None, *, daemon: bool) -> list[str]:
    """H-R9's verdict over its raw record: every problem, or nothing. Invalid
    evidence starts with ``INVALID``."""
    if not isinstance(record, Mapping):
        return ["the row kept no record"]
    if record.get("row") != ROW_H_R9:
        return [f"the record is for row {record.get('row')!r}, which sends no message"]
    problems = _common(record, daemon=daemon)
    if daemon:
        problems += host_problems(record.get("host"))
        if _identified(record) is None:
            problems.append(f"{INVALID}the owner was never identified")
    for name, value in (
        ("after_effect_control", AFTER_EFFECT_CONTROL),
        ("fence", FENCE),
    ):
        if record.get(name) != value:
            problems.append(f"the record does not carry its {name} statement")
    sends = _calls(record, MESSAGE_TOOL)
    if len(sends) != 1:
        return [*problems, f"{INVALID}the record holds {len(sends)} messages, not one"]
    send = sends[0]
    invalid = _h_r9_invalid(record, send)
    problems += invalid
    # With no loss after dispatch there is nothing of the product's to judge.
    if not any("nothing was lost" in p or "the loss came before" in p for p in invalid):
        if _mapping(record.get("fault")).get("exit") == "killed":
            problems += _h_r9_findings(record, send, daemon=daemon)
    problems += _session_problems(record, _usernames(record))
    return problems


# --- Comparisons ------------------------------------------------------------------


def problems_for(record: Mapping[str, Any] | None, *, daemon: bool) -> list[str]:
    """The verdict of whichever row of this module *record* is for."""
    if _mapping(record).get("row") == ROW_H_R9:
        return h_r9_problems(record, daemon=daemon)
    return h_r8_problems(record, daemon=daemon)


def invalid_evidence(problems: Sequence[str]) -> list[str]:
    """The problems that leave a row unmeasured rather than failed."""
    return [problem for problem in problems if problem.startswith(INVALID)]


def semantics(record: Mapping[str, Any]) -> dict[str, Any]:
    """What K0 compares: classifications only, no pid, time or path. A call
    against the stopped owner projects both safe branches alike: whether
    recovery waited for it is a race the contract allows either way."""
    row = record.get("row")
    found: dict[str, Any] = {"row": row, "mode": record.get("mode")}
    if row == ROW_H_R9:
        sends = _calls(record, MESSAGE_TOOL)
        found["send"] = send_category(sends[0]) if sends else None
        found["recipient_pages"] = len(
            _requests_for(record, person_path(RECIPIENT, "main_profile"))
        )
        found["replays"] = sum(
            1
            for item in _sequence(_mapping(record.get("send_window")).get("attempts"))
            if _mapping(item).get("attempt") == "replay"
        )
        found["fault"] = _mapping(record.get("fault")).get("kind")
        return found
    window = _mapping(record.get("read_window"))
    reads = _calls(record, PERSON_TOOL)
    found["classification"] = call_classification(window.get("attempts"))
    found["burial"] = _mapping(record.get("burial")).get("claimed")
    found["dispatched_to_failed"] = sum(
        1
        for r in _sequence(_mapping(record.get("responder")).get("requests"))
        if is_dispatch(_mapping(r))
    )
    if row == ROW_OWNER_ERROR:
        found["read"] = (
            NO_SILENT_CUT if reads and branch(reads[0]) != SILENT else SILENT
        )
        found["later"] = branch(reads[1]) if len(reads) > 1 else None
    else:
        found["read"] = branch(reads[0]) if reads else None
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
    O1 to O4 are the vectors' (``compare_to_direct``); nothing dispatched
    twice is each record's own verdict, so two valid records agree on it."""
    return _refusals([("Direct", direct, False), ("daemon", daemon, True)])
