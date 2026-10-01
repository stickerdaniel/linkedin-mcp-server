"""The owner lost before and after dispatch: the lines read, the declared
responder, the verdicts and the rows' wiring.

No browser. The **lines** the rows look for are produced by the product's
own code: the frontend's preflight against a real declared responder, its
burial of an owner, and the election's probe verdicts. The **responder** is a
real loopback listener met by the product's own preflight and election
probe. The **verdicts** start from an explicit valid record of each row and
change one observation at a time. The **wiring** runs the real row entry on
modelled actors with a host double whose reads are real requests to a real
origin: the row's own script kills, stops and resumes a modelled owner
through the real seams, binds a real responder, and holds a real gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import httpx2
import psutil
import pytest

from differential import harness, owner_loss
from differential.call_loss import (
    EXPECTED_SECTIONS,
    GATE_END_SECONDS,
    INVALID,
    PERSON_TOOL,
)
from differential.harness import RAISED, RETURNED, measure_host_quit_row
from differential.owner_loss import (
    AFTER_EFFECT_CONTROL,
    DIRECT_NO_FAULT,
    FENCE,
    FOLLOW_USERNAME,
    H_R8_CASES,
    K2_NOT_APPLICABLE,
    LATER_USERNAME,
    MESSAGE_TOOL,
    OWNER_LOSS_IDLE_TIMEOUT_SECONDS,
    RECIPIENT,
    RETIRING_ELSEWHERE,
    ROW_H_R9,
    ROW_OWNER_ERROR,
    ROW_RESPONDER_404,
    ROW_RESPONDER_500,
    ROW_UNREACHABLE,
    STOP,
    STOP_SECONDS,
    UNIT_MATRIX,
    UNOBSERVED,
    USERNAMES,
    DeclaredResponder,
    burial_reading,
    comparison_refusals,
    h_r8_problems,
    h_r9_problems,
    invalid_evidence,
    is_dispatch,
    is_preflight,
    problems_for,
    semantic_differences,
    semantics,
    send_category,
)
from differential.retirement_race import (
    DELIVERED,
    FAILED,
    NO_SILENT_CUT,
    UNANSWERED,
    attempts_in,
    call_classification,
)
from differential.synthetic_origin import (
    DEADLINE,
    GATE_DEADLINE_SECONDS,
    PEER_GONE,
    RELEASED_BY_ROW,
    SERVED,
    page_for,
    person_path,
)
from differential.test_call_loss import (  # noqa: F401 - fixtures
    _CalibrationScene,
    certificates,
    origin,
    owned,
)
from differential.test_preservation_gate import (  # noqa: F401 - fixtures
    _Watcher,
    profile,
)
from linkedin_mcp_server.daemon_liveness import CALL_HEADER, HEARTBEAT_PATH

MS = 1_000_000


def _free_port() -> int:
    """A loopback port nothing listens on, as a dead owner's is."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _attachment(tmp_path: Path, port: int) -> Any:
    """An attachment to a same-build owner published on *port*."""
    from linkedin_mcp_server import __version__
    from linkedin_mcp_server.config.schema import AppConfig
    from linkedin_mcp_server.daemon import Attachment
    from linkedin_mcp_server.daemon_descriptor import build, new_instance_id, new_token

    directory = tmp_path / "profile"
    directory.mkdir(exist_ok=True)
    config = AppConfig()
    config.browser.user_data_dir = str(directory)
    token = new_token()
    descriptor = build(
        instance_id=new_instance_id(),
        package_version=__version__,
        runtime_id="test-runtime",
        profile=directory,
        host="127.0.0.1",
        port=port,
        path="/mcp",
        token=token,
        config=config,
        log_path=tmp_path / "owner.log",
    )
    return Attachment(descriptor=descriptor, token=token)


@contextlib.contextmanager
def _frontend_lines(sink: Callable[[str], None]) -> Iterator[None]:
    """Every INFO line the frontend's proxy and election log, as its stderr
    shows them, handed to *sink*."""

    class Forward(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            sink(f"INFO {record.getMessage()}")

    handler = Forward(level=logging.INFO)
    loggers = [
        logging.getLogger("linkedin_mcp_server.daemon_proxy"),
        logging.getLogger("linkedin_mcp_server.daemon_election"),
    ]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    try:
        yield
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.removeHandler(handler)
            logger.setLevel(level)


async def _preflight(attachment: Any) -> Any:
    """The frontend's real preflight against whatever answers *attachment*."""
    from linkedin_mcp_server.daemon_proxy import FrontendCallHeartbeatMiddleware

    return await FrontendCallHeartbeatMiddleware(MagicMock())._preflight(
        attachment, "v1." + "0" * 32
    )


# --- The declared responder, met by the product's own requests ---------------------


@pytest.fixture
def responder_404() -> Iterator[DeclaredResponder]:
    responder = DeclaredResponder("127.0.0.1", _free_port(), 404)
    responder.start()
    try:
        yield responder
    finally:
        responder.stop()


@pytest.mark.parametrize(
    ("status", "classification", "buried"),
    [(404, "route_missing", True), (500, "owner_error", False)],
)
async def test_the_frontends_preflight_classifies_the_responders_answer(
    tmp_path, status, classification, buried
):
    from linkedin_mcp_server.daemon_proxy import DaemonProxyBackend
    from linkedin_mcp_server.config.schema import AppConfig

    responder = DeclaredResponder("127.0.0.1", _free_port(), status)
    responder.start()
    lines: list[str] = []
    try:
        attachment = _attachment(tmp_path, responder.server_address[1])
        with _frontend_lines(lines.append):
            refused = await _preflight(attachment)
            assert refused is not None and refused.nothing_was_sent is True
            backend = DaemonProxyBackend(
                attachment=attachment,
                auth_root=tmp_path,
                profile=tmp_path / "profile",
                config=AppConfig(),
            )
            backend.note_failure(
                attachment.descriptor.instance_id, refused.classification
            )
        requests = responder.requests()
    finally:
        responder.stop()

    attempts = attempts_in(lines)
    assert attempts[0] == {"attempt": "preflight", "classification": classification}
    assert call_classification(attempts) == classification
    # The frontend writes the owner off only for a class that buries, and its
    # line names it; a 500 buries nothing there.
    assert burial_reading(lines)["frontend"] == ([classification] if buried else [])
    [request] = requests
    assert is_preflight(request) and not is_dispatch(request)
    assert request["path"] == HEARTBEAT_PATH and request["authorized"] is True
    # The token went into no record.
    assert attachment.token not in json.dumps(requests)


def test_the_elections_probe_of_the_responder_is_refused_and_no_dispatch(
    tmp_path, responder_404
):
    from linkedin_mcp_server.daemon_election import Reach, _reachable

    attachment = _attachment(tmp_path, responder_404.server_address[1])
    assert _reachable(attachment, 5.0) is Reach.REFUSED
    requests = responder_404.requests()
    # An unbound listing: no call's header, so never counted as a dispatch.
    assert requests and not any(is_dispatch(request) for request in requests)
    assert {request["path"] for request in requests} == {"/mcp"}


def test_a_request_carrying_a_call_off_the_heartbeat_path_is_a_dispatch(
    responder_404,
):
    port = responder_404.server_address[1]
    with httpx2.Client() as client:
        client.post(
            f"http://127.0.0.1:{port}/mcp",
            headers={CALL_HEADER: "v1." + "1" * 32},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call"},
        )
    [request] = responder_404.requests()
    assert is_dispatch(request) and request["rpc"] == "tools/call"


async def test_an_unanswered_preflight_names_no_class(tmp_path):
    # A dead owner's port: nothing listens there.
    lines: list[str] = []
    with _frontend_lines(lines.append):
        refused = await _preflight(_attachment(tmp_path, _free_port()))
    assert refused is not None and refused.classification.value == "unreachable"
    assert call_classification(attempts_in(lines)) == UNANSWERED


def test_the_elections_probe_lines_are_the_ones_the_rows_count(tmp_path):
    from linkedin_mcp_server.config.schema import AppConfig
    from linkedin_mcp_server.daemon import OwnerLookup, OwnerState
    from linkedin_mcp_server.daemon_election import Reach, _live_lookup

    attachment = _attachment(tmp_path, _free_port())
    inspector = SimpleNamespace(
        inspect_until=lambda timeout: OwnerLookup(
            state=OwnerState.ATTACHABLE, attachment=attachment
        )
    )
    lines: list[str] = []
    with _frontend_lines(lines.append):
        for verdict in (Reach.REFUSED, Reach.SILENT):
            _live_lookup(
                tmp_path,
                tmp_path / "profile",
                AppConfig(),
                lambda _a, _t, verdict=verdict: verdict,
                set(),
                inspector=cast(Any, inspector),
                deadline=time.monotonic() + 5,
            )
    reading = burial_reading(lines)
    assert (reading["election_refused"], reading["election_silent"]) == (1, 1)


def test_a_dead_owners_address_bound_again_answers_with_the_declared_status(
    monkeypatch, tmp_path
):
    port = _free_port()
    published = SimpleNamespace(
        pid=42,
        instance_id="first",
        url=f"http://127.0.0.1:{port}/mcp",
        check_endpoint_is_local=lambda: None,
    )
    monkeypatch.setattr(harness.daemon_descriptor, "read", lambda _root: published)
    account = harness.ActorAccount(tmp_path / "profile")
    identified = harness.OwnerIdentity(42, 1.0, "first", "/root", object())

    responder, found = harness.bind_responder(account, identified, 500)
    assert responder is not None
    try:
        assert found["bound"] is True and found["address"] == ["127.0.0.1", port]
        with httpx2.Client() as client:
            answer = client.post(f"http://127.0.0.1:{port}{HEARTBEAT_PATH}")
        assert answer.status_code == 500
        # A port someone holds cannot be taken again: invalid evidence.
        _, again = harness.bind_responder(account, identified, 500)
        assert again["bound"] is False and "Error" in again["error"]
    finally:
        responder.stop()
        responder.stop()

    other = harness.OwnerIdentity(43, 1.0, "second", "/root", object())
    nothing, refused = harness.bind_responder(account, other, 404)
    assert nothing is None and refused["bound"] is False
    assert "does not name the owner the row identified" in refused["error"]


def test_the_declared_values_are_the_products():
    from linkedin_mcp_server.daemon_election import (
        DEFAULT_ELECTION_SECONDS,
        DEFAULT_SETTLEMENT_SECONDS,
    )
    from linkedin_mcp_server.daemon_liveness import HEARTBEAT_SECONDS
    from linkedin_mcp_server.linkedin.identifiers import person_profile_url

    # The stop outlasts a preflight's bound, which outlasts the product's own
    # read timeout; the stop and a read end before the owner could idle out.
    assert HEARTBEAT_SECONDS < owner_loss.PREFLIGHT_BOUND_SECONDS < STOP_SECONDS
    assert STOP_SECONDS + owner_loss.CALL_END_SECONDS > DEFAULT_ELECTION_SECONDS
    assert STOP_SECONDS < OWNER_LOSS_IDLE_TIMEOUT_SECONDS / 2
    assert (
        owner_loss.CALL_END_SECONDS
        >= DEFAULT_ELECTION_SECONDS + DEFAULT_SETTLEMENT_SECONDS
    )
    # The held page is let go by the row inside the gate's deadline.
    assert (
        owner_loss.RELEASE_SECONDS + owner_loss.RELEASE_TOLERANCE_SECONDS
        < GATE_DEADLINE_SECONDS
    )
    # send_message's first navigation is the recipient's profile page, which
    # the origin serves with no Message action; no messaging page is served.
    url = person_profile_url(RECIPIENT, "/")
    assert url.endswith(person_path(RECIPIENT, "main_profile"))
    body, status = page_for(person_path(RECIPIENT, "main_profile"))
    assert status == 200 and b"/messaging" not in body
    assert page_for("/messaging/compose/")[1] == 404


# --- The verdicts ------------------------------------------------------------------


def _request(path: str, ms: int, *, valid: bool | None = True) -> dict:
    return {
        "host": "www.linkedin.com",
        "path": path,
        "session_valid": valid,
        "t": 1000.0 + ms / 1000,
        "monotonic_ns": ms * MS,
    }


def _host() -> dict:
    return {
        "error": None,
        "alive_before_quit": True,
        "stdin_closed": True,
        "exited_on_quit": True,
        "exit_code": 0,
        "killed_by_harness": False,
        "eof_ns": 200_000 * MS,
        "exit_seen_ns": 200_500 * MS,
    }


def _call(tool: str, began: int, ended: int, **fields) -> dict:
    """A call record: monotonic in ms, the wall clock 1000 s at 0 ms."""
    return {
        "tool": tool,
        "began": 1000.0 + began / 1000,
        "ended": 1000.0 + ended / 1000,
        "began_monotonic_ns": began * MS,
        "ended_monotonic_ns": ended * MS,
        "outcome": RETURNED,
        **fields,
    }


def _read(began: int, ended: int, **fields) -> dict:
    ok = {
        "is_error": False,
        "marked_sections": list(EXPECTED_SECTIONS),
        "section_errors": [],
        "status": None,
        "retry_safe": None,
    }
    return _call(PERSON_TOOL, began, ended, **{**ok, **fields})


def _pages(username: str, *at: int) -> list[dict]:
    return [
        _request(person_path(username, section), ms)
        for section, ms in zip(EXPECTED_SECTIONS, at, strict=False)
    ]


def _lifetime(pid: Any, start: Any, *, ppid: int = 1, digest: str = "owner") -> list:
    """One lifetime as ``harness.launch_lifetimes`` records it."""
    return [pid, start, ppid, digest, None, start + 0.1, start + 100.0]


_A = [4242, 999.5, "owner-a"]
_B = [4343, 1002.5, "owner-b"]


def _base(row: str, *, daemon: bool) -> dict:
    record: dict[str, Any] = {
        "row": row,
        "mode": "daemon" if daemon else "direct",
        "platform": "linux",
        "idle_timeout_seconds": OWNER_LOSS_IDLE_TIMEOUT_SECONDS,
        "k2": dict(K2_NOT_APPLICABLE),
        "observation_problems": [],
        "script_error": None,
        "host": _host(),
        "cleanup_began_ns": 300_000 * MS,
        "egress": {"forwarded": ["www.linkedin.com"], "refused": []},
        "calls": [
            _call(harness.READ_TOOL, 100, 900, is_error=False, read_the_post=True)
        ],
        "requests": [_request("/feed/", 500)],
        "owner_processes": [_lifetime(*_A[:2])] if daemon else [],
        "gate_processes": [],
    }
    if daemon:
        record["owner_identified"] = list(_A)
    return record


def _h_r8(row: str, *, daemon: bool = True) -> dict:
    """An H-R8 lane. Kill lanes: the owner killed at 1 s and read gone at
    1.1 s, the read sent at 1.2 s through a successor started at 2 s, its
    pages at 3, 4 and 5 s. The stopped lane: stopped at 1 s, the read sent at
    1.2 s and its preflight seen failing at 3.3 s, resumed at 21 s, the read
    served at 22, 24 and 26 s; the later read at 31 to 38 s."""
    case = H_R8_CASES[row]
    user = USERNAMES[row]
    record = _base(row, daemon=daemon)
    record.update(
        username=user,
        case={"fault": case.fault, "expected": case.expected, "status": case.status},
        retiring=RETIRING_ELSEWHERE,
        unit_matrix=UNIT_MATRIX,
        unobserved=UNOBSERVED[case.fault],
        burial={"claimed": None},
    )
    if not daemon:
        record["fault"] = DIRECT_NO_FAULT
        record["calls"].append(_read(1_200, 9_000))
        record["requests"] += _pages(user, 3_000, 4_000, 5_000)
        record["read_window"] = {"attempts": [], "burial": burial_reading([])}
        if case.fault == STOP:
            record["calls"].append(_read(31_000, 38_000))
            record["requests"] += _pages(LATER_USERNAME, 32_000, 34_000, 36_000)
            record["later_window"] = {"attempts": [], "burial": burial_reading([])}
        return record
    if case.fault == STOP:
        record.update(
            fault={
                "stopped_ns": 1_000 * MS,
                "pid": _A[0],
                "error": None,
                "resumed_ns": 21_000 * MS,
                "resumed_by": "row",
                "resume_error": None,
            },
            preflight_failed_ns=3_300 * MS,
            read_window={
                "attempts": [
                    {"attempt": "preflight", "classification": None},
                    {"attempt": "replay", "classification": None},
                ],
                "burial": {"frontend": [], "election_refused": 0, "election_silent": 2},
            },
            owner_after_read={
                "lifetime": _A[:2],
                "instance_id": _A[2],
                "alive": True,
                "seen_ns": 30_100 * MS,
            },
            later_window={"attempts": [], "burial": burial_reading([])},
            owner_after_later={
                "lifetime": _A[:2],
                "instance_id": _A[2],
                "alive": True,
                "seen_ns": 38_100 * MS,
            },
        )
        record["calls"] += [_read(1_200, 30_000), _read(31_000, 38_000)]
        record["requests"] += _pages(user, 22_000, 24_000, 26_000)
        record["requests"] += _pages(LATER_USERNAME, 32_000, 34_000, 36_000)
        return record
    preflight = {"attempt": "preflight", "classification": None}
    burial: dict[str, Any] = {
        "frontend": [],
        "election_refused": 1,
        "election_silent": 0,
    }
    if case.status is not None:
        preflight["classification"] = case.expected
        if case.status == 404:
            burial["frontend"] = ["route_missing"]
            record["burial"] = {"claimed": "route_missing"}
        record["responder"] = {
            "bound": True,
            "status": case.status,
            "address": ["127.0.0.1", 51234],
            "bound_ns": 1_150 * MS,
            "error": None,
            "requests": [
                {
                    "method": "POST",
                    "path": HEARTBEAT_PATH,
                    "call": True,
                    "authorized": True,
                    "rpc": None,
                    "monotonic_ns": 1_300 * MS,
                },
                {
                    "method": "POST",
                    "path": "/mcp",
                    "call": False,
                    "authorized": True,
                    "rpc": "initialize",
                    "monotonic_ns": 1_400 * MS,
                },
            ],
            "closed_ns": 9_050 * MS,
        }
    record.update(
        prepared={"actor": "owner", "pid": _A[0], "guardian": 777},
        fault={
            "kind": "owner-killed",
            "target": "owner",
            "exit": "killed",
            "monotonic_ns": 1_000 * MS,
            "seen_ns": 1_050 * MS,
            "seen": 1001.05,
        },
        owner_after_fault={
            "lifetime": _A[:2],
            "instance_id": _A[2],
            "alive": False,
            "seen_ns": 1_100 * MS,
        },
        read_window={
            "attempts": [
                preflight,
                {"attempt": "election", "classification": "attached"},
                {"attempt": "replay", "classification": None},
            ],
            "burial": burial,
        },
        owner_after_read={
            "lifetime": _B[:2],
            "instance_id": _B[2],
            "alive": True,
            "seen_ns": 9_100 * MS,
        },
    )
    record["calls"].append(_read(1_200, 9_000))
    record["requests"] += _pages(user, 3_000, 4_000, 5_000)
    record["owner_processes"].append(_lifetime(*_B[:2]))
    return record


def _h_r9(*, daemon: bool = True) -> dict:
    """H-R9. The message sent at 1.2 s; its recipient's page entered the gate
    at 2 s and the actor killed at 3 s; the release asked for at 17 s. K3: the
    call answered unknown at 25 s through the election of a successor started
    at 10 s, and the following read 28.5-36 s. K1: the call raised at 3.1 s,
    the profile settled by 5 s, and a fresh host read."""
    record = _base(ROW_H_R9, daemon=daemon)
    held = person_path(RECIPIENT, "main_profile")
    record.update(
        username=RECIPIENT,
        tool=MESSAGE_TOOL,
        held={"path": held, "ordinal": 1, "deadline_seconds": GATE_DEADLINE_SECONDS},
        after_effect_control=AFTER_EFFECT_CONTROL,
        fence=FENCE,
        prepared={"actor": "owner" if daemon else "frontend", "pid": 4242},
        gates=[
            {
                "path": held,
                "ordinal": 1,
                "deadline_seconds": GATE_DEADLINE_SECONDS,
                "entered_monotonic_ns": 2_000 * MS,
                "release_requested_monotonic_ns": 17_000 * MS,
                "released_by": RELEASED_BY_ROW,
                "released_monotonic_ns": 17_005 * MS,
                "terminal": PEER_GONE,
                "wrote": False,
            }
        ],
        release={"scheduled_ns": 17_000 * MS, "requested_ns": 17_000 * MS},
        fault={
            "kind": "owner-killed" if daemon else "server-killed",
            "target": "owner" if daemon else "frontend",
            "exit": "killed",
            "monotonic_ns": 3_000 * MS,
            "seen_ns": 3_050 * MS,
            "seen": 1003.05,
        },
        call_open=False,
        watched_until_ns=27_500 * MS,
    )
    record["requests"].append(_request(held, 1_990))
    if daemon:
        record["calls"].append(
            _call(
                MESSAGE_TOOL,
                1_200,
                25_000,
                is_error=True,
                status="outcome_unknown",
                retry_safe=False,
            )
        )
        record.update(
            send_window={
                "attempts": [{"attempt": "election", "classification": "attached"}],
                "unknown_lines": 1,
            },
            owner_after_call={
                "lifetime": [4343, 1010.0],
                "instance_id": "owner-b",
                "alive": True,
                "seen_ns": 28_000 * MS,
            },
            follow_window={"attempts": [], "burial": burial_reading([])},
            owner_after_read={
                "lifetime": [4343, 1010.0],
                "instance_id": "owner-b",
                "alive": True,
                "seen_ns": 36_100 * MS,
            },
        )
        record["calls"].append(_read(28_500, 36_000))
        record["requests"] += _pages(FOLLOW_USERNAME, 29_000, 31_000, 33_000)
        record["owner_processes"].append(_lifetime(4343, 1010.0))
    else:
        record["calls"].append(
            _call(MESSAGE_TOOL, 1_200, 3_100, outcome=RAISED, exception="McpError")
        )
        record.update(
            send_window={"attempts": [], "unknown_lines": 0},
            settlement={
                "guardian_exit": "exited",
                "waited": [],
                "remaining": [],
                "unresolved": [],
                "lease": "free",
                "seen_ns": 5_000 * MS,
            },
            fresh={
                "made": True,
                "call": {"outcome": RETURNED, "is_error": False, "read_the_post": True},
                "forwarded": False,
                "quit_problems": [],
            },
        )
        record["host"] = {**_host(), "exit_code": -9, "alive_before_quit": False}
    return record


_VALID = [
    *[
        pytest.param(lambda row=row, d=d: _h_r8(row, daemon=d), d, id=f"{row}-{m}")
        for row in H_R8_CASES
        for d, m in ((True, "daemon"), (False, "direct"))
    ],
    pytest.param(_h_r9, True, id="H-R9-daemon"),
    pytest.param(lambda: _h_r9(daemon=False), False, id="H-R9-direct"),
]


@pytest.mark.parametrize(("build", "daemon"), _VALID)
def test_a_valid_record_passes(build, daemon):
    assert problems_for(build(), daemon=daemon) == []


def _read_of(record: dict, index: int = 1) -> dict:
    return record["calls"][index]


def _move_pages(record: dict, username: str, *at: int) -> None:
    record["requests"] = [
        r for r in record["requests"] if not r["path"].startswith(f"/in/{username}/")
    ] + _pages(username, *at)


def _dispatch(record: dict) -> None:
    record["responder"]["requests"].append(
        {
            "method": "POST",
            "path": "/mcp",
            "call": True,
            "authorized": True,
            "rpc": "initialize",
            "monotonic_ns": 1_500 * MS,
        }
    )


_UNREACH, _R404, _R500 = ROW_UNREACHABLE, ROW_RESPONDER_404, ROW_RESPONDER_500

#: (builder, daemon, change, the problem it must bring, whether invalid).
_CONTROLS = [
    # A tool dispatch to the failed instance fails.
    pytest.param(
        lambda: _h_r8(_R404),
        True,
        _dispatch,
        "a tool dispatch reached the failed instance",
        False,
        id="responder-dispatch-reached-it",
    ),
    pytest.param(
        lambda: _h_r8(_R500),
        True,
        _dispatch,
        "a tool dispatch reached the failed instance",
        False,
        id="responder-500-dispatch-reached-it",
    ),
    pytest.param(
        lambda: _h_r8(_UNREACH),
        True,
        lambda r: _move_pages(r, USERNAMES[_UNREACH], 1_300, 4_000, 5_000),
        "pages of the read are not shown after the successor's launch",
        False,
        id="unreachable-page-before-the-successor",
    ),
    pytest.param(
        lambda: _h_r8(_UNREACH),
        True,
        lambda r: r["read_window"]["attempts"].insert(
            0, {"attempt": "dispatch", "classification": "retiring"}
        ),
        "a dispatch reached it",
        False,
        id="unreachable-the-failed-owner-refused-the-call",
    ),
    # A successor not verified fails.
    pytest.param(
        lambda: _h_r8(_UNREACH),
        True,
        lambda r: r["owner_after_read"].update(lifetime=_A[:2], instance_id=_A[2]),
        "answered by the lost owner",
        False,
        id="unreachable-answered-by-the-lost-owner",
    ),
    pytest.param(
        lambda: _h_r8(_UNREACH),
        True,
        lambda r: r["owner_processes"].pop(),
        "not an owner the row was seen to launch",
        False,
        id="unreachable-successor-not-launched",
    ),
    pytest.param(
        lambda: _h_r8(_UNREACH),
        True,
        lambda r: (
            r["owner_processes"][1].__setitem__(1, 990.0),
            r["owner_after_read"].update(lifetime=[_B[0], 990.0]),
        ),
        "the successor was not started inside the window",
        False,
        id="unreachable-successor-from-before-the-call",
    ),
    pytest.param(
        lambda: _h_r8(_UNREACH),
        True,
        lambda r: r["read_window"]["attempts"].insert(
            0, r["read_window"]["attempts"].pop(1)
        ),
        "the frontend reported no failed preflight first",
        False,
        id="unreachable-election-before-the-preflight",
    ),
    pytest.param(
        lambda: _h_r8(_UNREACH),
        True,
        lambda r: r["calls"][0].update(outcome=RAISED, is_error=None),
        "the warm-up read is not recorded as returned",
        True,
        id="unreachable-warm-up-failed",
    ),
    pytest.param(
        lambda: _h_r8(_UNREACH),
        True,
        lambda r: r["owner_processes"].append(_lifetime(4545, 1003.0)),
        "the row launched other owners besides the successor",
        False,
        id="unreachable-two-successors",
    ),
    pytest.param(
        lambda: _h_r8(_UNREACH),
        True,
        lambda r: r["read_window"]["attempts"].pop(),
        "no replay line",
        False,
        id="unreachable-no-replay",
    ),
    pytest.param(
        lambda: _h_r8(_UNREACH),
        True,
        lambda r: _read_of(r).update(outcome=RAISED, is_error=None),
        "the read did not succeed on a successor",
        False,
        id="unreachable-read-failed",
    ),
    pytest.param(
        lambda: _h_r8(_R404),
        True,
        lambda r: r["read_window"]["attempts"][0].update(classification="owner_error"),
        "not 'route_missing'",
        False,
        id="responder-wrong-classification",
    ),
    # Claiming a burial with no line to show it fails.
    pytest.param(
        lambda: _h_r8(_R500),
        True,
        lambda r: r["burial"].update(claimed="owner_error"),
        "which no frontend line shows",
        False,
        id="responder-burial-claimed-unobserved",
    ),
    # Invalid evidence, never a finding.
    pytest.param(
        lambda: _h_r8(_UNREACH),
        True,
        lambda r: r["fault"].update(monotonic_ns=1_250 * MS),
        "the read was not sent after the owner was killed",
        True,
        id="unreachable-killed-after-the-send",
    ),
    pytest.param(
        lambda: _h_r8(_UNREACH),
        True,
        lambda r: r["fault"].update(exit="still running after the kill"),
        "not shown killed before the call",
        True,
        id="unreachable-kill-failed",
    ),
    pytest.param(
        lambda: _h_r8(_UNREACH),
        True,
        lambda r: r["owner_after_fault"].update(alive=True),
        "not shown gone before the call",
        True,
        id="unreachable-owner-still-alive",
    ),
    pytest.param(
        lambda: _h_r8(_UNREACH),
        True,
        lambda r: r["owner_after_read"].update(seen_ns=400_000 * MS),
        "after the harness's cleanup began",
        True,
        id="unreachable-read-after-cleanup",
    ),
    pytest.param(
        lambda: _h_r8(_R404),
        True,
        lambda r: r["responder"].update(bound=False, error="OSError: in use"),
        "was not bound again",
        True,
        id="responder-port-not-bound-again",
    ),
    pytest.param(
        lambda: _h_r8(_R404),
        True,
        lambda r: r["responder"]["requests"].pop(0),
        "the frontend never met the responder",
        True,
        id="responder-never-met",
    ),
    pytest.param(
        lambda: _h_r8(_R404),
        True,
        lambda r: r["responder"].update(status=500),
        "not the lane's 404",
        True,
        id="responder-wrong-status",
    ),
    # The stopped owner.
    pytest.param(
        lambda: _h_r8(ROW_OWNER_ERROR),
        True,
        lambda r: r["fault"].update(resumed_ns=None, resumed_by=None),
        "the stopped owner was not resumed by the row",
        True,
        id="stopped-never-resumed",
    ),
    pytest.param(
        lambda: _h_r8(ROW_OWNER_ERROR),
        True,
        lambda r: r["fault"].update(resumed_by="teardown"),
        "the stopped owner was not resumed by the row",
        True,
        id="stopped-resumed-only-by-the-teardown",
    ),
    pytest.param(
        lambda: _h_r8(ROW_OWNER_ERROR),
        True,
        lambda r: r.update(left_stopped=True),
        "the row left its owner stopped",
        True,
        id="stopped-left-stopped",
    ),
    pytest.param(
        lambda: _h_r8(ROW_OWNER_ERROR),
        True,
        lambda r: r["fault"].update(resumed_ns=5_000 * MS),
        "before its declared",
        True,
        id="stopped-resumed-early",
    ),
    pytest.param(
        lambda: _h_r8(ROW_OWNER_ERROR),
        True,
        lambda r: r.update(platform="win32"),
        "Windows has no portable stop",
        True,
        id="stopped-on-windows",
    ),
    pytest.param(
        lambda: _h_r8(ROW_OWNER_ERROR),
        True,
        lambda r: _move_pages(r, USERNAMES[ROW_OWNER_ERROR], 15_000, 24_000, 26_000),
        "a tool ran on the stopped owner",
        False,
        id="stopped-ran-a-tool",
    ),
    pytest.param(
        lambda: _h_r8(ROW_OWNER_ERROR),
        True,
        lambda r: r.update(preflight_failed_ns=None),
        "preflight is not shown to fail",
        False,
        id="stopped-no-bounded-preflight",
    ),
    pytest.param(
        lambda: _h_r8(ROW_OWNER_ERROR),
        True,
        lambda r: r.update(preflight_failed_ns=15_000 * MS),
        "preflight is not shown to fail",
        False,
        id="stopped-preflight-too-late",
    ),
    pytest.param(
        lambda: _h_r8(ROW_OWNER_ERROR),
        True,
        lambda r: _read_of(r).update(outcome="cancelled", is_error=None),
        "cut silently",
        False,
        id="stopped-read-cut-silently",
    ),
    pytest.param(
        lambda: _h_r8(ROW_OWNER_ERROR),
        True,
        lambda r: _read_of(r, 2).update(is_error=True),
        "the later read did not succeed",
        False,
        id="stopped-later-read-failed",
    ),
    pytest.param(
        lambda: _h_r8(ROW_OWNER_ERROR),
        True,
        lambda r: r["owner_processes"].append(_lifetime(4545, 1010.0)),
        "the stopped owner is not shown kept",
        False,
        id="stopped-owner-replaced",
    ),
    # Direct names no owner fault.
    pytest.param(
        lambda: _h_r8(_UNREACH, daemon=False),
        False,
        lambda r: r.update(fault={"kind": "owner-killed"}),
        "a Direct record names an owner fault",
        False,
        id="direct-names-a-fault",
    ),
    # H-R9, K3.
    pytest.param(
        _h_r9,
        True,
        lambda r: r["requests"].append(
            _request(person_path(RECIPIENT, "main_profile"), 26_000)
        ),
        "the mutating call was replayed",
        False,
        id="message-recipient-navigated-again",
    ),
    pytest.param(
        _h_r9,
        True,
        lambda r: _read_of(r).update(is_error=False, status=None, retry_safe=None),
        "reported as a success",
        False,
        id="message-reported-as-success",
    ),
    pytest.param(
        _h_r9,
        True,
        lambda r: _read_of(r).update(retry_safe=True),
        "retry_safe true",
        False,
        id="message-retry-safe",
    ),
    pytest.param(
        _h_r9,
        True,
        lambda r: r["send_window"]["attempts"].append(
            {"attempt": "replay", "classification": None}
        ),
        "the frontend replayed the mutating call",
        False,
        id="message-replay-line",
    ),
    pytest.param(
        _h_r9,
        True,
        lambda r: r["requests"].append(_request("/messaging/compose/", 4_000)),
        "the call went on toward the composer",
        False,
        id="message-went-on-to-the-composer",
    ),
    pytest.param(
        _h_r9,
        True,
        lambda r: r["egress"].update(forwarded=["www.linkedin.com", "example.com"]),
        "outside the synthetic origin",
        False,
        id="message-left-the-fence",
    ),
    pytest.param(
        _h_r9,
        True,
        lambda r: r["owner_processes"].pop(),
        "not an owner the row was seen to launch",
        False,
        id="message-successor-not-verified",
    ),
    pytest.param(
        _h_r9,
        True,
        lambda r: _read_of(r, 2).update(outcome=RAISED, is_error=None),
        "the read after the lost call did not succeed",
        False,
        id="message-no-read-afterwards",
    ),
    # A loss before the gate entry is invalid.
    pytest.param(
        _h_r9,
        True,
        lambda r: r["fault"].update(monotonic_ns=1_500 * MS),
        "the loss came before the recipient's page entered the gate",
        True,
        id="message-killed-before-the-entry",
    ),
    pytest.param(
        _h_r9,
        True,
        lambda r: r["gates"][0].update(entered_monotonic_ns=None),
        "nothing was lost after dispatch",
        True,
        id="message-never-entered",
    ),
    pytest.param(
        _h_r9,
        True,
        lambda r: r["gates"][0].update(terminal=DEADLINE),
        "ran out its deadline",
        True,
        id="message-hold-deadline",
    ),
    pytest.param(
        _h_r9,
        True,
        lambda r: r["gates"][0].update(released_monotonic_ns=2_500 * MS),
        "the hold had ended before the kill",
        True,
        id="message-hold-ended-first",
    ),
    pytest.param(
        _h_r9,
        True,
        lambda r: r["gates"][0].update(release_requested_monotonic_ns=20_000 * MS),
        "after its declared time",
        True,
        id="message-release-late",
    ),
    pytest.param(
        _h_r9,
        True,
        lambda r: r["gates"][0].update(released_by="teardown"),
        "the hold was released by 'teardown'",
        True,
        id="message-released-by-the-teardown",
    ),
    pytest.param(
        _h_r9,
        True,
        lambda r: r.update(cleanup_began_ns=25_000 * MS),
        "the origin was watched for less than",
        True,
        id="message-watched-too-briefly",
    ),
    # H-R9, K1.
    pytest.param(
        lambda: _h_r9(daemon=False),
        False,
        lambda r: _read_of(r).update(outcome=RETURNED, is_error=False),
        "is not shown failed to its caller: success",
        False,
        id="message-direct-success",
    ),
    pytest.param(
        lambda: _h_r9(daemon=False),
        False,
        lambda r: r["settlement"].update(lease="held"),
        "the profile lease was 'held' after the kill",
        False,
        id="message-direct-lease-held",
    ),
    pytest.param(
        lambda: _h_r9(daemon=False),
        False,
        lambda r: r["settlement"].update(guardian_exit=None),
        "guardian is not shown to drain and exit",
        False,
        id="message-direct-guardian-stayed",
    ),
    pytest.param(
        lambda: _h_r9(daemon=False),
        False,
        lambda r: r.update(fresh={"made": False, "why": "unsettled"}),
        "no read was made after the kill",
        False,
        id="message-direct-no-fresh-read",
    ),
    pytest.param(
        lambda: _h_r9(daemon=False),
        False,
        lambda r: r["settlement"].update(seen_ns=2_000 * MS),
        "the settlement is not shown read after the fault",
        True,
        id="message-direct-settlement-before-the-kill",
    ),
]


@pytest.mark.parametrize(
    ("build", "daemon", "change", "expected", "invalid"), _CONTROLS
)
def test_one_changed_observation_fails_the_record(
    build, daemon, change, expected, invalid
):
    record = build()
    change(record)
    problems = problems_for(record, daemon=daemon)
    matching = [problem for problem in problems if expected in problem]
    assert matching, problems
    # Invalid evidence is named as such, and a finding never is.
    assert all(p.startswith(INVALID) is invalid for p in matching), matching


def test_a_stopped_owners_read_may_fail_explicitly_but_never_run_on_it():
    record = _h_r8(ROW_OWNER_ERROR)
    _read_of(record).update(outcome=RAISED, is_error=None, marked_sections=[])
    _move_pages(record, USERNAMES[ROW_OWNER_ERROR])
    assert problems_for(record, daemon=True) == []
    assert semantics(record)["read"] == NO_SILENT_CUT
    assert semantics(_h_r8(ROW_OWNER_ERROR))["read"] == NO_SILENT_CUT


def test_a_404_burial_is_claimed_only_from_its_own_line():
    record = _h_r8(_R404)
    assert semantics(record)["burial"] == "route_missing"
    # Not claimed, a burial seen is simply not credited; claimed, it must be seen.
    record["burial"]["claimed"] = None
    assert problems_for(record, daemon=True) == []
    record["burial"]["claimed"] = "route_missing"
    record["read_window"]["burial"]["frontend"] = []
    assert any("no frontend line shows" in p for p in problems_for(record, daemon=True))


def test_the_message_categories_are_told_apart():
    assert send_category(_read_of(_h_r9())) == "outcome_unknown"
    assert send_category(_read_of(_h_r9(daemon=False))) == FAILED
    assert send_category({"outcome": RETURNED, "is_error": False}) == "success"
    assert send_category({"outcome": "cancelled"}) not in (FAILED, "success")


def test_invalid_evidence_is_told_apart_from_a_finding():
    record = _h_r8(_R404)
    record["responder"]["bound_ns"] = 9_999_999 * MS
    _dispatch(record)
    problems = problems_for(record, daemon=True)
    assert [p for p in problems if p not in invalid_evidence(problems)] == [
        "a tool dispatch reached the failed instance: 1 request(s) carrying the "
        "call off the heartbeat path"
    ]
    assert len(invalid_evidence(problems)) == 1


def test_a_record_that_is_missing_or_for_another_row_fails():
    assert h_r8_problems(None, daemon=True) == ["the row kept no record"]
    assert h_r9_problems(None, daemon=True) == ["the row kept no record"]
    assert h_r8_problems({"row": "H-CAL"}, daemon=True) == [
        "the record is for row 'H-CAL', which loses no owner before dispatch"
    ]
    assert h_r9_problems({"row": "H-CAL"}, daemon=True) == [
        "the record is for row 'H-CAL', which sends no message"
    ]
    record = _h_r8(_UNREACH)
    record["idle_timeout_seconds"] = 20.0
    assert any(
        "an idle timeout of 20.0" in p for p in problems_for(record, daemon=True)
    )
    for name in ("retiring", "unit_matrix", "unobserved", "k2"):
        record = _h_r8(_UNREACH)
        record[name] = None
        assert problems_for(record, daemon=True), name
    for name in ("after_effect_control", "fence"):
        record = _h_r9()
        record[name] = None
        assert problems_for(record, daemon=True), name


def test_the_repeat_compares_classifications_and_refuses_an_invalid_record():
    for row in H_R8_CASES:
        assert semantic_differences(_h_r8(row), _h_r8(row)) == [], row
    assert semantic_differences(_h_r9(), _h_r9()) == []
    # The stopped owner's read either way is no difference.
    failed = _h_r8(ROW_OWNER_ERROR)
    _read_of(failed).update(outcome=RAISED, is_error=None)
    _move_pages(failed, USERNAMES[ROW_OWNER_ERROR])
    assert semantic_differences(_h_r8(ROW_OWNER_ERROR), failed) == []
    assert semantics(_h_r8(_UNREACH))["read"] == DELIVERED
    broken = _h_r8(_R404)
    _dispatch(broken)
    [refusal] = semantic_differences(_h_r8(_R404), broken)
    assert refusal.startswith("the repeat record is not valid")


def test_the_candidate_is_held_to_direct_only_from_two_valid_records():
    for row in H_R8_CASES:
        assert comparison_refusals(_h_r8(row, daemon=False), _h_r8(row)) == [], row
    assert comparison_refusals(_h_r9(daemon=False), _h_r9()) == []
    [refusal] = comparison_refusals(None, _h_r9())
    assert refusal.startswith("the Direct record is not valid")


def test_windows_counts_a_venv_launcher_and_its_gate_as_one_owner_launch():
    """The shape measured on windows-latest: each owner start a venv launcher
    of the gate, the interpreter it ran, and the owner the gate started
    through its own launcher. Two starts, one successor."""
    record = _h_r8(_UNREACH)
    record["platform"] = "win32"
    record["gate_processes"] = [
        _lifetime(7256, 999.3, digest="gate"),
        _lifetime(7884, 999.31, ppid=7256, digest="gate"),
        _lifetime(8256, 1001.9, digest="gate"),
        _lifetime(8884, 1001.91, ppid=8256, digest="gate"),
    ]
    record["owner_processes"] = [
        _lifetime(2524, 999.49, ppid=7884),
        _lifetime(_A[0], _A[1], ppid=2524),
        _lifetime(3524, 1001.95, ppid=8884),
        _lifetime(_B[0], _B[1], ppid=3524),
    ]
    assert problems_for(record, daemon=True) == []
    record["gate_processes"].append(_lifetime(9000, 1004.0, digest="gate-3"))
    assert any(
        "the row launched other owners besides the successor" in p
        for p in problems_for(record, daemon=True)
    )


def test_every_row_here_is_declared_with_its_verdict_and_seams():
    for row, case in H_R8_CASES.items():
        lifecycle = harness.ROWS[row]
        assert lifecycle.idle_timeout == OWNER_LOSS_IDLE_TIMEOUT_SECONDS
        assert lifecycle.owner_loss and lifecycle.successor
        assert lifecycle.traced is case.kills
        assert harness.ROW_VERDICTS[row] is h_r8_problems
    lifecycle = harness.ROWS[ROW_H_R9]
    assert lifecycle.owner_loss and lifecycle.traced and lifecycle.successor
    assert harness.ROW_VERDICTS[ROW_H_R9] is h_r9_problems


def test_an_owner_loss_is_declared_only_with_its_script_and_alone():
    assert harness.lifecycle_problems(
        "H-NEW", harness.RowLifecycle(owner_loss=True, traced=True)
    ) == [
        "an owner loss with no script to make it",
        "an owner loss that combines with other scenarios",
    ]
    assert harness.lifecycle_problems("H-NEW", harness.RowLifecycle(traced=True)) == [
        "a trace with no owner-loss seams to attach it"
    ]


# --- The rows through the row entry -------------------------------------------------


class _Owner:
    """An owner's process as the row holds it: stopped and continued, or
    killed, only through the row's seams; gone once ``left`` says so."""

    def __init__(self, pid: int, left: Callable[[], bool]) -> None:
        self.pid = pid
        self.left = left
        self.kills = 0
        self.stops = 0
        self.resumes = 0
        self.stopped = False

    def status(self) -> str:
        if self.kills or self.left():
            raise psutil.NoSuchProcess(self.pid)
        return psutil.STATUS_STOPPED if self.stopped else psutil.STATUS_RUNNING

    def is_running(self) -> bool:
        return not (self.kills or self.left())

    def wait(self, timeout=None) -> None:
        if self.is_running():
            raise psutil.TimeoutExpired(timeout or 0, self.pid)

    def kill(self) -> None:
        self.kills += 1

    def suspend(self) -> None:
        self.stops += 1
        self.stopped = True

    def resume(self) -> None:
        self.resumes += 1
        self.stopped = False


def _owner_record(pid: int, start: float) -> dict:
    """The watcher's record of an owner the row started."""
    return {
        "t": time.time(),
        "kind": "process.start",
        "actor": "owner",
        "in_row": True,
        "pid": pid,
        "ppid": 1,
        "start_identity": start,
        "cmdline": ["python", "-m", harness.OWNER_MODULE],
    }


class _OwnerLossScene(_CalibrationScene):
    """The real row entry on modelled actors, for a row that loses its owner.

    The host double answers as the product's frontend does: a call that meets
    a dead or stopped owner reports its failed preflight (against a declared
    responder, the real preflight's own line), an election that starts a
    successor, and a replay that reads through it; a message lost mid-call is
    answered with an unknown outcome. Its reads are real requests to a real
    origin, the message's first one held at a real gate. Direct's server is
    tied and killed through the real seams, and a fresh host reads.
    """

    def __init__(self, monkeypatch, tmp_path, profile, origin, certificates):  # noqa: F811
        super().__init__(monkeypatch, tmp_path, profile, origin, certificates)
        self.daemon = True
        self.row = ROW_UNREACHABLE
        self.left = False
        self.hosts = 0
        self.background: list[asyncio.Future[Any]] = []
        self.on_stderr: Callable[[str], None] = lambda _line: None
        self.successor_start: float | None = None
        #: Planted faults: a page of the read before the successor started, a
        #: dispatch sent to the responder, a page read while stopped, the
        #: recipient's page asked for again, a lost message reported as sent.
        self.reads_before_successor = False
        self.dispatches_to_responder = False
        self.runs_while_stopped = False
        self.replays_the_message = False
        self.reports_success = False
        self.port = _free_port()
        self.owner = _Owner(42, lambda: self.left)
        self.server = _Owner(4242, lambda: False)
        for name, value in (
            ("STOP_SECONDS", 0.5),
            ("CALL_END_SECONDS", 10.0),
            ("RELEASE_SECONDS", 0.5),
            ("RELEASE_TOLERANCE_SECONDS", 1.0),
            ("CONTINUATION_SECONDS", 0.3),
        ):
            monkeypatch.setattr(owner_loss, name, value)
        monkeypatch.setattr(
            harness,
            "read_lock",
            lambda _path: {"now": None, "answer": {"state": "free"}},
        )
        monkeypatch.setattr(
            harness, "lifetime_exit_state", lambda observed, pid, seconds: "exited"
        )
        monkeypatch.setattr(
            harness,
            "wait_for_guardian",
            lambda observed, pid: None if self.daemon else (777, 0),
        )
        monkeypatch.setattr(
            harness,
            "associate_server",
            lambda pid, observed, **kw: (self.server, 1000.0),
        )
        self._published(monkeypatch)

    def _published(self, monkeypatch) -> None:
        scene = self
        published = self.tmp_path / "descriptor.json"
        log = self.tmp_path / "daemon.log"
        log.write_text("INFO the owner is serving\n")
        auth_root = str(harness.claim_account(self.tmp_path).auth_root)

        def descriptor_path(_root):
            return published if scene.daemon else self.tmp_path / "none.json"

        def read(_root):
            pid, instance = (
                (43, "second") if scene.successor_start is not None else (42, "first")
            )
            return SimpleNamespace(
                pid=pid,
                instance_id=instance,
                protocol_version=2,
                log_path=str(log),
                url=f"http://127.0.0.1:{scene.port}/mcp",
                check_endpoint_is_local=lambda: None,
            )

        def identify(*_a, **_k):
            if scene.successor_start is not None:
                return harness.OwnerIdentity(
                    43,
                    scene.successor_start,
                    "second",
                    auth_root,
                    _Owner(43, lambda: scene.left),
                ), None
            return harness.OwnerIdentity(
                42, 42.0, "first", auth_root, scene.owner
            ), None

        published.write_text("{}")
        monkeypatch.setattr(
            harness.daemon_descriptor, "descriptor_path", descriptor_path
        )
        monkeypatch.setattr(harness.daemon_descriptor, "read", read)
        monkeypatch.setattr(harness, "identify_owner", identify)
        self.records: list[dict] = [_owner_record(42, 42.0)]
        monkeypatch.setattr(_Watcher, "records", self.records)

    def _line(self, line: str) -> None:
        self.on_stderr(f"INFO {line}")

    def _elect(self) -> None:
        self._line("The published daemon is not answering; electing a new one")
        self.successor_start = time.time()
        self.records.append(_owner_record(43, self.successor_start))
        self._line("Attached to a replacement shared browser owner")

    def _record(self, session, name: str) -> dict:
        record: dict[str, Any] = {
            "tool": name,
            "began": time.time(),
            "began_monotonic_ns": time.monotonic_ns(),
        }
        session.calls.append(record)
        return record

    async def _pages_of(self, username: str) -> None:
        for section in self.pages:
            await asyncio.to_thread(self._read, person_path(username, section))

    def _returned(self, record: dict, **fields) -> dict:
        record.update(
            {
                "ended": time.time(),
                "ended_monotonic_ns": time.monotonic_ns(),
                "outcome": RETURNED,
                "is_error": False,
                "read_the_post": False,
                "marked_sections": list(self.pages),
                "section_errors": [],
                "status": None,
                "retry_safe": None,
                "text": "",
                **fields,
            }
        )
        return record

    async def _preflight_fails(self) -> None:
        """What the frontend reports as its preflight meets the lost owner."""
        status = H_R8_CASES[self.row].status if self.row in H_R8_CASES else None
        if status is None:
            self._line("The shared browser owner did not answer the call preflight")
            return
        from linkedin_mcp_server.config.schema import AppConfig
        from linkedin_mcp_server.daemon_election import _reachable
        from linkedin_mcp_server.daemon_proxy import DaemonProxyBackend

        attachment = _attachment(self.tmp_path, self.port)
        with _frontend_lines(self.on_stderr):
            refused = await _preflight(attachment)
            assert refused is not None
            # The recovery's own burial, then the election's own probe.
            DaemonProxyBackend(
                attachment=attachment,
                auth_root=self.tmp_path,
                profile=self.tmp_path / "profile",
                config=AppConfig(),
            ).note_failure(attachment.descriptor.instance_id, refused.classification)
            await asyncio.to_thread(_reachable, attachment, 5.0)
        if self.dispatches_to_responder:
            async with httpx2.AsyncClient() as client:
                await client.post(
                    f"http://127.0.0.1:{self.port}/mcp",
                    headers={CALL_HEADER: "v1." + "2" * 32},
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                )

    async def _call(self, session, name: str, arguments: dict) -> dict:
        if not self.daemon:
            if name == MESSAGE_TOOL:
                return await self._message_direct(session, name)
            return await super()._call(session, name, arguments)
        if name == MESSAGE_TOOL:
            return await self._message(session, name)
        if name != PERSON_TOOL:
            return await super()._call(session, name, arguments)
        record = self._record(session, name)
        username = arguments["linkedin_username"]
        if self.owner.kills and self.successor_start is None:
            await self._preflight_fails()
            if self.reads_before_successor:
                await self._pages_of(username)
                await asyncio.sleep(1.5)
            self._elect()
            self._line("Attached to a replacement owner; running the call again")
            await asyncio.sleep(0.05)
        elif self.owner.stopped:
            self._line("The shared browser owner did not answer the call preflight")
            if self.runs_while_stopped:
                await asyncio.to_thread(
                    self._read, person_path(username, "main_profile")
                )
            while self.owner.stopped:
                await asyncio.sleep(0.01)
            self._line("Attached to a replacement owner; running the call again")
        if not (self.reads_before_successor and username in USERNAMES.values()):
            await self._pages_of(username)
        return self._returned(record)

    async def _message(self, session, name: str) -> dict:
        """A message whose owner is killed while its first page is held."""
        record = self._record(session, name)
        held = asyncio.ensure_future(
            asyncio.to_thread(self._read, person_path(RECIPIENT, "main_profile"))
        )
        self.background.append(held)
        while not self.owner.kills and not held.done():
            await asyncio.sleep(0.01)
        if not self.owner.kills:
            return self._returned(record, marked_sections=[])
        self._elect()
        if self.replays_the_message:
            self._line("Attached to a replacement owner; running the call again")
            await asyncio.to_thread(self._read, person_path(RECIPIENT, "main_profile"))
        if self.reports_success:
            return self._returned(record, marked_sections=[])
        self._line(
            "Owner lost mid-call; reporting an unknown outcome rather than "
            "repeating a call that could change something"
        )
        return self._returned(
            record,
            marked_sections=[],
            is_error=True,
            status="outcome_unknown",
            retry_safe=False,
        )

    async def _message_direct(self, session, name: str) -> dict:
        """A message whose Direct server is killed while its page is held."""
        record = self._record(session, name)
        held = asyncio.ensure_future(
            asyncio.to_thread(self._read, person_path(RECIPIENT, "main_profile"))
        )
        self.background.append(held)
        while not self.server.kills and not held.done():
            await asyncio.sleep(0.01)
        record.update(
            ended=time.time(),
            ended_monotonic_ns=time.monotonic_ns(),
            outcome=RAISED,
            exception="McpError",
        )
        raise RuntimeError("Connection closed")

    async def host(self, *args, after_call=None, tool=harness.READ_TOOL, **kw):
        self.hosts += 1
        self.on_stderr = kw.get("on_stderr") or self.on_stderr
        kw.setdefault("arguments", harness.READ_TOOL_ARGUMENTS)
        kw.setdefault("started", lambda _pid: None)
        session = await super().host(*args, after_call=after_call, tool=tool, **kw)
        if self.daemon:
            session.stderr.append("INFO Forwarding to the shared browser owner")
        if kw.get("row_script") is not None:
            # Nothing needs the owner any longer: it idles out.
            self.left = True
        return session

    async def run(self, row: str = ROW_UNREACHABLE, *, daemon: bool = True, **options):
        self.row, self.daemon = row, daemon
        if not daemon:
            # A Direct row starts no owner for the watcher to see.
            self.records.clear()
        try:
            return await measure_host_quit_row(
                profile=self.tmp_path / "auth" / "profile",
                experiment="K3" if daemon else "K1",
                daemon=daemon,
                egress=cast(Any, (self.origin, SimpleNamespace(url="x", decisions=[]))),
                log=self.log,
                work_dir=self.tmp_path / "row",
                row=row,
                **options,
            )
        finally:
            await asyncio.gather(*self.background, return_exceptions=True)


@pytest.fixture
def losing(monkeypatch, tmp_path, profile, origin, certificates):  # noqa: F811
    return _OwnerLossScene(monkeypatch, tmp_path, profile, origin, certificates)


def _phases(scene) -> list[str]:
    return [e["name"] for e in scene.log.records() if e["kind"] == "phase"]


def _events(scene, kind: str) -> list[dict]:
    return [e for e in scene.log.records() if e["kind"] == kind]


async def test_an_owner_killed_between_calls_is_recovered_from_and_passes(losing):
    result = await losing.run(ROW_UNREACHABLE)

    assert result.failures == [], result.failures
    record = losing.published()["record"]
    assert record["problems"] == []
    # Killed once, through the handle the row tied, and seen gone first.
    assert losing.owner.kills == 1
    assert record["owner_after_fault"]["alive"] is False
    assert record["owner_after_read"]["instance_id"] == "second"
    # The successor is the owner the row settles in the identified one's place.
    assert result.owner is not None and result.owner.get("replaced") is True
    [loss] = _events(losing, "loss")
    assert (loss["loss"], loss["target"]) == ("owner-killed", "owner")
    attempts = [(e["attempt"], e["classification"]) for e in _events(losing, "attempt")]
    assert attempts == [("preflight", None), ("election", "attached"), ("replay", None)]
    assert _phases(losing) == ["owner killed", "read sent", "read returned"]


async def test_a_read_served_before_the_successor_fails_the_row(losing):
    losing.reads_before_successor = True
    result = await losing.run(ROW_UNREACHABLE)

    findings = [p for p in result.record["problems"] if not p.startswith(INVALID)]
    assert any("not shown after the successor's launch" in p for p in findings), (
        result.record["problems"]
    )


async def test_a_responder_on_the_dead_owners_port_is_met_and_classified(losing):
    result = await losing.run(ROW_RESPONDER_404)

    assert result.failures == [], result.failures
    record = result.record
    responder = record["responder"]
    assert responder["bound"] is True and responder["address"][1] == losing.port
    # The frontend's real preflight met it, then the election's probe;
    # nothing was dispatched to it.
    paths = [r["path"] for r in responder["requests"]]
    assert paths[0] == HEARTBEAT_PATH and set(paths[1:]) == {"/mcp"}
    assert not any(is_dispatch(r) for r in responder["requests"])
    assert record["burial"] == {"claimed": "route_missing"}
    # Stopped before the row was judged: nothing answers there any more.
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", losing.port), timeout=5).close()


async def test_a_dispatch_to_the_responder_fails_the_row(losing):
    losing.dispatches_to_responder = True
    result = await losing.run(ROW_RESPONDER_500)

    assert any(
        f"{ROW_RESPONDER_500}: a tool dispatch reached the failed instance" in failure
        for failure in result.failures
    ), result.failures


async def test_a_port_that_cannot_be_bound_again_is_invalid_evidence(losing):
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", losing.port))
        squatter.listen()
        result = await losing.run(ROW_RESPONDER_404)

    problems = result.record["problems"]
    assert problems and invalid_evidence(problems) == problems, problems
    assert result.record["responder"]["bound"] is False
    # No read was made against an address the row did not hold.
    assert [c["tool"] for c in result.record["calls"]] == [harness.READ_TOOL]


async def test_a_stopped_owner_is_resumed_by_the_row_and_serves_again(losing):
    result = await losing.run(ROW_OWNER_ERROR)

    assert result.failures == [], result.failures
    record = result.record
    assert (losing.owner.stops, losing.owner.resumes) == (1, 1)
    assert record["fault"]["resumed_by"] == "row"
    resumed = record["fault"]["resumed_ns"] - record["fault"]["stopped_ns"]
    assert resumed >= owner_loss.STOP_SECONDS * 1e9
    assert _phases(losing) == [
        "owner stopped",
        "read sent",
        "owner resumed",
        "read returned",
        "later sent",
        "later returned",
    ]


async def test_a_tool_run_on_the_stopped_owner_fails_the_row(losing):
    losing.runs_while_stopped = True
    result = await losing.run(ROW_OWNER_ERROR)

    findings = [p for p in result.record["problems"] if not p.startswith(INVALID)]
    assert any("a tool ran on the stopped owner" in p for p in findings), findings


async def test_a_stop_the_script_left_is_resumed_and_fails_as_the_harnesss(
    losing, monkeypatch
):
    async def stops_and_leaves(ctx) -> None:
        ctx.record.update(username=USERNAMES[ROW_OWNER_ERROR])
        assert ctx.owner_loss is not None and ctx.owner_loss.stop is not None
        ctx.owner_loss.stop()

    monkeypatch.setitem(
        harness.ROWS,
        ROW_OWNER_ERROR,
        dataclasses.replace(harness.ROWS[ROW_OWNER_ERROR], script=stops_and_leaves),
    )
    result = await losing.run(ROW_OWNER_ERROR)

    # Resumed once, by the harness, and the row fails for it.
    assert (losing.owner.stops, losing.owner.resumes) == (1, 1)
    assert result.record["left_stopped"] is True
    # Before the owner's exit was waited for, which a stopped owner never meets.
    assert any(
        f.startswith("teardown: the row left the identified owner stopped")
        and "after the host quit" in f
        for f in result.failures
    ), result.failures
    assert any("the row left its owner stopped" in p for p in result.record["problems"])


async def test_a_stop_left_by_a_row_that_failed_is_resumed_in_the_teardown(
    losing, monkeypatch
):
    async def stops_and_leaves(ctx) -> None:
        assert ctx.owner_loss is not None and ctx.owner_loss.stop is not None
        ctx.owner_loss.stop()

    async def fails_after_the_script(*args, **kwargs):
        await _OwnerLossScene.host(losing, *args, **kwargs)
        raise RuntimeError("planted: the host session failed")

    monkeypatch.setitem(
        harness.ROWS,
        ROW_OWNER_ERROR,
        dataclasses.replace(harness.ROWS[ROW_OWNER_ERROR], script=stops_and_leaves),
    )
    monkeypatch.setattr(harness, "run_host_session", fails_after_the_script)
    with pytest.raises(RuntimeError, match="planted"):
        await losing.run(ROW_OWNER_ERROR)

    assert (losing.owner.stops, losing.owner.resumes) == (1, 1)


async def test_a_message_lost_after_dispatch_is_unknown_and_not_replayed(losing):
    result = await losing.run(ROW_H_R9)

    assert result.failures == [], result.failures
    record = result.record
    [gate] = record["gates"]
    assert gate["path"] == person_path(RECIPIENT, "main_profile")
    assert gate["released_by"] == RELEASED_BY_ROW and gate["terminal"] == SERVED
    [send] = [c for c in record["calls"] if c["tool"] == MESSAGE_TOOL]
    assert (send["status"], send["retry_safe"], send["is_error"]) == (
        "outcome_unknown",
        False,
        True,
    )
    assert record["send_window"]["unknown_lines"] == 1
    assert record["owner_after_read"]["instance_id"] == "second"
    assert _phases(losing) == [
        "armed",
        "entered",
        "owner killed",
        "returned",
        "released",
        "watched",
        "follow sent",
        "follow returned",
        "followed",
    ]


async def test_a_replayed_message_fails_the_row(losing):
    losing.replays_the_message = True
    result = await losing.run(ROW_H_R9)

    problems = result.record["problems"]
    for expected in (
        "the mutating call was replayed",
        "the frontend replayed the mutating call",
    ):
        assert any(expected in p for p in problems), (expected, problems)


async def test_a_lost_message_reported_as_sent_fails_the_row(losing):
    losing.reports_success = True
    result = await losing.run(ROW_H_R9)

    assert any("reported as a success" in p for p in result.record["problems"])


async def test_a_message_that_ends_before_its_page_is_held_loses_nothing(
    losing, monkeypatch
):
    async def answers_at_once(session, name, arguments):
        if name != MESSAGE_TOOL:
            return await _OwnerLossScene._call(losing, session, name, arguments)
        record = losing._record(session, name)
        return losing._returned(record, is_error=True, marked_sections=[])

    monkeypatch.setattr(losing, "_call", answers_at_once)
    result = await losing.run(ROW_H_R9)

    problems = result.record["problems"]
    assert problems and invalid_evidence(problems) == problems, problems
    # Nothing was killed: there was no dispatch to lose.
    assert losing.owner.kills == 0


async def test_the_direct_server_killed_under_a_message_settles_and_reads_again(
    losing,
):
    result = await losing.run(ROW_H_R9, daemon=False)

    assert result.failures == [], result.failures
    record = result.record
    assert losing.server.kills == 1 and losing.hosts == 2
    [send] = [c for c in record["calls"] if c["tool"] == MESSAGE_TOOL]
    assert send["outcome"] == RAISED
    assert record["settlement"]["guardian_exit"] == "exited"
    assert record["settlement"]["seen_ns"] < record["cleanup_began_ns"]
    assert record["fresh"]["made"] is True
    [loss] = _events(losing, "loss")
    assert (loss["loss"], loss["target"]) == ("server-killed", "frontend")
    assert record["gates"][0]["released_by"] == RELEASED_BY_ROW


async def test_the_direct_column_of_a_killing_lane_traces_its_server_and_kills_none(
    losing,
):
    result = await losing.run(ROW_UNREACHABLE, daemon=False)

    assert result.failures == [], result.failures
    assert result.record["prepared"]["actor"] == "frontend"
    assert losing.server.kills == 0 and _events(losing, "loss") == []
    assert result.record["fault"] == DIRECT_NO_FAULT


def test_a_held_page_released_by_the_row_ends_within_its_bound():
    # The scene's shortened release still sits inside its gate's own end wait.
    assert owner_loss.RELEASE_SECONDS < GATE_END_SECONDS


def test_the_unobserved_statements_name_every_fault():
    assert set(UNOBSERVED) == {case.fault for case in H_R8_CASES.values()}


def test_a_responder_records_requests_while_another_thread_reads_them():
    responder = DeclaredResponder("127.0.0.1", _free_port(), 404)
    responder.start()
    try:
        port = responder.server_address[1]
        errors: list[BaseException] = []

        def post() -> None:
            try:
                with httpx2.Client() as client:
                    client.post(f"http://127.0.0.1:{port}{HEARTBEAT_PATH}")
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=post) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert not errors
        assert len(responder.requests()) == 4
    finally:
        responder.stop()
