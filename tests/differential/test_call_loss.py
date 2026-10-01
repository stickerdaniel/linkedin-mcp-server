"""The held read and its row: the gate, the person pages, the call records,
the row declarations, and H-CAL's verdict and wiring.

No browser. The **gate** and the **person pages** are the real synthetic
origin over loopback TLS, asked by a plain client that trusts the run's CA in
its own context only. The **call records** go through ``timed_call`` with a
client double, and the ``before_stop`` hook through ``run_host_session`` on
the host stub's stand-in server. The **declarations** are refused through
the real row entry, ``measure_host_quit_row``. The **verdict** starts from an
explicit valid record and changes one observation. The **wiring** runs the
real row entry in Direct mode on the preservation gate's modelled actors,
with a host double whose reads are real requests to a real origin, so the
row's own script arms, waits for and releases a real gate.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import os
import socket
import ssl
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import mcp.types as mcp_types
import pytest

from differential import harness, lease_probe, unconfirmed_close
from differential.call_loss import (
    CALIBRATION_IDLE_TIMEOUT_SECONDS,
    CALIBRATION_USERNAME,
    EXPECTED_SECTIONS,
    HELD_SECTION,
    K2_NOT_APPLICABLE,
    NEXT_SECTION,
    PERSON_TOOL,
    ROW_H_CAL,
    calibration_problems,
    semantic_differences,
)
from differential.events import EventLog, validate
from differential.harness import (
    CANCELLED,
    FEED_READ,
    MUST_NOT_REPAIR,
    MUST_REMAIN_CLEARED,
    ORDINARY,
    RAISED,
    RETURNED,
    DaemonCleanup,
    InitialCall,
    PostQuit,
    RowLifecycle,
    judge_row,
    lifecycle_problems,
    measure_host_quit_row,
    preservation_policy_refusals,
    run_host_session,
    timed_call,
    tool_summary,
)
from differential.synthetic_origin import (
    CA_FILE,
    DEADLINE,
    GATE_DEADLINE_SECONDS,
    PEER_GONE,
    PERSON_MARKERS,
    PERSON_PAGES,
    RELEASED_BY_ROW,
    RELEASED_BY_TEARDOWN,
    SERVED,
    SyntheticOrigin,
    issue_certificates,
    person_path,
)
from differential.test_host_stub import _STAND_IN_SERVER
from differential.test_preservation_gate import (  # noqa: F401 - fixtures
    _SETTLED,
    _Oracle,
    _Watcher,
    profile,
)
from differential.test_row_judgement import _healthy
from differential.unconfirmed_close import R7Setup
from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.core.auth import _LOGIN_TITLE_PATTERNS
from linkedin_mcp_server.daemon_auth import MARKER_KEY
from linkedin_mcp_server.daemon_liveness import REFUSAL_KEY
from linkedin_mcp_server.linkedin.fields import PERSON_SECTIONS

MS = 1_000_000

#: Taken before any fixture replaces it.
REAL_ACTOR_ENVIRONMENT = harness.actor_environment


@pytest.fixture(autouse=True)
def owned(monkeypatch):
    """Fresh registries, so one test's retained worker never gates the next."""
    monkeypatch.setattr(unconfirmed_close, "_OWNED", [])
    monkeypatch.setattr(unconfirmed_close, "_RETAINED", [])
    monkeypatch.setattr(lease_probe, "_OWNED", [])


# --- A client of the real origin -------------------------------------------------


@pytest.fixture
def certificates(tmp_path) -> Path:
    directory = tmp_path / "certificates"
    issue_certificates(directory)
    return directory


@pytest.fixture
def origin(certificates) -> Iterator[SyntheticOrigin]:
    served = SyntheticOrigin(certificates)
    served.start()
    try:
        yield served
    finally:
        served.stop()


def _connect(origin: SyntheticOrigin, certificates: Path) -> ssl.SSLSocket:
    """A TLS connection to the origin that trusts the run's CA in this
    context alone: no trust store is touched."""
    context = ssl.create_default_context(cafile=str(certificates / CA_FILE))
    raw = socket.create_connection(("127.0.0.1", origin.port), timeout=10)
    return context.wrap_socket(raw, server_hostname="www.linkedin.com")


def _send(connection: ssl.SSLSocket, path: str, cookie: str | None = None) -> None:
    lines = [f"GET {path} HTTP/1.1", "Host: www.linkedin.com", "Connection: close"]
    if cookie is not None:
        lines.append(f"Cookie: li_at={cookie}")
    connection.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())


def _answer(connection: ssl.SSLSocket) -> tuple[int, bytes]:
    data = b""
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            break
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), body


def _get(
    origin: SyntheticOrigin, certificates: Path, path: str, cookie: str | None = None
) -> tuple[int, bytes]:
    with _connect(origin, certificates) as connection:
        _send(connection, path, cookie)
        return _answer(connection)


class _Background:
    """One request on its own thread, whose answer is read when it comes."""

    def __init__(self, origin, certificates, path) -> None:
        self.answer: tuple[int, bytes] | None = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run, args=(origin, certificates, path), daemon=True
        )
        self._thread.start()

    def _run(self, origin, certificates, path) -> None:
        try:
            self.answer = _get(origin, certificates, path)
        except BaseException as exc:  # noqa: BLE001 - read by the test
            self.error = exc

    def join(self, seconds: float = 10.0) -> tuple[int, bytes]:
        self._thread.join(seconds)
        assert not self._thread.is_alive(), "the request never came back"
        assert self.error is None, self.error
        assert self.answer is not None
        return self.answer


HELD = person_path("synthetic-held", HELD_SECTION)


# --- The gate -----------------------------------------------------------------


def test_a_released_hold_answers_only_after_the_row_lets_it_go(
    origin, certificates, tmp_path
):
    log = EventLog(tmp_path / "evidence", run="gate")

    def report(kind: str, **fields: Any) -> None:
        log.emit(experiment="K3", row=ROW_H_CAL, actor="origin", kind=kind, **fields)

    gate = origin.hold(HELD, on_event=report)
    connection = _connect(origin, certificates)
    try:
        _send(connection, HELD)
        assert gate.entered.wait(10)
        # Entered, and nothing of the answer has been written yet.
        connection.settimeout(0.3)
        with pytest.raises(TimeoutError):
            connection.recv(1)
        connection.settimeout(10)
        gate.release()
        status, body = _answer(connection)
    finally:
        connection.close()
    assert gate.ended.wait(10)
    assert status == 200 and PERSON_MARKERS[HELD_SECTION].encode() in body
    record = gate.as_record()
    assert record["terminal"] == SERVED and record["wrote"] is True
    assert record["released_by"] == RELEASED_BY_ROW
    assert (
        record["entered_monotonic_ns"]
        <= record["release_requested_monotonic_ns"]
        <= record["released_monotonic_ns"]
    )
    # Both events reached the log, valid as the registry requires.
    events = log.records()
    assert [event["kind"] for event in events] == ["gate.entered", "gate.released"]
    for event in events:
        validate(event)
    assert events[1]["terminal"] == SERVED


def test_a_hold_nobody_releases_ends_at_its_deadline_and_is_still_answered(
    origin, certificates
):
    gate = origin.hold(HELD, seconds=0.3)
    status, body = _get(origin, certificates, HELD)
    assert gate.ended.wait(10)
    record = gate.as_record()
    assert record["terminal"] == DEADLINE
    assert record["released_by"] is None and record["wrote"] is True
    assert status == 200 and PERSON_MARKERS[HELD_SECTION].encode() in body
    held_for = record["released_monotonic_ns"] - record["entered_monotonic_ns"]
    assert held_for >= 300 * MS


def test_a_hold_whose_peer_left_ends_as_peer_gone(origin, certificates):
    gate = origin.hold(HELD, seconds=GATE_DEADLINE_SECONDS)
    connection = _connect(origin, certificates)
    _send(connection, HELD)
    assert gate.entered.wait(10)
    connection.close()
    # Seen while held, long before the deadline.
    assert gate.ended.wait(5)
    record = gate.as_record()
    assert record["terminal"] == PEER_GONE
    assert record["wrote"] is False and record["released_by"] is None


def test_the_gate_holds_only_its_ordinal_of_its_exact_path_and_nothing_else_waits(
    origin, certificates
):
    gate = origin.hold(HELD, ordinal=2)
    # The first request of the path, and a longer path, pass at once.
    assert _get(origin, certificates, HELD)[0] == 200
    assert _get(origin, certificates, HELD + "more/")[0] == 404
    assert not gate.entered.is_set()
    second = _Background(origin, certificates, HELD)
    assert gate.entered.wait(10)
    # Held, and the origin still answers everything else meanwhile.
    started = time.monotonic()
    assert _get(origin, certificates, "/feed/")[0] == 200
    assert time.monotonic() - started < 5
    assert not gate.ended.is_set()
    gate.release()
    assert second.join()[0] == 200
    assert gate.ended.wait(5) and gate.terminal == SERVED


def test_stopping_the_origin_lets_a_held_request_go(certificates):
    served = SyntheticOrigin(certificates)
    served.start()
    gate = served.hold(HELD)
    request = _Background(served, certificates, HELD)
    try:
        assert gate.entered.wait(10)
    finally:
        served.stop()
    assert gate.ended.wait(5)
    assert gate.released_by == RELEASED_BY_TEARDOWN
    assert request.join()[0] == 200


@pytest.mark.parametrize(
    ("options", "message"),
    [
        pytest.param({"seconds": 30.0}, "deadline", id="at-the-limits"),
        pytest.param({"seconds": 0.0}, "deadline", id="no-deadline"),
        pytest.param({"ordinal": 0}, "ordinal", id="no-ordinal"),
    ],
)
def test_a_gate_refuses_a_hold_it_could_not_bound(origin, options, message):
    with pytest.raises(ValueError, match=message):
        origin.hold(HELD, **options)


# --- The person pages ---------------------------------------------------------


def test_the_origin_serves_each_person_section_the_product_navigates(
    origin, certificates
):
    for section, suffix in PERSON_PAGES.items():
        # The suffix the product navigates for that section.
        assert PERSON_SECTIONS[section][0] == suffix
    for username in ("synthetic-one", "synthetic-two"):
        for section, marker in PERSON_MARKERS.items():
            path = person_path(username, section)
            status, body = _get(origin, certificates, path)
            assert status == 200, path
            text = body.decode()
            assert {m for m in PERSON_MARKERS.values() if m in text} == {marker}
            assert "<main>" in text and "rememberme-div" not in text
            title = text.split("<title>", 1)[1].split("</title>", 1)[0].lower()
            assert not any(pattern in title for pattern in _LOGIN_TITLE_PATTERNS)
    # Two usernames are two paths, and nothing else is served.
    assert person_path("synthetic-one", HELD_SECTION) != HELD
    assert _get(origin, certificates, "/in/synthetic-one/details/skills/")[0] == 404
    assert _get(origin, certificates, "/in/Synthetic%20One/")[0] == 404


# --- Call records -------------------------------------------------------------


class _Client:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.sent = asyncio.Event()

    async def call_tool_mcp(self, name, arguments, timeout=None):
        self.sent.set()
        if isinstance(self.answer, BaseException):
            raise self.answer
        if self.answer is None:
            await asyncio.Event().wait()
        return self.answer


async def test_a_cancelled_call_keeps_its_terminal_record_and_stays_cancelled():
    client = _Client(None)
    records: list[dict] = []
    task = asyncio.ensure_future(
        timed_call(cast(Any, client), PERSON_TOOL, {}, records=records)
    )
    await asyncio.wait_for(client.sent.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [record] = records
    assert record["outcome"] == CANCELLED
    assert record["exception"] == "CancelledError"
    assert record["began_monotonic_ns"] <= record["ended_monotonic_ns"]
    assert "nothing_was_sent" not in record


async def test_a_failed_call_keeps_its_record_and_raises():
    records: list[dict] = []
    with pytest.raises(ConnectionError):
        await timed_call(
            cast(Any, _Client(ConnectionError("planted"))),
            PERSON_TOOL,
            {},
            records=records,
        )
    [record] = records
    assert record["outcome"] == RAISED and record["exception"] == "ConnectionError"
    assert record["ended_monotonic_ns"] >= record["began_monotonic_ns"]
    # A client's exception says nothing of whether the request was sent.
    assert "nothing_was_sent" not in record


async def test_an_error_result_keeps_its_status_and_markers_and_no_value():
    secrets = ("cookie-value-1", "generation-2", "instance-3")
    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="unknown")],
        is_error=True,
        structured_content={
            "status": "outcome_unknown",
            "retry_safe": False,
            "message": "in flight",
        },
        _meta={
            REFUSAL_KEY: {"daemon": "retiring", "instance": secrets[2]},
            MARKER_KEY: {
                "reason": "stale",
                "replayable": True,
                "browser_open": False,
                "generation": secrets[1],
            },
            "li_at": secrets[0],
        },
    )
    records: list[dict] = []
    summary = await timed_call(
        cast(Any, _Client(result)), "send_message", {}, records=records
    )
    assert records == [summary]
    assert summary["outcome"] == RETURNED and summary["is_error"] is True
    assert summary["status"] == "outcome_unknown"
    assert summary["retry_safe"] is False
    assert summary["meta"]["refusal"] == "retiring"
    assert summary["meta"]["auth"] == {
        "reason": "stale",
        "replayable": True,
        "browser_open": False,
    }
    assert summary["meta"]["keys"] == sorted([REFUSAL_KEY, MARKER_KEY, "li_at"])
    published = json.dumps(summary)
    assert not any(secret in published for secret in secrets)


def test_a_person_read_names_each_section_carrying_its_own_page():
    sections = {name: f"text {marker}" for name, marker in PERSON_MARKERS.items()}
    # A section whose text came from another page does not count.
    sections["education"] = f"text {PERSON_MARKERS['experience']}"
    result = mcp_types.CallToolResult(
        content=[], structured_content={"url": "u", "sections": sections}
    )
    assert tool_summary(result)["marked_sections"] == ["experience", "main_profile"]


def test_the_declared_first_call_is_what_the_row_judges(profile):  # noqa: F811
    person = InitialCall(PERSON_TOOL, {}, EXPECTED_SECTIONS)
    healthy = _healthy(profile, daemon=False)
    read = {"is_error": False, "read_the_post": False, "text": ""}

    def judged(initial: InitialCall, tool: dict) -> list[str]:
        host = dataclasses.replace(healthy.host, tool=tool)
        return judge_row(dataclasses.replace(healthy, initial=initial, host=host))[1]

    every = {**read, "marked_sections": list(EXPECTED_SECTIONS)}
    assert judged(person, every) == []
    assert judged(person, {**read, "marked_sections": ["main_profile"]}) == [
        person.unmet
    ]
    # The feed read keeps its own failure, word for word.
    assert judged(FEED_READ, every) == [
        f"{harness.READ_TOOL} did not return the synthetic post"
    ]


# --- The host's hook before its corrective stop ----------------------------------


async def test_before_stop_runs_before_the_corrective_stop_despite_the_failure(
    tmp_path,
):
    server = tmp_path / "stand_in_server.py"
    server.write_text(_STAND_IN_SERVER)
    processes: list[Any] = []
    seen: dict[str, Any] = {}

    async def give_up() -> None:
        raise RuntimeError("the row gave up before its quit")

    async def before_stop() -> None:
        # Inside the client's unwinding, which must not cancel this wait.
        await asyncio.sleep(0.05)
        seen["running"] = processes[0].returncode is None

    session = await run_host_session(
        [sys.executable, str(server), "marker", "slow", str(tmp_path / "die")],
        env=dict(os.environ),
        cwd=tmp_path,
        on_stderr=lambda _line: None,
        after_call=give_up,
        on_process=processes.append,
        before_stop=before_stop,
    )
    assert session.error == "RuntimeError: the row gave up before its quit"
    # The hook saw the server as the failure left it; the stop came after.
    assert seen == {"running": True}
    assert session.killed_by_harness is True
    assert session.before_stop_error is None


async def test_a_failing_before_stop_is_recorded_and_the_stop_still_runs(tmp_path):
    server = tmp_path / "stand_in_server.py"
    server.write_text(_STAND_IN_SERVER)

    async def give_up() -> None:
        raise RuntimeError("gave up")

    async def before_stop() -> None:
        raise OSError("planted")

    session = await run_host_session(
        [sys.executable, str(server), "marker", "slow", str(tmp_path / "die")],
        env=dict(os.environ),
        cwd=tmp_path,
        on_stderr=lambda _line: None,
        after_call=give_up,
        before_stop=before_stop,
    )
    assert session.before_stop_error == "OSError: planted"
    assert session.killed_by_harness is True


# --- Row declarations -----------------------------------------------------------


async def _refused(monkeypatch, tmp_path, **row) -> str:
    """Run the row entry and require a refusal before anything is touched."""
    staged = AsyncMock()
    monkeypatch.setattr(harness, "stage_signed_in_session", staged)
    monkeypatch.setattr(
        harness,
        "claim_account",
        lambda _: pytest.fail("the profile was claimed before the refusal"),
    )
    monkeypatch.setattr(
        harness,
        "settlement_problems",
        lambda: pytest.fail("an earlier row was asked about before the refusal"),
    )
    log = EventLog(tmp_path / "evidence", run="refused")
    row.setdefault("daemon", True)
    with pytest.raises(ValueError) as refusal:
        await measure_host_quit_row(
            profile=tmp_path / "auth" / "profile",
            experiment="K3",
            egress=cast(Any, (SimpleNamespace(), SimpleNamespace())),
            log=log,
            work_dir=tmp_path / "row",
            **row,
        )
    staged.assert_not_awaited()
    assert log.records() == []
    assert not (tmp_path / "row").exists()
    return str(refusal.value)


async def test_an_undeclared_row_is_refused_before_anything_is_staged(
    monkeypatch, tmp_path
):
    message = await _refused(monkeypatch, tmp_path, row="H-R99")
    assert "'H-R99' is no declared row" in message


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param({"kill_actor": True}, id="kill-actor"),
        pytest.param({"job_query_shim": object()}, id="job-query-shim"),
        pytest.param(
            {"unconfirmed_close": R7Setup(None, False, 0)}, id="unconfirmed-close"
        ),
    ],
)
async def test_the_calibration_combines_with_no_other_scenario(
    monkeypatch, tmp_path, scenario
):
    message = await _refused(monkeypatch, tmp_path, row=ROW_H_CAL, **scenario)
    assert message.startswith(f"{ROW_H_CAL} combines with no")


async def test_a_recorded_row_without_a_verdict_is_refused(monkeypatch, tmp_path):
    monkeypatch.setitem(harness.ROWS, "H-NEW", RowLifecycle(recorded=True))
    message = await _refused(monkeypatch, tmp_path, row="H-NEW")
    assert "a record with no verdict to judge it" in message


@pytest.mark.parametrize("idle", [0.0, -1.0, math.nan, math.inf, True])
async def test_an_idle_timeout_that_is_no_duration_is_refused(
    monkeypatch, tmp_path, idle
):
    message = await _refused(
        monkeypatch, tmp_path, row=harness.ROW_H_R1, idle_timeout=idle
    )
    assert "idle timeout" in message


def test_a_declaration_names_only_known_ends_and_policies():
    assert lifecycle_problems("H-R1", RowLifecycle()) == []
    problems = lifecycle_problems(
        "H-R1",
        RowLifecycle(termination="lost", preservation="anything", idle_timeout=0.0),
    )
    assert problems == [
        "no termination 'lost'",
        "no preservation policy 'anything'",
        "an idle timeout of 0.0",
    ]
    assert lifecycle_problems(ROW_H_CAL, RowLifecycle()) == [
        "a verdict with no record to judge"
    ]
    # Every row the registry declares holds together, so none is refused late.
    for row, lifecycle in harness.ROWS.items():
        assert lifecycle_problems(row, lifecycle) == [], row


def test_a_row_whose_session_is_the_finding_refuses_the_repairing_session():
    assert preservation_policy_refusals(ORDINARY) == []
    for policy in (MUST_NOT_REPAIR, MUST_REMAIN_CLEARED):
        [reason] = preservation_policy_refusals(policy)
        assert policy in reason and "repair" in reason
    assert preservation_policy_refusals("unheard-of") != []


# --- The calibration's verdict ----------------------------------------------------

_U = CALIBRATION_USERNAME


def _request(path: str, ms: int, *, valid: bool | None = True) -> dict:
    return {
        "host": "www.linkedin.com",
        "path": path,
        "session_valid": valid,
        "t": 1000.0 + ms / 1000,
        "monotonic_ns": ms * MS,
    }


def _valid_record(*, daemon: bool = True) -> dict:
    """One valid H-CAL record: the feed warm-up, then the read, its held page
    entered at 4 s and let go at 4.1 s, and education asked for at 6 s."""
    return {
        "row": ROW_H_CAL,
        "mode": "daemon" if daemon else "direct",
        "platform": "linux",
        "idle_timeout_seconds": CALIBRATION_IDLE_TIMEOUT_SECONDS,
        "k2": dict(K2_NOT_APPLICABLE),
        "observation_problems": [],
        "script_error": None,
        "username": _U,
        "host": {
            "error": None,
            "alive_before_quit": True,
            "stdin_closed": True,
            "exited_on_quit": True,
            "exit_code": 0,
            "killed_by_harness": False,
            "eof_ns": 9_000 * MS,
            "exit_seen_ns": 9_500 * MS,
        },
        "calls": [
            {
                "tool": harness.READ_TOOL,
                "outcome": RETURNED,
                "is_error": False,
                "began_monotonic_ns": 100 * MS,
                "ended_monotonic_ns": 900 * MS,
            },
            {
                "tool": PERSON_TOOL,
                "outcome": RETURNED,
                "is_error": False,
                "marked_sections": list(EXPECTED_SECTIONS),
                "section_errors": [],
                "began_monotonic_ns": 1_000 * MS,
                "ended_monotonic_ns": 8_000 * MS,
            },
        ],
        "gates": [
            {
                "path": person_path(_U, HELD_SECTION),
                "ordinal": 1,
                "entered_monotonic_ns": 4_000 * MS,
                "release_requested_monotonic_ns": 4_010 * MS,
                "released_by": RELEASED_BY_ROW,
                "released_monotonic_ns": 4_100 * MS,
                "terminal": SERVED,
                "wrote": True,
            }
        ],
        "requests": [
            _request("/feed/", 500),
            _request(person_path(_U, "main_profile"), 1_100),
            _request(person_path(_U, HELD_SECTION), 3_990),
            _request(person_path(_U, NEXT_SECTION), 6_000),
        ],
    }


def _gate(record: dict, **changes) -> dict:
    record["gates"][0].update(changes)
    return record


def _person(record: dict, **changes) -> dict:
    record["calls"][1].update(changes)
    return record


@pytest.mark.parametrize("daemon", [True, False])
def test_a_valid_calibration_record_passes(daemon):
    assert calibration_problems(_valid_record(daemon=daemon), daemon=daemon) == []


_BROKEN = [
    pytest.param(
        lambda r: _gate(r, entered_monotonic_ns=None, terminal=None),
        "the held section's request never entered the gate",
        id="never-entered",
    ),
    pytest.param(
        # Released at once, but the handler resumed 25 s later: still served.
        lambda r: _gate(
            r,
            released_monotonic_ns=r["gates"][0]["entered_monotonic_ns"] + 25_000 * MS,
        ),
        "past the gate's 20.0s deadline",
        id="let-go-past-the-deadline",
    ),
    pytest.param(
        lambda r: _gate(r, released_monotonic_ns=None),
        "the hold's entry or end has no time",
        id="hold-end-untimed",
    ),
    pytest.param(
        lambda r: _gate(r, terminal=DEADLINE, released_by=None),
        "the hold ran out its deadline before the row released it",
        id="deadline",
    ),
    pytest.param(
        lambda r: _gate(r, terminal=PEER_GONE, wrote=False),
        "the held request's peer was gone before its answer was written",
        id="peer-gone",
    ),
    pytest.param(
        lambda r: _gate(r, released_by=RELEASED_BY_TEARDOWN),
        "the hold was released by 'teardown', not by the row",
        id="released-by-teardown",
    ),
    pytest.param(
        lambda r: {**r, "gates": []},
        "the record holds no gate on the held section",
        id="no-gate",
    ),
    pytest.param(
        lambda r: {
            **r,
            "requests": r["requests"][:3]
            + [_request(person_path(_U, NEXT_SECTION), 4_050)],
        },
        "the education page is not shown requested after the hold on the "
        "experience page let it go",
        id="education-before-release",
    ),
    pytest.param(
        lambda r: {**r, "requests": r["requests"][:3]},
        "the education page was requested 0 times, not once",
        id="education-never-asked",
    ),
    pytest.param(
        lambda r: {
            **r,
            "requests": r["requests"]
            + [_request(person_path(_U, HELD_SECTION), 7_000)],
        },
        "the experience page was requested 2 times, not once",
        id="held-page-twice",
    ),
    pytest.param(
        lambda r: _person(r, marked_sections=["main_profile", "experience"]),
        "the read did not return the synthetic sections ['education']",
        id="section-missing",
    ),
    pytest.param(
        lambda r: _person(r, outcome=RAISED, is_error=None),
        "the get_person_profile call did not return a result",
        id="call-raised",
    ),
    pytest.param(
        lambda r: {**r, "calls": r["calls"][:1]},
        "the record holds no single get_person_profile call",
        id="no-call",
    ),
    pytest.param(
        lambda r: {**r, "script_error": "RuntimeError: planted"},
        "the row's script failed: RuntimeError: planted",
        id="script-error",
    ),
    pytest.param(
        lambda r: {**r, "idle_timeout_seconds": 20.0},
        "the row ran with an idle timeout of 20.0",
        id="other-idle",
    ),
    pytest.param(
        lambda r: {
            **r,
            "requests": r["requests"][:2]
            + [_request(person_path(_U, HELD_SECTION), 3_990, valid=False)]
            + r["requests"][3:],
        },
        "these pages did not carry the staged session",
        id="unsigned-page",
    ),
    pytest.param(
        lambda r: {**r, "host": {**r["host"], "exit_code": 1}},
        "the host's quit was not a normal EOF exit",
        id="abnormal-quit",
    ),
    pytest.param(
        lambda r: {key: value for key, value in r.items() if key != "host"},
        "the server was not shown alive when the host quit",
        id="no-host",
    ),
]


@pytest.mark.parametrize(("break_it", "expected"), _BROKEN)
def test_a_broken_calibration_record_fails(break_it, expected):
    problems = calibration_problems(break_it(_valid_record()), daemon=True)
    assert any(expected in problem for problem in problems), problems


def test_a_missing_record_fails():
    assert calibration_problems(None, daemon=True) == ["the row kept no record"]


def test_deadline_peer_gone_and_never_entered_are_three_different_findings():
    # Nobody released any of the three, so only how the hold ended differs.
    found = [
        set(
            calibration_problems(
                _gate(_valid_record(), released_by=None, **changes), daemon=True
            )
        )
        for changes in (
            {"terminal": DEADLINE, "wrote": True},
            {"terminal": PEER_GONE, "wrote": False},
            {"entered_monotonic_ns": None, "terminal": None, "wrote": None},
        )
    ]
    assert all(found)
    assert len({frozenset(problems) for problems in found}) == 3


def test_the_repeat_compares_classifications_not_times():
    reference, repeat = _valid_record(), _valid_record()
    for name in ("entered_monotonic_ns", "released_monotonic_ns"):
        repeat["gates"][0][name] += 5 * MS
    assert semantic_differences(reference, repeat, daemon=True) == []
    assert semantic_differences(None, repeat, daemon=True) == [
        "the reference record is not valid: ['the row kept no record']"
    ]
    broken = _person(_valid_record(), marked_sections=["main_profile"])
    [refusal] = semantic_differences(reference, broken, daemon=True)
    assert refusal.startswith("the repeat record is not valid")


# --- The calibration through the row entry ---------------------------------------


class _CalibrationScene:
    """The real row entry in Direct mode: modelled actors, a real origin.

    The host double answers each call by making the requests the product
    would make, as real HTTPS requests to the origin from a thread: the feed
    for the warm-up, and the person pages named in ``pages`` for the read. A
    held one waits at the real gate until the row's script lets it go.
    """

    def __init__(self, monkeypatch, tmp_path, profile, origin, certificates):  # noqa: F811
        directory, staged = profile
        self.tmp_path = tmp_path
        self.origin, self.certificates = origin, certificates
        self.cookie = staged.li_at
        origin.accept_session(staged.li_at)
        self.pages: list[str] = list(EXPECTED_SECTIONS)
        self.fail_read = False
        self.environments: list[str] = []
        self.preservation = AsyncMock(return_value=PostQuit(valid=True))
        self.log = EventLog(tmp_path / "evidence", run="calibration")
        account = harness.ActorAccount(directory)
        scene = self

        def actor_environment(*args, **kwargs):
            env = REAL_ACTOR_ENVIRONMENT(*args, **kwargs)
            scene.environments.append(env[EnvironmentKeys.BROWSER_IDLE_TIMEOUT])
            return env

        monkeypatch.setattr(harness, "claim_account", lambda _: account)
        monkeypatch.setattr(harness, "row_identity", lambda: {})
        monkeypatch.setattr(harness, "evidence_refusal", lambda *a, **k: None)
        monkeypatch.setattr(
            harness, "stage_signed_in_session", AsyncMock(return_value=staged)
        )
        monkeypatch.setattr(
            harness, "resolved_browser_executable", AsyncMock(return_value="/b/c")
        )
        monkeypatch.setattr(harness, "actor_environment", actor_environment)
        monkeypatch.setattr(harness, "Watcher", _Watcher)
        monkeypatch.setattr(harness, "SignalOracle", _Oracle)
        monkeypatch.setattr(harness, "run_host_session", self.host)
        monkeypatch.setattr(
            harness.daemon_descriptor,
            "descriptor_path",
            lambda _root: tmp_path / "no-descriptor.json",
        )
        monkeypatch.setattr(
            harness,
            "retire_daemon_state",
            lambda *a: DaemonCleanup("dir", True, False, True, True),
        )
        monkeypatch.setattr(harness, "observe_preservation", self.preservation)
        monkeypatch.setattr(harness.psutil, "process_iter", lambda *a, **k: [])
        monkeypatch.setattr(
            _Watcher, "summary", {**_SETTLED, "max_roots": {account.browser_key: 1}}
        )
        monkeypatch.setattr(_Watcher, "records", [])

    def _read(self, path: str) -> None:
        status, _ = _get(self.origin, self.certificates, path, self.cookie)
        assert status == 200, path

    async def _call(self, session, name: str, arguments: dict) -> dict:
        record: dict[str, Any] = {
            "tool": name,
            "began": time.time(),
            "began_monotonic_ns": time.monotonic_ns(),
        }
        session.calls.append(record)
        if name == PERSON_TOOL and self.fail_read:
            record.update(
                ended=time.time(),
                ended_monotonic_ns=time.monotonic_ns(),
                outcome=RAISED,
                exception="RuntimeError",
            )
            raise RuntimeError("planted: the read failed")
        if name == PERSON_TOOL:
            username = arguments["linkedin_username"]
            for section in self.pages:
                await asyncio.to_thread(self._read, person_path(username, section))
        else:
            await asyncio.to_thread(self._read, "/feed/")
        record.update(
            ended=time.time(),
            ended_monotonic_ns=time.monotonic_ns(),
            outcome=RETURNED,
            is_error=False,
            read_the_post=name == harness.READ_TOOL,
            marked_sections=list(self.pages) if name == PERSON_TOOL else [],
            section_errors=[],
            text="",
        )
        return record

    async def host(
        self, *args, after_call=None, tool, arguments, row_script=None, **kw
    ):
        session = harness.HostSession(
            alive_before_quit=True, stdin_closed=True, exited_on_quit=True, exit_code=0
        )
        kw["started"](4242)
        session.tool = await self._call(session, tool, arguments)
        if after_call is not None:
            await after_call()
        if row_script is not None:

            async def call(name, arguments):
                summary = await self._call(session, name, arguments)
                session.scripted.append(summary)
                return summary

            try:
                await row_script(call, SimpleNamespace())
            except Exception as exc:  # noqa: BLE001 - as the real session keeps it
                session.script_error = f"{type(exc).__name__}: {exc}"
        session.eof_monotonic_ns = time.monotonic_ns()
        session.exit_seen_monotonic_ns = time.monotonic_ns()
        return session

    async def run(self, **options):
        return await measure_host_quit_row(
            profile=self.tmp_path / "auth" / "profile",
            experiment="K1",
            daemon=False,
            egress=cast(Any, (self.origin, SimpleNamespace(url="x", decisions=[]))),
            log=self.log,
            work_dir=self.tmp_path / "row",
            row=ROW_H_CAL,
            **options,
        )

    def published(self) -> dict:
        return json.loads((self.tmp_path / "row" / "failures.json").read_text())


@pytest.fixture
def calibration(monkeypatch, tmp_path, profile, origin, certificates):  # noqa: F811
    return _CalibrationScene(monkeypatch, tmp_path, profile, origin, certificates)


async def test_a_healthy_calibration_passes_and_publishes_its_record(calibration):
    result = await calibration.run()

    assert result.failures == [], result.failures
    assert calibration.preservation.await_count == 1
    record = calibration.published()["record"]
    assert record["problems"] == []
    [gate] = record["gates"]
    assert gate["terminal"] == SERVED and gate["released_by"] == RELEASED_BY_ROW
    # Read again from the packet, the verdict is the same.
    assert calibration_problems(record, daemon=False) == []
    kinds = [event["kind"] for event in calibration.log.records()]
    assert kinds.count("gate.entered") == 1 and kinds.count("gate.released") == 1
    # The declared idle timeout, in the actors and the record alike.
    assert calibration.environments == [str(CALIBRATION_IDLE_TIMEOUT_SECONDS)]
    assert record["idle_timeout_seconds"] == CALIBRATION_IDLE_TIMEOUT_SECONDS


async def test_a_read_that_never_reaches_the_held_page_fails_the_calibration(
    calibration,
):
    calibration.pages = ["main_profile"]
    result = await calibration.run()

    assert (
        f"{ROW_H_CAL}: the held section's request never entered the gate"
        in result.failures
    ), result.failures


async def test_a_failed_read_fails_the_calibration_and_keeps_its_record(calibration):
    calibration.fail_read = True
    result = await calibration.run()

    assert (
        f"{ROW_H_CAL}: the row's script failed: RuntimeError: planted: the read "
        f"failed" in result.failures
    ), result.failures
    calls = calibration.published()["record"]["calls"]
    assert [call["outcome"] for call in calls] == [RETURNED, RAISED]


async def test_a_row_that_kept_no_observations_fails(calibration, monkeypatch):
    async def nothing(ctx) -> None:
        return None

    monkeypatch.setitem(
        harness.ROWS,
        ROW_H_CAL,
        dataclasses.replace(harness.ROWS[ROW_H_CAL], script=nothing),
    )
    result = await calibration.run()

    for expected in (
        "the record names no username",
        "the record holds no gate on the held section",
        f"the record holds no single {PERSON_TOOL} call",
    ):
        assert f"{ROW_H_CAL}: {expected}" in result.failures, result.failures


async def test_the_teardown_lets_go_what_the_script_left_held(calibration, monkeypatch):
    left = person_path("synthetic-left", HELD_SECTION)
    requests: list[_Background] = []

    async def leaves_it_held(ctx) -> None:
        gate = ctx.hold(left)
        requests.append(_Background(calibration.origin, calibration.certificates, left))
        assert await ctx.entered(gate, 10)

    monkeypatch.setitem(
        harness.ROWS,
        ROW_H_CAL,
        dataclasses.replace(harness.ROWS[ROW_H_CAL], script=leaves_it_held),
    )
    result = await calibration.run()

    assert requests[0].join()[0] == 200
    assert result.record is not None
    [gate] = result.record["gates"]
    assert gate["released_by"] == RELEASED_BY_TEARDOWN
    assert gate["terminal"] == SERVED
    assert result.failures


async def test_a_row_that_must_not_repair_launches_no_preservation(
    calibration, monkeypatch
):
    monkeypatch.setitem(
        harness.ROWS,
        ROW_H_CAL,
        dataclasses.replace(harness.ROWS[ROW_H_CAL], preservation=MUST_NOT_REPAIR),
    )
    result = await calibration.run()

    calibration.preservation.assert_not_awaited()
    refused = f"post-quit not run: the row's preservation policy is {MUST_NOT_REPAIR}"
    assert any(f.startswith(refused) for f in result.failures), result.failures


async def test_an_idle_timeout_given_to_the_row_is_the_one_every_use_reads(
    calibration,
):
    result = await calibration.run(idle_timeout=33.0)

    assert calibration.environments == ["33.0"]
    assert result.record is not None
    assert result.record["idle_timeout_seconds"] == 33.0
    # The calibration's verdict holds the row to its declared value.
    assert any("an idle timeout of 33.0" in f for f in result.failures)
